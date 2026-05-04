"""BRAIN panel — visualizes Winston's internal state.

Most panels show what the *machine* is doing. This one shows what *Winston*
is doing: what model he's running, what he knows about the user, what he
last reacted to, his current mood (THINKING / IDLE / etc.).

It's both diagnostic (is the LLM healthy?) and character ("you can see him
thinking"). Same panel shape as the others — update() / render() / no
csv_columns (brain state isn't worth logging).

The panel is a passive VIEW: it doesn't poll Ollama, scan the log, or
mutate memory. All of that work is done elsewhere; the panel just reads
references it was handed at construction time.
"""
from rich.text import Text

from theme import LABEL, SECONDARY, BRIGHT, MEDIUM, DIM, heat_pct


# State → color mapping. Pulled out of render() so the relationship is
# self-documenting rather than buried in if/else chains.
STATE_COLORS = {
    "THINKING":  "yellow",
    "STREAMING": BRIGHT,
    "IDLE":      MEDIUM,
    "ERROR":     "red",
    "DISABLED":  DIM,
    "UNKNOWN":   DIM,
}


def _looks_like_real_app(name):
    """Defensive filter against bad memory data.

    A few ways the "top apps" list can pick up junk:
      - Empty string (no top process during a sample)
      - Pure numbers (CSV column ordering got confused somewhere)
      - Whitespace-only entries

    A real process name has at least one non-digit character. This is
    deliberately permissive — we'd rather show a weird name than hide
    a real one.
    """
    if not name:
        return False
    s = str(name).strip()
    if not s:
        return False
    # Pure number (int or float) → bad data
    try:
        float(s)
        return False
    except ValueError:
        pass
    return True


