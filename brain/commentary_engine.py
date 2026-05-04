"""Backend-agnostic LLM commentary orchestrator.

The engine owns: state machine, Q&A history, streaming buffer, trigger
runner, heartbeat / stale-quiet timing, prompt-building dispatch, model
tier selection.

The engine does NOT own: rendering, threading, timers. The frontend owns
those. Same engine plugs into the Textual TUI (cli/display.py) and the
PyQt6 GUI (gui/main.py); only rendering differs.

Why this split:
  - Triggers used to live in cli/display.py:CommentaryPanel — that meant
    every new frontend had to re-implement the trigger / heartbeat /
    stale-quiet evaluation, the cooldown gating, the "alert preempts
    only" rule, the multi-turn Q&A history, and the model tier picks.
  - Now those rules live here once. Frontends call engine.evaluate_triggers()
    at 1Hz and fire whatever the engine tells them to fire.

Threading model:
  Stream callbacks (`on_chunk`, `on_done`, `on_error`) are written to be
  called from any thread — they only mutate engine state via simple
  scalar/list operations that are atomic in CPython. The frontend marshals
  worker-thread chunk arrivals to the UI thread via its own mechanism
  (Textual's `call_from_thread`, Qt's queued signals) BEFORE passing them
  to the engine. This way the engine can ignore threading entirely.

Typical frontend flow (pseudocode):

    engine = CommentaryEngine(sections, config, memory)

    # Once at mount:
    if config.startup_greeting:
        # Walk greeting → retrospective → init triggers + first routine
        system, user, tier = engine.build_greeting()
        fire_stream(system, user, tier)
        # When stream finalizes, build_retrospective(), then begin_regular_loop()

    # 1Hz tick:
    result = engine.evaluate_triggers()
    if result == ("event", evt):
        system, user, tier = engine.build_triggered(evt)
        fire_stream(system, user, tier)
    elif result in (("heartbeat", None), ("stale", None)):
        system, user, tier = engine.build_observation()
        fire_stream(system, user, tier)

    # User submits a question:
    engine.handle_user_question(q)
    system, user, tier = engine.build_conversational(q)
    fire_stream(system, user, tier)

    # Typewriter at TYPEWRITER_TPS:
    result = engine.typewriter_advance()
    if result == "finalize":
        # message done — schedule end_cooldown after pause
        # if startup_step is set, advance it

`fire_stream` is the frontend helper that wires `generate_stream_async`'s
chunk/done/error callbacks (after marshaling) into engine.on_chunk /
on_done / on_error.
"""
import time
from datetime import datetime


