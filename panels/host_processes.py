"""Windows-host process poller for WSL.

Why this lives outside ProcessesPanel:
  Both the PROCESSES panel and the LLM commentary need to see what's
  running on the Windows host. If the daemon thread lived only inside
  ProcessesPanel, the brain would have to reach across the panel
  boundary to read it — gross, and prone to import cycles.

  Instead, this module owns ONE shared poller (`get_shared_poller()`),
  started lazily on first call. Anyone — panels, prompt builders,
  logger, memory learner — can pull `snapshot()` for the latest cached
  list of (cpu_pct, mem, name, pid) tuples. The thread spins up at
  most once per process.

WSL→Windows polling cost (same lesson as panels/network.py):
  Each PowerShell spawn is 50-200 ms across the WSL boundary. Doing
  that on the UI thread would visibly stall the dashboard. The daemon
  thread polls every WIN_POLL_INTERVAL_SEC and caches the result so
  consumers read sub-µs.

CPU% calculation:
  Windows `Get-Process` returns `CPU` as cumulative seconds of CPU
  time, not a rate. We sample twice and divide the delta by elapsed
  wall time and core count — this matches psutil's cpu_percent()
  semantics so the merged top-N table can compare apples to apples.

Disable mechanism:
  WINSTON_NO_THREADS=1 or WINSTON_NO_HOST_PROCS=1 in env: poller never
  starts and snapshot() returns []. Same env-var convention as the
  other daemon-thread panels (network, lhm, gpu).
"""
import json
import os
import platform
import shutil
import subprocess
import threading
import time

import psutil


# ──────────────── Polling cadence ────────────────
# 5 s — same logic as NetworkPanel: too fast and the PowerShell-spawn
# GIL handoff thrash hurts stdin reading; 5 s is the sweet spot we
# verified empirically.
WIN_POLL_INTERVAL_SEC = 5.0

# Cap on rows pulled from PowerShell. The merged top-N display is
# capped further downstream in ProcessesPanel.
WIN_PROC_LIMIT = 30


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
    """Resolve powershell.exe — PATH first, then known WSL fallbacks."""
    found = shutil.which("powershell.exe")
    if found:
        return found
    for path in _POWERSHELL_FALLBACK_PATHS:
        if os.path.exists(path):
            return path
    return None


# Sort by working-set so we always get the user's heavy memory consumers
# (games, browsers) even when their CPU% is low at the moment of polling.
# CPU rank can swap places between samples; memory is a stable signal.
_PS_COMMAND = (
    "$ErrorActionPreference='SilentlyContinue';"
    "Get-Process | Where-Object { $_.CPU -ne $null } |"
    "  Sort-Object -Descending WorkingSet64 |"
    f"  Select-Object -First {WIN_PROC_LIMIT} Id, ProcessName, CPU, WorkingSet64 |"
    "  ConvertTo-Json -Compress -Depth 2"
)


def _poll_once(ps_path):
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
    # PowerShell returns a single dict (one row) or a list. Normalize.
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


# ──────────────── Poller ────────────────
class WindowsProcessPoller(threading.Thread):
    """Daemon thread that snapshots Windows processes every WIN_POLL_INTERVAL_SEC.

    Public surface:
      - start():     start the daemon thread (called by get_shared_poller())
      - stop():      signal the thread to exit
      - snapshot():  list of (cpu_pct, mem, name, pid) — sorted CPU desc
      - top():       (cpu_pct, mem, name, pid) for the busiest process, or None
      - is_active(): bool — is the thread running?

    All accessors are thread-safe; consumers can call from anywhere.
    """

    def __init__(self, ps_path):
        super().__init__(name="winston-winproc-poller", daemon=True)
        self._ps_path = ps_path
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest = []  # [(cpu_pct, mem, name, pid), ...]
        # PID → (timestamp, cumulative_cpu_seconds) for delta math.
        self._prev = {}
        try:
            self._cpu_count = max(1, psutil.cpu_count(logical=True) or 1)
        except Exception:
            self._cpu_count = 1

    def stop(self):
        self._stop.set()

    def is_active(self):
        return self.is_alive() and not self._stop.is_set()

    def run(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            rows = _poll_once(self._ps_path)
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
                    # Match psutil: 100% per core × num_cores possible.
                    cpu_pct = (delta / elapsed) * 100.0 / self._cpu_count
                else:
                    cpu_pct = 0.0
                new_prev[pid] = (now, cpu_total)
                tuples.append((cpu_pct, r["mem"], r["name"], pid))
            self._prev = new_prev
            tuples.sort(key=lambda t: -t[0])
            with self._lock:
                self._latest = tuples
            elapsed = time.monotonic() - t0
            wait = max(0.1, WIN_POLL_INTERVAL_SEC - elapsed)
            if self._stop.wait(wait):
                return

    def snapshot(self):
        """Return a sorted (CPU desc) list of (cpu_pct, mem, name, pid)."""
        with self._lock:
            return list(self._latest)

    def top(self):
        """Convenience: the single busiest Windows process, or None."""
        with self._lock:
            return self._latest[0] if self._latest else None


# ──────────────── Shared singleton ────────────────
_shared_poller = None
_shared_lock = threading.Lock()


def get_shared_poller():
    """Get-or-create the shared module-level poller.

    Returns a `WindowsProcessPoller` (running) on WSL, or `None` when:
      - we're not on WSL
      - WINSTON_NO_THREADS / WINSTON_NO_HOST_PROCS env var is set
      - powershell.exe can't be found

    First call starts the daemon thread; subsequent calls return the
    same instance. Safe to call from multiple threads — guarded by a
    module-level lock.
    """
    global _shared_poller
    with _shared_lock:
        if _shared_poller is not None:
            return _shared_poller
        if not _is_wsl():
            return None
        if (os.environ.get("WINSTON_NO_THREADS")
                or os.environ.get("WINSTON_NO_HOST_PROCS")):
            return None
        ps = _find_powershell()
        if not ps:
            return None
        _shared_poller = WindowsProcessPoller(ps)
        _shared_poller.start()
        return _shared_poller


def snapshot():
    """Quick accessor for consumers that don't want to manage the poller
    object. Returns the latest cached list, or [] if the poller isn't
    running (non-WSL, disabled, or PowerShell missing)."""
    p = get_shared_poller()
    return p.snapshot() if p else []


def top():
    """The single busiest Windows process, or None."""
    p = get_shared_poller()
    return p.top() if p else None


def is_available():
    """Is Windows-process polling actually working on this host? Useful
    for prompt builders to decide whether to mention host processes."""
    return get_shared_poller() is not None
