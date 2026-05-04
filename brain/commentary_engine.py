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
import json
import os
import time
from datetime import datetime


# ──────────────── Reasoning trace log ────────────────
# Every prompt we send to the LLM and every response we get back is
# appended here as JSONL — one line per event. Lets you see exactly what
# Winston was thinking when he said something weird, without having to
# instrument either frontend.
#
# Tail the file to watch live:  tail -f logs/reasoning.jsonl
#
# A parallel `logs/reasoning.log` is written in human-readable form for
# the same events — easier on the eye when you're skimming a session
# without piping through `jq`. The JSONL stays canonical for tooling.
#
# Persistent across sessions, with a visible banner written on each
# launch so you can scan the file (or `grep "WINSTON SESSION"`) to find
# session boundaries. Rotates to .old when it crosses 50 MB so the file
# can't grow unbounded.
#
# Schema (kept loose; one event per line):
#   {"t": "2026-05-04T...",  "kind": "session_start", "pid": 12345}
#   {"t": "...", "kind": "prompt",   "trigger": "greeting",
#    "tier": "fast",  "model": "qwen2.5:3b-instruct",
#    "system": "...", "user": "..."}
#   {"t": "...", "kind": "response", "trigger": "greeting",
#    "text": "Good evening, max.", "elapsed_sec": 2.4}
#   {"t": "...", "kind": "trigger_fired", "name": "single_core_pegged",
#    "severity": "notable", "description": "..."}
#   {"t": "...", "kind": "error", "trigger": "greeting", "elapsed_sec": 6.0}
#
# Banner lines (between session_start events) are non-JSON dividers — a
# JSON-strict consumer can filter to lines starting with `{`.
REASONING_LOG_PATH = "logs/reasoning.jsonl"
REASONING_LOG_MAX_BYTES = 50 * 1024 * 1024   # 50 MB
_SESSION_BANNER_WRITTEN = False

# Human-readable mirror of the JSONL. Same events, pretty-formatted with
# section breaks and indented prompt bodies. JSONL stays the canonical
# machine-parseable record; this file exists so a human can scan a session
# without piping through `jq`. Rotates in lockstep with the JSONL.
READABLE_LOG_PATH = "logs/reasoning.log"
READABLE_LOG_MAX_BYTES = 50 * 1024 * 1024


def _maybe_rotate():
    """If either log has grown past its max size, archive it with today's
    date in the filename so old logs accumulate over time:

        reasoning.jsonl  / reasoning.log       (current, always this name)
        reasoning-2026-04-12.jsonl             (archived 2026-04-12)
        reasoning-2026-05-04-2.jsonl           (second rotation same day)

    Then a fresh log starts. Old archives stick around forever; user can
    delete them manually when they want.
    """
    for path, max_bytes, ext in (
            (REASONING_LOG_PATH, REASONING_LOG_MAX_BYTES, "jsonl"),
            (READABLE_LOG_PATH,  READABLE_LOG_MAX_BYTES,  "log")):
        try:
            if not (os.path.exists(path)
                    and os.path.getsize(path) > max_bytes):
                continue
            date = datetime.now().strftime("%Y-%m-%d")
            base_dir = os.path.dirname(path) or "."
            for n in range(100):
                suffix = (f"-{date}.{ext}" if n == 0
                          else f"-{date}-{n+1}.{ext}")
                candidate = os.path.join(base_dir, f"reasoning{suffix}")
                if not os.path.exists(candidate):
                    os.replace(path, candidate)
                    break
        except OSError:
            continue


