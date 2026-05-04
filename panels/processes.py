"""Top-N processes panel.

Two data sources, merged into one ranked list:

  1. WSL/Linux processes via psutil (always available; cheap)
  2. Windows-host processes via PowerShell `Get-Process`, polled on a
     daemon thread (only when running under WSL — otherwise irrelevant)

Why both: when running under WSL, psutil only sees Linux-side processes.
The user's actual high-CPU workload — Ark, Chrome, Discord, anything
launched from Windows — is invisible to psutil. The PowerShell poller
is the only way to see those, but spawning PowerShell on every UI tick
would block the dashboard for 50–200ms per call (same issue NetworkPanel
fought). So we spin up a daemon thread that polls every few seconds and
caches the result; ProcessesPanel.update() reads the cache without ever
crossing WSL→Windows on the UI thread.

CPU% for Windows processes is computed by sampling cumulative CPU time
across two PowerShell calls and dividing by elapsed time and core count
(same definition psutil uses). The very first poll has no prior sample,
so all win_procs report 0.0% CPU until the second poll completes.
"""
import json
import os
import platform
import shutil
import subprocess
import threading
import time

import psutil
from rich.text import Text

from panels.base import fmt_bytes
from theme import LABEL, SECONDARY, BRIGHT, MEDIUM, DIM, heat_pct


# ──────────────── WSL → Windows host poller ────────────────
# Same lessons as panels/network.py:
# - Spawn cost (~50-200ms) makes a UI-thread call unacceptable
# - Daemon thread + cached result keeps the UI free
# - 5s interval is the sweet spot for WSL→Windows polling
WIN_POLL_INTERVAL_SEC = 5.0
WIN_PROC_LIMIT = 30   # cap on PowerShell-side rows; merged list is then capped further


def _is_wsl():
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/version", "r") as f:
            content = f.read().lower()
            return "microsoft" in content or "wsl" in content
    except OSError:
        return False


_POWERSHELL_FALLBACK_PATHS = [
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    "/mnt/c/Program Files/PowerShell/7/pwsh.exe",
    "/mnt/c/Windows/SysWOW64/WindowsPowerShell/v1.0/powershell.exe",
]


def _find_powershell():
    found = shutil.which("powershell.exe")
    if found:
        return found
    for path in _POWERSHELL_FALLBACK_PATHS:
        if os.path.exists(path):
            return path
    return None


# Compact PowerShell command — one process per object, JSON output. We
# read CPU as cumulative seconds (Windows TotalProcessorTime) and turn
# that into a % across two consecutive samples on the Linux side.
#
# `Get-Process | ... | Select Id, ProcessName, CPU, WorkingSet64` is
# fast (cumulative — no sampling delay) but `CPU` here is total seconds,
# not a rate. We compute the rate ourselves between polls.
_PS_COMMAND = (
    "$ErrorActionPreference='SilentlyContinue';"
    "Get-Process | Where-Object { $_.CPU -ne $null } |"
    f"  Sort-Object -Descending WorkingSet64 |"
    f"  Select-Object -First {WIN_PROC_LIMIT} Id, ProcessName, CPU, WorkingSet64 |"
    "  ConvertTo-Json -Compress -Depth 2"
)


