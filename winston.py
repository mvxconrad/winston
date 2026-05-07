"""WINSTON — entry point.

This file is intentionally small. It only owns the *structural shape* of
Winston: which panels exist, what gets imported, how the section list is
built, the .env loader, and the dispatch to whichever face the user
asked for.

All tunable behavior — refresh rates, LLM settings, trigger thresholds,
TTS provider — lives in config.py. Edit there to change Winston's
behavior. Edit here when you're adding or removing a panel.

Four modes share this entry point. The brain is identical in all —
same panels, same memory, same triggers, same commentary engine.
Only the rendering layer and lifecycle differ.

  python3 winston.py             → watchdog (default): dormant in tray,
                                   wakes on hardware spikes, speaks,
                                   hides back to tray. Controlled by
                                   config.WATCHDOG_MODE.
  python3 winston.py --presence  → always-visible orb + voice (old default)
  python3 winston.py --gui       → desktop dashboard (PyQt6, all panels)
  python3 winston.py --cli       → terminal dashboard (Textual TUI)

The data layer (panels/, brain/, theme.py, config.py, logger.py) is the
same for all. Each face's `run()` takes the same `(sections, logger,
config=)` signature.
"""
import faulthandler
import os
import sys
from pathlib import Path

# Print full C-level traceback if Python segfaults / Qt crashes —
# critical for diagnosing the silent-exit bug on Windows where
# Python "exits clean" but actually died inside Qt/sounddevice.
faulthandler.enable()


def _load_dotenv():
    """Tiny .env loader — no python-dotenv dep needed.

    Reads `.env` from the repo root and copies KEY=VALUE pairs into
    os.environ if they're not already set. Quoted values are unquoted.
    Lines starting with # are ignored.

    Must run BEFORE importing brain.voice.* because tts_elevenlabs
    reads ELEVENLABS_API_KEY at client construction time. The .env file
    is gitignored — don't commit secrets.
    """
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


import config

from panels.cpu_graph import CpuGraphPanel
from panels.cpu       import CpuPanel
from panels.ram       import RamPanel
from panels.system    import SystemPanel
from panels.disk      import DiskPanel
from panels.temps     import TempsPanel
from panels.gpu       import GpuPanel
from panels.network   import NetworkPanel
from panels.processes import ProcessesPanel

from logger import Logger


# Section list: (panel_instance, refresh_hz). Order here drives the
# layout in whichever face is active.
sections = [
    (CpuGraphPanel(),                                config.CPU_GRAPH_HZ),
    (CpuPanel(),                                     config.CPU_CORES_HZ),
    (RamPanel(),                                     config.RAM_HZ),
    (SystemPanel(),                                  config.SYSTEM_HZ),
    (DiskPanel(),                                    config.DISK_HZ),
    (TempsPanel(refresh_sec=config.LHM_FETCH_INTERVAL_SEC), config.TEMPS_HZ),
    (GpuPanel(),                                     config.GPU_HZ),
    (NetworkPanel(),                                 config.NETWORK_HZ),
    (ProcessesPanel(limit=getattr(config, "PROCESSES_LIMIT", 14)), config.PROCESSES_HZ),
]

logger = Logger()


# Mode dispatch. Order is intentional: explicit face flags win over
# default. If multiple flags are passed, first-match wins.
#
# --presence  → always-visible orb (old default behavior)
# --gui       → full PyQt6 dashboard with all panels
# --cli       → Textual TUI in the terminal
# (default)   → presence + watchdog mode (dormant in tray, wakes on spikes)
#
# The watchdog lifecycle is handled inside gui.presence.run() — when
# watchdog=True, the orb starts hidden and only appears on trigger fire.
# config.WATCHDOG_MODE controls whether the default dispatch uses watchdog.
_watchdog = False

if "--gui" in sys.argv:
    from gui.main import run
elif "--cli" in sys.argv:
    from cli.display import run
elif "--presence" in sys.argv:
    # Explicit old-style presence: always-visible orb, no watchdog.
    from gui.presence import run
    _watchdog = False
else:
    # Default: presence face with watchdog mode. Config knob
    # (WATCHDOG_MODE) is the final authority — run() reads it.
    from gui.presence import run
    _watchdog = True

# gui.presence.run accepts the extra kwarg; gui.main.run and cli.display.run
# don't, so we only pass it when dispatching to presence.
if "--gui" in sys.argv or "--cli" in sys.argv:
    run(sections, logger, config=config)
else:
    # Presence path (default or --presence). Pass watchdog explicitly so
    # run() doesn't fall back to config.WATCHDOG_MODE — the user's flag
    # should be authoritative.
    run(sections, logger, config=config, watchdog=_watchdog)
