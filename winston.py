"""WINSTON — entry point.

This file is intentionally small. It only owns the *structural shape* of
Winston: which panels exist, what gets imported, how the section list is
built, and the call to run().

All tunable behavior — refresh rates, LLM settings, trigger thresholds —
lives in config.py. Edit there to change Winston's behavior. Edit here
when you're adding or removing a panel.

Two frontends share this entry point:
  python3 winston.py           → terminal UI (cli/display.py)
  python3 winston.py --gui     → desktop app (gui/main.py)

The data layer (panels/, brain/, theme.py, config.py, logger.py) is the
same for both — only the rendering layer differs. Both `run()` functions
take the same `(sections, logger, config=)` signature.
"""
import sys
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
# layout in whichever frontend is active.
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

if "--gui" in sys.argv:
    from gui.main import run
else:
    from cli.display import run

run(sections, logger, config=config)