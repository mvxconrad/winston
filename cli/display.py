"""WINSTON display — hand-rolled scrolling braille CPU graph, tight layout."""
import platform
import socket
from datetime import datetime

import psutil
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static, Input


# ──────────────── Custom chrome ────────────────
class StatusBar(Static):
    """Top-row status line. Refreshes are driven by the App's frame loop
    (no own set_interval) so all UI updates ride a single clock — eliminates
    visual jitter from independent timers and frees the asyncio loop from
    one more competing timer."""

    def on_mount(self):
        # Initial paint. After this, WinstonApp._frame_tick calls
        # refresh_status() at 1Hz aligned with the master frame.
        self.refresh_status()

    def refresh_status(self):
        host = socket.gethostname()
        boot = datetime.fromtimestamp(psutil.boot_time())
        up = datetime.now() - boot
        days = up.days
        hours, rem = divmod(up.seconds, 3600)
        mins, _ = divmod(rem, 60)
        uptime = f"{days}d {hours:02d}h {mins:02d}m"
        now = datetime.now().strftime("%H:%M:%S")
        os_name = platform.system().upper()

        line = (
            f"[bold bright_green]◤ WINSTON[/bold bright_green] "
            f"[grey50]v0.5[/grey50]   "
            f"[grey50]HOST[/grey50] [bright_green]{host}[/bright_green]   "
            f"[grey50]OS[/grey50] [bright_green]{os_name}[/bright_green]   "
            f"[grey50]UP[/grey50] [bright_green]{uptime}[/bright_green]"
            f"{' ' * 4}"
            f"[grey50]TIME[/grey50] [bright_green]{now}[/bright_green]"
        )
        self.update(line)


class FooterBar(Static):
    def render(self):
        return (
            "[grey50] [/grey50][bold bright_green]Q[/bold bright_green]"
            "[grey50] quit  [/grey50]"
            "[bold bright_green]R[/bold bright_green]"
            "[grey50] reset history  [/grey50]"
            "[bold bright_green]G[/bold bright_green]"
            "[grey50] process map (soon)[/grey50]"
        )


