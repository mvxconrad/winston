"""WINSTON — configuration.

Every knob that controls Winston's behavior lives here. Edit this file to
change refresh rates, LLM behavior, trigger thresholds, the user's name,
etc. Restart Winston for changes to take effect.

winston.py stays small and structural — just imports, panel wiring, and
the run() call. Anything you'd want to "tune" lives here.

Organized by area:
  - Refresh rates
  - LLM commentary basics
  - Commentary cadence + triggers
  - Trigger config (per-trigger thresholds and cooldowns)

If you're looking for "is feature X enabled" or "what threshold does X use",
this is the file.
"""


# ─────────────────────────── Refresh rates (Hz) ───────────────────────────
# Each panel updates at its own rate. The display layer schedules these
# independently. The logger always ticks at LOGGER_HZ regardless.
#
# Rough guidance:
#   4 Hz  — snappy UI for fast-changing data (CPU)
#   2 Hz  — comfortable for moderately-changing data (RAM, network, GPU)
#   1 Hz  — slow-changing or expensive (processes, temps)
#   0.5+  — rarely-changing structural data (load avg, system info)
#   0.1   — practically static data (disk usage)

# Master frame rate. The whole dashboard refreshes on this single clock so
# panel updates land in the same paint pass — no visual jitter from panels
# on different cadences. Per-panel hz settings below still control how often
# each panel re-fetches its DATA; the frame rate just sets when the screen
# is repainted. 30 is comfortable; 60 is silky if your CPU has the headroom.
FRAME_HZ       = 30.0

# GPU-busy throttle: when a game is running the GPU spikes and the terminal
# emulator gets starved of redraws by Windows, making Winston feel laggy.
# We watch the GPU util and drop the dashboard's effective frame rate to
# ~5fps when the GPU is sustained above GPU_BUSY_PCT for GPU_BUSY_HOLD_SEC.
# Resumes full speed after GPU_CALM_HOLD_SEC of cool-down. Pure cosmetic
# throttle — background data threads keep running at their normal rates.
GPU_BUSY_PCT       = 50      # threshold (%)
GPU_BUSY_HOLD_SEC  = 3.0     # how long above threshold before throttling
GPU_CALM_HOLD_SEC  = 5.0     # how long below threshold before resuming

CPU_GRAPH_HZ   = 4.0
CPU_CORES_HZ   = 4.0
RAM_HZ         = 2.0
SYSTEM_HZ      = 0.5
DISK_HZ        = 0.1
TEMPS_HZ       = 1.0
GPU_HZ         = 2.0
NETWORK_HZ     = 2.0
PROCESSES_HZ   = 1.0

LOGGER_HZ      = 1.0  # fixed-rate CSV writes (clean time-series for analysis)

# How often TempsPanel re-fetches from LHM HTTP / PowerShell. Decoupled
# from TEMPS_HZ — the panel update is cheap, the fetch is expensive.
LHM_FETCH_INTERVAL_SEC = 3


# ─────────────────────────── LLM basics ───────────────────────────
# Master switch. False disables ALL LLM calls — Winston runs as pure
# monitoring with the COMMENTARY panel showing a static placeholder.
LLM_ENABLED = True

# Your name. Used in greetings ("Good morning, max."). None to skip.
USER_NAME = "max"

# Which Ollama model. Must already be pulled (run `ollama list` to see).
# Recommended: qwen2.5:7b-instruct (smart + fast, ~5GB VRAM)
# Alternatives: llama3.1:8b, qwen2.5:3b (faster, lower VRAM, less nuanced)
LLM_MODEL = "qwen2.5:7b-instruct"

# ─── Model tiering (free up VRAM during games) ────────────────
# Two-tier model strategy: a SMALL model for the boring 90% case (routine
# heartbeats, trigger commentary), a BIGGER QUALITY model for moments
# where it matters (alerts, conversational questions, startup ritual).
#
# The fast model stays VRAM-resident (keep_alive=-1, sub-second responses).
# The quality model uses a positive keep_alive, so Ollama unloads it after
# that idle window — your GPU is free for games again 5min after Winston's
# last quality-tier message.
#
# Set LLM_USE_TIERED to False to use LLM_MODEL for everything (simple mode).
LLM_USE_TIERED = True

# Small/fast model. Used for greeting, retrospective, heartbeat, routine,
# and all triggered events. Loaded on demand and unloaded after each call
# (see LLM_FAST_KEEP_ALIVE_SEC below) so VRAM is fully free between
# commentaries.
LLM_MODEL_FAST = "qwen2.5:3b-instruct"

# Big/quality model. Used ONLY for user questions (the `/`-ask path).
LLM_MODEL_QUALITY = "qwen2.5:7b-instruct"

# How long Ollama keeps each model loaded after its last use.
#
# 0 = unload immediately after the answer finishes. Both models are
# unloaded by default so the GPU is fully free between Winston's
# commentaries — heartbeat fires once every 5 minutes, triggered events
# even less often, so the cold-load cost is paid rarely. The 3B is small
# (~2GB) and cold-loads in 1-3 seconds; the 7B is ~6GB and cold-loads in
# 5-10 seconds. You'll see "thinking…" for that brief window each time
# Winston has something to say.
#
# Bump either to 60-300 if you want quick follow-ups (model stays in
# VRAM during that window after the last call) and don't mind the
# memory sitting there idle.
LLM_FAST_KEEP_ALIVE_SEC = 0
LLM_QUALITY_KEEP_ALIVE_SEC = 0

