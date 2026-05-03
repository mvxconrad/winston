"""WINSTON display — hand-rolled scrolling braille CPU graph, tight layout."""
import platform
import socket
from datetime import datetime

import psutil
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static


# ──────────────── Custom chrome ────────────────
class StatusBar(Static):
    def on_mount(self):
        self.set_interval(1.0, self.refresh_status)
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
    """LLM-powered commentary panel — chat-log style with typewriter.

    Shows a scrolling history of messages; older messages stay visible
    above the newest, fading in brightness. Uses a typewriter buffer to
    decouple LLM generation speed from display speed: tokens arrive fast,
    panel emits them at config['typewriter_tps'] for a more deliberate feel.

    On startup (if config['startup_greeting']):
      1. Greeting: "Good morning, max."
      2. Retrospective: brief comment on last 24h of logs
      3. Begin regular commentary loop

    Threading: LLM worker runs on its own thread (brain.client) and fires
    callbacks from THAT thread. We use Textual's call_from_thread() to
    bounce state mutations to the UI thread.
    """

    # Cursor blink rate while streaming
    CURSOR_BLINK_HZ = 2.5

    def __init__(self, sections=None, config=None, **kwargs):
        super().__init__(**kwargs)
        self._sections = sections or []
        self._config = config or {"enabled": False}

        # Chat-log history: list of (timestamp_str, message) tuples.
        # Capped at config['lines'] - 1 (one slot reserved for currently-
        # streaming message). Older messages slide off the top.
        self._history = []
        self._max_history = max(1, self._config.get("lines", 5)) - 1

        # Typewriter machinery. The LLM stream writes into _streaming_buffer
        # as fast as tokens arrive. The typewriter timer advances
        # _typed_chars at config['typewriter_tps'] rate. We display the
        # buffer truncated to _typed_chars.
        self._streaming_buffer = ""   # what the LLM has produced so far
        self._typed_chars = 0          # how much the typewriter has revealed
        self._stream_complete = False  # LLM has signalled done

        # Possible states: THINKING, STREAMING, IDLE, ERROR, DISABLED
        if self._config.get("enabled", False):
            self._state = "THINKING"
        else:
            self._state = "DISABLED"

        self._cursor_visible = True
        # Whether the current message is part of startup ritual
        # ("greeting", "retrospective", or None for regular commentary)
        self._startup_step = None
        # Set when we're in the inter-message pause (current done, next not yet started)
        self._cooldown_active = False

    # ──────────────── Lifecycle ────────────────
    def on_mount(self):
        if not self._config.get("enabled", False):
            self._paint()
            return

        # Cursor blink
        self.set_interval(1.0 / self.CURSOR_BLINK_HZ, self._toggle_cursor)
        # Typewriter — emits chars at the configured rate
        tps = self._config.get("typewriter_tps", 25)
        self.set_interval(1.0 / tps, self._typewriter_tick)

        # Begin startup sequence (if enabled), else jump to regular loop
        if self._config.get("startup_greeting", True):
            self._startup_step = "greeting"
            self._trigger_greeting()
        else:
            self._begin_regular_loop()
        self._paint()

    # ──────────────── Startup sequence ────────────────
    def _trigger_greeting(self):
        from brain.client import generate_stream_async
        from brain.prompt import build_greeting_prompt

        try:
            system, user = build_greeting_prompt(
                user_name=self._config.get("user_name")
            )
        except Exception:
            self._on_startup_step_done()
            return

        self._begin_streaming()
        generate_stream_async(
            user, system=system, model=self._config.get("model"),
            on_chunk=self._on_chunk_worker,
            on_done=self._on_startup_done_worker,
            on_error=self._on_startup_error_worker,
        )

    def _trigger_retrospective(self):
        from brain.prompt import build_retrospective_prompt
        from brain.history import summarize_recent

        try:
            stats = summarize_recent(hours=24)
            system, user = build_retrospective_prompt(stats)
        except Exception:
            self._on_startup_step_done()
            return

        if system is None or user is None:
            self._on_startup_step_done()
            return

        # Wait the inter-message pause before starting next message
        pause = self._config.get("inter_message_pause_sec", 2.0)
        self.set_timer(pause, lambda: self._begin_retrospective_call(system, user))

    def _begin_retrospective_call(self, system, user):
        from brain.client import generate_stream_async
        self._begin_streaming()
        generate_stream_async(
            user, system=system, model=self._config.get("model"),
            on_chunk=self._on_chunk_worker,
            on_done=self._on_startup_done_worker,
            on_error=self._on_startup_error_worker,
        )

    def _on_startup_step_done(self):
        if self._startup_step == "greeting":
            self._startup_step = "retrospective"
            self._trigger_retrospective()
        elif self._startup_step == "retrospective":
            self._startup_step = None
            pause = self._config.get("inter_message_pause_sec", 2.0)
            self.set_timer(pause, self._begin_regular_loop)

    def _begin_regular_loop(self):
        interval = self._config.get("interval_sec", 30.0)
        self.set_interval(interval, self._trigger_llm)
        self._trigger_llm()

    # ──────────────── Regular LLM trigger ────────────────
    def _trigger_llm(self):
        # Don't queue another call while one is in flight or being typed out
        if self._state in ("STREAMING", "THINKING") or self._cooldown_active:
            return

        from brain.client import generate_stream_async
        from brain.prompt import build_observation_prompt

        try:
            system, user = build_observation_prompt(self._sections)
        except Exception:
            self._state = "ERROR"
            self._paint()
            return

        self._begin_streaming()
        generate_stream_async(
            user, system=system, model=self._config.get("model"),
            on_chunk=self._on_chunk_worker,
            on_done=self._on_done_worker,
            on_error=self._on_error_worker,
        )

    # ──────────────── Streaming + typewriter ────────────────
    def _begin_streaming(self):
        """Reset state for a fresh LLM call."""
        self._streaming_buffer = ""
        self._typed_chars = 0
        self._stream_complete = False
        self._state = "THINKING"
        self._paint()

    def _typewriter_tick(self):
        """Advance the typewriter cursor by one character if there's more to show."""
        if self._typed_chars < len(self._streaming_buffer):
            self._typed_chars += 1
            self._paint()
        elif self._stream_complete and self._state != "IDLE":
            # All tokens received AND all chars shown — message is fully done
            self._finalize_message()

    def _finalize_message(self):
        """Move the just-finished streaming message into history."""
        msg = self._streaming_buffer.strip()
        if msg:
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S")
            self._history.append((ts, msg))
            # Trim history to max
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        self._streaming_buffer = ""
        self._typed_chars = 0
        self._stream_complete = False
        self._state = "IDLE"
        self._cooldown_active = True
        self._paint()

        # Cooldown: after the inter-message pause, allow next message to start
        pause = self._config.get("inter_message_pause_sec", 2.0)
        self.set_timer(pause, self._end_cooldown)

    def _end_cooldown(self):
        self._cooldown_active = False
        # If this was a startup step, advance to the next one now
        if self._startup_step is not None:
            self._on_startup_step_done()

    # ──────────────── Worker-thread callbacks ────────────────
    # Fire from the LLM worker thread; we marshal back to UI thread.
    def _on_chunk_worker(self, chunk):
        self.app.call_from_thread(self._on_chunk, chunk)

    def _on_done_worker(self, _full_text):
        self.app.call_from_thread(self._on_done)

    def _on_error_worker(self):
        self.app.call_from_thread(self._on_error)

    def _on_startup_done_worker(self, _full_text):
        self.app.call_from_thread(self._on_done)

    def _on_startup_error_worker(self):
        self.app.call_from_thread(self._on_startup_error)

    # ──────────────── UI-thread state updates ────────────────
    def _on_chunk(self, chunk):
        if self._state == "THINKING":
            self._state = "STREAMING"
        self._streaming_buffer += chunk
        # Don't update _typed_chars here — let the typewriter tick advance it
        self._paint()

    def _on_done(self):
        # LLM is done generating, but typewriter may still be catching up.
        # Mark stream complete; _typewriter_tick will call _finalize_message
        # once it's caught up to the buffer length.
        self._stream_complete = True

    def _on_error(self):
        self._state = "ERROR"
        self._paint()
        # Still treat as a finished step so startup can advance
        if self._startup_step is not None:
            self._cooldown_active = True
            pause = self._config.get("inter_message_pause_sec", 2.0)
            self.set_timer(pause, self._end_cooldown)

    def _on_startup_error(self):
        self._on_error()

    # ──────────────── Rendering ────────────────
    def _toggle_cursor(self):
        if self._state in ("STREAMING", "THINKING"):
            self._cursor_visible = not self._cursor_visible
            self._paint()

    def _paint(self):
        """Render the history + currently-streaming message."""
        if self._state == "DISABLED":
            self.update("[bold bright_green]>[/bold bright_green] "
                        "[grey50]analysis subsystem :: disabled[/grey50] "
                        "[grey50](LLM_ENABLED = False in winston.py)[/grey50]")
            return

        # Build line list: [old, old, old, current_streaming]
        lines = []

        # Older history lines, dimmed
        for ts, msg in self._history:
            safe = msg.replace("[", r"\[")
            lines.append(f"[grey50]{ts}[/grey50]  "
                         f"[bold grey50]>[/bold grey50] "
                         f"[#1a8c1a]{safe}[/#1a8c1a]")

        # Current line — depends on state
        if self._state == "THINKING":
            cursor = "█" if self._cursor_visible else " "
            lines.append(f"[grey50]--:--:--[/grey50]  "
                         f"[bold bright_green]>[/bold bright_green] "
                         f"[grey50]thinking…[/grey50] "
                         f"[bright_green]{cursor}[/bright_green]")
        elif self._state == "STREAMING":
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S")
            visible = self._streaming_buffer[:self._typed_chars]
            safe = visible.replace("[", r"\[")
            cursor = "█" if self._cursor_visible else " "
            lines.append(f"[grey50]{ts}[/grey50]  "
                         f"[bold bright_green]>[/bold bright_green] "
                         f"[bright_green]{safe}[/bright_green]"
                         f"[bright_green]{cursor}[/bright_green]")
        elif self._state == "ERROR":
            lines.append(f"[grey50]--:--:--[/grey50]  "
                         f"[bold bright_green]>[/bold bright_green] "
                         f"[red]analysis error[/red] "
                         f"[grey50](LLM unreachable)[/grey50]")
        # IDLE state — no current line, just history

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
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "reset_history", "Reset"),
    ]

    def __init__(self, sections, logger, logger_hz=1.0, llm_config=None):
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
                                      id="commentary_panel")
        # Panel height = line count + borders (2) + padding (0) + a little
        # buffer (1). With config['lines'] of 5 -> 8 cells.
        commentary.styles.height = self.llm_config.get("lines", 5) + 3
        commentary.border_title = "─ COMMENTARY ─"
        yield commentary

        yield FooterBar(id="footer_bar")

    def on_mount(self) -> None:
        # Panels were already primed in run() before this app started.
        # Just do an immediate widget refresh so the prepopulated data shows.
        for panel, _hz in zip(self.sections, self.section_rates):
            for w in self._panel_widgets.get(id(panel), []):
                try:
                    w.refresh_panel()
                except Exception:
                    pass

        # Schedule each panel on its own interval
        for panel, hz in zip(self.sections, self.section_rates):
            interval = 1.0 / hz
            self.set_interval(interval, self._make_tick(panel))

        # Logger ticks at its own rate, regardless of panel rates
        self.set_interval(1.0 / self.logger_hz, self._log_tick)

    def _make_tick(self, panel):
        """Return a closure that updates a specific panel and refreshes its widgets."""
        def tick():
            try:
                panel.update()
            except Exception:
                # Don't let one panel's error kill the whole app
                return
            # Refresh associated widgets
            for w in self._panel_widgets.get(id(panel), []):
                try:
                    w.refresh_panel()
                except Exception:
                    pass
        return tick

    def _log_tick(self):
        """Logger tick — write a row at LOGGER_HZ."""
        try:
            self.logger.log(self.sections)
        except Exception:
            pass

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

    def on_unmount(self) -> None:
        self.logger.close()


def run(sections, logger, logger_hz=1.0,
        llm_enabled=True, llm_model="qwen2.5:7b-instruct", user_name=None,
        commentary_interval_sec=30.0, startup_greeting=True,
        typewriter_tps=25, inter_message_pause_sec=2.0, commentary_lines=5):
    """Prime panels with one update each (so the screen renders with data
    immediately) THEN start the Textual app. Slow panels like DiskPanel
    finish their initial scan during this priming phase, before the user
    sees the UI — eliminates the "panel is blank for 5 seconds" startup.
    """
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

    llm_config = {
        "enabled": llm_enabled,
        "model": llm_model,
        "user_name": user_name,
        "interval_sec": commentary_interval_sec,
        "startup_greeting": startup_greeting,
        "typewriter_tps": typewriter_tps,
        "inter_message_pause_sec": inter_message_pause_sec,
        "lines": commentary_lines,
    }
    app = WinstonApp(sections, logger, logger_hz=logger_hz, llm_config=llm_config)
    app.run()