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

        # Chat-log history: list of (timestamp_str, message, kind) tuples.
        # kind is "winston" (LLM output) or "user" (user question).
        # Capped at config['lines'] - 1 (one slot reserved for streaming).
        self._history = []
        self._max_history = max(1, self._config.get("lines", 5)) - 1

        # Q&A history for multi-turn context (separate from display history).
        # List of (user_question, winston_answer) pairs. Last 3 fed back to
        # the LLM as context so follow-ups make sense.
        self._qa_history = []
        # The user question that's currently being answered (so when the
        # answer streams in, we know to record the pair into _qa_history)
        self._pending_user_question = None

        # Typewriter machinery — same as before
        self._streaming_buffer = ""
        self._typed_chars = 0
        self._stream_complete = False

        # Possible states: THINKING, STREAMING, IDLE, ERROR, DISABLED
        if self._config.get("enabled", False):
            self._state = "THINKING"
        else:
            self._state = "DISABLED"

        self._cursor_visible = True
        self._startup_step = None
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
        """Start the trigger-driven commentary loop.

        Two timers:
          1. 1Hz trigger tick — pushes baselines, evaluates triggers, fires
             commentary if a trigger says so.
          2. Heartbeat — every config['heartbeat_interval_sec'] seconds,
             fires a routine commentary regardless. Reassures the user
             Winston is alive even when nothing's happening.
        """
        from brain.triggers import TriggerRunner
        self._trigger_runner = TriggerRunner(self._config.get("triggers", {}))
        # Time of last commentary firing (any kind — trigger, heartbeat, user)
        # Used by the stale-quiet check.
        import time
        self._last_fire_time = time.monotonic()
        # When the last heartbeat fired (separate from _last_fire_time
        # because user input/triggers don't reset the heartbeat clock —
        # we still want one ~5min after launch even if you've been busy).
        self._last_heartbeat_time = time.monotonic()

        # 1Hz tick: evaluate triggers
        self.set_interval(1.0, self._tick_triggers)

        # Fire one routine commentary right away so the panel has fresh
        # content as soon as startup ritual is done
        self._trigger_routine("startup commentary")

    # ──────────────── Trigger-driven loop ────────────────
    def _tick_triggers(self):
        """Called every second. Updates baselines, checks for events,
        fires commentary if appropriate."""
        import time
        now = time.monotonic()

        # Always update baselines, even if we're streaming. Baselines should
        # reflect actual ongoing state, not only when we're idle.
        try:
            event = self._trigger_runner.tick(self._sections)
        except Exception:
            event = None

        # If we're currently streaming, the only thing that should preempt
        # is an `alert`-tier event. Notable preempts routine but we don't
        # know if the current stream is routine or notable here, so we
        # play it conservative: only alerts preempt. (Stage 5.5 may revisit.)
        is_busy = self._state in ("STREAMING", "THINKING") or self._cooldown_active

        if event is not None:
            if is_busy and event.severity != "alert":
                return  # let current message finish; this trigger's cooldown
                        # already started so it won't fire again immediately
            self._trigger_event(event)
            self._last_fire_time = now
            return

        # No event fired. Check if it's time for a heartbeat.
        if is_busy:
            return

        heartbeat_interval = self._config.get("heartbeat_interval_sec", 300)
        if heartbeat_interval > 0 and (now - self._last_heartbeat_time) >= heartbeat_interval:
            self._last_heartbeat_time = now
            self._last_fire_time = now
            self._trigger_routine("heartbeat")
            return

        # Stale check — nothing's happened in a long time at all
        stale = self._config.get("stale_quiet_threshold_sec", 900)
        if stale > 0 and (now - self._last_fire_time) >= stale:
            self._last_fire_time = now
            self._trigger_routine("stale quiet")

    def _trigger_event(self, event):
        """Fire commentary for a specific trigger event."""
        from brain.client import generate_stream_async
        from brain.prompt import build_triggered_prompt

        try:
            system, user = build_triggered_prompt(self._sections, event)
        except Exception:
            return

        self._begin_streaming()
        generate_stream_async(
            user, system=system, model=self._config.get("model"),
            on_chunk=self._on_chunk_worker,
            on_done=self._on_done_worker,
            on_error=self._on_error_worker,
        )

    def _trigger_routine(self, _reason):
        """Fire a routine commentary (heartbeat / stale / startup)."""
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
            self._history.append((ts, msg, "winston"))
            # Trim history to max
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

            # If this answered a user question, record the pair for multi-turn
            if self._pending_user_question is not None:
                self._qa_history.append((self._pending_user_question, msg))
                # Cap Q&A history at last 5 (we only feed back last 3 anyway)
                if len(self._qa_history) > 5:
                    self._qa_history = self._qa_history[-5:]
                self._pending_user_question = None

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

    # ──────────────── User question handler ────────────────
    def ask_user(self, question):
        """Handle a user question typed into the conversational input.

        Preempts whatever is currently streaming (user's question is alert-
        tier — they're waiting for an answer). Adds the question to the chat
        log, then fires an LLM call with current state + Q&A history as
        context.
        """
        if not self._config.get("enabled", False):
            return
        if not question or not question.strip():
            return

        from brain.client import generate_stream_async
        from brain.prompt import build_conversational_prompt
        from datetime import datetime

        # If something's mid-stream, finalize it abruptly so the chat log
        # doesn't lose the partial message. This is the simplest race-
        # condition handling — Stage 5.5 will replace this with proper
        # tier-based preemption.
        if self._state == "STREAMING":
            # Force-finalize what's been typed so far
            self._typed_chars = len(self._streaming_buffer)
            self._finalize_message()
            self._cooldown_active = False  # don't make user wait through cooldown

        # Add the user's question to the chat log
        ts = datetime.now().strftime("%H:%M:%S")
        self._history.append((ts, question.strip(), "user"))
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        # Build prompt with snapshot + multi-turn context + the question
        try:
            system, user_prompt = build_conversational_prompt(
                self._sections, question.strip(), history=self._qa_history
            )
        except Exception:
            self._state = "ERROR"
            self._paint()
            return

        # Track the pending question so we can pair it with the answer
        self._pending_user_question = question.strip()

        self._begin_streaming()
        generate_stream_async(
            user_prompt, system=system, model=self._config.get("model"),
            on_chunk=self._on_chunk_worker,
            on_done=self._on_done_worker,
            on_error=self._on_error_worker,
        )

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

        lines = []

        # Color fade for older messages — newest gets bright green, each
        # older line steps down through medium green, dim green, to grey.
        # The fade tells the eye where "now" is at a glance.
        FADE_PALETTE = ["bright_green", "#3aa83a", "#1a8c1a", "#0a5a0a", "grey50"]

        history_count = len(self._history)
        for idx, entry in enumerate(self._history):
            # Tolerate old (ts, msg) format if it slipped in
            if len(entry) == 3:
                ts, msg, kind = entry
            else:
                ts, msg = entry
                kind = "winston"
            safe = msg.replace("[", r"\[")

            # Distance from the end determines fade depth.
            # The newest history entry (idx == count-1) gets the second-
            # brightest color (because position 0 of the palette is reserved
            # for the actively-streaming line).
            distance_from_end = (history_count - 1) - idx
            color = FADE_PALETTE[min(distance_from_end + 1, len(FADE_PALETTE) - 1)]

            if kind == "user":
                # User lines: keep the cyan accent, fade the body to grey
                # at the same rate as winston lines.
                lines.append(f"[grey50]{ts}[/grey50]  "
                             f"[bold bright_cyan]?[/bold bright_cyan] "
                             f"[#3a8a9c]{safe}[/#3a8a9c]")
            else:
                lines.append(f"[grey50]{ts}[/grey50]  "
                             f"[bold {color}]>[/bold {color}] "
                             f"[{color}]{safe}[/{color}]")

        # Current streaming line — always rendered in the brightest green
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
        "user_name":               config.USER_NAME,
        "startup_greeting":        config.STARTUP_GREETING,
        "typewriter_tps":          config.TYPEWRITER_TPS,
        "inter_message_pause_sec": config.INTER_MESSAGE_PAUSE_SEC,
        "lines":                   config.COMMENTARY_LINES,
        "heartbeat_interval_sec":  config.HEARTBEAT_INTERVAL_SEC,
        "stale_quiet_threshold_sec": config.STALE_QUIET_THRESHOLD_SEC,
        "triggers":                config.TRIGGERS,
    }
    app = WinstonApp(sections, logger,
                     logger_hz=config.LOGGER_HZ,
                     llm_config=llm_config)
    app.run()