class CommentaryEngine:
    def __init__(self, sections, config, memory):
        self.sections = sections
        self.config = config or {}
        self.memory = memory

        # ── State machine ──
        # THINKING / STREAMING / IDLE / ERROR / DISABLED
        if self.config.get("enabled", False):
            self.state = "IDLE"
        else:
            self.state = "DISABLED"

        # ── Streaming buffer + typewriter cursor ──
        self.streaming_buffer = ""
        self.typed_chars = 0
        self.stream_complete = False

        # ── History ──
        # Display history: list of (ts, msg, kind) where kind is
        # "winston" or "user". Capped at config["lines"] - 1.
        self.history = []
        self._max_history = max(1, self.config.get("lines", 5)) - 1

        # Q&A history for multi-turn context (separate from display
        # history). Last 5 pairs kept; last 3 sent into the prompt.
        self.qa_history = []
        self.pending_user_question = None

        # Startup ritual progression: "greeting" → "retrospective" → None.
        self.startup_step = None

        # Last trigger event (consumed by BRAIN panel diagnostic).
        self.last_event = None

        # Trigger runner is created lazily by init_triggers() so it doesn't
        # eagerly import brain.triggers when LLM is disabled.
        self._trigger_runner = None
        self._cooldown_active = False
        self._last_fire_time = time.monotonic()
        self._last_heartbeat_time = time.monotonic()

    # ──────────────── Properties ────────────────
    @property
    def is_busy(self):
        return (self.state in ("STREAMING", "THINKING")
                or self._cooldown_active)

    # ──────────────── Model tier selection ────────────────
    def pick_model(self, tier):
        """Returns (model_name, keep_alive) for the given tier.

        tier: "fast" | "quality"
        When use_tiered is False, both tiers fall back to single model.
        """
        cfg = self.config
        if not cfg.get("use_tiered", False):
            return cfg.get("model"), -1
        if tier == "quality":
            return (cfg.get("model_quality") or cfg.get("model"),
                    cfg.get("quality_keep_alive_sec", 0))
        return (cfg.get("model_fast") or cfg.get("model"),
                cfg.get("fast_keep_alive_sec", 0))

    # ──────────────── Stream lifecycle ────────────────
    def begin_streaming(self):
        """Reset state for a fresh LLM call. Frontend calls this right
        before `generate_stream_async`."""
        self.streaming_buffer = ""
        self.typed_chars = 0
        self.stream_complete = False
        self.state = "THINKING"

    def on_chunk(self, chunk):
        """Frontend calls this on the UI thread for each chunk."""
        if self.state == "THINKING":
            self.state = "STREAMING"
        self.streaming_buffer += chunk

    def on_done(self, _full_text):
        """Stream finished. Typewriter may still be catching up; the
        engine waits for typewriter_advance() to call finalize."""
        self.stream_complete = True

    def on_error(self):
        self.state = "ERROR"

    def typewriter_advance(self):
        """Frontend calls at typewriter_tps. Returns one of:

          "advanced" — typed_chars incremented, frontend should repaint
          "finalize" — message just finalized, frontend should advance
                       startup ritual (if any) and start cooldown timer
          None       — nothing happened (idle or buffer empty)
        """
        if self.typed_chars < len(self.streaming_buffer):
            self.typed_chars += 1
            return "advanced"
        if self.stream_complete and self.state != "IDLE":
            self._finalize_message()
            return "finalize"
        return None

    def force_finalize(self):
        """Snap the current stream to its end. Used when a user question
        arrives mid-stream so the chat log keeps the partial message."""
        self.typed_chars = len(self.streaming_buffer)
        self._finalize_message()
        self._cooldown_active = False

    def end_cooldown(self):
        """Frontend calls this after the inter-message-pause has elapsed."""
        self._cooldown_active = False

    def _finalize_message(self):
        msg = self.streaming_buffer.strip()
        if msg:
            ts = datetime.now().strftime("%H:%M:%S")
            self.history.append((ts, msg, "winston"))
            if len(self.history) > self._max_history:
                self.history = self.history[-self._max_history:]
            if self.pending_user_question is not None:
                self.qa_history.append((self.pending_user_question, msg))
                if len(self.qa_history) > 5:
                    self.qa_history = self.qa_history[-5:]
                self.pending_user_question = None
        self.streaming_buffer = ""
        self.typed_chars = 0
        self.stream_complete = False
        self.state = "IDLE"
        self._cooldown_active = True

    # ──────────────── User questions ────────────────
    def handle_user_question(self, question):
        """Records the user message + flags it as pending. Returns the
        cleaned question string (or None if it was empty / disabled)."""
        if self.state == "DISABLED":
            return None
        question = (question or "").strip()
        if not question:
            return None

        # Force-finalize any in-flight stream so the chat log keeps a
        # record of what was being said. Same approach as before.
        if self.state == "STREAMING":
            self.force_finalize()

        ts = datetime.now().strftime("%H:%M:%S")
        self.history.append((ts, question, "user"))
        if len(self.history) > self._max_history:
            self.history = self.history[-self._max_history:]
        self.pending_user_question = question
        return question

    # ──────────────── Trigger orchestration ────────────────
    def init_triggers(self):
        """Lazily create the TriggerRunner. Frontend calls this once,
        right before starting the 1Hz tick."""
        from brain.triggers import TriggerRunner
        self._trigger_runner = TriggerRunner(self.config.get("triggers", {}))
        now = time.monotonic()
        self._last_fire_time = now
        self._last_heartbeat_time = now

    def evaluate_triggers(self):
        """Called by frontend at 1Hz. Returns:

          ("event", TriggerEvent) — fire triggered commentary
          ("heartbeat", None)     — fire routine (heartbeat is due)
          ("stale", None)         — fire routine (stale-quiet defense)
          None                     — do nothing this tick

        Cooldown / busy / preemption rules are enforced here so the
        frontend doesn't need to know about them.
        """
        if self._trigger_runner is None:
            return None
        now = time.monotonic()
        try:
            event = self._trigger_runner.tick(self.sections)
        except Exception:
            event = None

        if event is not None:
            # If we're mid-stream, only `alert`-tier events preempt.
            if self.is_busy and event.severity != "alert":
                return None
            self._last_fire_time = now
            return ("event", event)

        # No event. If we're busy, nothing else fires.
        if self.is_busy:
            return None

        heartbeat = self.config.get("heartbeat_interval_sec", 300)
        if heartbeat > 0 and (now - self._last_heartbeat_time) >= heartbeat:
            self._last_heartbeat_time = now
            self._last_fire_time = now
            return ("heartbeat", None)

        stale = self.config.get("stale_quiet_threshold_sec", 900)
        if stale > 0 and (now - self._last_fire_time) >= stale:
            self._last_fire_time = now
            return ("stale", None)

        return None

    # ──────────────── Prompt builders ────────────────
    # Each returns (system, user, tier) or (None, None, None) on failure.
    # Frontend calls one of these, then `pick_model(tier)` to get the
    # actual model + keep_alive, then fires `generate_stream_async`.
    def build_greeting(self):
        from brain.prompt import build_greeting_prompt
        try:
            system, user = build_greeting_prompt(
                user_name=self.config.get("user_name"),
                memory=self.memory,
            )
        except Exception:
            return None, None, None
        return system, user, "fast"

    def build_retrospective(self):
        from brain.prompt import build_retrospective_prompt
        from brain.history import summarize_recent
        try:
            stats = summarize_recent(hours=24)
            system, user = build_retrospective_prompt(stats)
        except Exception:
            return None, None, None
        if system is None or user is None:
            return None, None, None
        return system, user, "fast"

    def build_triggered(self, event):
        from brain.prompt import build_triggered_prompt
        try:
            system, user = build_triggered_prompt(
                self.sections, event, memory=self.memory,
            )
        except Exception:
            return None, None, None
        self.last_event = (event.name, event.severity,
                           datetime.now().strftime("%H:%M:%S"))
        # All triggered events use fast — popping the 7B during alerts
        # is the worst possible moment for a game session.
        return system, user, "fast"

    def build_observation(self):
        from brain.prompt import build_observation_prompt
        try:
            system, user = build_observation_prompt(
                self.sections, memory=self.memory,
            )
        except Exception:
            return None, None, None
        return system, user, "fast"

    def build_conversational(self, question):
        from brain.prompt import build_conversational_prompt
        try:
            system, user = build_conversational_prompt(
                self.sections, question, history=self.qa_history,
                memory=self.memory,
            )
        except Exception:
            return None, None, None
        return system, user, "quality"
