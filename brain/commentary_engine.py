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
import re
import time
from datetime import datetime


# ──────────────── Memory-marker parsing ────────────────
# Winston writes markers at the end of his responses to update his own
# memory. They look like:
#
#   [REMEMBER: max usually codes at night]
#       Free-form fact about the user. Stored in memory.notes.
#
#   [APP: <name> key=val, key=val, -dropkey]
#       Structured attrs on an app. Multi-key, MERGES with existing
#       attrs (so setting `nickname=ark` doesn't blow away `feeling=favorite`).
#       Prefix a key with `-` to delete it.
#
#   [FORGET: <text of an existing note>]
#       Removes a previously-saved note (text matched case-insensitively).
#
# We strip every marker from the displayed text and route the payload
# to the right Memory.* method.
_MARKER_RE = re.compile(
    r"\[(REMEMBER|APP|FORGET)\s*:\s*(.+?)\]",
    re.IGNORECASE | re.DOTALL,
)


def _parse_app_marker(payload):
    """Parse the body of an [APP: ...] marker into (name, attrs_dict).

    Format: `<name> key1=val1, key2=val2, -dropkey`
      - Name is the first whitespace-separated token. Used to look up
        the app entry; the canonical `name` field is NEVER renamed by
        markers (we keep CSV-derived process names stable so they keep
        matching incoming process events).
      - Rest is comma-separated. Each item is either:
          key=value   → set attrs[key] = value
          -key        → delete attrs[key] (encoded as None for set_app_attrs)
      - Special: if the model passes `name=X`, we silently redirect it
        to `nickname=X` — Winston's intent is "call it X going forward",
        which is what nickname is for. The canonical `name` stays put.
      - Values can contain spaces but not commas.
      - Keys are lowercased; values keep their case.

    Returns (None, None) if the payload can't be parsed.
    """
    payload = payload.strip()
    if not payload:
        return None, None
    # Split off the app name (first word).
    parts = payload.split(None, 1)
    name = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    attrs = {}
    if rest:
        for chunk in rest.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if chunk.startswith("-"):
                key = chunk[1:].strip().lower()
                # Prevent deletion of the canonical name too.
                if key and key != "name":
                    attrs[key] = None  # delete sentinel
                continue
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                k = k.strip().lower()
                v = v.strip()
                if not k:
                    continue
                # Lock canonical name. The model wanting to rename → goes
                # to nickname instead. If a nickname is also being set in
                # the same marker, the explicit nickname wins.
                if k == "name":
                    if "nickname" not in attrs:
                        attrs["nickname"] = v
                    continue
                attrs[k] = v
    return name, attrs


def _extract_markers(text):
    """Pull every [REMEMBER:]/[APP:]/[FORGET:] marker out of `text`.

    Returns (cleaned_text, markers) where markers is a list of
    (kind, payload) tuples. cleaned_text has markers removed plus any
    trailing whitespace/blank lines they left behind tidied up.
    """
    markers = []
    for m in _MARKER_RE.finditer(text):
        markers.append((m.group(1).upper(), m.group(2).strip()))
    cleaned = _MARKER_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, markers


# ──────────────── Preamble / wrapper detection ────────────────
# Models occasionally leak internal narration into the response:
#   "Got it! Let's update the references. Now, let's respond to the
#    user: \"Sure, from now on..."
# That whole prefix up to the inner-quoted reply is garbage from the
# user's POV. We detect it during streaming so the typewriter can skip
# past it instead of typing it out then deleting on finalize.

# Patterns we know mean "what follows is the real response":
_RESPOND_NOW_RE = re.compile(
    r"(?:Now,?\s*)?(?:Let'?s|I'?ll|I\s+will)\s+(?:respond\s+to\s+(?:the\s+)?user|reply|answer)\s*[:\-]\s*",
    re.IGNORECASE,
)
# A whole-message wrapping quote: starts with " and ends with ", with
# nothing more outside. We strip both quotes after marker-extraction.
_FULL_WRAP_QUOTES_RE = re.compile(r'^[\s\n]*"\s*(.+?)\s*"[\s\n]*$', re.DOTALL)
# Planning preambles to drop when they sit at the start of the buffer.
# Each match consumes everything up through the ending punctuation of
# the planning sentence.
_PLANNING_PREFIX_RE = re.compile(
    r"^\s*(?:Got\s+it[!.]?\s+)?"
    r"(?:Let'?s|Let\s+me|Now,?\s*let'?s|And\s+(?:then\s+)?let'?s|Now\s+I'?ll)\s+"
    r"(?:think|update|consider|infer|note|address|process|reflect|review|look|see|do|move\s+on|respond)\b"
    r"[^.\n]*[.\n:]",
    re.IGNORECASE,
)


