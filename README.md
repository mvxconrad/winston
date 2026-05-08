<h1 align="center">W I N S T O N</h1>

<p align="center">
  <b>W</b>ell-trained &nbsp;·&nbsp; <b>I</b>ntuitive &nbsp;·&nbsp; <b>N</b>eural &nbsp;·&nbsp; <b>S</b>ystem &nbsp;·&nbsp;
  <b>T</b>ranslating &nbsp;·&nbsp; <b>O</b>bserved &nbsp;·&nbsp; <b>N</b>umbers
</p>

<p align="center"><i>Most system monitors show numbers. Winston watches the same numbers, learns what they mean for you, and talks about your machine like a person who knows you would.</i></p>

A personal system monitor with a local-LLM butler. Watches CPU, RAM, GPU, disks, network, temperatures, and processes. Logs everything to CSV. A local Ollama model reads panel state and writes commentary that **gets more personal over time** — what apps you run, how you feel about them, when you run them, what your machine usually looks like. The bridge between raw hardware telemetry and useful observation is persistent memory ([see Memory](#memory)). Two frontends share the same engine: a PyQt6 desktop app (`--gui`) and a Textual TUI (default).

## Quick links

- [Run it](#run)
- [Deploy (WSL → Windows)](#deploy-wsl--windows)
- [Panels](#panels)
- [LLM commentary](#llm-commentary)
- [Memory](#memory) — what makes Winston more than a fancy `top`
- [Architecture](#architecture)
- [WSL setup (temps + network)](#wsl-setup-temps--network)
- [Diagnostic env vars](#diagnostic-env-vars)
- [Project layout](#project-layout)
- [Roadmap](#roadmap)

## Stack

[Textual](https://github.com/Textualize/textual) + [Rich](https://github.com/Textualize/rich) (TUI), [PyQt6](https://pypi.org/project/PyQt6/) + [pyqtgraph](https://pyqtgraph.org/) (GUI), [psutil](https://github.com/giampaolo/psutil), [pynvml](https://pypi.org/project/pynvml/) (optional), [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) (optional, for WSL temps), [Ollama](https://ollama.com) (optional, for commentary), [ElevenLabs](https://elevenlabs.io) + [Piper](https://github.com/rhasspy/piper) (TTS), [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (STT).

## GUI (desktop)

`python winston.py --gui` — native PyQt6 window, GPU-accelerated charts via pyqtgraph, scrollable, smooth at 60 fps even while gaming.

![Winston GUI](docs/winston_gui_v1.png)
*Same panels as the TUI plus the BRAIN view (state · model · top apps · memory stats).*

## CLI (TUI)

`python winston.py` — runs in any terminal, hand-rolled braille graphs, full keyboard control.

![Winston at idle](docs/screenshot-idle.png)
*Idle — greeted, summarized the day, sitting quietly.*

![Winston reacting to a load spike](docs/screenshot-active.png)
*Triggered — `yes > /dev/null` on four cores. The `single_core_pegged` trigger fired.*

## Voice (presence orb)

`python winston.py --presence` — a floating translucent orb that sits on your screen. Hold **Space** to push-to-talk. Winston listens via faster-whisper (STT), thinks via Ollama, and responds aloud via ElevenLabs Flash v2.5 (primary) or Piper (local fallback). The orb pulses with audio amplitude and shifts color with state (green idle, bright green listening, amber thinking, bright green speaking).

## Watchdog (default)

`python winston.py` — the default mode. Winston starts hidden in the system tray with no visible window. The brain runs in the background monitoring the same triggers as every other mode. When a hardware spike fires (CPU pegged, thermal alert, memory pressure, etc.), the orb pops up, Winston speaks the observation, then hides back to the tray after a configurable linger period (`WATCHDOG_LINGER_SEC`, default 8s). Click the tray icon to summon Winston — he greets you with a time-aware hello and stays visible until you close him. Push-to-talk works the same as presence mode.

Controlled by `WATCHDOG_MODE`, `WATCHDOG_LINGER_SEC`, and `WATCHDOG_SUPPRESS_HEARTBEAT` in `config.py`.

## Run

```bash
pip install textual psutil pynvml PyQt6 pyqtgraph
ollama pull qwen2.5:3b-instruct           # routine commentary
ollama pull qwen2.5:7b-instruct           # loaded on demand for /-asked questions
python winston.py                          # watchdog (default) — dormant in tray, wakes on spikes
python winston.py --presence               # always-visible orb + voice
python winston.py --gui                    # PyQt6 desktop dashboard
python winston.py --cli                    # Textual TUI
```

`Q` quit · `R` reset graphs · `/` focus the ASK input. GUI also has `F11` fullscreen and `Ctrl+↑/↓/←/→` snap. Presence/watchdog: **Space** push-to-talk · **J** open dashboard · **Ctrl+Q** quit.

All tunables live in `config.py` (refresh rates, LLM behavior, trigger thresholds). `winston.py` is just imports + the section list.

## Deploy (WSL → Windows)

If you develop in WSL but want Winston running natively on Windows (for proper system tray, native audio, accurate process monitoring), a single command syncs everything:

```bash
cd ~/projects/sysmonitor
chmod +x deploy.sh
./deploy.sh
```

What it does (re-runnable, idempotent):

1. **Syncs source** to `C:\Users\<you>\Winston` via rsync (only changed files)
2. **Creates a Python venv** on the Windows side (if missing)
3. **Installs pip dependencies** (PyQt6, faster-whisper, elevenlabs, etc.)
4. **Downloads Piper voice model** (~50 MB, one-time)
5. **Copies `.env`** (ElevenLabs API key) if present
6. **Generates launchers** — `winston.vbs` (no console, daily driver), `winston.bat` (debug), `winston-debug.bat` (presence mode)
7. **Creates a Windows Startup shortcut** so Winston launches on login

Requirements: WSL2 with rsync, Windows Python 3.10+ on PATH, Ollama running on Windows. Override the auto-detected username with `WIN_USER=yourname ./deploy.sh` if needed.

## Panels

| Panel | Rate | What |
| --- | --- | --- |
| **CPU LOAD** | 4 Hz | Hand-rolled scrolling braille graph (htop/btop-style). |
| **CORES** | 4 Hz | Per-core bars, 8-sample MA so blips don't flicker. |
| **MEMORY** | 2 Hz | Single bar with `X of Y` size inline. |
| **SYSTEM** | 0.5 Hz | Load avgs, procs, threads, swap, disk I/O, uptime. |
| **DISK** | 0.1 Hz | Disk usage. Skips WSL `/` (vhdx on `C:`). |
| **TEMPS** | 1 Hz | One row per device category — best representative sensor each, BIOS trip-points and firmware placeholders filtered. |
| **GPU** | 2 Hz | Util + power inline, VRAM + size inline, CORE/HOTSPOT/VRAM temps inline. |
| **NETWORK** | renders 2 Hz, host source polled **every 5 s** | Windows host stats via PowerShell (sees Chrome traffic). 5 s is intentional — see [the investigation](#investigation-networkpanel-and-dropped-keystrokes) below. |
| **PROCESSES** | 1 Hz | Top 14 by CPU, merged from psutil (Linux/WSL) + PowerShell `Get-Process` (Windows host, daemon-cached every 5 s). Windows rows tagged `[win]`. |
| **COMMENTARY** | event-driven | LLM-generated; see next section. |
| **BRAIN** | 1 Hz, dirty-checked | Winston's internal state — current model, top apps from [memory](#memory), last trigger fired, memory.json stats. Toggleable via `SHOW_BRAIN_PANEL`. |

## LLM commentary

A local Ollama model reads panel state and writes dry one-liners. Three trigger paths:

1. **Startup ritual** — time-aware greeting (`Good morning, max.` / `Up late tonight, max?`) followed by a one-line retrospective from the last 24h of logs.
2. **Event-driven** — `brain/triggers.py` evaluates per-second triggers (single core pegged, sustained CPU, thermal alerts, memory pressure, network burst, new heavy process, host app busy). When one fires, Winston comments on the specific event. Hysteresis on `new_heavy_process` (sustain_sec=3) kills 1-tick spike announcements.
3. **Heartbeat** — every `HEARTBEAT_INTERVAL_SEC` (default 5 min) a routine status comment so Winston isn't silent during quiet periods.

Output streams token-by-token via a typewriter buffer (decouples LLM gen rate from display rate). Press `/` to focus the ASK input and ask a question — last 3 Q&A pairs are kept as multi-turn context.

Every prompt is personalized from [memory](#memory) — Winston sees what you call each app (`nickname`), what kind of thing it is (`type`), how you feel about it (`feeling`), and what your machine usually looks like, so his commentary references *you* instead of restating telemetry.

### Tiered model loading (so VRAM stays free for games)

Both models default to `keep_alive=0` — they load on demand, generate the answer, and unload immediately. Between commentaries, Winston holds zero VRAM. Cold loads are quick because Ollama keeps the weights in system RAM:

| Path | Model | Cold load | Triggers |
| --- | --- | --- | --- |
| Greeting, retrospective, heartbeat, routine, all triggered events | **qwen2.5:3b-instruct** (~2 GB) | 1-3 s | Startup ritual, every 5 min heartbeat, occasional event triggers |
| User questions via `/` | **qwen2.5:7b-instruct** (~6 GB) | 5-10 s | Only on `/`-ask |

You'll see "thinking…" for that brief window each time Winston has something to say. Bump `LLM_FAST_KEEP_ALIVE_SEC` or `LLM_QUALITY_KEEP_ALIVE_SEC` in `config.py` (e.g. to 60-300) if you want quick follow-ups at the cost of VRAM idle.

## Memory

What makes Winston more than a fancy `top`. The dashboard shows numbers; memory is what lets him *recognize* what those numbers mean for you. `logs/memory.json` is the canonical store. Two ways things end up there:

**1. Auto-derived from observation.** Every second the CSV log gains a row. `learn_from_log()` scans the last 7 days and ranks apps by how long they were the top process — building per-app fingerprints (avg CPU, peak CPU, avg GPU when this app was top, hours seen). Runs on launch.

**2. User-told in conversation.** When you tell Winston something — *"Ark is my favorite game"* — he ends his reply with an invisible marker that gets parsed, stripped from the displayed text, and applied to memory:

- `[APP: <process-name> key=value, key=value, -dropkey]` — per-app structured attrs. Multiple keys allowed, **they merge** (setting `nickname=Ark` doesn't erase a prior `feeling=favorite`). Prefix `-key` to drop a single attr.
- `[REMEMBER: <fact>]` — free-form note about you that doesn't bind to one app.
- `[FORGET: <existing note>]` — revoke a saved note.

Markers are physically cut from the streaming buffer the moment the closing `]` arrives, so the user never sees them flicker. Same treatment for "Now let's respond:" planning preambles the model occasionally leaks.

### What memory looks like

```json
{
  "user": { "name": "max" },
  "machine": { "host": "PC", "cpu": "...", "gpu": "...", "ram_gb": 30.9 },
  "apps": {
    "arkascended": {
      "name": "ArkAscended",      // canonical (locked, never renamed)
      "hours": 0.45,              // ↓ AUTO from CSV scan
      "avg_cpu": 16.5,
      "peak_cpu": 41.6,
      "avg_gpu_when_top": 39.1,
      "nickname": "Ark",          // ↓ user-told via [APP:] markers
      "type": "game",
      "feeling": "favorite"
    }
  },
  "notes": [
    { "ts": "...", "text": "max is gaming whenever ArkAscended is running", "source": "user" }
  ]
}
```

`AUTO_APP_KEYS` (name, hours, avg_cpu, peak_cpu, avg_gpu_when_top) come first in each entry's JSON; user-added keys append after. `learn_from_log` rebuilds the auto fields on each scan but preserves user keys.

### Suggested attribute keys

Not enforced — Winston can invent his own when nothing fits.

| Key | What | Example values |
| --- | --- | --- |
| `nickname` | What you call it | `Ark`, `Doom`, `vscode` |
| `type` | What kind of app | `game`, `ide`, `browser`, `comm`, `music`, `dev` |
| `feeling` | How you feel about it | `favorite`, `hate`, `necessary`, `fun` |
| `role` | When/why it runs | `work`, `leisure`, `background`, `daily` |
| `launched_via` | How it gets started | `steam`, `epic`, `manually`, `cron` |
| `notes` | Short per-app fact | `"crashes on launch"`, `"streaming only"` |

### Robustness — input forgiveness, no hardcoded user data

Winston isn't perfect at marker syntax. The memory layer corrects on the fly:

- **Suffix stripping** — `_normalize_app_key("ArkAscended.exe")` → `arkascended`. Same canonical key whether the model writes the `.exe` or not.
- **Nickname-aware lookup** — `[APP: Ark ...]` after `nickname=Ark` is set finds the existing entry, doesn't fork a duplicate.
- **Unique prefix matching** — `[APP: Ark ...]` lands on `arkascended` if no other key starts with "ark". 3-char floor + uniqueness requirement keep "node" from accidentally swallowing "nodejs".
- **Attribute bucket auto-correction** — `type=favorite` (mis-bucketed; "favorite" is a feeling-word) auto-swaps to `feeling=favorite`. Vocabularies are starter sets; new values pass through unchanged.
- **Migration on load** — older memory.json files with `.exe` duplicates, orphan short-name entries, mis-bucketed attrs, or angle-bracket placeholder leaks (`<max>`) get cleaned up automatically when the file loads.

### How prompts read memory

The personality block at the top of each prompt is built from memory. Apps render with **user-told facts FIRST**, auto stats trail in parens. Nicknames replace canonical names so the model sees "Ark" not "ArkAscended" in the snapshot. Notes auto-flip from third-person storage to second-person at render time ("max codes at night" stored → "you code at night" in the prompt) so the model addresses you, not a third party. The user's name is read from `memory.get_user_name()` — nothing about the prompt is hardcoded to a specific person.

### How to inspect

- **BRAIN panel** — top apps + last trigger + `MEMORY N apps · M notes · learned HH:MM:SS`.
- **`cat logs/memory.json`** — full store, hand-readable.
- **`tail -f logs/reasoning.log`** — every prompt and response in real-time, with markers Winston emitted (and what they did to memory).

## Architecture

### SensorHub — single source of truth

All hardware data flows through `sensor_hub.py:SensorHub`. One daemon thread polls every panel; both the orb and the dashboard read the same panel objects. No duplicate polling, no second process.

Two tiers of panels:

| Tier | Panels | When polled |
| --- | --- | --- |
| **Essential** | CPU, RAM, System, Temps, GPU, Network, Processes | Always — needed for triggers |
| **Dashboard-only** | CpuGraphPanel, DiskPanel | Only while the dashboard is open |

When you open the dashboard from the orb (J key, double-click, or voice command "open the dashboard"), it creates the window **in-process** — same panels, same hub, no subprocess. `hub.activate_all()` starts polling the extras. Close the dashboard and `hub.deactivate_extras()` drops back to essentials. The dashboard never calls `panel.update()` — it only refreshes view widgets from the already-updated data.

### Master frame loop (10 FPS)

The dashboard refreshes on a single 10 Hz clock (`FRAME_HZ` in `config.py`). Each tick checks per-panel rate gating and only refreshes views whose data has been updated. Satellite mode (dashboard opened from the orb) runs at 5 fps to save CPU for games.

### The 5 ms rule: heavy work goes on a thread

Anything that takes more than ~5 ms must run on a daemon thread. The UI panel just reads from a thread-safe cache. This is how btop adds 30 panels without lagging.

| Thread | Module | Why |
| --- | --- | --- |
| `winston-lhm-poller` | `panels/lhm.py` | LHM HTTP fetch — GPU and Temps panels share the cache. |
| `winston-net-poller` | `panels/network.py` | PowerShell `Get-NetAdapterStatistics` (50-200 ms each). |
| `winston-gpu-poller` | `panels/gpu.py` | pynvml driver calls (11-40 ms spikes on WSL). |
| `winston-llm-worker` | `brain/client.py` | Ollama HTTP streaming. |

### LLM calls are FIFO, single-flight

`brain/client.py` runs *one* worker thread with a `queue.Queue`. Reasons: UI thread never blocks on Ollama (calls take seconds), and parallel calls would just thrash a single GPU. FIFO matches user expectations.

### Lazy stream timers in CommentaryPanel

The typewriter (25 Hz) and cursor blink (2.5 Hz) timers are only scheduled while a stream is in flight. Idle Winston has zero CommentaryPanel timers running.

### Two frontends, one orchestrator

Winston ships two frontends — `cli/display.py` (Textual TUI, default) and `gui/main.py` (PyQt6 desktop, `--gui`) — and the LLM commentary logic lives in **neither**. It lives in `brain/commentary_engine.py:CommentaryEngine`, which owns:

- The state machine (THINKING / STREAMING / IDLE / ERROR / DISABLED)
- Q&A history + multi-turn context (last 3 pairs)
- Streaming buffer + typewriter cursor advancement
- Trigger evaluation (the 7 triggers in `brain/triggers.py` plus the 1Hz busy-gate / heartbeat / stale-quiet rules)
- Prompt-building dispatch (`build_greeting / build_retrospective / build_triggered / build_observation / build_conversational`)
- Model tier selection

Each frontend is a thin renderer + timer driver that calls into the engine. Adding a third frontend (web, mobile) just means writing another renderer — no LLM logic to duplicate.

Why this matters: when we first added the GUI, the trigger evaluation got duplicated and rules drifted between frontends. Extracting into the engine fixed both copies at once and means future trigger / heartbeat tweaks live in one place.

### Investigation: NetworkPanel and dropped keystrokes

This bug took a while to find — keeping the writeup short here, but it's worth knowing why `NETWORK` polls so slowly.

Symptom: typing into the ASK input dropped ~1 in 20 characters. Felt like network lag, but local.

What it actually was: `panels/network.py` polls `Get-NetAdapterStatistics` via PowerShell on a daemon thread. Every poll spawns a fresh PowerShell process across the WSL→Windows boundary — 50-200 ms of process creation per call. Even though `subprocess.run()` releases the GIL during the wait, the GIL handoffs around subprocess return + `text=True` decoding repeatedly poked the asyncio loop, and stdin reading occasionally lost a character to the noise.

Tested 0.5 s / 2 s / 5 s polling: 0.5 s dropped 1 in 20, 2 s dropped occasional letters, **5 s clean**. So `POLL_INTERVAL_SEC = 5.0` in `panels/network.py`, with a comment.

Confirmed via `WINSTON_NO_NET=1` (typing perfect) vs `WINSTON_NO_LHM=1` (typing still dropped) — only the cross-OS PowerShell path causes drops. LHM JSON parsing on its thread doesn't, because that work is local; same for pynvml via ctypes.

Proper future fix: keep a long-lived PowerShell session and pipe queries to its stdin — eliminates per-call process churn, can go back to 1-2 Hz polling. ~30 line change.

## Diagnostic env vars

For chasing future UI lag or input drops:

```bash
# Disable background threads (bisect GIL contention):
WINSTON_NO_THREADS=1   # both NetworkPanel and LHM threads off
WINSTON_NO_NET=1       # only NetworkPanel thread off
WINSTON_NO_LHM=1       # only LHM thread off

# Disable specific panel ticks (they still draw their primed values):
WINSTON_DISABLE_PANELS=GpuPanel,StatusBar python3 winston.py

# Catch slow ticks (logs to /tmp/winston_timing.log):
WINSTON_TIMING=1 WINSTON_TIMING_MS=5 python3 winston.py
```

## WSL setup (temps + network)

WSL2 doesn't include lm-sensors, and its virtual NIC only sees traffic launched from inside WSL. Winston bridges to Windows for both.

### LibreHardwareMonitor (for temps)

```powershell
winget install LibreHardwareMonitor.LibreHardwareMonitor
```

Launch as Administrator, then:
- **Options → Remote Web Server → Run** (port 8085)
- **Options → Run On Windows Startup**
- **Options → Minimize To Tray**

Firewall (PowerShell as Admin):

```powershell
New-NetFirewallRule -DisplayName "LHM Web Server (WSL only)" `
  -Direction Inbound -LocalPort 8085 -Protocol TCP -Action Allow `
  -Profile Any -RemoteAddress 127.0.0.1,172.16.0.0/12
```

Locked to localhost + WSL subnet only. Winston auto-detects the Windows host IP from `/proc/net/route`.

### Ollama (for commentary)

Install Ollama on Windows. Same firewall pattern as LHM, port `11434`. Pull both models so tiering works (the 3B is the resident one and the only one that truly needs to be there):

```powershell
ollama pull qwen2.5:3b-instruct
ollama pull qwen2.5:7b-instruct
```

## Project layout

```
winston.py              # entry point — picks frontend, creates SensorHub
sensor_hub.py           # single source of truth — daemon thread polls panels,
                        # two-tier activation (essential vs dashboard-only)
config.py               # all tunables (FRAME_HZ, GPU_BUSY_*, panel hz, LLM, triggers)
theme.py                # color decisions: heat_pct() / heat_temp() helpers
logger.py               # 1 Hz CSV writer
input_test.py           # standalone Textual harness for input-drop debugging

panels/                 # data layer — SHARED across frontends
  base.py               # bar gauges, braille graph, fmt_bytes
  cpu_graph.py          # CPU-history data class
  cpu.py · ram.py · system.py · disk.py · processes.py
  temps.py              # multi-backend (native/LHM/WMI), smart device labels
  gpu.py                # pynvml/nvidia-smi + LHM enrichment, own poll thread
  network.py            # PowerShell host stats (5 s poll), smoothed, peak-tracked
  lhm.py                # shared LHM HTTP poller (background thread, single cache)
  brain.py              # BRAIN panel data class (rendered by both frontends)

brain/                  # LLM layer — SHARED across frontends
  client.py             # Ollama client; FIFO worker thread
  prompt.py             # all prompt builders + personality block
  baselines.py          # rolling mean/stddev for anomaly detection
  triggers.py           # trigger functions + TriggerRunner
  history.py            # single-pass O(n) CSV scanner
  memory.py             # persistent JSON-backed memory + marker pipeline.
                        # See [Memory](#memory).
  commentary_engine.py  # backend-agnostic orchestrator — state machine,
                        # trigger evaluation, heartbeat, stale-quiet,
                        # Q&A history, marker parsing/cutting on stream.
                        # Both frontends consume this.

cli/                    # TUI frontend — only renders engine state
  display.py            # Textual app + CommentaryPanel renderer
  ui/ask.py             # legacy modal popup (not currently wired)

gui/                    # desktop frontend — only renders engine state
  main.py               # PyQt6 + pyqtgraph; QMainWindow + view widgets
  presence.py           # voice orb face: PresenceWindow + PresenceFace +
                        # watchdog lifecycle (tray icon, linger timer,
                        # trigger-driven show/hide)
  orb.py                # reusable QWidget — glowing circle, state-colored,
                        # amplitude-reactive. Decoupled via callbacks.

brain/voice/            # voice pipeline
  voice_engine.py       # STT + TTS orchestrator, push-to-talk state machine
  tts_elevenlabs.py     # ElevenLabs Flash v2.5 (primary TTS)
  tts_piper.py          # Piper local fallback TTS
  stt.py                # faster-whisper STT (base.en, CPU)
  speaker.py            # PortAudio playback with drain callback

deploy.sh               # one-command WSL → Windows deploy (see above)

logs/                   # gitignored
  raw/observations.csv  # 1 row/sec time-series CSV
  memory.json           # canonical persistent memory
  reasoning.jsonl       # every prompt + response (machine-parseable)
  reasoning.log         # same events, human-readable mirror
```

**Adding a panel:** drop a class in `panels/` with `update()`, `render(width=None)`, `csv_headers()`, `csv_columns()`. Add it to the section list in `winston.py` and a layout slot in BOTH `cli/display.py` (`compose()`) and `gui/main.py` (`WinstonGui.__init__` row layout). The data class is shared.

**Adding a trigger:** write a function in `brain/triggers.py` taking `(sections, baselines, cfg)` and returning a `TriggerEvent` or `None`. Register it in `TRIGGER_FUNCTIONS` and add a config block in `config.py:TRIGGERS`. Both frontends pick it up automatically — the engine evaluates all registered triggers at 1 Hz.

**Adding anything that does I/O, subprocess, or syscalls:** put the slow work on a daemon thread. UI panel reads from a lock-guarded cache. See [the 5 ms rule](#the-5-ms-rule-heavy-work-goes-on-a-thread).

## Roadmap

See [`TODO.md`](TODO.md). Short version of what's next:

- **Streaming TTS** — pre-buffer ~1.5s of LLM output then begin TTS while the model is still generating. Cuts perceived latency roughly in half on longer replies.
- **Orb visual polish** — state-aware text color, amplitude-scaled glow layer, final sizing pass.
- **5.5b** — fully tier-aware preemption (notable preempts routine mid-stream, not just alerts).
- **5.6** — tools the LLM can call (`read_log`, `get_process_details`, `query_baseline`).
- **5.7** — refactor: split `display.py` and `panels/temps.py` along natural seams.
- **9** — process graph view (`G` opens an Obsidian-style force-directed graph; nodes = processes, size = memory, color = CPU).

## Why "Winston"

The backronym came after the name. The idea: an AI butler watching over your machine. Always knows what's going on, quietly notices when things are off, tells you what matters.
