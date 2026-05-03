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

## Stage 5.5: Smart Triggers + Priority Tiers (next)
The current 30s polling is dumb — it talks regardless of whether anything
is happening. Replace with a trigger system that fires when something is
actually noteworthy. Tiered priority handles race conditions cleanly.

- [ ] `brain/triggers.py` — runs at 1Hz, scores current state vs recent baseline
- [ ] Rolling baselines per metric (CPU avg, GPU temp, RAM, network, etc.)
- [ ] Trigger conditions:
  - [ ] CPU avg jumped >2x baseline
  - [ ] GPU temp crossed thresholds (70°C, 80°C, 85°C)
  - [ ] New process appeared in top 5
  - [ ] Network rate jumped >5x baseline
  - [ ] Stale check: if nothing's fired in N minutes, force a routine update
- [ ] Priority tiers:
  - [ ] `routine`  — slow typewriter (~5 tps), can be interrupted
  - [ ] `notable`  — medium typewriter (~15 tps), preempts routine
  - [ ] `alert`    — instant display, always preempts
- [ ] Race-condition handling:
  - [ ] Lower-tier message in flight gets interrupted by higher-tier
  - [ ] Resume interrupted message after high-priority finishes? (decide later)
- [ ] User prompt input is treated as `alert` tier (immediate response)

## Stage 5.6: Conversational Mode
- [ ] Text input below COMMENTARY panel (toggleable with hotkey)
- [ ] Route user query through LLM with current state as context
- [ ] Multi-turn: model can ask follow-up questions
- [ ] Eventually: tools the model can call (read_log, get_process_details, etc.)

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