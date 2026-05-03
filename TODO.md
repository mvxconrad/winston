# WINSTON — Roadmap

## Stage 1: Visual Foundation ✅
- [x] Live-updating panel (no scroll)
- [x] CPU + RAM display
- [x] Project name & README

## Stage 2: Better Stats Display
- [ ] Per-core CPU heat map
- [ ] Visual progress bars for CPU/RAM
- [ ] Top 5 processes by CPU usage
- [ ] Disk usage panel
- [ ] Network up/down speeds
- [ ] Uptime display

## Stage 3: Visual Polish
- [ ] Multi-panel layout (split screen)
- [ ] Matrix theme (green/black/red)
- [ ] Rolling history graph for CPU
- [ ] Color-coded warning thresholds (green→yellow→red)

## Stage 4: GPU Support
- [ ] nvidia-smi or pynvml integration
- [ ] GPU usage, VRAM, temp panel
- [ ] GPU-aware process detection (which app uses GPU)

## Stage 5: AI Commentary (Claude API first, local LLM later)
- [ ] Anthropic API key setup
- [ ] Claude commentary panel
- [ ] Periodic insights based on stats over time
- [ ] Anomaly detection ("Chrome usually uses 2GB, now using 8GB")
- [ ] Predictions ("at this download rate, ARK update done in 23 min")

## Stage 6: Local LLM
- [ ] Install Ollama in WSL
- [ ] Swap Claude API for local model endpoint
- [ ] Optimize prompts for smaller models

## Stage 7: Long-term Tracking
- [ ] Persist data to local SQLite database
- [ ] Daily/weekly summaries
- [ ] Pattern detection over days/weeks
- [ ] "You've been at >80% CPU 47 min today, take a break"

## Stage 8: Stretch / Cool Ideas
- [ ] System tray notifications
- [ ] Health/wellness commentary (hours at desk, etc.)
- [ ] Cost tracking (API spend, electricity estimate)
- [ ] Cross-machine: monitor your desktop from anywhere