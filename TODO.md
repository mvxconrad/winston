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

## Stage 5.8: v0.8 review cleanup (next up)
Things noticed after shipping v0.8 — small but high-value polish before
moving to Stage 6.

### Memory schema cleanup
Currently `logs/memory.json` stores the same per-app payload twice:
once in `behavior[name]` (keyed by name) and once in `top_apps[]`
(list of dicts). One canonical store, derived views at read time.

- [ ] One source of truth: `apps` keyed by process name, each entry
      holds `hours / avg_cpu / peak_cpu / avg_gpu_when_top`
- [ ] `get_top_apps(n)` computes the ranking from `apps` at call time
      (sort by hours desc, take first n) — no separate `top_apps[]`
      to keep in sync
- [ ] Migrate existing `memory.json` files in place (one-shot read of
      old shape → write new shape) so nobody has to delete the file
- [ ] `BrainPanel.render()` and `brain/prompt.py` updated to use the
      new accessor

### CPU spike during commentary streaming
When Winston is streaming a reply, our own python3 process jumps from
~6% to ~30% CPU. The typewriter repaints the entire commentary block
on every chunk; combined with the markup-parse cost on each
`self.update()`, that's a lot of work per token.

- [ ] Throttle commentary repaints to the master frame rate (~30Hz)
      instead of one paint per chunk. Buffer chunks, paint on next
      frame.
- [ ] Verify with `top` that python3 stays <15% during streaming

### Activate model tiering by default ✅
Done in v0.8.1.

- [x] `LLM_USE_TIERED = True` as default
- [x] qwen2.5:3b-instruct stays resident — handles greeting, retrospective,
      heartbeat, routine, and all triggered events (including alerts).
      Originally I had alerts using the 7B for nuance, but that pops the
      7B into VRAM at the worst moment (something just went wrong → game
      stutters → 7B loads). 3B's observation is plenty for thermal/RAM
      alerts.
- [x] qwen2.5:7b-instruct loads on-demand only for `/`-asked user
      questions, with `LLM_QUALITY_KEEP_ALIVE_SEC=0` so it unloads the
      moment the answer finishes. Trade: ~5-10s cold load every new
      question. Worth it for keeping the GPU free during gaming.
- [ ] BRAIN panel shows which model is actually loaded right now
      (already does — verify it updates correctly across tier switches)

### Tools Winston asked for
He literally requested these when asked what would help him do his job
better. Reasonable Tier 1 (read-only) ideas to add to Stage 5.6:

- [ ] `track_app(name)` — pin an app for deeper monitoring (per-app CPU
      time, runtime distribution, GPU correlation)
- [ ] `recent_runs(name, hours)` — when did this app last run, for how
      long, what state was the system in
- [ ] Custom usage-pattern alerts — "warn me when Chrome's RAM exceeds
      its 95th percentile"


## Stage 5.9: Cloud LLM backend (Claude API as a slash command)
For when the local 7B isn't quite smart enough — type
`/claude what's been eating my GPU lately?` and the question goes to
Claude with the same Winston persona + state context. Local Ollama still
handles everything else.

### Difficulty: easy-medium (~2-3 hours)
The hard part is already done — `brain/client.py` exposes a clean
`generate_stream_async(prompt, on_chunk, on_done, on_error, ...)` shape,
and `CommentaryPanel` is already backend-agnostic at the callback level.
We just need a second client that fits the same shape.

### Implementation sketch
- [ ] `brain/claude_client.py` — mirrors `brain/client.py`'s public API:
      - `generate_stream_async(prompt, on_chunk, on_done, on_error, system=, model=, ...)`
      - Internally uses `anthropic.Anthropic().messages.stream(...)` —
        SDK fires per-token deltas via `text_stream`, same shape as
        Ollama's chunks
      - Own FIFO worker thread (don't share the Ollama queue — different
        latency profile, different rate limits)
- [ ] Slash command parser in `CommentaryPanel.ask_user()` — detect
      `/claude ...` prefix, strip it, route to `claude_client` instead
      of the Ollama `generate_stream_async`
- [ ] Same prompt builders work as-is (`brain/prompt.py`) — Claude takes
      a `system=` and `messages=[]`, both compatible with what we
      already build for Ollama
- [ ] **Use prompt caching** on the system prompt + machine facts +
      memory block — they're stable across calls and cut input tokens
      ~90%. `cache_control: {"type": "ephemeral"}` on those blocks.
- [ ] Persona consistency: keep the existing Winston system prompt
      verbatim. One extra line: "Match Winston's voice — dry,
      observant, concise. Don't introduce yourself as Claude."

### Config (add to config.py)
- [ ] `CLAUDE_ENABLED = False` (default off; opt-in)
- [ ] `CLAUDE_MODEL = "claude-sonnet-4-6"` — right cost/quality tier
      for "smarter Winston question." Opus 4.7 if you want max smart.