# DIAGNOSTIC FLAG — set False if you suspect the brain panel is causing
# UI lag (e.g. dropped keystrokes in the ASK input). When False, the
# brain panel widget isn't created and its 1Hz tick doesn't run. Memory
# + personalization still work fully; you just don't see the BRAIN panel.
SHOW_BRAIN_PANEL = True

# Whether to do the startup ritual on launch:
#   1. Greeting (time-aware: "Good morning, max." / "Good evening, max." /
#      "Up late tonight, max?" depending on hour)
#   2. Retrospective summary of last 24h from observation log
#   3. Begin regular commentary loop
STARTUP_GREETING = True


# ─────────────────────────── Commentary display ───────────────────────────
# How fast the typewriter reveals tokens to the UI. The LLM generates much
# faster (~80 tok/s); buffering and slowing makes the output feel deliberate.
# Stage 5.5 will let triggers override this per-tier (alerts type instantly).
TYPEWRITER_TPS = 25

# Pause between messages — gives the eye time to land on a finished message
# before the next one starts typing.
INTER_MESSAGE_PAUSE_SEC = 2.0

# How tall the COMMENTARY panel is (in lines of chat history visible).
# Older lines stay above the streaming line, dimmed.
COMMENTARY_LINES = 5


# ─────────────────────────── Commentary cadence ───────────────────────────
# Winston's commentary is event-driven (triggers fire when something
# interesting happens), with a periodic heartbeat as a baseline so it
# doesn't go totally silent.

# Heartbeat: a routine "all good / here's the state" comment fires this
# often even if no triggers have fired. 5 minutes is comfortable — present
# but not noisy. Set to 0 to disable heartbeats entirely (events only).
HEARTBEAT_INTERVAL_SEC = 300

# Stale check: if NOTHING has fired (no triggers, no heartbeat, no user
# input) for this long, force a routine update. This is a defense against
# misconfigured triggers leaving Winston silent for hours.
STALE_QUIET_THRESHOLD_SEC = 900  # 15 minutes


# ─────────────────────────── Triggers ───────────────────────────
# Each trigger watches one type of system event. When its conditions are
# met, it fires a TriggerEvent which becomes a commentary in the panel.
#
# Severity tiers control display behavior:
#   routine  — heartbeat-style, slow typewriter, can be preempted
#   notable  — something changed, medium typewriter, preempts routine
#   alert    — concerning, instant display, preempts anything
#
# Per-trigger config:
#   enabled       — turn this trigger on/off
#   cooldown_sec  — minimum time between consecutive fires of THIS trigger
#                    (prevents repeating the same observation 50 times)
#   thresholds    — the levels at which the trigger fires (varies per type)

TRIGGERS = {

    # Single core pegged while average looks fine.
    # Catches things like `yes > /dev/null` or runaway threads — cases
    # where average CPU% is misleading because one core is at 100%.
    "single_core_pegged": {
        "enabled":              True,
        "cooldown_sec":         180,
        "core_threshold_pct":   90,    # any single core above this %
        "avg_threshold_pct":    30,    # while average is below this %
        "severity":             "notable",
    },

    # Sustained high CPU across the whole machine.
    "cpu_sustained_high": {
        "enabled":              True,
        "cooldown_sec":         300,
        "avg_threshold_pct":    70,    # average above this for...
        "duration_sec":         30,    # ...this long
        "severity":             "notable",
    },

    # CPU thermal warnings.
    "cpu_thermal": {
        "enabled":              True,
        "cooldown_sec":         300,
        "notable_temp_c":       85,    # warmer-than-comfortable
        "alert_temp_c":         95,    # likely throttling
    },

    # GPU thermal warnings. Hot-spot temps run higher than die so the
    # thresholds are higher than CPU.
    "gpu_thermal": {
        "enabled":              True,
        "cooldown_sec":         300,
        "notable_temp_c":       82,
        "alert_temp_c":         92,
    },

    # Memory pressure — RAM near full or significant swap usage.
    "memory_pressure": {
        "enabled":              True,
        "cooldown_sec":         300,
        "notable_ram_pct":      85,
        "alert_ram_pct":        95,
        "notable_swap_pct":     20,    # any meaningful swap
        "alert_swap_pct":       50,
    },

    # Network burst — current rate well above recent baseline.
    # Uses anomaly detection: triggers when current rate is N standard
    # deviations above the rolling 5-min mean.
    "network_burst": {
        "enabled":              True,
        "cooldown_sec":         60,    # short — bursts are brief, want to catch them
        "sigma":                4.0,   # how many stddevs above baseline
        "min_rate_mbps":        20,    # don't trigger on tiny bursts
        "severity":             "notable",
    },

    # New heavy process — top-1 process changed AND new top is significant.
    "new_heavy_process": {
        "enabled":              True,
        "cooldown_sec":         120,
        "min_cpu_pct":          20,    # must be using at least this much CPU
        "severity":             "notable",
    },
}