def _find_skip_target(buffer, current_typed):
    """If the buffer at `current_typed` starts with a recognized planning/
    wrapper preamble, return the byte index AFTER it so the typewriter
    can jump past. Returns None when nothing skippable is detected.

    Called every typewriter tick. Cheap — short regex matches anchored
    to current position.
    """
    if current_typed >= len(buffer):
        return None

    # Pattern 1: explicit "Now let's respond to the user:" — skip past it
    # AND past any opening " that follows, so the visible content is the
    # actual reply rather than `"Sure, from now on..."`.
    sub = buffer[current_typed:]
    m = _RESPOND_NOW_RE.search(sub)
    if m and m.start() < 200:  # don't skip over half a paragraph
        end = current_typed + m.end()
        # Skip a single opening quote if present.
        if end < len(buffer) and buffer[end] in '"“\'':
            end += 1
        return end

    # Pattern 2: planning preamble at the start ("Got it! Let me update...")
    # Only fires when current_typed is still close to 0 — a one-liner
    # leak at the top of the response.
    if current_typed < 80:
        m2 = _PLANNING_PREFIX_RE.match(sub)
        if m2:
            end = current_typed + m2.end()
            # Eat trailing whitespace/newlines.
            while end < len(buffer) and buffer[end] in " \t\r\n":
                end += 1
            return end

    return None