class CommentaryPanel(Static):
    """Textual frontend for `brain.commentary_engine.CommentaryEngine`.

    All orchestration logic — state machine, Q&A history, trigger
    evaluation, heartbeat / stale-quiet, prompt building, model tiering —
    lives in the engine. This class only does:
      - Render engine state into Rich markup via `self.update(...)`
      - Drive Textual timers (typewriter, cursor blink, 1Hz triggers)
      - Marshal stream chunks from worker thread → engine via
        `app.call_from_thread`
      - Translate engine "fire this prompt" results into actual
        `generate_stream_async` calls

    Same engine is shared with the PyQt6 GUI in gui/main.py.
    """

    # Cursor blink rate while streaming
    CURSOR_BLINK_HZ = 2.5

    def __init__(self, sections=None, config=None, memory=None, **kwargs):
        super().__init__(**kwargs)
        from brain.commentary_engine import CommentaryEngine
        self.engine = CommentaryEngine(sections or [], config or {}, memory)
        self._cursor_visible = True
        # Lazy stream timers — nothing scheduled until a stream starts.
        self._cursor_timer = None
        self._typewriter_timer = None

    # ──────────────── Read-only accessors (for BrainPanel) ────────
    def get_state(self):
        return self.engine.state

    def get_last_event(self):
        return self.engine.last_event

    # ──────────────── Lifecycle ────────────────
    def on_mount(self):
        if not self.engine.config.get("enabled", False):
            self._paint()
            return
        if self.engine.config.get("startup_greeting", True):
            self.engine.startup_step = "greeting"
            self._fire_step("greeting")
        else:
            self._begin_regular_loop()
        self._paint()

    # ──────────────── Stream timer machinery ────────────────
    # Why lazy: the typewriter ticks at 25Hz and the blinker at 2.5Hz, but
    # both are no-ops while idle. Even an empty asyncio callback at 25Hz
    # was enough to drop ~10% of typed characters in the ASK input when
    # combined with all the other dashboard timers. Holding the timers off
    # until there's actually something to type out keeps the loop quiet.
    def _start_stream_timers(self):
        if self._cursor_timer is None:
            self._cursor_timer = self.set_interval(
                1.0 / self.CURSOR_BLINK_HZ, self._toggle_cursor)
        if self._typewriter_timer is None:
            tps = self.engine.config.get("typewriter_tps", 25)
            self._typewriter_timer = self.set_interval(
                1.0 / tps, self._typewriter_tick)

    def _stop_stream_timers(self):
        if self._cursor_timer is not None:
            try:
                self._cursor_timer.stop()
            except Exception:
                pass
            self._cursor_timer = None
        if self._typewriter_timer is not None:
            try:
                self._typewriter_timer.stop()
            except Exception:
                pass
            self._typewriter_timer = None

    # ──────────────── Stream firing (uses engine for prompts) ─────
    def _fire_stream(self, system, prompt, tier):
        """Common path for every LLM call. Engine begins the stream;
        we hand brain.client our marshaling callbacks."""
        from brain.client import generate_stream_async
        self.engine.begin_streaming()
        self._start_stream_timers()
        self._paint()
        model, keep_alive = self.engine.pick_model(tier)
        generate_stream_async(
            prompt, system=system, model=model, keep_alive=keep_alive,
            on_chunk=self._on_chunk_worker,
            on_done=self._on_done_worker,
            on_error=self._on_error_worker,
        )

    def _fire_step(self, step):
        """Fire whichever startup step we're on."""
        if step == "greeting":
            system, prompt, tier = self.engine.build_greeting()
        else:  # retrospective
            system, prompt, tier = self.engine.build_retrospective()
        if system is None:
            self._on_startup_step_done()
            return
        self._fire_stream(system, prompt, tier)

    def _begin_regular_loop(self):
        """Init the trigger runner, fire one routine, start the 1Hz tick."""
        self.engine.startup_step = None
        self.engine.init_triggers()
        # Fire one observation as the "startup commentary".
        system, prompt, tier = self.engine.build_observation()
        if system is not None:
            self._fire_stream(system, prompt, tier)
        # 1Hz trigger tick
        self.set_interval(1.0, self._tick_triggers)

    # ──────────────── 1Hz trigger tick ────────────────
    def _tick_triggers(self):
        import time
        _t0 = time.monotonic()
        try:
            result = self.engine.evaluate_triggers()
            if result is not None:
                kind, payload = result
                if kind == "event":
                    system, prompt, tier = self.engine.build_triggered(payload)
                else:  # heartbeat / stale → routine observation
                    system, prompt, tier = self.engine.build_observation()
                if system is not None:
                    self._fire_stream(system, prompt, tier)
        finally:
            try:
                self.app._record_timing("CommentaryPanel.triggers", _t0)
            except Exception:
                pass

    # ──────────────── User question ────────────────
    def ask_user(self, question):
        """Route a user question through the engine + fire conversational
        prompt (quality tier)."""
        recorded = self.engine.handle_user_question(question)
        if recorded is None:
            return
        system, prompt, tier = self.engine.build_conversational(recorded)
        if system is None:
            self.engine.state = "ERROR"
            self._paint()
            return
        self._fire_stream(system, prompt, tier)

    # ──────────────── Typewriter / cursor ────────────────
    def _typewriter_tick(self):
        result = self.engine.typewriter_advance()
        if result == "advanced":
            self._paint()
        elif result == "finalize":
            self._stop_stream_timers()
            self._paint()
            # Cooldown: after the inter-message pause, end cooldown so the
            # next message can start. If we were in the startup ritual,
            # advance to the next step at the same time.
            pause = self.engine.config.get("inter_message_pause_sec", 2.0)
            self.set_timer(pause, self._end_cooldown)

    def _end_cooldown(self):
        self.engine.end_cooldown()
        if self.engine.startup_step is not None:
            self._on_startup_step_done()

    def _on_startup_step_done(self):
        if self.engine.startup_step == "greeting":
            self.engine.startup_step = "retrospective"
            pause = self.engine.config.get("inter_message_pause_sec", 2.0)
            self.set_timer(pause, lambda: self._fire_step("retrospective"))
        else:
            pause = self.engine.config.get("inter_message_pause_sec", 2.0)
            self.set_timer(pause, self._begin_regular_loop)

    def _toggle_cursor(self):
        if self.engine.state in ("STREAMING", "THINKING"):
            self._cursor_visible = not self._cursor_visible
            self._paint()

    # ──────────────── Worker-thread callbacks ────────────────
    # Stream callbacks fire from brain.client's worker thread. We marshal
    # to the UI thread before mutating engine state so the engine never
    # has to think about threading.
    def _on_chunk_worker(self, chunk):
        self.app.call_from_thread(self._on_chunk, chunk)

    def _on_done_worker(self, full_text):
        self.app.call_from_thread(self._on_done, full_text)

    def _on_error_worker(self):
        self.app.call_from_thread(self._on_error)

    # ──────────────── UI-thread slots ────────────────
    def _on_chunk(self, chunk):
        self.engine.on_chunk(chunk)
        self._paint()

    def _on_done(self, full_text):
        self.engine.on_done(full_text)

    def _on_error(self):
        self.engine.on_error()
        self._stop_stream_timers()
        self._paint()
        # If the failing call was part of the startup ritual, advance
        # anyway so the rest of the ritual can run.
        if self.engine.startup_step is not None:
            pause = self.engine.config.get("inter_message_pause_sec", 2.0)
            self.set_timer(pause, self._on_startup_step_done)

    # ──────────────── Rendering ────────────────
    def _paint(self):
        """Render engine state into Rich markup."""
        e = self.engine
        if e.state == "DISABLED":
            self.update("[bold bright_green]>[/bold bright_green] "
                        "[grey50]analysis subsystem :: disabled[/grey50] "
                        "[grey50](LLM_ENABLED = False in config.py)[/grey50]")
            return

        # Newest gets bright green, older steps through medium → dim → grey.
        FADE_PALETTE = ["bright_green", "#3aa83a", "#1a8c1a", "#0a5a0a", "grey50"]

        lines = []
        history_count = len(e.history)
        for idx, entry in enumerate(e.history):
            if len(entry) == 3:
                ts, msg, kind = entry
            else:
                ts, msg = entry
                kind = "winston"
            safe = msg.replace("[", r"\[")
            distance_from_end = (history_count - 1) - idx
            color = FADE_PALETTE[min(distance_from_end + 1, len(FADE_PALETTE) - 1)]

            if kind == "user":
                lines.append(f"[grey50]{ts}[/grey50]  "
                             f"[bold bright_cyan]?[/bold bright_cyan] "
                             f"[#3a8a9c]{safe}[/#3a8a9c]")
            else:
                lines.append(f"[grey50]{ts}[/grey50]  "
                             f"[bold {color}]>[/bold {color}] "
                             f"[{color}]{safe}[/{color}]")

        if e.state == "THINKING":
            cursor = "█" if self._cursor_visible else " "
            lines.append(f"[grey50]--:--:--[/grey50]  "
                         f"[bold bright_green]>[/bold bright_green] "
                         f"[grey50]thinking…[/grey50] "
                         f"[bright_green]{cursor}[/bright_green]")
        elif e.state == "STREAMING":
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S")
            visible = e.streaming_buffer[:e.typed_chars]
            safe = visible.replace("[", r"\[")
            cursor = "█" if self._cursor_visible else " "
            lines.append(f"[grey50]{ts}[/grey50]  "
                         f"[bold bright_green]>[/bold bright_green] "
                         f"[bright_green]{safe}[/bright_green]"
                         f"[bright_green]{cursor}[/bright_green]")
        elif e.state == "ERROR":
            lines.append(f"[grey50]--:--:--[/grey50]  "
                         f"[bold bright_green]>[/bold bright_green] "
                         f"[red]analysis error[/red] "
                         f"[grey50](LLM unreachable)[/grey50]")

        self.update("\n".join(lines))


