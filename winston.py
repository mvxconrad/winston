"""WINSTON entry point.

Refresh rates (Hz) are tunable per panel below. The display layer schedules
each panel on its own interval. The logger always ticks at LOGGER_HZ
regardless — gives you regular time-series rows for later analysis.

Rough guidance:
  4 Hz  — snappy UI feel for fast-changing data (CPU)
  2 Hz  — comfortable for moderately-changing data (RAM, network, GPU)
  1 Hz  — slow-changing or expensive to compute (processes, temps)
  0.5+  — rarely-changing structural data (load avg, system info)
  0.1   — practically static data (disk usage)

Going faster doesn't add real info for things like disk %; going slower
than 1 Hz on UI things makes them feel laggy. Tweak to taste.
"""
from panels.cpu_graph import CpuGraphPanel
from panels.cpu import CpuPanel
from panels.ram import RamPanel
from panels.system import SystemPanel
from panels.disk import DiskPanel
from panels.temps import TempsPanel
from panels.gpu import GpuPanel
from panels.network import NetworkPanel
from panels.processes import ProcessesPanel
from logger import Logger
from display import run


# ─────────────────── Refresh rates (Hz) ───────────────────
CPU_GRAPH_HZ   = 4.0
CPU_CORES_HZ   = 4.0
RAM_HZ         = 2.0
SYSTEM_HZ      = 0.5     # load avg, proc count — slow movers
DISK_HZ        = 0.1     # every 10s — disk fills slowly
TEMPS_HZ       = 1.0
GPU_HZ         = 2.0
NETWORK_HZ     = 2.0
PROCESSES_HZ   = 1.0     # process scan is the most expensive op

LOGGER_HZ      = 1.0     # fixed-rate CSV writes (gives clean time-series)

# How often TempsPanel actually re-fetches from LHM HTTP / PowerShell.
# Decoupled from TEMPS_HZ — the panel update is cheap, the fetch is expensive.
LHM_FETCH_INTERVAL_SEC = 3


# ─────────────────── Section list ───────────────────
# Each tuple: (panel_instance, refresh_hz)
sections = [
    (CpuGraphPanel(),                                CPU_GRAPH_HZ),
    (CpuPanel(),                                     CPU_CORES_HZ),
    (RamPanel(),                                     RAM_HZ),
    (SystemPanel(),                                  SYSTEM_HZ),
    (DiskPanel(),                                    DISK_HZ),
    (TempsPanel(refresh_sec=LHM_FETCH_INTERVAL_SEC), TEMPS_HZ),
    (GpuPanel(),                                     GPU_HZ),
    (NetworkPanel(),                                 NETWORK_HZ),
    (ProcessesPanel(),                               PROCESSES_HZ),
]

logger = Logger()
run(sections, logger, logger_hz=LOGGER_HZ)