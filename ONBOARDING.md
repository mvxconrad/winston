# Winston — Developer Onboarding

Welcome. This doc gets you from zero to running Winston and understanding where everything lives.

---

## What is Winston?

A local AI system monitor that watches your hardware and talks about it like Jarvis. Runs entirely on your machine — no cloud required (Claude API optional for smarter answers). Three frontends share one engine: a floating voice orb, a PyQt6 dashboard, and a Textual TUI.

**Stack:** Python 3.12, PyQt6, psutil, Ollama (local LLM), faster-whisper (STT), ElevenLabs/Piper (TTS).

---

## Branches

| Branch | What's on it | Status |
|--------|-------------|--------|
| `main` | Stable. TUI + GUI + voice orb, SensorHub, triggers, memory, deploy script. | Ship-ready |
| `feature/recon` | Everything on main PLUS: multi-tab dashboard (HARDWARE / COMMAND tabs), WinstonCore HUD circle, WinstonState unified state, push-to-talk everywhere, tactical green-on-black palette. Next up: RECON tab with CesiumJS globe. | Active development |

**Start from `feature/recon`** — it's the active branch. `main` will get a merge once RECON ships.

---

## Prerequisites

You need all of these on the **Windows** side (Winston runs natively on Windows, developed in WSL):

1. **Python 3.12** — [python.org](https://python.org), check "Add to PATH"
2. **Ollama** — [ollama.com](https://ollama.com), then pull models:
   ```
   ollama pull qwen2.5:3b-instruct
   ollama pull qwen2.5:7b-instruct
   ```
3. **LibreHardwareMonitor** — `winget install LibreHardwareMonitor.LibreHardwareMonitor`
   - Run as Admin → Options → Remote Web Server → Run (port 8085)
4. **WSL2** with `rsync` installed (`sudo apt install rsync`)

Optional: **ElevenLabs API key** in `.env` for premium TTS (Piper works as local fallback).

---

## First Run

```bash
# Clone and switch to active branch
git clone git@github.com:mvxconrad/winston.git
cd winston
git checkout feature/recon

# Deploy WSL → Windows (creates venv, installs deps, syncs files)
./deploy.sh

# On Windows — run with console (for debugging)
C:\Users\<you>\Winston\winston.bat

# Or run the voice orb (no console window)
# Double-click C:\Users\<you>\Winston\winston.vbs
```

The deploy script rsyncs source to `C:\Users\<you>\Winston\`, creates a Python venv, installs all pip deps, and downloads the Piper voice model. Re-run it after every edit cycle — it only copies changed files.

---

## Run Modes

| Command | What you get |
|---------|-------------|
| `winston.bat` | Watchdog mode — sits in system tray, orb pops up on triggers |
| `winston.bat --presence` | Always-visible floating orb |
| `winston.bat --gui` | Dashboard only (no voice) |
| `winston.bat` (default) | Same as watchdog |

From the orb: **double-click** or press **J** to open the dashboard. **Hold Space** to push-to-talk.

---

## Architecture (the important parts)

### Data flow

```
psutil / pynvml / LHM
        ↓
   panels/*.py          ← each panel knows how to read one subsystem
        ↓
   SensorHub            ← single daemon thread polls all panels, shared cache
        ↓
   ┌────┴────┐
   │         │
  Orb    Dashboard      ← both read from the SAME panel objects, no duplicate polling
   │         │
   └────┬────┘
        ↓
  brain/triggers.py     ← 1Hz tick, evaluates 7 triggers against rolling baselines
        ↓
  brain/commentary_engine.py  ← state machine, prompt building, LLM orchestration
        ↓
  brain/client.py       ← Ollama HTTP streaming (FIFO single-flight queue)
        ↓
  voice_engine.py       ← STT (faster-whisper) + TTS (ElevenLabs/Piper)
```

### Key design rules

- **SensorHub is the single source of truth** for hardware data. Nothing else calls `panel.update()`.
- **CommentaryEngine is backend-agnostic** — both frontends consume it. Adding a third frontend means writing a renderer, not duplicating LLM logic.
- **WinstonState (QObject)** is the single source of truth for Winston's visual state. All WinstonCore widgets subscribe to it.
- **One process.** The dashboard opens in-process from the orb. No subprocess.Popen.
- **5ms rule:** anything slower than 5ms runs on a daemon thread. The UI only reads thread-safe caches.

---

## File Map

```
winston.py                  # Entry point — picks frontend, creates SensorHub
sensor_hub.py               # Daemon thread polls panels, two-tier activation
config.py                   # ALL tunables (fps, thresholds, LLM, triggers)
theme.py                    # Color helpers shared across frontends

panels/                     # Data layer — shared across all frontends
  cpu.py, ram.py, gpu.py, temps.py, network.py, processes.py, system.py, disk.py
  lhm.py                   # LibreHardwareMonitor HTTP poller (background thread)
  brain.py                  # BRAIN panel data class

brain/                      # LLM + intelligence layer
  commentary_engine.py      # ★ The orchestrator — state machine, triggers, prompts
  client.py                 # Ollama HTTP client (FIFO worker thread)
  prompt.py                 # All prompt builders + personality block
  triggers.py               # 7 trigger functions + TriggerRunner
  baselines.py              # Rolling mean/stddev for anomaly detection
  memory.py                 # Persistent JSON memory + marker pipeline
  history.py                # CSV log scanner for retrospectives

brain/voice/                # Voice pipeline
  voice_engine.py           # STT + TTS orchestrator, push-to-talk state machine
  tts_elevenlabs.py         # ElevenLabs Flash v2.5 (primary)
  tts_piper.py              # Piper local fallback
  stt.py                    # faster-whisper (base.en, CPU)
  speaker.py                # PortAudio playback with drain callback

gui/                        # PyQt6 desktop frontend
  main.py                   # ★ Dashboard — tabs, layout, view widgets, event filter
  command.py                # ★ Command tab + WinstonCore HUD circle widget
  presence.py               # ★ Floating orb — PresenceWindow + PresenceFace + watchdog
  winston_state.py          # WinstonState QObject (state + amplitude signals)
  orb.py                    # Legacy orb widget (kept for reference)

cli/                        # Textual TUI frontend (alternate)
  display.py                # Textual app + CommentaryPanel

deploy.sh                   # One-command WSL → Windows deploy
logs/                       # gitignored — CSV telemetry, memory.json, reasoning traces
```

**Stars (★)** = the files you'll touch most.

---

## How the HUD circle works

`gui/command.py:WinstonCore` is a QPainter widget that draws concentric arcs, gravity-bounce ticks, orbital particles, and a breathing core glow. It's used in three places:

1. **Floating orb** — `show_label=False`, paints a radial gradient disc behind it
2. **Hardware tab** — top-right panel, `show_label=True`
3. **Command tab** — center column, `show_label=True`

All three subscribe to the same `WinstonState` object. State changes (IDLE → LISTENING → THINKING → SPEAKING) automatically change animation speed, tick energy, and color.

Performance: pre-built 256-entry color palettes, sin/cos lookup tables, PreciseTimer. Runs at `WINSTON_FPS` from config (default 60, Max runs at 100).

---

## Known Issues

- **Grey rectangle around floating orb on Windows.** Persists despite every Qt transparency trick in the book. Tracked in TODO.md. Not a dealbreaker but annoying.
- **Command tab needs cleanup** — vitals/triggers/alerts panels have placeholder data, not wired to live SensorHub yet.

---

## Config Quick Reference

Everything lives in `config.py`. Key knobs:

| Setting | Default | What it does |
|---------|---------|-------------|
| `WINSTON_FPS` | 60 | HUD circle animation framerate |
| `FRAME_HZ` | 10 | Dashboard data refresh rate |
| `LLM_ENABLED` | True | Kill switch for all LLM calls |
| `LLM_USE_TIERED` | True | 3B for routine, 7B for user questions |
| `WATCHDOG_MODE` | True | Start hidden in tray vs always-visible |
| `HEARTBEAT_INTERVAL_SEC` | 300 | How often Winston speaks unprompted |
| `TRIGGERS` | (dict) | All 7 trigger thresholds + cooldowns |

---

## Dev Workflow

```bash
# Edit code in WSL
cd ~/projects/sysmonitor
vim gui/command.py

# Deploy to Windows
./deploy.sh

# Test on Windows
# Double-click winston.bat or winston.vbs
# Use winston-debug.bat for presence mode with console output
```

Logs land in `C:\Users\<you>\Winston\logs\`. The deploy script pulls them back to WSL on each run so you can review reasoning traces from your editor.

---

## What's Next

See `TODO.md` for the full roadmap. The immediate priorities:

1. **Clean up Command tab** — wire live data into vitals/triggers/alerts
2. **RECON tab** — CesiumJS globe, fly-to animations, voice commands ("take me to Tokyo"), crime heatmaps ("light it up")
3. **Claude API backend** — `/claude` slash command for smarter answers

Questions? Ask Max or read the README for deeper architectural context.
