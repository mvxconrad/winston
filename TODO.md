# WINSTON — Roadmap

## Stage 1: Visual Foundation ✅
- [x] Live-updating panel (no scroll)
- [x] CPU + RAM display
- [x] Project name & README

## Stage 2: Better Stats Display ✅
- [x] Per-core CPU bar gauge layout (2-column)
- [x] Smoothed bars (8-sample MA) so cores don't visually flicker
- [x] Visual progress bars for CPU/RAM
- [x] Top processes by CPU usage
- [x] Disk usage panel (skips misleading WSL `/` mount)
- [x] Network up/down speeds
- [x] Uptime display (in status bar AND system panel)
- [x] Swap usage in system panel
- [x] Disk I/O rates in system panel

## Stage 3: Visual Polish ✅
- [x] Multi-panel layout (Textual grid, no Textual blue)
- [x] Custom black/green chrome (status bar, footer, round borders)
- [x] Hand-rolled scrolling braille CPU graph (htop/btop-style smooth scroll)
- [x] 7-stop heatmap palette (`theme.py` is single source of truth)
- [x] Color-coded warning thresholds across temps/CPU/RAM/disk/processes
- [x] Smart bytes formatting (KB/MB/GB/TB)

## Stage 4: GPU Support ✅
- [x] pynvml integration with nvidia-smi fallback
- [x] GPU usage, VRAM, temp panel
- [x] Power draw + power limit (`9W of 220W`)
- [x] CORE / HOTSPOT / VRAM temps inline (HOTSPOT and VRAM via LHM)
- [x] LHM enrichment for sensors nvidia-smi doesn't expose
- [ ] GPU-aware process detection (which app uses GPU) — future