# ──────────────── Panel widgets ────────────────
class PanelWidget(Static):
    """Generic data-panel wrapper."""
    def __init__(self, panel, **kwargs):
        super().__init__(**kwargs)
        self.panel = panel

    def refresh_panel(self):
        avail = max(20, self.size.width - 4)
        try:
            self.update(self.panel.render(width=avail))
        except TypeError:
            self.update(self.panel.render())

        # If the panel exposes a dynamic title, sync it to the border.
        # Used by panels whose label varies with content (e.g. DISK vs DISKS,
        # or "PROCESSES (8)" with count). Most panels don't define this and
        # keep their static title set at compose time.
        title = getattr(self.panel, "title", None)
        if title is not None:
            new_border_title = f"─ {title} ─"
            if self.border_title != new_border_title:
                self.border_title = new_border_title


class CpuGraphWidget(Static):
    """CPU graph — hand-rolled scrolling braille graph.

    Why not plotext? Plotext recomputes layout every frame, which makes
    axis labels and gridlines visually jitter. This widget draws into
    fixed character positions: data scrolls left, axis stays put. Same
    technique htop/btop use.
    """
    def __init__(self, data_panel, **kwargs):
        super().__init__(**kwargs)
        self.data_panel = data_panel

    def refresh_panel(self):
        from rich.text import Text
        from panels.base import braille_graph, health_for

        text = Text()

        # Width available for the graph itself. Reserve 5 chars on the left
        # for the y-axis labels: "100% ".
        avail = max(40, self.size.width - 4)
        axis_w = 5
        graph_w = avail - axis_w   # in CHARACTERS — each braille char = 2 data points
        if graph_w < 10:
            graph_w = 10

        # Use the last (graph_w * 2) data points so each char column
        # represents two seconds of data (since each braille char is 2 wide).
        # That gives us a 60-wide graph showing 120s of history.
        # We pad with zeros on the left if we don't have enough data.
        WINDOW_PIXELS = graph_w * 2
        history = list(self.data_panel.history)[-WINDOW_PIXELS:]
        if len(history) < WINDOW_PIXELS:
            history = [0.0] * (WINDOW_PIXELS - len(history)) + history

        # Build a 4-row braille graph
        GRAPH_HEIGHT_ROWS = 5
        rows = braille_graph(history, width=graph_w, height=GRAPH_HEIGHT_ROWS, max_val=100)

        # Color: smooth heatmap from theme. Whole graph reflects current load.
        from theme import heat_pct, DIM, MEDIUM
        current = self.data_panel.last_value
        graph_color = heat_pct(current)

        # Header line — current/avg/peak readings
        avg = self.data_panel.average
        peak = self.data_panel.peak
        text.append(f"{current:5.1f}%", style=f"bold {graph_color}")
        text.append("  avg ", style=DIM)
        text.append(f"{avg:.1f}%", style=MEDIUM)
        text.append("  peak ", style=DIM)
        text.append(f"{peak:.1f}%", style=heat_pct(peak))
        text.append("\n")

        # Y-axis labels for each row, top to bottom.
        y_labels_5 = ["100% ", " 75% ", " 50% ", " 25% ", "  0% "]

        # Render each graph row with axis label on left
        for i, row in enumerate(rows):
            text.append(y_labels_5[i], style=DIM)
            text.append(row, style=graph_color)
            text.append("\n")

        # Bottom axis: time labels pinned to character positions.
        text.append("     ", style=DIM)
        text.append("─" * graph_w, style=DIM)
        text.append("\n")

        text.append("     ", style=DIM)
        # Build the label row character-by-character
        # We want labels at positions 0, graph_w//2, graph_w-1
        # Labels: "-120s"(5), "-60s"(4), "now"(3)
        label_row = [" "] * graph_w
        # "-120s" at position 0
        for i, c in enumerate("-120s"):
            if i < graph_w:
                label_row[i] = c
        # "-60s" at middle
        mid = graph_w // 2
        for i, c in enumerate("-60s"):
            pos = mid - 1 + i  # center the label slightly
            if 0 <= pos < graph_w:
                label_row[pos] = c
        # "now" right-aligned
        for i, c in enumerate("now"):
            pos = graph_w - 3 + i
            if 0 <= pos < graph_w:
                label_row[pos] = c
        text.append("".join(label_row), style=DIM)

        self.update(text)


