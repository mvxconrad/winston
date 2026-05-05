"""WINSTON — entry point.

This file is intentionally small. It only owns the *structural shape* of
Winston: which panels exist, what gets imported, how the section list is
built, the .env loader, and the dispatch to whichever face the user
asked for.

All tunable behavior — refresh rates, LLM settings, trigger thresholds,
TTS provider — lives in config.py. Edit there to change Winston's
behavior. Edit here when you're adding or removing a panel.

Three faces share this entry point. The brain is identical in all three
— same panels, same memory, same triggers, same commentary engine.
Only the rendering layer differs.

  python3 winston.py             → presence (default): small orb + voice
  python3 winston.py --gui       → desktop dashboard (PyQt6, all panels)
  python3 winston.py --cli       → terminal dashboard (Textual TUI)

The data layer (panels/, brain/, theme.py, config.py, logger.py) is the
same for all. Each face's `run()` takes the same `(sections, logger,
config=)` signature.
"""
import os
import sys
from pathlib import Path


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


# Mode dispatch. Order is intentional: --gui and --cli win over default
# (presence). If multiple flags are passed, last-wins is fine since
# only one face will actually run.
if "--gui" in sys.argv:
    from gui.main import run
elif "--cli" in sys.argv:
    from cli.display import run
else:
    # Default face: small orb + voice + full Winston brain. The
    # dashboard is one keypress away if you want numbers.
    from gui.presence import run

run(sections, logger, config=config)