def _poll_windows_processes(ps_path):
    """Run one PowerShell snapshot. Returns list of dicts:
       [{"Id":1234, "ProcessName":"chrome", "CPU":42.13, "WorkingSet64":12345678}, ...]
    or [] on any failure (timeout, broken JSON, no PS, etc.)."""
    if not ps_path:
        return []
    try:
        result = subprocess.run(
            [ps_path, "-NoProfile", "-NonInteractive", "-Command", _PS_COMMAND],
            capture_output=True, timeout=10, text=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    # PowerShell returns either a single dict (one row) or a list. Normalize.
    if isinstance(data, dict):
        data = [data]
    out = []
    for row in data:
        try:
            pid = int(row.get("Id", 0))
            name = str(row.get("ProcessName", "") or "")
            cpu_total_sec = float(row.get("CPU") or 0.0)
            mem = int(row.get("WorkingSet64") or 0)
            out.append({"pid": pid, "name": name,
                        "cpu_total_sec": cpu_total_sec, "mem": mem})
        except (TypeError, ValueError):
            continue
    return out


class _HostProcessPoller(threading.Thread):
    """Daemon thread that snapshots Windows processes every WIN_POLL_INTERVAL_SEC.

    Results are cached as `self.latest`: a list of (cpu_pct, mem, name, pid)
    tuples — same shape ProcessesPanel uses for psutil rows. CPU% is computed
    from the delta between consecutive samples so the units match psutil.
    """

    def __init__(self, ps_path):
        super().__init__(name="winston-winproc-poller", daemon=True)
        self._ps_path = ps_path
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.latest = []  # [(cpu_pct, mem, name, pid), ...]
        # PID → (timestamp, cumulative_cpu_seconds) for delta math.
        self._prev = {}
        # Logical CPU count for "1.0% per core" → "fraction of total" math.
        try:
            self._cpu_count = max(1, psutil.cpu_count(logical=True) or 1)
        except Exception:
            self._cpu_count = 1

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            rows = _poll_windows_processes(self._ps_path)
            now = time.monotonic()
            tuples = []
            new_prev = {}
            for r in rows:
                pid = r["pid"]
                cpu_total = r["cpu_total_sec"]
                prev = self._prev.get(pid)
                if prev is not None:
                    prev_t, prev_cpu = prev
                    elapsed = max(0.001, now - prev_t)
                    delta = max(0.0, cpu_total - prev_cpu)
                    # Match psutil: 100% per core * num_cores possible.
                    cpu_pct = (delta / elapsed) * 100.0 / self._cpu_count
                else:
                    cpu_pct = 0.0
                new_prev[pid] = (now, cpu_total)
                tuples.append((cpu_pct, r["mem"], r["name"], pid))
            self._prev = new_prev
            tuples.sort(key=lambda t: -t[0])
            with self._lock:
                self.latest = tuples
            # Sleep the rest of the interval, watching for stop.
            elapsed = time.monotonic() - t0
            wait = max(0.1, WIN_POLL_INTERVAL_SEC - elapsed)
            if self._stop.wait(wait):
                return

    def snapshot(self):
        with self._lock:
            return list(self.latest)


# ──────────────── Panel ────────────────
class ProcessesPanel:
    """Top-N table merging Linux psutil rows + Windows host rows (via poller).

    Attributes used by views:
      procs:     list[(cpu_pct, mem, name, pid)]   — psutil-side, every update()
      win_procs: list[(cpu_pct, mem, name, pid)]   — PowerShell-side, daemon-cached
      limit:     max rows shown by the consuming view
    """

    def __init__(self, limit=8):
        self.limit = limit
        self.procs = []
        self.win_procs = []
        self._poller = None

        # Spin up the Windows poller only when:
        #   - we're on WSL (otherwise irrelevant)
        #   - user hasn't disabled threads via env var
        #   - PowerShell is actually findable
        self._stop_event = threading.Event()
        no_threads = (os.environ.get("WINSTON_NO_THREADS")
                      or os.environ.get("WINSTON_NO_HOST_PROCS"))
        if _is_wsl() and not no_threads:
            ps_path = _find_powershell()
            if ps_path:
                self._poller = _HostProcessPoller(ps_path)
                self._poller.start()

    @property
    def title(self):
        return "PROCESSES (host+wsl)" if self.win_procs else "PROCESSES"

    def update(self):
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info']):
            try:
                info = p.info
                cpu = info['cpu_percent'] or 0.0
                mem = info['memory_info'].rss if info['memory_info'] else 0
                procs.append((cpu, mem, info['name'] or '?', info['pid']))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda p: -p[0])
        self.procs = procs[:self.limit]
        # Pull whatever the poller has cached. Empty list until the
        # poller's first sample-pair completes (~5s after launch).
        if self._poller is not None:
            self.win_procs = self._poller.snapshot()[:self.limit]

    def render(self, width=None):
        # TUI render path — keeps backwards compatibility with cli/display.py.
        if width is None:
            width = 40
        text = Text()
        name_w = max(20, width - 30)
        text.append(f"{'PID':>6}  {'NAME':<{name_w}}  {'CPU%':>6}  {'MEM':>7}\n",
                    style=SECONDARY)

        # Same merge ProcessesView does, but render to Rich Text.
        merged = [(cpu, mem, name, pid, "lin") for cpu, mem, name, pid in self.procs]
        merged += [(cpu, mem, name, pid, "win") for cpu, mem, name, pid in self.win_procs]
        merged.sort(key=lambda r: -r[0])
        merged = merged[:max(self.limit, 14)]

        for cpu, mem, name, pid, origin in merged:
            display_name = name if len(name) <= name_w else name[:name_w - 1] + "…"
            tag = " [win]" if origin == "win" else ""
            display_name = (display_name + tag) if tag else display_name
            if cpu < 1:
                cpu_style = DIM
                name_style = DIM
            else:
                cpu_color = heat_pct(cpu)
                cpu_style = f"bold {cpu_color}" if cpu > 50 else cpu_color
                name_style = BRIGHT if cpu > 50 else MEDIUM
            text.append(f"{pid:>6}  ", style=SECONDARY)
            text.append(f"{display_name:<{name_w}}  ", style=name_style)
            text.append(f"{cpu:5.1f}%", style=cpu_style)
            text.append(f"  {fmt_bytes(mem):>7}\n", style=SECONDARY)

        return text

    def csv_headers(self):
        return ["top_proc_name", "top_proc_cpu"]

    def csv_columns(self):
        # Top across both sources, so the CSV captures Windows-host load
        # too — important so brain.memory's learn_from_log can see games.
        merged = list(self.procs) + list(self.win_procs)
        if not merged:
            return ["", 0.0]
        merged.sort(key=lambda p: -p[0])
        return [merged[0][2], merged[0][0]]