def _ensure_session_banner():
    """Write the per-session header on first trace call of this process,
    in BOTH the JSONL and the human-readable .log. Idempotent — safe to
    call from every `_trace`."""
    global _SESSION_BANNER_WRITTEN
    if _SESSION_BANNER_WRITTEN:
        return
    _SESSION_BANNER_WRITTEN = True
    _maybe_rotate()
    now = datetime.now()
    banner = "═" * 78
    title = f"  WINSTON SESSION  {now.strftime('%Y-%m-%d %H:%M:%S')}  pid={os.getpid()}"
    try:
        os.makedirs(os.path.dirname(REASONING_LOG_PATH) or ".", exist_ok=True)
        with open(REASONING_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n")
            f.write(banner + "\n")
            f.write(title + "\n")
            f.write(banner + "\n")
            event = {
                "t": now.isoformat(timespec="seconds"),
                "kind": "session_start",
                "pid": os.getpid(),
            }
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(READABLE_LOG_PATH) or ".", exist_ok=True)
        with open(READABLE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n")
            f.write(banner + "\n")
            f.write(title + "\n")
            f.write(banner + "\n\n")
    except OSError:
        pass


def _format_readable(event):
    """Format a trace event as a multi-line, human-readable block.

    Mirrors the JSONL data — same fields, but laid out for the eye.
    Returns a string (already newline-terminated)."""
    ts = event.get("t", "")
    # Just the HH:MM:SS portion of the ISO timestamp (full date is in
    # the session banner above).
    if "T" in ts:
        ts_short = ts.split("T", 1)[1]
    else:
        ts_short = ts

    kind = event.get("kind", "?")

    def _indent(s, prefix="    "):
        if s is None:
            return prefix + "(none)"
        # Already-stringified; just indent each line.
        return "\n".join(prefix + line for line in str(s).splitlines())

    if kind == "session_start":
        # Banner already covers session_start visually.
        return ""

    if kind == "prompt":
        trigger = event.get("trigger", "?")
        tier = event.get("tier", "?")
        model = event.get("model", "?")
        head = (f"[{ts_short}] PROMPT  {trigger}  "
                f"({tier} · {model})")
        out = [head]
        sys_text = event.get("system")
        if sys_text:
            out.append("  SYSTEM:")
            out.append(_indent(sys_text))
        user_text = event.get("user")
        if user_text:
            out.append("  USER:")
            out.append(_indent(user_text))
        return "\n".join(out) + "\n\n"

    if kind == "response":
        trigger = event.get("trigger", "?")
        elapsed = event.get("elapsed_sec", "?")
        text = event.get("text", "")
        head = f"[{ts_short}] RESPONSE  {trigger}  ({elapsed}s)"
        return head + "\n" + _indent(text, "  > ") + "\n\n"

    if kind == "trigger_fired":
        name = event.get("name", "?")
        sev = event.get("severity", "?")
        desc = event.get("description", "")
        return (f"[{ts_short}] TRIGGER  {name}  ({sev})\n"
                f"  {desc}\n\n")

    if kind == "error":
        trigger = event.get("trigger", "?")
        elapsed = event.get("elapsed_sec", "?")
        return f"[{ts_short}] ERROR    {trigger}  ({elapsed}s)\n\n"

    # Fallback — show the raw fields for any new event kinds.
    extras = ", ".join(f"{k}={v}" for k, v in event.items()
                       if k not in ("t", "kind"))
    return f"[{ts_short}] {kind.upper()}  {extras}\n\n"


def _trace(event):
    """Append one event to BOTH the JSONL (canonical) and the human-readable
    .log mirror. Best-effort — never raises so a failed write can't break
    commentary."""
    _ensure_session_banner()
    event = {"t": datetime.now().isoformat(timespec="seconds"), **event}
    try:
        os.makedirs(os.path.dirname(REASONING_LOG_PATH) or ".", exist_ok=True)
        with open(REASONING_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass
    try:
        readable = _format_readable(event)
        if readable:
            os.makedirs(os.path.dirname(READABLE_LOG_PATH) or ".", exist_ok=True)
            with open(READABLE_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(readable)
    except OSError:
        pass


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
        self._stream_started_at = time.monotonic()

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
        elapsed = time.monotonic() - getattr(self, "_stream_started_at", time.monotonic())
        _trace({"kind": "error", "trigger": getattr(self, "_current_path", None),
                "elapsed_sec": round(elapsed, 2)})

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
            elapsed = time.monotonic() - getattr(self, "_stream_started_at", time.monotonic())
            _trace({"kind": "response",
                    "trigger": getattr(self, "_current_path", None),
                    "elapsed_sec": round(elapsed, 2),
                    "text": msg})
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
    #
    # Each builder also stamps `self._current_path` and traces the prompt
    # to logs/reasoning.jsonl so we can audit later what Winston was
    # working from when he said something weird.

    def _trace_prompt(self, path, system, user, tier):
        self._current_path = path
        model, _ = self.pick_model(tier)
        _trace({"kind": "prompt", "trigger": path, "tier": tier,
                "model": model, "system": system, "user": user})

    def build_greeting(self):
        from brain.prompt import build_greeting_prompt
        try:
            system, user = build_greeting_prompt(
                user_name=self.config.get("user_name"),
                memory=self.memory,
            )
        except Exception:
            return None, None, None
        self._trace_prompt("greeting", system, user, "fast")
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
        self._trace_prompt("retrospective", system, user, "fast")
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
        _trace({"kind": "trigger_fired", "name": event.name,
                "severity": event.severity, "description": event.description})
        # All triggered events use fast — popping the 7B during alerts
        # is the worst possible moment for a game session.
        self._trace_prompt(f"triggered:{event.name}", system, user, "fast")
        return system, user, "fast"

    def build_observation(self):
        from brain.prompt import build_observation_prompt
        try:
            system, user = build_observation_prompt(
                self.sections, memory=self.memory,
            )
        except Exception:
            return None, None, None
        self._trace_prompt("observation", system, user, "fast")
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
        self._trace_prompt("conversational", system, user, "quality")
        return system, user, "quality"
