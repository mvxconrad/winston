# WINSTON

**W**ell-trained **I**ntuitive **N**eural **S**ystem **T**ranslating **O**bserved **N**umbers

A personal system monitor for the terminal. Watches CPU, RAM, GPU, disks, network, temperatures, and processes. Logs everything to CSV. Eventually, a local LLM reads what's happening and generates commentary.

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

### NETWORK (2Hz)
On WSL, defaults to **Windows host stats** via PowerShell `Get-NetAdapterStatistics`. WSL2's virtual interfaces only see traffic launched from inside WSL — Chrome, games, and most desktop apps run on Windows and never touch the WSL counters. Querying the host gives the real numbers.

The PowerShell call is ~50ms. Doing it synchronously at 2Hz blocked the UI noticeably, so it runs on a background thread and the panel reads cached values.

Rates are smoothed over a 2-second rolling window to avoid spikes/zeros from uneven tick spacing. Peak download/upload rates are tracked persistently — on init, the panel scans `logs/raw/observations.csv` (single-pass O(n) max) to find the highest historical rates and seeds the graph scale with those. Graphs scale to observed peaks, so a speedtest fills the bars dramatically while routine YouTube traffic shows as a small fraction. The peak only ever grows.

### PROCESSES (1Hz)
Top 8 by CPU. CPU% column uses the same heatmap palette as everything else, so a process at 50% looks visually consistent with a CPU panel at 50%.

### COMMENTARY (placeholder)
Currently cycles through hardcoded status messages. This is where Stage 5 (LLM commentary) will plug in.

## Architecture notes

### Refresh rate decoupling
Each panel updates at its own rate (constants at the top of `winston.py`). The display layer schedules per-panel intervals — no master tick that everyone hangs off. Logger ticks independently at 1Hz so the CSV is always evenly spaced regardless of how fast individual panels update.

### Theme is a single source of truth
All color decisions live in `theme.py`. Panels never define colors locally — they import `heat_pct(percent)` and `heat_temp(celsius)` which return colors from a 7-stop palette. Switching the entire app's color scheme is a one-file edit.

### Background polling threads
Anything that takes more than a few ms (PowerShell calls, HTTP fetches) runs on a daemon thread, not the UI thread. The shared LHM poller (`panels/lhm.py`) fetches once and both the GPU and Temps panels read from the same cache — no duplicate HTTP requests.

### Bytes formatting
`fmt_bytes()` returns `KB / MB / GB / TB / PB` (powers of 1024 with conventional suffixes). Same as htop, btop, and Windows File Explorer.

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
winston.py              # entry point — refresh-rate constants, section list
display.py              # Textual app, custom chrome, layout CSS, CpuGraphWidget
theme.py                # all color decisions; heat_pct() / heat_temp() helpers
logger.py               # CSV writer
panels/
  __init__.py
  base.py               # bar gauges, braille graph, fmt_bytes
  cpu_graph.py          # data class — display.py builds the actual graph widget
  cpu.py                # per-core bar grid
  ram.py                # memory bar
  system.py             # load avg, procs, threads, swap, I/O, uptime
  disk.py               # Windows + Linux mount listing
  temps.py              # multi-backend (native / LHM / WMI), smart device labels
  gpu.py                # pynvml or nvidia-smi + LHM enrichment
  network.py            # PowerShell host stats on WSL, smoothed rates, peak tracking
  processes.py          # top N by CPU
  lhm.py                # shared LHM HTTP poller (background thread, single cache)
logs/
  raw/
    observations.csv    # 1 row/sec, all panel data — gitignored
diag_lhm.py             # standalone connectivity diagnostic for LHM/WSL setup
perf_diag.py            # times each panel's update/render to find slowness
```

Adding a new panel: drop a class in `panels/` with `update()`, `render(width=None)`, `csv_headers()`, `csv_columns()` methods. Optional `title` property for dynamic panel titles. Add it to the section list in `winston.py` with its refresh rate, and add a layout slot in `display.py`.

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