- [ ] API key from `ANTHROPIC_API_KEY` env var only — never read or
      write to disk, never log

### UX touches
- [ ] BRAIN panel `MODEL` line shows `claude-sonnet-4-6` while a
      `/claude` answer is streaming, then back to local model when idle
- [ ] STATE shows `THINKING (claude)` for cloud calls — distinguishes
      from local thinking visually
- [ ] Optional: small "via claude" subscript on the streamed answer in
      COMMENTARY so it's obvious which backend answered

### Things to think about before shipping
- **Cost.** Sonnet is roughly $3/M input + $15/M output. With prompt
  caching that's a few cents per question. Add a soft daily cap
  (`CLAUDE_DAILY_USD_CAP`, default $1?) and refuse if exceeded.
- **Failure mode.** API down or key invalid → fall back to local 7B
  with a banner: "(claude unavailable — answered with local 7B)"
- **Streaming.** Anthropic SDK chunks vary in size; `sanitize_chunk()`
  should still apply (cheap).
- **Privacy.** Don't include the full CSV log or all of memory in the
  prompt — only the panel snapshot + last 3 Q&A. We don't want to ship
  process names to a third party by surprise.

### Why slash command, not a button
A button needs a new Textual widget, focus management, layout slot,
keybinding. The slash command is a 5-line parser in `ask_user()`. Both
end up in the same input field. If we later want a button it just
calls the same code path.


## Stage 6: Local LLM tuning
- [ ] Compare 7B vs 3B speed/quality tradeoff with our current prompts
- [ ] Optimize prompts for the 3B (it's doing 90% of the work now —
      worth tightening)
- [ ] Try llama3.1:8b for `/`-asks once the Claude option is in (so
      there's a "smarter local" path before going cloud)


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


## Stage 10: PyQt6 desktop app — the big rewrite
The terminal is the bottleneck. When a game runs hot, Windows starves
the terminal of redraws, ASCII can only encode so much, and there's no
real scroll. The interim throttle (`GPU_BUSY_*` in `config.py`) makes
gaming bearable but the long-term answer is a real desktop app — Winston
running in its own GPU-accelerated window like btop's GUI cousins.

### Why PyQt6 (not web, not Electron)
Stays in pure Python. No frontend stack to maintain, no API/server layer,
no browser tab. User runs `python winston.py` (or a packaged `.exe`) and
a window opens. pyqtgraph hits 60fps for live charts trivially. Native
look, native scroll, native window controls.

### Architecture
The data layer is already separable — only `display.py` ties things to
Textual. Replace `display.py` with `gui.py` and the `panels/` + `brain/`
modules don't change.

- [ ] `gui.py` — `QApplication` + `QMainWindow` mirroring the current
      Textual layout
- [ ] One `QTimer` per panel rate (or a single 60Hz master like the
      Textual frame loop) — same data-update + widget-refresh split
- [ ] `pyqtgraph.PlotWidget` for the CPU graph + network graphs (real
      lines, real axes, real anti-aliasing — no braille)
- [ ] Real `QProgressBar` for CPU/RAM/disk/GPU bars (or `QFrame` with
      stylesheets if we want the heatmap-colored chunky look kept)
- [ ] `QTextEdit` (read-only) for COMMENTARY — proper scroll, proper
      wrapping, proper text selection
- [ ] `QLineEdit` for the ASK input — focus management is built-in
- [ ] Keep `theme.py` as the single source of color truth — Qt accepts
      hex strings, so `heat_pct(percent)` returns a valid value for
      both Rich and Qt without changes

### What we get for free
- Smooth 60fps even while gaming (Qt renders on GPU, doesn't compete
  with the game for the same surface)
- Real scroll, real selection, real copy/paste
- Real charts — pyqtgraph supports millions of points, log scale,
  zoom/pan with mouse, all without performance worry
- Window resizing actually works
- System tray icon possibility (Stage 8 item gets easier)
- Packageable into a Windows `.exe` via PyInstaller — single click to
  launch, no Python install needed

### Effort: ~2 weeks part-time
- Week 1: skeleton — main window, layout, data binding, panel widgets
- Week 2: charts, COMMENTARY/BRAIN polish, theme tuning, packaging

### Things to think about
- Keep the TUI as an alternate frontend? `winston.py --tui` falls back
  to current Textual app, default is the GUI. Same data layer.
- High-DPI scaling — Qt handles it well but the layout math needs a
  pass.
- Windows packaging: PyInstaller builds a ~50MB single exe. Acceptable
  for a personal tool, ugly for distribution.
- Auto-start on Windows boot? Easy with a startup-folder shortcut to
  the packaged exe.