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


# ─────────────────── LLM commentary config ───────────────────
# All AI behavior is configured here. Disable, slow down, swap models, or
# personalize the greeting from one place. Defaults to "on" with sensible
# values for a local Ollama setup.

# Master switch. Set False to disable all LLM calls — Winston runs as a
# pure monitoring tool and the COMMENTARY panel shows a static placeholder.
LLM_ENABLED = True

# Your name. Used in the startup greeting ("Good morning, max."). Set to
# None or "" to skip the personal address.
USER_NAME = "max"

# Which Ollama model to use. Must already be pulled (run `ollama list` to see).
# Good picks: qwen2.5:7b-instruct (current default, smart + fast)
#             llama3.1:8b (slightly bigger, similar quality)
#             qwen2.5:3b (faster, lower VRAM, less nuanced)
LLM_MODEL = "qwen2.5:7b-instruct"

# How often Winston asks the LLM for new commentary (seconds).
# Lower = more responsive but more GPU work. 30s is a comfortable default.
COMMENTARY_INTERVAL_SEC = 30.0

# Whether to do the startup ritual (greeting + retrospective from log)
# before the regular commentary loop kicks in.
STARTUP_GREETING = True

# Typewriter pacing — Ollama generates faster than is comfortable to read.
# We buffer LLM tokens internally and emit them to the UI at this rate.
# Lower = more deliberate, higher = closer to raw model speed.
# Stage 5.5 (TODO) will replace this with per-tier rates so urgent
# alerts can render instantly.
TYPEWRITER_TPS = 25     # tokens/chunks per second emitted to the UI

# How long the panel stays on a fully-rendered message before the next
# commentary cycle is allowed to start typing. Gives the eye time to land.
INTER_MESSAGE_PAUSE_SEC = 2.0

# How many lines tall the COMMENTARY panel is. Older lines stay visible
# above the newest one. Most recent message is the brightest; older ones
# fade. Set to 1 for the old single-line behavior.
COMMENTARY_LINES = 5


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
run(sections, logger, logger_hz=LOGGER_HZ,
    llm_enabled=LLM_ENABLED,
    llm_model=LLM_MODEL,
    user_name=USER_NAME,
    commentary_interval_sec=COMMENTARY_INTERVAL_SEC,
    startup_greeting=STARTUP_GREETING,
    typewriter_tps=TYPEWRITER_TPS,
    inter_message_pause_sec=INTER_MESSAGE_PAUSE_SEC,
    commentary_lines=COMMENTARY_LINES)