# WINSTON

**W**ell-trained **I**ntuitive **N**eural **S**ystem **T**ranslating **O**bserved **N**umbers

A personal system monitor for the terminal. Watches CPU, RAM, GPU, disk, network, and temperatures. Logs everything for later, and eventually translates what it sees into AI commentary on your machine's life.

```
◤ WINSTON v0.4   HOST PC   OS LINUX   UP 0d 04h 32m   TIME 22:47:11

┍─ CPU LOAD ──────────────────────────────────────────────┑
│ 25.6%   avg 6.4%   peak 78.2%                           │
│ 100 ⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒│
│  80 ⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⢀⣄⡀⠒│
│  60 ⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⢀⣶⣶⣶⣄⠒⠒⢸⣿⣿⡆⠒│
└─────────────────────────────────────────────────────────┘
```

## What it monitors

- **CPU**: aggregate load with a gridded braille graph + per-core bar gauges
- **Memory**: usage bar, used / total
- **System**: load averages (1m / 5m / 15m), process count, thread count, uptime
- **Disks**: per-mount usage bars including Windows drives via WSL (e.g. `C:` shows alongside `/`)
- **Temperatures**: CPU package, cores, AIO/motherboard sensors when available
- **GPU**: utilization, VRAM, temperature (NVIDIA via nvidia-smi or pynvml)
- **Network**: RX / TX rates with mini history graphs
- **Processes**: top 8 by CPU usage with memory footprint

## Stack

- [Textual](https://github.com/Textualize/textual) — TUI framework, handles full-screen layout
- [Rich](https://github.com/Textualize/rich) — text styling and rendering
- [psutil](https://github.com/giampaolo/psutil) — cross-platform system stats

Optional:

- [pynvml](https://pypi.org/project/pynvml/) — faster NVIDIA GPU reads (falls back to nvidia-smi)
- [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) — temperature data source on Windows/WSL

## Run

```bash
pip install textual psutil
python winston.py
```

Press `Q` to quit, `R` to reset history graphs.

## Project structure

```
winston.py              # entry point — wires up panels and runs the app
display.py              # Textual app, layout, custom chrome (status bar, footer)
theme.py                # green/black palette, three-tier brightness hierarchy
logger.py               # CSV writer for raw observations
panels/
  __init__.py
  base.py               # shared helpers: bar gauges, braille graph, color tiers
  cpu_graph.py          # hero panel: aggregate CPU braille graph with gridlines
  cpu.py                # per-core bar gauge grid
  ram.py                # memory bar
  system.py             # load avg, process count, uptime
  disk.py               # per-mount usage (handles WSL Windows drives)
  temps.py              # temperatures (multi-backend: native / LHM / WMI)
  gpu.py                # GPU stats (pynvml / nvidia-smi)
  network.py            # RX / TX with history graphs
  processes.py          # top processes by CPU
logs/
  raw/
    observations.csv    # one row per second, all panel data
```

Adding a new panel = drop a class in `panels/` with `update()`, `render(width=None)`, `csv_headers()`, `csv_columns()` methods. Then add it to the section list in `winston.py` and a slot in `display.py`'s layout.

## Temperatures on WSL

WSL2 doesn't expose hardware sensors directly — the kernel doesn't include the lm-sensors drivers. Winston works around this by reading from [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) running on Windows.

### One-time setup

Install LHM on Windows:

```powershell
winget install LibreHardwareMonitor.LibreHardwareMonitor
```

Launch it (run as Administrator for full sensor coverage). Then:

- **Options → Remote Web Server → Run** (enables HTTP endpoint on port 8085)
- **Options → Run On Windows Startup** (so you don't have to do this every boot)
- **Options → Minimize To Tray** (keeps it out of your way)

Add a firewall rule so WSL can reach LHM (PowerShell as Administrator):

```powershell
New-NetFirewallRule -DisplayName "LHM Web Server (WSL only)" `
  -Direction Inbound -LocalPort 8085 -Protocol TCP -Action Allow `
  -Profile Private -RemoteAddress 127.0.0.1,172.16.0.0/12
```

This is locked down to localhost + the WSL subnet only. Other devices on your wifi can't reach it.

### How Winston finds it

The temps panel auto-detects in this order:

1. Native Linux sensors (psutil) — works on bare Linux/macOS
2. LibreHardwareMonitor at `localhost:8085`
3. LibreHardwareMonitor at the Windows host gateway IP (auto-detected from `/proc/net/route` — no config needed, works after reboots, after moving houses, after wifi changes)
4. PowerShell + WMI ACPI thermal zones (basic fallback, often returns nothing on modern hardware)
5. Polite "no sensors" message

Whichever returns data first wins. The panel shows which backend it's using at the top (`via lhm`, `via wmi`, etc.).

## Logging

Every tick (1Hz) Winston appends a row to `logs/raw/observations.csv` containing every panel's data. After a day of running you'll have ~86,400 rows. Used for:

- Long-term trends (how hot does my CPU get during gaming sessions?)
- Eventual AI commentary input — the raw firehose for analysis later

The file is gitignored. Don't commit it.

## Roadmap

See `TODO.md`. Next major thing is **Stage 5: AI commentary** — wiring up Claude to read recent observations and produce insights about what your machine is doing. The "COMMENTARY" panel at the bottom of the dashboard is the placeholder waiting for that.

Stage 9 is the long shot: **Process Graph View** — Obsidian-style force-directed graph of running processes, with size/color encoding memory and CPU, edges showing parent-child and IPC relationships. Pop it open with `G`, see the *shape* of what your computer is doing.

## Why "Winston"

Backronym after the fact, but the idea is: the AI butler watching over your machine. Always knows what's going on. Quietly notices when things are off. Tells you what matters.
