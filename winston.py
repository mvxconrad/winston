"""WINSTON — entry point.

This file is intentionally small. It only owns the *structural shape* of
Winston: which panels exist, what gets imported, how the section list is
built, and the call to run().

All tunable behavior — refresh rates, LLM settings, trigger thresholds —
lives in config.py. Edit there to change Winston's behavior. Edit here
when you're adding or removing a panel.

To launch:  python3 winston.py
"""
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

from logger  import Logger
from display import run


# Section list: (panel_instance, refresh_hz). Order here drives the layout
# in display.py — re-arrange in display.py's compose() if you want a
# different visual order.
sections = [
    (CpuGraphPanel(),                                config.CPU_GRAPH_HZ),
    (CpuPanel(),                                     config.CPU_CORES_HZ),
    (RamPanel(),                                     config.RAM_HZ),
    (SystemPanel(),                                  config.SYSTEM_HZ),
    (DiskPanel(),                                    config.DISK_HZ),
    (TempsPanel(refresh_sec=config.LHM_FETCH_INTERVAL_SEC), config.TEMPS_HZ),
    (GpuPanel(),                                     config.GPU_HZ),
    (NetworkPanel(),                                 config.NETWORK_HZ),
    (ProcessesPanel(),                               config.PROCESSES_HZ),
]

logger = Logger()
run(sections, logger, config=config)