## Stage 4.5: WSL Integration (unplanned but huge) ✅
- [x] LibreHardwareMonitor HTTP bridge for temps
- [x] Auto-detect Windows host IP from `/proc/net/route`
- [x] Shared LHM background poller (one cache, all panels read from it)
- [x] PowerShell host network stats (sees Chrome traffic, not just WSL traffic)
- [x] Background thread for PowerShell calls (doesn't block UI)
- [x] Smart sensor labeling with parent-chain context
- [x] Filter BIOS trip-points and firmware placeholders (NZXT 85°C bug)
- [x] Persistent network peak tracking from CSV log

## Stage 5: AI Commentary ✅
- [x] Install Ollama on Windows host
- [x] Firewall rule for port 11434 (mirror LHM rule)
- [x] Pull qwen2.5:7b-instruct
- [x] Verify connectivity from WSL: `curl http://<host>:11434/api/tags`
- [x] Python client with sync/async/streaming APIs (`brain/client.py`)
- [x] Observation summary prompt builder (`brain/prompt.py`)
- [x] CSV log scanner for retrospectives (`brain/history.py`)
- [x] Wire COMMENTARY panel to call Ollama every 30s
- [x] Streaming token display with blinking cursor
- [x] Time-of-day-aware greeting on startup ("Good morning, max")
- [x] 24-hour retrospective on startup
- [x] Multi-line chat-log style history with timestamps
- [x] Configurable typewriter speed (decouple LLM gen rate from UI display rate)

## Stage 5.5: Smart Triggers ✅ (mostly)
Polling replaced with event-driven triggers + heartbeat. `brain/triggers.py`
ticks at 1Hz, evaluates triggers against rolling baselines, fires
commentary when something actually happened.

- [x] `brain/triggers.py` — 1Hz tick, evaluates registered triggers
- [x] `brain/baselines.py` — rolling mean/stddev per metric (5-min window)
- [x] All trigger thresholds + cooldowns config-driven (`config.TRIGGERS`)
- [x] Starter triggers shipped:
  - [x] `single_core_pegged` (catches `yes > /dev/null` etc.)
  - [x] `cpu_sustained_high` (avg above threshold for N seconds)
  - [x] `cpu_thermal` (notable + alert temp bands)
  - [x] `gpu_thermal` (notable + alert temp bands)
  - [x] `memory_pressure` (RAM near full or swap usage)
  - [x] `network_burst` (anomaly vs rolling baseline)
  - [x] `new_heavy_process` (top-1 changed and is significant)
- [x] Heartbeat cadence (config: `HEARTBEAT_INTERVAL_SEC`, default 5min)
- [x] Stale-quiet defense (force a routine if NOTHING fires for 15min)
- [x] Per-trigger cooldowns prevent the same observation firing 50x
- [x] Triggered prompt — LLM gets the trigger description + state, comments on event specifically
- [x] Per-core CPU details in routine prompt (catches single-core peg even without trigger)
- [x] Color fade for chat history (newest bright → oldest grey)

## Stage 5.5b: Priority tier preemption (deferred)
Currently `alert`-tier triggers preempt the in-flight stream, but
`notable`-tier doesn't (we don't know what the current message's tier is).
Stage 5.5b makes preemption fully tier-aware.

- [ ] Track current message's tier on `CommentaryPanel`
- [ ] `notable` preempts `routine` mid-stream
- [ ] `alert` preempts anything mid-stream (already partly working)
- [ ] Per-tier typewriter speeds: routine slow, notable medium, alert instant
- [ ] Decide: resume interrupted message after high-priority finishes? (probably no)

## Stage 5.6: Conversational Mode (partial — input shipped, tools pending)
- [x] Text input below COMMENTARY panel (`/` to focus, vim-style)
- [x] Route user query through LLM with current state as context
- [x] Multi-turn: keeps last 3 Q&A pairs as context for follow-ups
- [x] User questions preempt routine commentary
- [ ] Tools the model can call (Tier 1: read-only)
  - [ ] `read_log(minutes)` — pull recent CSV rows
  - [ ] `get_process_details(pid)` — full info on a specific process
  - [ ] `query_baseline(metric, window)` — "what's normal for this?"
- [ ] Tools (Tier 2: user-confirmed actions)
- [ ] Tools (Tier 3: sudo allow-list, very long-term)

## Stage 5.7: Refactor pass (cleanup before more features)
Before adding more stuff, split the files that have grown >500 lines.
Refactor along natural seams, not just for line count.

- [ ] `display.py` (~750 lines) — split:
  - [ ] Move `CommentaryPanel` → `brain/commentary.py` (LLM stuff lives with LLM stuff)
  - [ ] Move `CpuGraphWidget` → `panels/cpu_graph_widget.py` (next to its data class)
  - [ ] Keep `WinstonApp`, `PanelWidget`, `StatusBar`, `FooterBar` in `display.py`
- [ ] `panels/temps.py` (~500 lines) — split:
  - [ ] Move backend functions (`_try_native`, `_try_lhm_http`, `_try_powershell_wmi`)
        and the `smart_label` helper to `panels/temps_backends.py`
  - [ ] `panels/temps.py` keeps just the `TempsPanel` class
- [ ] `panels/network.py` (~450 lines) — leave for now, it's one cohesive job
- [ ] `panels/gpu.py` (~360 lines) — leave for now, well-organized internally

## Stage 6: Local LLM tuning
- [ ] Compare 7B vs 3B model speed/quality tradeoff
- [ ] Optimize prompts for smaller models
- [ ] Try qwen2.5:3b for routine, llama3.1:8b for alerts (small fast / big smart)

## Stage 7: Long-term Tracking
- [ ] Migrate from CSV to SQLite when log gets unwieldy
- [ ] Daily/weekly summaries
- [ ] Pattern detection over days/weeks
- [ ] "You've been at >80% CPU 47 min today, take a break"

## Stage 8: Stretch / Cool Ideas
- [ ] System tray notifications
- [ ] Health/wellness commentary (hours at desk, etc.)
- [ ] Cost tracking (API spend, electricity estimate)
- [ ] Cross-machine: monitor your desktop from anywhere
- [ ] Theme variants (amber CRT, cyan terminal, etc. — `theme.py` is set up for this)

## Stage 9: Process Graph View (the big one)
- [ ] `press G` opens process map in popup window
- [ ] Nodes = processes, size = memory, color = CPU load
- [ ] Edges = parent/child process tree
- [ ] Later: edges include network connections, shared file handles
- [ ] AI reads graph state for commentary ("chrome cluster grew 40%")
- [ ] Eventually: web UI mode for live graph alongside TUI