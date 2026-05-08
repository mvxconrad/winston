"""Top-N processes panel.

Two data sources, merged into one ranked list:

  1. WSL/Linux processes via psutil (always available; cheap)
  2. Windows-host processes via the shared poller in
     `panels/host_processes.py` (only meaningful under WSL)

The Windows-side daemon thread lives in its own module so brain/* can
also import the snapshot for prompts and memory learning. This panel
is just one consumer of that shared cache.

CSV columns include BOTH the merged top (so a Windows game shows up
when it dominates) AND a separate top_winproc_* pair (so memory's
log-learner can rank Windows apps even if a Linux process happened to
be the global top at the moment of write).
"""
import os
import psutil
from rich.text import Text

from panels.base import fmt_bytes
from panels.host_processes import get_shared_poller
from theme import LABEL, SECONDARY, BRIGHT, MEDIUM, DIM, heat_pct


# ──────────────── Process-name enrichment ────────────────
# psutil reports `name` as the executable basename ("python3"). On a dev
# machine the user has 5 different python processes running and the
# "python3 / python3 / python3" rows are useless — the user can't tell
# which is Winston, which is a Jupyter kernel, which is some random tool.
#
# Solution: when the process is a generic interpreter (python, node,
# bash, etc.), look at the cmdline and extract the script being run.
# `python3 winston.py --gui`  → "winston.py"
# `node /path/to/server.js`   → "server.js"
# We also tag the process that IS Winston (this PID and its parent's
# children, which would catch the Ollama worker thread if it forked) with
# [self] so it's obvious which row is "us watching ourselves".
_WINSTON_PID = os.getpid()

# Interpreters whose executable name tells you nothing on its own.
# Anything else, we leave the name as-is.
_GENERIC_INTERPRETERS = frozenset({
    "python", "python2", "python3", "python.exe", "python3.exe",
    "node", "nodejs", "node.exe",
    "bash", "sh", "zsh", "fish",
    "ruby", "perl",
    "java",
    "powershell.exe", "pwsh.exe",
})


def _enrich_name(pid, base_name):
    """Turn a generic interpreter name into something meaningful.

    Cheap when the name isn't generic — we don't open /proc/<pid>/cmdline
    on every process, only on the ones that need disambiguating.

    Two-step:
      1. If `base_name` is a generic interpreter (python3, node, …),
         look at cmdline and append the script — `python3 (winston.py)`.
      2. If the pid IS this Winston process, append `[self]` so the user
         can spot which row is the dashboard itself.
    Both can apply: `python3 (winston.py) [self]`.
    """
    name = base_name or "?"
    # Step 1: cmdline-based enrichment for generic interpreters.
    if base_name and base_name.lower() in _GENERIC_INTERPRETERS:
        try:
            cmdline = psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            cmdline = None
        if cmdline:
            # Find the first arg that looks like a script (skip flags
            # like -u, -m module, --no-color). For "python -m foo" we
            # pick "(-m foo)"; for "python winston.py" we pick
            # "(winston.py)". Falls through with no enrichment if
            # everything in args is a flag.
            args = cmdline[1:]
            i = 0
            while i < len(args):
                arg = args[i]
                if arg == "-m" and i + 1 < len(args):
                    name = f"{base_name} (-m {args[i+1]})"
                    break
                if arg == "-c":
                    name = f"{base_name} (-c)"
                    break
                if arg.startswith("-"):
                    i += 1
                    continue
                name = f"{base_name} ({os.path.basename(arg)})"
                break

    # Step 2: tag self LAST so it's always visible regardless of step 1.
    if pid == _WINSTON_PID:
        name = f"{name} [self]"
    return name