def _strip_wrappers(text):
    """Final cleanup at message-finalize. Removes a wrapping pair of quotes
    when the entire reply is one quoted block, plus any trailing planning
    preambles that the streaming filter didn't catch."""
    if not text:
        return text
    m = _FULL_WRAP_QUOTES_RE.match(text)
    if m:
        text = m.group(1).strip()
    else:
        # Streaming-time preamble skip can eat the leading " of a wrapped
        # reply, leaving a dangling trailing ". Strip it so the message
        # doesn't render with a stray quote at the end. Same for stray
        # leading " when only one side survived.
        text = re.sub(r'^[\s]*["“]\s*(?=\S)', "", text)
        text = re.sub(r'\s*["”]\s*$', "", text)
    # Strip trailing closer phrases that leaked despite the prompt rules.
    text = re.sub(
        r"(?:\s*(?:How\s+can\s+I\s+assist[^.?!]*[.?!]"
        r"|Let\s+me\s+know\s+if[^.?!]*[.?!]"
        r"|Hope\s+this\s+helps[^.?!]*[.?!]?))$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    return text


def _apply_markers(memory, markers):
    """Route extracted markers to the right Memory.* method.

    No-op when memory is None. Returns a summary dict so we can log
    what Winston decided to learn into the reasoning trace.
    """
    summary = {"added": 0, "attrs_changed": 0, "forgotten": 0}
    if memory is None or not markers:
        return summary
    for kind, payload in markers:
        try:
            if kind == "REMEMBER":
                if memory.add_note(payload, source="user"):
                    summary["added"] += 1
            elif kind == "APP":
                name, attrs = _parse_app_marker(payload)
                if name and attrs:
                    if memory.set_app_attrs(name, attrs):
                        summary["attrs_changed"] += 1
            elif kind == "FORGET":
                summary["forgotten"] += memory.forget_note(payload)
        except Exception:
            # Never let a malformed marker break the stream finalization.
            continue
    if any(summary.values()):
        try:
            memory.save()
        except Exception:
            pass
    return summary


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
        # Markers found mid-stream are physically REMOVED from the
        # buffer (so the user never sees them flicker), but we still
        # need their payloads at finalize-time to apply to memory.
        # Stash them here as (kind, payload) tuples.
        self._stream_markers = []
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

        Two skip paths run BEFORE we advance one char:
          1. Memory markers ([REMEMBER:], [APP:], [FORGET:]) — jump past
             the entire bracket block so the user never sees it.
          2. Internal-narration preambles ("Got it! Let's update memory.
             Now let's respond to the user: \"...\"") — jump past the
             preamble so the visible stream starts at the actual reply.
             Without this the user has to watch the model type its
             planning thoughts before the real answer arrives.
        """
        if self.typed_chars < len(self.streaming_buffer):
            buf = self.streaming_buffer
            i = self.typed_chars

            # ── Path 1: marker skip ──
            # The display layer renders `buffer[:typed_chars]`, so
            # advancing typed_chars past a marker is NOT enough — the
            # marker text would still be in the rendered slice. We
            # physically CUT the marker out of streaming_buffer when
            # we recognize it, then stash the payload in
            # `self._stream_markers` for memory-application at finalize.
            #
            # When we see `[`, hold the typewriter until we have enough
            # chars to either confirm it's a marker (and then cut it)
            # or rule one out (and then advance past `[`).
            if buf[i] == "[":
                MARKER_PREFIXES = ("[REMEMBER:", "[APP:", "[FORGET:")
                MAX_PFX_LEN = max(len(p) for p in MARKER_PREFIXES)
                have = buf[i:i + MAX_PFX_LEN].upper()
                could_be = any(p.startswith(have) for p in MARKER_PREFIXES)
                is_marker = any(have.startswith(p) for p in MARKER_PREFIXES)

                if is_marker:
                    end = buf.find("]", i)
                    if end != -1:
                        # Capture the marker payload before we cut.
                        m = _MARKER_RE.match(buf, i)
                        if m:
                            self._stream_markers.append(
                                (m.group(1).upper(), m.group(2).strip())
                            )
                        # Cut from buffer, including any trailing
                        # whitespace/newlines so we don't leave a blank line.
                        cut_to = end + 1
                        while cut_to < len(buf) and buf[cut_to] in " \t\r\n":
                            cut_to += 1
                        self.streaming_buffer = buf[:i] + buf[cut_to:]
                        # typed_chars stays at i — buffer[:i] was already
                        # visible; what's at position i now is what came
                        # after the marker.
                        return "advanced"
                    # Marker open but not yet closed — wait.
                    return None

                if could_be:
                    # Not enough chars yet — wait rather than reveal `[`.
                    return None

                # Not a marker (e.g. literal `[note]` in prose). Advance.

            # ── Path 2: internal-narration / wrapper preamble skip ──
            # Same approach as markers: physically cut the preamble out
            # of the buffer so it doesn't sit in `buffer[:typed_chars]`.
            target = _find_skip_target(buf, i)
            if target is not None and target > i:
                self.streaming_buffer = buf[:i] + buf[target:]
                return "advanced"

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
        # Markers found mid-stream were already cut from streaming_buffer
        # and stashed in `self._stream_markers`. Run _extract_markers one
        # more time in case any straggler arrived after the last typewriter
        # tick (or the model broke its own marker syntax such that we
        # didn't recognize it during streaming).
        raw = self.streaming_buffer.strip()
        msg, late_markers = _extract_markers(raw)
        # Final cleanup: planning preamble the streaming filter didn't
        # catch + strip wrapping quotes if the whole reply was quoted.
        msg = _strip_wrappers(msg)
        all_markers = list(self._stream_markers) + list(late_markers)
        self._stream_markers = []
        learn_summary = _apply_markers(self.memory, all_markers)
        # Keep the variable name the rest of the function uses.
        markers = all_markers

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
            trace_event = {
                "kind": "response",
                "trigger": getattr(self, "_current_path", None),
                "elapsed_sec": round(elapsed, 2),
                "text": msg,
            }
            # Only attach the learning audit when something was actually
            # learned — keeps the reasoning log noise-free for routine
            # commentary that didn't hit any markers.
            if any(learn_summary.values()):
                trace_event["learned"] = learn_summary
                trace_event["markers"] = [
                    {"kind": k, "payload": p} for k, p in markers
                ]
            _trace(trace_event)
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