# ──────────────── Main app ────────────────
class WinstonApp(App):
    CSS = """
    Screen {
        background: black;
    }

    StatusBar {
        height: 1;
        background: black;
        padding: 0 1;
    }

    FooterBar {
        height: 1;
        dock: bottom;
        background: black;
        padding: 0 1;
    }

    /* All panels share this — round borders, tight padding, no margin */
    .panel {
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }

    /* Hero CPU graph — hand-rolled scrolling braille graph */
    #cpu_graph_panel {
        height: 11;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }

    /* Row 1: CORES (2fr) | MEMORY | SYSTEM */
    #row1 {
        height: 11;
    }
    #cores_panel {
        width: 2fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }
    #memory_panel {
        width: 1fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }
    #system_panel {
        width: 1fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }

    /* Row 2: DISKS (narrow) | TEMPS (wider) | GPU (wider) */
    #row2 {
        height: 10;
    }
    #disk_panel {
        width: 1fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }
    #temps_panel {
        width: 2fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }
    #gpu_panel {
        width: 2fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }

    /* Row 3: NETWORK | PROCESSES */
    #row3 {
        height: 12;
    }
    #network_panel {
        width: 1fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }
    #processes_panel {
        width: 2fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }

    /* Commentary — taller, holds the LLM chat-log (height set dynamically) */
    #commentary_panel {
        height: 9;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }

    /* Brain panel — Winston's internal state. Sits below commentary. */
    #brain_panel {
        height: 9;
        border: round cyan;
        border-title-color: ansi_bright_cyan;
        border-title-style: bold;
        padding: 0 1;
    }

    /* Conversational input — single-line text input below commentary */
    #user_input {
        height: 3;
        border: round green;
        border-title-color: ansi_bright_cyan;
        border-title-style: bold;
        padding: 0 1;
        background: black;
        color: ansi_bright_cyan;
    }
    #user_input:focus {
        border: round ansi_bright_cyan;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "reset_history", "Reset"),
        ("slash", "focus_input", "Ask"),
    ]

    def __init__(self, sections, logger, logger_hz=1.0, llm_config=None, memory=None):
        super().__init__()
        # sections is a list of (panel_instance, refresh_hz) tuples
        # We split into parallel lists for convenience
        self.sections = [s[0] for s in sections]
        self.section_rates = [s[1] for s in sections]
        self.logger = logger
        self.logger_hz = logger_hz
        # LLM config dict — passed through to CommentaryPanel.
        # See run() for the keys.
        self.llm_config = llm_config or {"enabled": False}
        # Persistent memory (brain.memory.Memory or None)
        self.memory = memory
        self.brain_panel = None  # set in compose() if enabled

        (self.cpu_graph,
         self.cpu,
         self.ram,
         self.system,
         self.disk,
         self.temps,
         self.gpu,
         self.network,
         self.processes) = self.sections

        # Map each panel object → list of widgets that visualize it.
        # Built up in compose(); used by per-panel tick handlers.
        self._panel_widgets = {}

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status_bar")

        cpu_g = CpuGraphWidget(self.cpu_graph, id="cpu_graph_panel")
        cpu_g.border_title = "─ CPU LOAD ─"
        self._panel_widgets.setdefault(id(self.cpu_graph), []).append(cpu_g)
        yield cpu_g

        with Horizontal(id="row1"):
            cores = PanelWidget(self.cpu, id="cores_panel")
            cores.border_title = "─ CORES ─"
            self._panel_widgets.setdefault(id(self.cpu), []).append(cores)
            yield cores

            mem = PanelWidget(self.ram, id="memory_panel")
            mem.border_title = "─ MEMORY ─"
            self._panel_widgets.setdefault(id(self.ram), []).append(mem)
            yield mem

            sysm = PanelWidget(self.system, id="system_panel")
            sysm.border_title = "─ SYSTEM ─"
            self._panel_widgets.setdefault(id(self.system), []).append(sysm)
            yield sysm

        with Horizontal(id="row2"):
            disk = PanelWidget(self.disk, id="disk_panel")
            disk.border_title = "─ DISKS ─"
            self._panel_widgets.setdefault(id(self.disk), []).append(disk)
            yield disk

            temps = PanelWidget(self.temps, id="temps_panel")
            temps.border_title = "─ TEMPS ─"
            self._panel_widgets.setdefault(id(self.temps), []).append(temps)
            yield temps

            gpu = PanelWidget(self.gpu, id="gpu_panel")
            gpu.border_title = "─ GPU ─"
            self._panel_widgets.setdefault(id(self.gpu), []).append(gpu)
            yield gpu

        with Horizontal(id="row3"):
            net = PanelWidget(self.network, id="network_panel")
            net.border_title = "─ NETWORK ─"
            self._panel_widgets.setdefault(id(self.network), []).append(net)
            yield net

            procs = PanelWidget(self.processes, id="processes_panel")
            procs.border_title = "─ PROCESSES ─"
            self._panel_widgets.setdefault(id(self.processes), []).append(procs)
            yield procs

        commentary = CommentaryPanel(sections=self.sections,
                                      config=self.llm_config,
                                      memory=self.memory,
                                      id="commentary_panel")
        # Panel height = line count + borders (2) + padding (0) + a little
        # buffer (1). With config['lines'] of 5 -> 8 cells.
        commentary.styles.height = self.llm_config.get("lines", 5) + 3
        commentary.border_title = "─ COMMENTARY ─"
        yield commentary

        # Brain panel: Winston's internal state, below commentary. Optional —
        # gated by config.SHOW_BRAIN_PANEL so it can be turned off without
        # touching code (used for diagnosing UI issues).
        show_brain = (self.llm_config.get("enabled", False)
                      and self.llm_config.get("show_brain_panel", True))
        if show_brain:
            from panels.brain import BrainPanel
            from brain.client import status as client_status
            brain = BrainPanel(
                memory=self.memory,
                get_state=commentary.get_state,
                get_last_event=commentary.get_last_event,
                client_status=client_status,
            )
            self.brain_panel = brain
            brain_widget = PanelWidget(brain, id="brain_panel")
            brain_widget.border_title = "─ BRAIN ─"
            self._panel_widgets.setdefault(id(brain), []).append(brain_widget)
            yield brain_widget

        # Conversational input — only shown if LLM is enabled. Press / to focus.
        if self.llm_config.get("enabled", False):
            user_input = Input(
                placeholder="ask Winston something… (press / to focus)",
                id="user_input",
            )
            user_input.border_title = "─ ASK ─"
            yield user_input

        yield FooterBar(id="footer_bar")

    def on_mount(self) -> None:
        # ── Diagnostic env vars (for input-drop bisection) ────────────
        # WINSTON_DISABLE_PANELS=GpuPanel,CpuGraphPanel  → skip those
        #   panels' ticks entirely. Useful for narrowing which panel is
        #   blocking the event loop long enough to drop keystrokes.
        # WINSTON_TIMING=1 → log any tick > THRESHOLD ms to a file. Lets
        #   you see which tick spiked at the moment a keystroke vanished.
        import os
        import time as _t
        self._disabled_panels = set(
            x.strip() for x in os.environ.get("WINSTON_DISABLE_PANELS", "").split(",")
            if x.strip()
        )
        self._timing_enabled = bool(os.environ.get("WINSTON_TIMING"))
        self._timing_threshold_ms = float(os.environ.get("WINSTON_TIMING_MS", "10"))
        self._timing_path = os.environ.get("WINSTON_TIMING_LOG",
                                           "/tmp/winston_timing.log")
        if self._timing_enabled:
            # Truncate the log on each launch so the file reflects this run.
            try:
                with open(self._timing_path, "w") as f:
                    f.write(f"# winston timing log — threshold {self._timing_threshold_ms}ms\n")
                    f.write("# columns: wall_time_iso  source  duration_ms\n")
            except OSError:
                self._timing_enabled = False
        if self._disabled_panels:
            print(f"[diag] WINSTON_DISABLE_PANELS active: "
                  f"{sorted(self._disabled_panels)}")
        if self._timing_enabled:
            print(f"[diag] WINSTON_TIMING active: "
                  f"logging ticks > {self._timing_threshold_ms}ms to "
                  f"{self._timing_path}")

        # Panels were already primed in run() before this app started.
        # Just do an immediate widget refresh so the prepopulated data shows.
        for panel, _hz in zip(self.sections, self.section_rates):
            for w in self._panel_widgets.get(id(panel), []):
                try:
                    w.refresh_panel()
                except Exception:
                    pass

        # Prime the brain panel (it's not in self.sections, so the loop
        # above doesn't catch it).
        if self.brain_panel is not None:
            try:
                self.brain_panel.update()
            except Exception:
                pass
            for w in self._panel_widgets.get(id(self.brain_panel), []):
                try:
                    w.refresh_panel()
                except Exception:
                    pass

        # ── Master frame loop ────────────────────────────────────────
        # ONE timer drives the whole dashboard. Each panel keeps its own
        # natural rate (CPU at 4Hz, disk at 0.1Hz, etc) but updates only
        # fire when the per-panel interval has elapsed since the last
        # update. Widget refreshes are batched — every panel that updated
        # this frame gets refreshed in a single pass, so Textual's
        # compositor sees one coherent batch instead of 14 uncoordinated
        # refreshes per second. That's why this fixes both the visual
        # jitter (mismatched cadences) AND the input-drop bug (compositor
        # passes piling up against stdin processing).
        now = _t.monotonic()
        self._panel_intervals = {}
        self._panel_due_at = {}
        for panel, hz in zip(self.sections, self.section_rates):
            interval = 1.0 / hz
            self._panel_intervals[id(panel)] = interval
            # Stagger the first-due times so panels don't all fire on the
            # same frame on launch — spreads load over the first second.
            self._panel_due_at[id(panel)] = now + interval

        self._log_interval = 1.0 / self.logger_hz
        self._log_due_at = now + self._log_interval

        self._brain_due_at = now + 1.0
        self._status_due_at = now + 1.0

        # Resolve the StatusBar widget once so the frame loop doesn't
        # query the DOM 30 times a second.
        try:
            self._status_bar = self.query_one("#status_bar", StatusBar)
        except Exception:
            self._status_bar = None

        # Read the master frame rate from config. Default 30 if not set
        # (e.g. older config.py without FRAME_HZ).
        import config as _cfg
        frame_hz = float(getattr(_cfg, "FRAME_HZ", 30.0))
        self._frame_interval = 1.0 / frame_hz
        self.set_interval(self._frame_interval, self._frame_tick)

        # ── GPU-busy throttle ──────────────────────────────────────
        # When a game is running the GPU spikes and the terminal emulator
        # gets starved of redraws by Windows, making Winston feel laggy.
        # We watch GpuPanel's cached util (already on its own thread) and
        # when it sustains above GPU_BUSY_PCT for GPU_BUSY_HOLD_SEC, we
        # drop the dashboard's effective frame rate by skipping most ticks.
        # Once util drops back, we resume full speed within the same hold
        # window. Pure cosmetic throttle — data threads keep running.
        self._gpu_busy = False
        self._gpu_busy_since = None
        self._gpu_calm_since = None
        self._busy_skip_counter = 0
        # Reads from cached GpuPanel state so this is sub-µs per frame.
        self._gpu_panel = next(
            (p for p in self.sections if type(p).__name__ == "GpuPanel"),
            None,
        )

    def _update_gpu_busy_state(self, now):
        """Return True if we should be in busy-throttle mode this frame.

        Uses hysteresis so the throttle doesn't oscillate when GPU util
        bobs around the threshold:
          - Need GPU_BUSY_HOLD_SEC of sustained util > GPU_BUSY_PCT to
            ENTER busy mode
          - Need GPU_CALM_HOLD_SEC of util < GPU_BUSY_PCT to EXIT
        """
        import config as _cfg
        busy_pct = getattr(_cfg, "GPU_BUSY_PCT", 50)
        busy_hold = getattr(_cfg, "GPU_BUSY_HOLD_SEC", 3.0)
        calm_hold = getattr(_cfg, "GPU_CALM_HOLD_SEC", 5.0)

        if self._gpu_panel is None or not self._gpu_panel.gpus:
            return False
        util = self._gpu_panel.gpus[0].get("util") or 0

        if util > busy_pct:
            self._gpu_calm_since = None
            if self._gpu_busy_since is None:
                self._gpu_busy_since = now
            elif (not self._gpu_busy
                  and (now - self._gpu_busy_since) >= busy_hold):
                self._gpu_busy = True
        else:
            self._gpu_busy_since = None
            if self._gpu_calm_since is None:
                self._gpu_calm_since = now
            elif (self._gpu_busy
                  and (now - self._gpu_calm_since) >= calm_hold):
                self._gpu_busy = False

        return self._gpu_busy

    def _record_timing(self, source, started_at):
        """Append a timing line to the log if the elapsed exceeds threshold.
        Cheap when WINSTON_TIMING is off — early returns without I/O."""
        if not self._timing_enabled:
            return
        import time as _t
        elapsed_ms = (_t.monotonic() - started_at) * 1000.0
        if elapsed_ms < self._timing_threshold_ms:
            return
        try:
            from datetime import datetime as _dt
            with open(self._timing_path, "a") as f:
                f.write(f"{_dt.now().isoformat(timespec='milliseconds')}  "
                        f"{source}  {elapsed_ms:.1f}ms\n")
        except OSError:
            pass

    async def _frame_tick(self):
        """Master tick. Runs at FRAME_HZ. Each iteration:
          - For each panel whose interval has elapsed: panel.update() then
            refresh its widgets.
          - Same gating for brain panel (1Hz), status bar (1Hz), logger.
        Items not yet due are skipped — they'll fire on a later frame.

        Async + explicit `await asyncio.sleep(0)` yields between work
        segments. Even if no individual segment is slow, several segments
        in the same frame add up to a contiguous block where stdin can't
        be read. Yielding lets the asyncio loop process keystrokes between
        each panel's update — drops to 1-2ms gaps instead of 8-15ms gaps.
        """
        import asyncio
        import time as _t
        now = _t.monotonic()

        # ── GPU-busy throttle ──
        # Drop to ~5fps (skip ~5 of every 6 frames) when a game is running.
        # Logger + critical 1Hz things still fire because the inner due-time
        # gating handles that — most data panels just won't refresh as
        # often. Frees the terminal from competing with the game for redraws.
        if self._update_gpu_busy_state(now):
            self._busy_skip_counter += 1
            if self._busy_skip_counter % 6 != 0:
                return
            self._busy_skip_counter = 0

        # ── Data panels ──
        # One panel per yield: gives input a turn between every panel's
        # update + refresh. The yield is a no-op when stdin is idle.
        for panel in self.sections:
            cls = type(panel).__name__
            if cls in self._disabled_panels:
                continue
            if now < self._panel_due_at.get(id(panel), 0):
                continue
            t0 = _t.monotonic()
            self._panel_due_at[id(panel)] = now + self._panel_intervals[id(panel)]
            try:
                panel.update()
            except Exception:
                # Don't let one panel's error kill the whole app
                continue
            for w in self._panel_widgets.get(id(panel), []):
                try:
                    w.refresh_panel()
                except Exception:
                    pass
            self._record_timing(cls, t0)
            await asyncio.sleep(0)

        # ── Brain panel (1Hz, dirty-check) ──
        if (self.brain_panel is not None
                and "BrainPanel" not in self._disabled_panels
                and now >= self._brain_due_at):
            t0 = _t.monotonic()
            self._brain_due_at = now + 1.0
            bp = self.brain_panel
            try:
                bp.update()
                if bp.is_dirty():
                    for w in self._panel_widgets.get(id(bp), []):
                        try:
                            w.refresh_panel()
                        except Exception:
                            pass
                    self._record_timing("BrainPanel", t0)
                else:
                    self._record_timing("BrainPanel(skip)", t0)
            except Exception:
                pass
            await asyncio.sleep(0)

        # ── Status bar (1Hz) ──
        if (self._status_bar is not None
                and "StatusBar" not in self._disabled_panels
                and now >= self._status_due_at):
            t0 = _t.monotonic()
            self._status_due_at = now + 1.0
            try:
                self._status_bar.refresh_status()
            except Exception:
                pass
            self._record_timing("StatusBar", t0)
            await asyncio.sleep(0)

        # ── Logger ──
        if ("Logger" not in self._disabled_panels
                and now >= self._log_due_at):
            t0 = _t.monotonic()
            self._log_due_at = now + self._log_interval
            try:
                self.logger.log(self.sections)
            except Exception:
                pass
            self._record_timing("Logger", t0)

    def action_reset_history(self) -> None:
        for s in self.sections:
            for attr in ("history", "rx_history", "tx_history"):
                if hasattr(s, attr):
                    h = getattr(s, attr)
                    if hasattr(h, "clear"):
                        h.clear()
            if hasattr(s, "histories"):
                for h in s.histories:
                    h.clear()

    def action_focus_input(self) -> None:
        """Focus the conversational input. Bound to '/' key (vim-style)."""
        try:
            self.query_one("#user_input", Input).focus()
        except Exception:
            # Input not present (LLM disabled). Silently ignore.
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """User pressed Enter in the input box. Hand the question to the
        commentary panel and clear the input."""
        if event.input.id != "user_input":
            return
        question = event.value.strip()
        event.input.value = ""  # clear for next question
        if not question:
            return
        try:
            commentary = self.query_one("#commentary_panel", CommentaryPanel)
            commentary.ask_user(question)
        except Exception:
            pass
        # Return focus to the main app so / works again without clicking out
        event.input.blur()

    def on_unmount(self) -> None:
        self.logger.close()


def run(sections, logger, config=None):
    """Prime panels with one update each (so the screen renders with data
    immediately) THEN start the Textual app. Slow panels like DiskPanel
    finish their initial scan during this priming phase, before the user
    sees the UI — eliminates the "panel is blank for 5 seconds" startup.

    `config` is the config module (typically `import config`); it owns all
    the tunable behavior. See config.py for what's available.
    """
    if config is None:
        # Fallback for someone calling run() programmatically without a
        # config module — load the default one.
        import config as default_config
        config = default_config

    print("WINSTON :: priming sensors...", end=" ", flush=True)

    # Prime psutil
    psutil.cpu_percent(percpu=True)
    psutil.cpu_percent()
    for p in psutil.process_iter():
        try:
            p.cpu_percent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Run each panel's first update so it has data before screen draws
    for panel, _hz in sections:
        try:
            panel.update()
        except Exception:
            pass

    print("ready.")

    # Build the LLM config dict the panel expects, sourced from config.py.
    # We keep the panel's interface as a dict (decoupled from the config
    # module shape) so panel code stays testable in isolation.
    llm_config = {
        "enabled":                 config.LLM_ENABLED,
        "model":                   config.LLM_MODEL,
        "use_tiered":              getattr(config, "LLM_USE_TIERED", False),
        "model_fast":              getattr(config, "LLM_MODEL_FAST", config.LLM_MODEL),
        "model_quality":           getattr(config, "LLM_MODEL_QUALITY", config.LLM_MODEL),
        "fast_keep_alive_sec":     getattr(config, "LLM_FAST_KEEP_ALIVE_SEC", 0),
        "quality_keep_alive_sec":  getattr(config, "LLM_QUALITY_KEEP_ALIVE_SEC", 0),
        "show_brain_panel":        getattr(config, "SHOW_BRAIN_PANEL", True),
        "user_name":               config.USER_NAME,
        "startup_greeting":        config.STARTUP_GREETING,
        "typewriter_tps":          config.TYPEWRITER_TPS,
        "inter_message_pause_sec": config.INTER_MESSAGE_PAUSE_SEC,
        "lines":                   config.COMMENTARY_LINES,
        "heartbeat_interval_sec":  config.HEARTBEAT_INTERVAL_SEC,
        "stale_quiet_threshold_sec": config.STALE_QUIET_THRESHOLD_SEC,
        "triggers":                config.TRIGGERS,
    }

    # Bootstrap persistent memory.
    memory = None
    if config.LLM_ENABLED:
        try:
            from brain.memory import Memory
            memory = Memory()
            memory.set_user(name=config.USER_NAME)
            gpu_panel = next((p for p, _ in sections
                              if type(p).__name__ == "GpuPanel"), None)
            ram_panel = next((p for p, _ in sections
                              if type(p).__name__ == "RamPanel"), None)
            memory.set_machine_facts(gpu_panel=gpu_panel, ram_panel=ram_panel)
            print("WINSTON :: scanning log for personalization...", end=" ",
                  flush=True)
            info = memory.learn_from_log(hours=168)
            if info.get("log_missing"):
                print("(no log yet — Winston starts learning today)")
            else:
                print(f"learned {info.get('ranked_apps', 0)} apps "
                      f"from {info.get('rows_scanned', 0)} rows.")
            memory.save()
        except Exception as e:
            print(f"(memory init failed: {e!r} — continuing without it)")
            memory = None

    app = WinstonApp(sections, logger,
                     logger_hz=config.LOGGER_HZ,
                     llm_config=llm_config,
                     memory=memory)
    app.run()