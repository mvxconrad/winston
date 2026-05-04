# WINSTON

**W**ell-trained **I**ntuitive **N**eural **S**ystem **T**ranslating **O**bserved **N**umbers

A personal system monitor for the terminal. Watches CPU, RAM, GPU, disks, network, temperatures, and processes. Logs everything to CSV. A local LLM (Ollama) reads what's happening and produces dry, observant commentary — both on a heartbeat and event-driven when something noteworthy happens.

![Winston at idle](docs/screenshot-idle.png)

*Idle state — Winston greeted at 04:43, summarized the day, and is sitting quietly. Conversational input below for asking questions.*

![Winston reacting to a load spike](docs/screenshot-active.png)

*Triggered state — `yes > /dev/null` running on four cores. The `single_core_pegged` trigger fired, Winston is mid-sentence calling it out specifically.*

## Stack

- [Textual](https://github.com/Textualize/textual) — TUI framework
- [Rich](https://github.com/Textualize/rich) — text styling
- [psutil](https://github.com/giampaolo/psutil) — cross-platform system stats
- [pynvml](https://pypi.org/project/pynvml/) — NVIDIA GPU stats (optional)
- [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) — temperature data on Windows (optional but recommended on WSL)

## Run

```bash
pip install textual psutil pynvml
python winston.py
```

`Q` to quit, `R` to reset history graphs.

## Panels

Each panel runs at its own refresh rate (configurable in `winston.py`). The logger writes a row at fixed 1Hz regardless of panel rates — gives clean time-series data for analysis.

### CPU LOAD (4Hz)
Hand-rolled scrolling braille graph. Plotext was too jittery — re-laying out the full chart every frame caused axis labels to shift. This draws into fixed character positions: data scrolls right-to-left, axis labels are pinned. Same technique htop/btop use.

### CORES (4Hz)
Per-core bars. Bars are rendered from an 8-sample moving average so OS-scheduler-driven blips don't make cores visually flicker between 0% and 100%. The displayed `%` is still live.

### MEMORY / RAM (2Hz)
Single bar with size shown below (`3.2GB of 30.9GB`). Uses the same `X of Y` format as DISK and GPU VRAM for visual consistency.

### SYSTEM (0.5Hz)
Compact 6-line summary: load averages (1m/5m/15m), process count, thread count, swap usage, disk I/O rates, uptime. Disk I/O fades dim when idle (<1KB/s) so it doesn't visually compete when nothing's happening.

### DISK (0.1Hz)
Disk usage. Skips the WSL `/` mount — it lives inside a vhdx file on `C:` so showing it as a separate disk is misleading. Title auto-pluralizes (DISK vs DISKS) based on count.

### TEMPS (1Hz, LHM polled every 2s)
One row per device category — CPU, GPU, AIO, SSD, MOBO. Each row picks the most representative sensor for that device (CPU Tctl over individual cores, GPU Hot Spot over VRAM/Core, AIO Liquid over Sensor Critical). Filters out:
- BIOS trip-points (Critical Temp, Tj Max, etc.) — these are config values, not measurements
- Firmware placeholders (NZXT Krakens report exactly 85.0°C for sensors they don't actually have)
- Out-of-range readings (<5°C or >130°C)

Labels and bars are colored by temperature severity using a 7-stop heatmap palette — eye lands on hot rows.

### GPU (2Hz)
Util bar with `25W of 220W` power draw inline. VRAM bar with `2.1GB of 12.0GB` size inline. Three temp readings on one line: CORE / HOTSPOT / VRAM, each colored by its own value. HOTSPOT and VRAM come from LHM (nvidia-smi doesn't expose them).

GPU name strips redundant prefixes when space is tight: "NVIDIA GeForce RTX 4070 SUPER" → "RTX 4070 SUPER".

### NETWORK (panel renders at 2Hz, host source polled every 5s)
On WSL, defaults to **Windows host stats** via PowerShell `Get-NetAdapterStatistics`. WSL2's virtual interfaces only see traffic launched from inside WSL — Chrome, games, and most desktop apps run on Windows and never touch the WSL counters. Querying the host gives the real numbers.

PowerShell polling runs on a background thread (`winston-net-poller`) — never on the UI thread. **Polling interval is 5 seconds**, not faster, because each poll spawns a fresh PowerShell process across the WSL→Windows boundary (50-200ms each). Even on a daemon thread, faster polling caused visible input drops in the ASK box — see the *NetworkPanel and dropped keystrokes* note in [Architecture notes](#architecture-notes) for the full story.

Rates are smoothed over a 2-second rolling window to avoid spikes/zeros from uneven tick spacing. Peak download/upload rates are tracked persistently — on init, the panel scans `logs/raw/observations.csv` (single-pass O(n) max) to find the highest historical rates and seeds the graph scale with those. Graphs scale to observed peaks, so a speedtest fills the bars dramatically while routine YouTube traffic shows as a small fraction. The peak only ever grows.

### PROCESSES (1Hz)
Top 8 by CPU. CPU% column uses the same heatmap palette as everything else, so a process at 50% looks visually consistent with a CPU panel at 50%.

### COMMENTARY (event-driven, plus heartbeat + startup ritual)
A local Ollama LLM (default `qwen2.5:7b-instruct`) reads the panel state and produces dry, observant commentary. Three trigger paths:

1. **Startup ritual** — time-aware greeting (`Good morning, max.` / `Up late tonight, max?`) followed by a one-line retrospective summarizing the last 24h of logs.
2. **Event-driven** — `brain/triggers.py` evaluates per-second triggers (single core pegged, sustained CPU, thermal alerts, memory pressure, network burst, new heavy process). When one fires, Winston comments on the specific event with severity-aware tone.
3. **Heartbeat** — every `HEARTBEAT_INTERVAL_SEC` (default 5min) a routine "all good / here's the state" comment fires so Winston isn't silent during quiet periods.

Output streams token-by-token into the panel via a typewriter buffer that decouples LLM generation rate (~80 tok/s) from display rate (configurable, default 25 tps) — feels deliberate rather than blasted onto the screen. Press `/` to focus the ASK input below the panel and ask a question; the answer streams in and preempts whatever was streaming. Last 3 Q&A pairs are kept as multi-turn context.

The panel renders a fading-color chat log (newest message bright green, older lines fade through medium → dim → grey). Cursor blinks during THINKING/STREAMING states.

### BRAIN (1Hz, dirty-checked)
Winston's internal state, not the machine's: which model is loaded, the LLM client's current state (THINKING / STREAMING / IDLE / ERROR), what Winston knows about you (top apps from log scan with mini GPU-load bars), and the last trigger event that fired. Updates at 1Hz with a dirty-check skip — when nothing's changed, the tick is a near no-op and never re-renders.

Disabled with `SHOW_BRAIN_PANEL = False` in `config.py` if you want a tighter layout.

## Architecture notes

### Master frame loop (30 FPS)
The whole dashboard refreshes on a single clock — `FRAME_HZ = 30` in `config.py`. One `set_interval` in `display.py:WinstonApp.on_mount` drives `_frame_tick`, which checks each panel's per-panel rate and only fires `panel.update()` when the interval has elapsed. Widget refreshes are batched per frame so Textual's compositor sees one coherent dirty pass instead of many uncoordinated ones.

This is what btop/k9s/htop do, and it matters for two reasons:

1. **Visual consistency** — every visible refresh lands on the same 33ms grid, so panels don't jitter against each other's cadences.
2. **Input responsiveness** — fewer competing timers means the asyncio loop is free to read keystrokes between frames. With 14 separate `set_interval` calls (the previous design), the compositor was getting refresh requests at uncoordinated moments and stdin processing got starved.

Per-panel `*_HZ` constants in `config.py` still control how often each panel re-fetches data — they just gate when `update()` runs *within* the frame loop instead of being independent timers.

### The 5ms rule: heavy work goes on a thread
**Anything that takes more than ~5ms must run on a daemon thread, not the UI thread.** The UI panel just reads from a thread-safe cache. This is how btop adds 30 panels without lagging.

Currently on background threads:

| Thread | Module | Why |
| --- | --- | --- |
| `winston-lhm-poller` | `panels/lhm.py` | LHM HTTP fetch (~10-50ms) — both GPU and Temps panels read its cache |
| `winston-net-poller` | `panels/network.py` | PowerShell `Get-NetAdapterStatistics` (~50ms) |
| `winston-gpu-poller` | `panels/gpu.py` | pynvml / nvidia-smi calls into the WSL→Windows driver bridge (11-40ms spikes) |
| `winston-llm-worker` | `brain/client.py` | Ollama HTTP streaming — sub-second to several seconds per call |

`panel.update()` on the UI thread is then a sub-millisecond cache copy.

When adding a new feature: if it makes a syscall, an HTTP call, a subprocess call, or anything else that could exceed ~5ms — put it on a thread. Don't pollute the UI thread.

### LLM calls are FIFO, single-flight
`brain/client.py` runs **one** worker thread (`winston-llm-worker`) with a `queue.Queue` of jobs. All LLM calls — greeting, retrospective, triggered commentary, conversational answers — go through this queue. Two reasons:

1. **The UI thread never blocks on Ollama.** Calls can take seconds; the worker thread eats that latency, the UI thread keeps painting and reading input.
2. **Running multiple Ollama calls in parallel would just thrash a single GPU.** Two requests don't get answered twice as fast — they slow each other down. FIFO matches user expectations: "answer my question, *then* the next periodic update."

### Lazy stream timers in CommentaryPanel
The typewriter (25 Hz) and cursor blink (2.5 Hz) timers are only started in `_begin_streaming()` and stopped in `_finalize_message()` / `_on_error()`. While Winston is idle (no LLM call in flight), neither timer is scheduled — keeps the asyncio loop quiet between messages.

### Theme is a single source of truth
All color decisions live in `theme.py`. Panels never define colors locally — they import `heat_pct(percent)` and `heat_temp(celsius)` which return colors from a 7-stop palette. Switching the entire app's color scheme is a one-file edit.

### Bytes formatting
`fmt_bytes()` returns `KB / MB / GB / TB / PB` (powers of 1024 with conventional suffixes). Same as htop, btop, and Windows File Explorer.

### NetworkPanel and dropped keystrokes — the story behind the 5s poll
This one took a while to find, so it's worth writing down.

Symptom: typing into the ASK input dropped roughly 1 in 20 characters. Felt like network lag in a remote terminal but it was happening locally. Worse on long sentences, fine on short bursts.

What we ruled out (each tested in isolation):
- Textual version (8.2.5)
- Terminal/WSL itself (typing in plain bash was fine)
- Per-chunk LLM streaming repaints
- The brain panel (`SHOW_BRAIN_PANEL=False` — still dropped)
- Heavy panel render rates
- Synchronous CSV writes
- `psutil.process_iter` on the UI thread
- Eight progressively-loaded test apps that mimicked Winston's tick pattern (CPU sampling, Rich rendering, braille graphs, real `psutil` calls). All passed.

What the timing log eventually showed: nothing. Every individual UI-thread tick was under 10ms. Total UI-thread work was ~36ms/sec — only 3.6% utilization. The asyncio loop had plenty of headroom. So the culprit wasn't on the UI thread.

What it actually was: `panels/network.py` polls `Get-NetAdapterStatistics` via PowerShell on a daemon thread. The original interval was 0.5s (2Hz). Every poll spawns a fresh PowerShell process across the WSL→Windows boundary — 50-200ms of process creation per call. Even though `subprocess.run()` releases the GIL during the wait, the GIL handoffs around subprocess return + `text=True` decoding repeatedly poked the asyncio loop, and stdin reading occasionally lost a character to the noise.

We confirmed this with two env vars:
- `WINSTON_NO_NET=1` (network thread off, everything else on) → typing perfect
- `WINSTON_NO_LHM=1` (LHM thread off, network on) → typing still dropped

So: NetworkPanel's PowerShell loop was the only culprit. LHM JSON parsing on its thread (`panels/lhm.py`) wasn't enough to disrupt input, because that work is local — `urllib` releases the GIL cleanly during HTTP, and `json.loads` on ~10KB takes ~1-3ms. GPU's pynvml calls go through `ctypes` which also releases the GIL cleanly during the local `libnvidia-ml.so` calls. **What makes the network case bad is specifically that PowerShell-on-WSL spawns a Windows process every poll.**

Fix:
- Bumped `POLL_INTERVAL_SEC` from 0.5s to 5s in `panels/network.py`.
- Tested 0.5s / 2s / 5s — 0.5s dropped 1 in 20, 2s dropped occasional letters, 5s clean.
- Network rates feel less live but typing is solid; you still see speedtest-scale bursts in the graph because the peak tracking is persistent.

Proper future fix (not done yet): keep a long-lived PowerShell session and pipe `Get-NetAdapterStatistics` queries to its stdin. Eliminates per-call process churn so polling can go back to 1-2Hz without input cost. ~30 line change.

### Diagnostic env vars
For chasing future UI lag or input drops:

```bash
# Bisect background-thread GIL contention:
WINSTON_NO_THREADS=1 python3 winston.py    # both NetworkPanel and LHM threads off
WINSTON_NO_NET=1 python3 winston.py        # only NetworkPanel thread off
WINSTON_NO_LHM=1 python3 winston.py        # only LHM thread off

# Bisect UI-thread panel updates:
WINSTON_DISABLE_PANELS=GpuPanel,StatusBar python3 winston.py

# Catch slow ticks:
WINSTON_TIMING=1 WINSTON_TIMING_MS=5 python3 winston.py    # logs to /tmp/winston_timing.log
```

`WINSTON_DISABLE_PANELS` skips the named panels' ticks entirely (the panels still draw their primed values, they just don't refresh). `WINSTON_TIMING` writes a row to `/tmp/winston_timing.log` whenever a UI-thread tick exceeds the threshold (default 10ms, settable via `WINSTON_TIMING_MS`).

## Temperatures on WSL

WSL2's kernel doesn't include lm-sensors, so reading CPU/GPU/etc. temps directly from inside WSL doesn't work. Workaround: run [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) on Windows and bridge to it.

### One-time setup

```powershell
winget install LibreHardwareMonitor.LibreHardwareMonitor
```

Launch it as Administrator. Then:
- **Options → Remote Web Server → Run** (HTTP endpoint on port 8085)
- **Options → Run On Windows Startup**
- **Options → Minimize To Tray**

Add a firewall rule so WSL can reach LHM (PowerShell as Administrator):

```powershell
New-NetFirewallRule -DisplayName "LHM Web Server (WSL only)" `
  -Direction Inbound -LocalPort 8085 -Protocol TCP -Action Allow `
  -Profile Any -RemoteAddress 127.0.0.1,172.16.0.0/12
```

Locked to localhost + WSL subnet only. Other devices on the network can't reach it.

### How Winston finds LHM

Detection order:
1. Native Linux sensors (psutil) — works on bare Linux/macOS
2. Cached known-good host (last IP that worked, cached across LHM polls)
3. `localhost:8085`
4. Windows host gateway IP — auto-detected from `/proc/net/route`, no config needed; works after reboots, network changes, etc.
5. PowerShell WMI ACPI thermal zones (basic fallback, often returns nothing)
6. Polite "no sensors" message

The active backend is shown at the top of the TEMPS panel (`via LHM`, `via WMI`, etc.).

## Project layout

```
winston.py              # entry point — imports, section list, run() call (small + structural)
config.py               # all tunable behavior — refresh rates, LLM/trigger thresholds, FRAME_HZ
display.py              # Textual app, custom chrome, layout CSS, master frame loop, CpuGraphWidget
theme.py                # all color decisions; heat_pct() / heat_temp() helpers
logger.py               # CSV writer
input_test.py           # standalone Textual test app — bisect harness for input-drop debugging
panels/
  __init__.py
  base.py               # bar gauges, braille graph, fmt_bytes
  cpu_graph.py          # data class — display.py builds the actual graph widget
  cpu.py                # per-core bar grid
  ram.py                # memory bar
  system.py             # load avg, procs, threads, swap, I/O, uptime
  disk.py               # Windows + Linux mount listing
  temps.py              # multi-backend (native / LHM / WMI), smart device labels
  gpu.py                # pynvml or nvidia-smi + LHM enrichment, polled on its own thread
  network.py            # PowerShell host stats on WSL (5s poll), smoothed rates, peak tracking
  processes.py          # top N by CPU
  lhm.py                # shared LHM HTTP poller (background thread, single cache)
  brain.py              # BRAIN panel — visualizes Winston's internal LLM state
brain/
  __init__.py
  client.py             # Ollama client — sync, async, streaming; FIFO worker thread
  prompt.py             # observation/greeting/retrospective/triggered/conversational builders
  baselines.py          # rolling mean/stddev for per-metric anomaly detection
  triggers.py           # event-driven commentary — fires when conditions are met
  history.py            # single-pass O(n) CSV scanner for log retrospectives
  memory.py             # persistent JSON-backed memory of the user (most-used apps, machine facts)
ui/
  ask.py                # (legacy modal popup; not currently wired)
logs/
  raw/
    observations.csv    # 1 row/sec, all panel data — gitignored
  memory.json           # Winston's persistent memory of the user — gitignored
```

Adding a new panel: drop a class in `panels/` with `update()`, `render(width=None)`, `csv_headers()`, `csv_columns()` methods. Optional `title` property for dynamic panel titles. Add it to the section list in `winston.py` with its refresh rate, and add a layout slot in `display.py`.

Adding anything that does I/O, subprocess, or syscalls: put the slow work on a daemon thread and have `update()` read from a lock-guarded cache. See [The 5ms rule](#the-5ms-rule-heavy-work-goes-on-a-thread) above.

## Logging

Every second the logger appends one row to `logs/raw/observations.csv` with every panel's data. After a day: ~86,400 rows, ~12MB. Used for:
- Long-term trends
- Persistent network peak tracking
- Eventually: LLM commentary input

Gitignored.

## Roadmap

See `TODO.md`. Next up is **Stage 5: AI commentary** — wiring a local LLM (Ollama running qwen2.5:7b on the Windows host) to read recent observations and generate insights. The COMMENTARY panel at the bottom is already in the layout, just needs the LLM connection.

Long-shot is **Stage 9: Process Graph View** — Obsidian-style force-directed graph of running processes, with size encoding memory and color encoding CPU load. Pop it open with `G`, see the *shape* of what your computer is doing.

## Why "Winston"

The backronym came after the name. The idea: an AI butler watching over your machine. Always knows what's going on, quietly notices when things are off, tells you what matters.