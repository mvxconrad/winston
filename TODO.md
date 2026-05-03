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

## Stage 5: AI Commentary (current focus)
- [ ] Install Ollama on Windows host
- [ ] Firewall rule for port 11434 (mirror LHM rule)
- [ ] Pull qwen2.5:7b
- [ ] Verify connectivity from WSL: `curl http://<host>:11434/api/tags`
- [ ] Wire COMMENTARY panel to call Ollama every 30-60s
- [ ] Build observation summary prompt (recent stats → text context)
- [ ] Anomaly detection ("Chrome usually uses 2GB, now using 8GB")
- [ ] Predictions ("at this download rate, ARK update done in 23 min")

## Stage 6: Local LLM tuning
- [ ] Compare 7B vs 3B model speed/quality tradeoff
- [ ] Optimize prompts for smaller models
- [ ] Streaming output to commentary panel (token-by-token)

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