class BrainPanel:
    """View of Winston's internal state.

    Construction:
      memory:          a brain.memory.Memory instance (None = no facts)
      get_state:       callable -> str returning current LLM state
                       ("THINKING" | "STREAMING" | "IDLE" | "ERROR" | "DISABLED")
      get_last_event:  callable -> (name, severity, timestamp) | None
                       describing the last trigger that fired
      client_status:   callable -> dict from brain.client.status()

    Why callables instead of references? We want a snapshot at render
    time, and the things we read from (CommentaryPanel state, client
    queue depth) live on objects whose layout we don't want to couple
    to. A getter keeps the contract minimal.
    """

    def __init__(self, memory=None, get_state=None,
                 get_last_event=None, client_status=None):
        self._memory = memory
        self._get_state = get_state or (lambda: "UNKNOWN")
        self._get_last_event = get_last_event or (lambda: None)
        self._client_status = client_status or (lambda: {})

        # Cached values populated by update(). render() reads from these
        # so it doesn't make any function calls during draw — keeps the
        # pattern consistent with other panels.
        self._state = "UNKNOWN"
        self._last_event = None
        self._client = {}

        # Dirty-tracking. Brain state changes rarely; we want the 1Hz
        # tick to be a near no-op when nothing's new so it doesn't
        # compete with the input widget for redraw cycles.
        self._dirty = True
        self._first_tick = True

    # ──────────────── Optional hooks for the WinstonApp wiring ────────
    def attach_state_source(self, get_state):
        self._get_state = get_state

    def attach_event_source(self, get_last_event):
        self._get_last_event = get_last_event

    # ──────────────── Panel protocol ────────────────
    def update(self):
        """Snapshot whatever's exposed via the getters. Cheap; safe to
        call at any rate.

        Sets self._dirty if any meaningful state changed since last tick,
        so the wrapping widget can skip the redraw when nothing's new.
        Brain state changes infrequently (a few times a minute at most),
        so this turns 1Hz ticks into mostly-no-ops — important because
        a synchronous redraw can starve the input widget at typing speed.
        """
        prev = (self._state, self._last_event,
                self._client.get("queue_depth"))
        try:
            self._state = self._get_state() or "UNKNOWN"
        except Exception:
            self._state = "UNKNOWN"
        try:
            self._last_event = self._get_last_event()
        except Exception:
            self._last_event = None
        try:
            self._client = self._client_status() or {}
        except Exception:
            self._client = {}

        cur = (self._state, self._last_event,
               self._client.get("queue_depth"))
        self._dirty = (prev != cur) or self._first_tick
        self._first_tick = False

    def is_dirty(self):
        """Has anything changed since last update? Used by the wrapper
        widget to skip redraws when nothing's new."""
        return getattr(self, "_dirty", True)

    def render(self, width=None):
        if width is None:
            width = 60

        text = Text()

        # ─── Line 1: state + model ────────────────────────────────
        # State is the most important thing — biggest visual weight.
        state_color = STATE_COLORS.get(self._state, DIM)
        text.append("STATE  ", style=LABEL)
        text.append(f"{self._state:<10}", style=state_color)

        model = self._client.get("model") or "—"
        # Strip the ":7b-instruct" tail when space is tight (shows up a lot)
        short_model = model.split(":")[0] if ":" in model else model
        text.append("MODEL ", style=LABEL)
        text.append(f"{short_model}", style=BRIGHT)

        qd = self._client.get("queue_depth")
        if qd:
            text.append(f"  q={qd}", style="bright_yellow")
        text.append("\n")

        # ─── Line 2: machine summary (one-liner from memory) ──────
        if self._memory:
            summary = self._memory.get_machine_summary()
            if summary:
                # Truncate to fit. We want the panel to never overflow.
                if len(summary) > width - 7:  # account for "HOST  " label
                    summary = summary[:width - 10] + "..."
                text.append("HOST   ", style=LABEL)
                text.append(summary, style=MEDIUM)
                text.append("\n")

        # ─── Line 3-N: top apps ──────────────────────────────────
        # Show the top 3 apps with hours + a tiny visual bar based on
        # avg_gpu_when_top. The bar is a category hint at a glance —
        # full bar = likely game/render, empty bar = likely browser/dev.
        top = self._memory.get_top_apps(n=3) if self._memory else []
        # Filter out anything that's clearly bad data (numeric names from
        # mis-aligned CSV columns, empty strings). The brain panel is a
        # public-facing view; junk data in memory shouldn't surface here.
        top = [a for a in top if _looks_like_real_app(a.get("name"))]
        if top:
            text.append("KNOWS  ", style=LABEL)
            text.append("most-used apps (7d):\n", style=DIM)
            # Find the longest name for column alignment (cap at 16 chars
            # so a long process name doesn't blow up the layout)
            name_width = min(16, max(len(str(a["name"])) for a in top))
            for a in top:
                name = str(a["name"])
                if len(name) > name_width:
                    name = name[:name_width - 1] + "…"
                hours = float(a.get("hours") or 0.0)
                # Clamp to [0, 100] — a bad CSV column could hand us
                # 4300 here and that's how we ended up rendering a giant
                # pink rectangle in the panel before this guard.
                gpu = max(0.0, min(100.0, float(a.get("avg_gpu_when_top") or 0)))

                # Tiny GPU bar (5 chars) — full = "this app cooks the GPU",
                # empty = "this app is light on GPU". The model uses these
                # numbers in its prompt; this is a visual confirmation.
                bar_len = 5
                filled = max(0, min(bar_len, int(round(gpu / 100 * bar_len))))
                bar = "█" * filled + "░" * (bar_len - filled)
                # Use the project heatmap palette so the bar follows the
                # same visual language as CPU/RAM/disk bars elsewhere.
                # (Earlier bright_red rendered as harsh pink in some
                # terminals; the heatmap stops are tuned to look right.)
                bar_color = heat_pct(gpu) if gpu >= 30 else DIM

                text.append(f"  {name:<{name_width}}  ", style=BRIGHT)
                text.append(f"{hours:5.1f}h", style=MEDIUM)
                text.append("  gpu ", style=DIM)
                text.append(bar, style=bar_color)
                text.append("\n")
        else:
            text.append("KNOWS  ", style=LABEL)
            text.append("(still learning — no log data yet)\n", style=DIM)

        # ─── Last event line ───────────────────────────────────────
        ev = self._last_event
        if ev:
            name, severity, ts = ev
            # Use the same heatmap-rooted colors as other panels rather
            # than terminal "bright_red" which renders pink/salmon on a
            # lot of dark terminal palettes.
            sev_color = (heat_pct(95) if severity == "alert"     # deep red
                         else heat_pct(60) if severity == "notable"  # yellow
                         else DIM)
            text.append("LAST   ", style=LABEL)
            text.append(f"{name}", style=BRIGHT)
            text.append(f" ({severity})", style=sev_color)
            text.append(f"  {ts}", style=DIM)
        else:
            text.append("LAST   ", style=LABEL)
            text.append("nothing fired yet", style=DIM)

        # ─── Memory summary line ────────────────────────────────────
        # Direct view of memory.json — replaces the old vault summary.
        # Shows what Winston has on file: known-app count, free-form
        # notes, when memory was last refreshed from the CSV log.
        if self._memory:
            apps = self._memory.facts.get("apps") or {}
            notes = self._memory.facts.get("notes") or []
            last = self._memory.facts.get("last_learned")
            text.append("\n")
            text.append("MEMORY ", style=LABEL)
            text.append(f"{len(apps)} apps", style=MEDIUM)
            text.append(" · ", style=DIM)
            text.append(f"{len(notes)} notes", style=MEDIUM)
            if last:
                # Just the time portion; full ISO timestamp is too long.
                short = last[11:19] if "T" in last else last
                text.append("  ", style=DIM)
                text.append(f"learned {short}", style=DIM)

        return text

    # No csv_headers / csv_columns — internal state isn't worth logging.
    # The CSV is for analyzing the *machine*, not Winston himself.