class ProcessesPanel:
    """Top-N table merging Linux psutil rows + Windows host rows.

    Attributes used by views:
      procs:     list[(cpu_pct, mem, name, pid)]   — psutil-side, every update()
      win_procs: list[(cpu_pct, mem, name, pid)]   — host-side, daemon-cached
      limit:     max rows shown by the consuming view
    """

    def __init__(self, limit=8):
        self.limit = limit
        self.procs = []
        self.win_procs = []
        # Hand off the daemon-thread lifecycle to the shared module so
        # both this panel and brain/* read the same cache.
        self._poller = get_shared_poller()

    @property
    def title(self):
        return "PROCESSES (host+wsl)" if self.win_procs else "PROCESSES"

    # Noise processes to hide — these either show inverted idle time
    # (System Idle Process) or are Windows kernel bookkeeping that
    # clutters the top-N list without being actionable.
    _HIDDEN = frozenset({"System Idle Process", "Idle"})

    def update(self):
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info']):
            try:
                info = p.info
                name = info['name'] or '?'
                if name in self._HIDDEN:
                    continue
                cpu = info['cpu_percent'] or 0.0
                mem = info['memory_info'].rss if info['memory_info'] else 0
                procs.append((cpu, mem, name, info['pid']))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda p: -p[0])
        # self.procs holds RAW psutil names. Display-time enrichment
        # (`python3 (winston.py) [self]`) happens in render() / GUI views
        # via display_name() so the CSV log stays clean — otherwise the
        # enriched string ends up as a "tracked app" in memory.json.
        self.procs = procs[:self.limit]

        # Pull whatever the shared poller has cached. Empty list until
        # the poller's first sample-pair completes (~5s after launch).
        if self._poller is not None:
            self.win_procs = self._poller.snapshot()[:self.limit]

    @staticmethod
    def display_name(pid, raw_name):
        """Display-time name enrichment. Cheap (cmdline read only on the
        top-N rows). Frontends call this when rendering each row."""
        return _enrich_name(pid, raw_name)

    def render(self, width=None):
        # TUI render path — keeps backwards compatibility with cli/display.py.
        if width is None:
            width = 40
        text = Text()
        name_w = max(20, width - 30)
        text.append(f"{'PID':>6}  {'NAME':<{name_w}}  {'CPU%':>6}  {'MEM':>7}\n",
                    style=SECONDARY)

        # Same merge ProcessesView does, but render to Rich Text.
        # Linux side: enrich raw psutil names at render time so CSV stays
        # clean. Windows side: enrichment N/A (PowerShell already gives
        # us proper process names like ArkAscended).
        merged = [(cpu, mem, _enrich_name(pid, name), pid, "lin")
                  for cpu, mem, name, pid in self.procs]
        merged += [(cpu, mem, name, pid, "win")
                   for cpu, mem, name, pid in self.win_procs]
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

    # ──────────────── CSV log ────────────────
    # Two pairs of columns:
    #   top_proc_*    — single global top across both sources (back-compat
    #                   with older log-learners; useful as "what was loud")
    #   top_winproc_* — top WINDOWS process specifically, so the memory
    #                   learner can build an app ranking that includes
    #                   games + browsers, not just whatever Linux process
    #                   happened to be busy at sample time.
    def csv_headers(self):
        return ["top_proc_name", "top_proc_cpu",
                "top_winproc_name", "top_winproc_cpu"]

    def csv_columns(self):
        # Global top (across Linux + Windows).
        merged = list(self.procs) + list(self.win_procs)
        if merged:
            merged.sort(key=lambda p: -p[0])
            top_name, top_cpu = merged[0][2], merged[0][0]
        else:
            top_name, top_cpu = "", 0.0

        # Windows-only top — picks the busiest, but if everyone's idle,
        # the WindowsProcessPoller returns rows sorted by working-set
        # (memory), so we still get the dominant memory-resident app
        # (e.g. Ark when it's loaded but currently waiting on input).
        if self.win_procs:
            wname, wcpu = self.win_procs[0][2], self.win_procs[0][0]
        else:
            wname, wcpu = "", 0.0

        return [top_name, top_cpu, wname, wcpu]
