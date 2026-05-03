# WINSTON — Roadmap

## Stage 1: Visual Foundation ✅
- [x] Live-updating panel (no scroll)
- [x] CPU + RAM display
- [x] Project name & README

## Stage 2: Better Stats Display
- [x] Per-core CPU heatmap layout (2-column bar gauges)
- [x] Visual progress bars for CPU/RAM
- [x] Top processes by CPU usage
- [x] Disk usage panel
- [x] Network up/down speeds
- [x] Uptime display (in status bar)

## Stage 3: Visual Polish
- [x] Multi-panel layout (split screen)
- [x] Custom black/green chrome (no Textual blue)
- [x] Big braille aggregate CPU graph (the hero)
- [x] Color-coded warning thresholds (green→yellow→red)
- [ ] Border title corner glyphs (◤ ◢ ◣ ◥) — minor polish
- [ ] Subtle background pattern / scanlines — optional flair

## Stage 4: GPU Support
- [ ] nvidia-smi or pynvml integration
- [ ] GPU usage, VRAM, temp panel
- [ ] GPU-aware process detection

## Stage 5: AI Commentary (Claude API first, local later)
- [ ] Anthropic API key setup
- [ ] Wire up commentary panel (placeholder is in layout)
- [ ] Periodic insights based on stats over time
- [ ] Anomaly detection ("Chrome usually uses 2GB, now using 8GB")
- [ ] Predictions ("at this download rate, ARK update done in 23 min")

## Stage 6: Local LLM
- [ ] Install Ollama in WSL
- [ ] Swap Claude API for local model endpoint
- [ ] Optimize prompts for smaller models

## Stage 7: Long-term Tracking
- [ ] Persist data to SQLite
- [ ] Daily/weekly summaries
- [ ] Pattern detection over days/weeks
- [ ] "You've been at >80% CPU 47 min today, take a break"

## Stage 8: Stretch / Cool Ideas
- [ ] System tray notifications
- [ ] Health/wellness commentary (hours at desk, etc.)
- [ ] Cost tracking (API spend, electricity estimate)
- [ ] Cross-machine: monitor your desktop from anywhere

## Stage 9: Process Graph View (the big one)
- [ ] `press G` opens process map in popup window (matplotlib)
- [ ] Nodes = processes, size = memory, color = CPU load
- [ ] Edges = parent/child process tree
- [ ] Later: edges include network connections, shared file handles
- [ ] AI reads graph state for commentary ("chrome cluster grew 40%")
- [ ] Eventually: web UI mode for live graph alongside TUI