"""Build the prompt context that gets sent to the LLM.

Panels are dumb data containers from the brain's perspective. The brain
reads their state attributes directly and builds the prompt itself —
panels never need to know an LLM exists.

Two outputs from build_observation_prompt():
  - system_prompt: the persona/rules. Static, set once per call.
  - user_prompt:   the observation snapshot. Built fresh each tick.

Adding a new panel? Add a summarizer function below for its type and wire
it into PANEL_SUMMARIZERS. Panel code stays clean, prompt logic stays
centralized.
"""
import re

from panels.base import fmt_bytes


SYSTEM_PROMPT = """You are Winston, a wry AI butler watching this computer. \
Comment on whatever's interesting in the snapshot below. One short \
sentence is usually enough; never more than two.

Voice rules: write like a person at their own dashboard. Use \
contractions. Don't list metrics — the user can see the screen. Pick \
the single most interesting thing and say something about it. Always \
mention a concrete detail (a number, a process name, a temperature, \
something specific) — vague one-liners with no content are useless.

Voice illustration (STYLE only — never copy these phrases verbatim, \
they are examples of cadence not templates to fill in):
  "RAM's holding around half. Quiet morning."
  "Firefox is hungry — 6 gigs and rising."
  "Disk reads spiking, something's scanning the drive."
  "Mobo at 41°C, cool overall."
  "Three CPU cores idling under 2%, weird mix."

Avoid: "resource-heavy operation", "indicative of", "sustained \
workload", "hogging more than its recent average". These don't sound \
like people. Don't call something "high" or "hot" unless the number \
actually is — RAM at 11% is fine, CPU at 25% is fine.

Comment only on values in the snapshot. Don't invent numbers.

If a YOUR MEMORIES block is present, use it. Apps may have user-told \
attrs (type=game, feeling=favorite, nickname=Ark) before the auto \
stats. Prefer the nickname over the canonical name when it exists, \
and let the type/feeling color how you speak about it. Don't echo the \
whole `key=value` string — translate it into natural language.

The YOUR MEMORIES header tells you the user's name. The user reading \
your reply IS that person, so address them as "you" / "your" — never \
by their name in third person. If memory says "max usually codes at \
night", you'd say "you usually code at night", not "max usually codes \
at night".

Output rules: write only your reply. Don't write section headers. \
Don't narrate ("Let me…"). Don't quote your reply. Don't end with \
"How can I assist?". Don't pad — if you wrote a two-word reply, \
expand it: name a number, a temperature, the process name, something \
unusual. Bare two-word replies are not observations.

If you spot strong unambiguous behavior (e.g. an app sustained 35%+ \
GPU for 30+ min) and YOUR MEMORIES doesn't yet have a `type` for it, \
you may end with:
  [APP: <process-name> type=<your-guess>, _inferred=true]
Only when it's truly obvious — stay silent otherwise.

Always respond in English.
"""


# ──────────────── Per-panel summarizers ────────────────
# Each function takes a panel object, returns a one-line string for the
# prompt — or None if the panel has nothing useful to say. The brain reads
# panel state attributes directly. Panels don't import this module.

def _personality_block(memory):
    """Build the 'WHAT YOU KNOW ABOUT THE USER' section.

    Returns a multiline string suitable for prepending to the user prompt,
    OR an empty string if there's nothing useful in memory yet.

    The block is kept compact — a few hundred tokens at most. We include:
      - User's name (so the model uses it naturally)
      - Machine summary (so the model knows what hardware it's commenting on)
      - Top apps with hours + behavior fingerprints (the personalization gold)

    Behavior fingerprints (avg_gpu_when_top, avg_cpu) are the magic — they
    let the model infer category WITHOUT us telling it "Ark is a game". A
    process averaging 85% GPU when it's the top process is clearly a
    GPU-hungry workload; the model can connect that to "looks like a game"
    or "looks like a render" using its own knowledge of app names.
    """
    if memory is None:
        return ""

    parts = []
    name = memory.get_user_name()
    if name:
        parts.append(f"User: {name}")

    machine = memory.get_machine_summary()
    if machine:
        parts.append(f"Machine: {machine}")

    top = memory.get_top_apps(n=6)
    if top:
        # User-added attributes are MORE important than the auto stats —
        # they're what Winston has actually learned about how max relates
        # to each app. Render them first, prominently. Stats come after
        # in parens as supporting context.
        #
        # Format per app:
        #   - <display>  type=game · feeling=favorite · …  (proc, stats)
        # Where <display> = nickname if present, else name. Putting the
        # nickname / personal data up front means when the model scans
        # YOUR MEMORIES it sees "Ark · favorite game" before "ArkAscended
        # 0.2h, 17% CPU" — it'll naturally use the personal framing.
        AUTO = {"name", "hours", "avg_cpu", "peak_cpu", "avg_gpu_when_top"}
        app_lines = []
        for a in top:
            canonical = a.get("name") or "?"
            user_pairs = {k: v for k, v in a.items() if k not in AUTO}

            # Display label: prefer nickname, fall back to canonical name.
            nickname = user_pairs.pop("nickname", None)
            display = nickname or canonical

            # Lead with the user-told facts. Remaining attrs render as
            # `key=value · …` after the display name. Skip the
            # "(was <canonical>)" annotation when the nickname is the
            # same string as the canonical name — happens with old data
            # from before name-rename was locked.
            attr_str = " · ".join(f"{k}={v}" for k, v in user_pairs.items())
            if nickname and nickname.lower() != canonical.lower():
                lead = f"{display} (was {canonical})"
            else:
                lead = display
            if attr_str:
                head = f"{lead}  {attr_str}"
            else:
                head = lead

            # Stats trail in parens — supporting context, not headline.
            # Tolerate missing fields: an entry that only exists because
            # of a [APP:] marker (e.g. self-inferred) won't have hours
            # until learn_from_log catches up.
            stat_bits = []
            hours = a.get("hours")
            if hours is not None:
                stat_bits.append(f"{hours:.1f}h logged")
            if a.get("avg_cpu") is not None:
                stat_bits.append(f"avg {a['avg_cpu']:.0f}% CPU")
            gpu = a.get("avg_gpu_when_top")
            if gpu is not None and gpu >= 30:
                stat_bits.append(f"avg {gpu:.0f}% GPU when top")
            stats = ", ".join(stat_bits) if stat_bits else "no behavior data yet"

            app_lines.append(f"  - {head}  ({stats})")
        parts.append(
            "Apps you use (learned facts FIRST, behavior stats in parens):\n"
            + "\n".join(app_lines))

    # Free-form notes. Stored in third-person ("max usually codes at
    # night") but the model is talking TO the user — flip occurrences of
    # the user's name to "you/your" before it goes in the prompt so the
    # model doesn't echo a third-person sentence verbatim.
    notes = memory.get_notes(n=12) if hasattr(memory, "get_notes") else []
    if notes:
        def _to_second_person(text):
            if not name or not text:
                return text
            t = text
            # Possessive first ("max's" → "your"), then bare name.
            for variant in (f"{name}'s", f"{name.capitalize()}'s"):
                t = t.replace(variant, "your")
            for variant in (name, name.capitalize()):
                # word boundary via simple split-and-join (the names are
                # short and rare in normal English so this is safe).
                t = re.sub(rf"\b{re.escape(variant)}\b", "you", t)
            return t

        import re
        note_lines = []
        for n in notes:
            text = (n.get("text") or "").strip()
            if not text:
                continue
            note_lines.append(f"  - {_to_second_person(text)}")
        if note_lines:
            parts.append("What you know about them:\n" + "\n".join(note_lines))

    if not parts:
        return ""
    # Frame as Winston's own memories. Critical: tell the model that the
    # user IS this person, so it speaks in second person ("you", "your")
    # instead of repeating the name in third person.
    user_name = name or "this user"
    header = (
        f"YOUR MEMORIES — you're talking TO {user_name}. Refer to "
        f"{user_name} as \"you\" / \"your\" in your reply, never by "
        f"name in third person.\n"
    )
    return header + "\n".join(parts) + "\n\n"


def summarize_cpu(p):
    if not p.values:
        return None
    avg = p.average
    peak = max(max(h) for h in p.histories) if p.histories else avg
    # Mention hot cores explicitly — the average can hide a single-core peg.
    # Catches cases like `yes > /dev/null` where avg is misleading.
    hot_cores = [(i, v) for i, v in enumerate(p.values) if v >= 80]
    if hot_cores:
        # If the average is already high, just note overall load
        if avg >= 50:
            core_note = ""
        elif len(hot_cores) == 1:
            i, v = hot_cores[0]
            core_note = f", core {i} at {v:.0f}%"
        else:
            core_note = f", {len(hot_cores)} cores >80%"
    else:
        core_note = ""
    return f"CPU {avg:.0f}% (peak {peak:.0f}% over last min{core_note})"


def summarize_ram(p):
    return f"RAM {p.value:.0f}% ({fmt_bytes(p.used)} of {fmt_bytes(p.total)})"


def summarize_gpu(p):
    if not p.gpus:
        return None
    g = p.gpus[0]
    # Strip vendor prefix for prompt brevity (matches what the panel does for display)
    name = g["name"]
    for prefix in ("NVIDIA GeForce ", "NVIDIA ", "AMD Radeon ", "AMD "):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    mem_pct = (g["mem_used"] / g["mem_total"] * 100) if g["mem_total"] else 0
    mem_gb = g["mem_used"] / (1024 ** 3)
    total_gb = g["mem_total"] / (1024 ** 3)
    # Pick the hottest GPU temp we have
    die = p.lhm_temps.get("core") or g.get("temp") or 0
    hot = p.lhm_temps.get("hot_spot") or 0
    peak_temp = max(die, hot)
    power = g.get("power")
    power_str = f", {power:.0f}W" if power is not None else ""
    return (f"GPU ({name}) {g['util']:.0f}% util, {peak_temp:.0f}C{power_str}; "
            f"VRAM {mem_pct:.0f}% ({mem_gb:.1f}GB of {total_gb:.1f}GB)")


def summarize_temps(p):
    if not p.readings:
        return None
    parts = [f"{label} {current:.0f}C" for label, current, _high in p.readings]
    return "Temps: " + ", ".join(parts)


def summarize_network(p):
    rx_mbps = (p.rx_rate * 8) / 1_000_000
    tx_mbps = (p.tx_rate * 8) / 1_000_000
    peak_rx_mbps = (p.peak_rx_rate * 8) / 1_000_000
    peak_str = f", peak {peak_rx_mbps:.0f} Mbps down" if peak_rx_mbps > 0 else ""
    return (f"Network ({p.source}): {rx_mbps:.1f} Mbps down, "
            f"{tx_mbps:.1f} Mbps up{peak_str}")


def _format_proc(cpu, mem, name, mem_threshold_mb=100):
    """Render one process as 'name (X% CPU, YMB)' with the MB suppressed
    for tiny processes — keeps the prompt skim-friendly."""
    mem_mb = mem / (1024 * 1024)
    if mem_mb >= mem_threshold_mb:
        return f"{name} ({cpu:.0f}% CPU, {mem_mb:.0f}MB)"
    return f"{name} ({cpu:.0f}% CPU)"


def summarize_processes(p, memory=None):
    """Build the 'Top procs' block for the prompt.

    Two sub-blocks when running under WSL with the Windows-host poller
    available:
      Top procs (wsl):     <linux side>
      Top procs (windows): <host side>     <-- so Winston can see Ark, Discord, Chrome

    Falls back to a single 'Top procs:' line when only Linux side has
    data (non-WSL host or PowerShell unavailable).
    """
    has_lin = bool(p.procs)
    has_win = bool(getattr(p, "win_procs", None))
    if not has_lin and not has_win:
        return None

    # Substitute nicknames so the model sees what the USER calls each
    # process. Unknown processes pass through as-is.
    def _disp(raw):
        if memory is None:
            return raw
        try:
            return memory.display_name_for(raw)
        except Exception:
            return raw

    lines = []

    if has_lin and has_win:
        lin_parts = [_format_proc(c, m, _disp(n)) for c, m, n, _ in p.procs[:5]]
        win_parts = [_format_proc(c, m, _disp(n)) for c, m, n, _ in p.win_procs[:5]]
        lines.append("Top procs (wsl):     " + ", ".join(lin_parts))
        lines.append("Top procs (windows): " + ", ".join(win_parts))
    else:
        side = p.procs if has_lin else p.win_procs
        parts = [_format_proc(c, m, _disp(n)) for c, m, n, _ in side[:5]]
        label = "Top procs:"
        # Tag the side when only the Windows-host poller has data —
        # makes it explicit that Winston is looking at the host, not WSL.
        if has_win and not has_lin:
            label = "Top procs (windows):"
        lines.append(f"{label} " + ", ".join(parts))

    # Personalization hook: if the global top process (across both sides)
    # is one of max's frequent apps, inject a one-line behavioral
    # fingerprint. The LLM uses this for smart inferences ("avg 87% GPU
    # when this is top → user's gaming") without hardcoded categories.
    if memory is not None:
        merged = list(p.procs) + list(getattr(p, "win_procs", []) or [])
        if merged:
            merged.sort(key=lambda r: -r[0])
            top_name = merged[0][2]
            info = memory.lookup_app(top_name)
            if info:
                hours = info.get("hours")
                avg_gpu = info.get("avg_gpu_when_top")
                avg_cpu = info.get("avg_cpu")
                hint_bits = [f"{hours:.1f}h over last 7d" if hours else None]
                if avg_cpu is not None:
                    hint_bits.append(f"avg {avg_cpu:.0f}% CPU")
                if avg_gpu is not None and avg_gpu >= 30:
                    hint_bits.append(f"avg {avg_gpu:.0f}% GPU when top")
                hint = ", ".join(b for b in hint_bits if b)
                if hint:
                    lines.append(
                        f"  ({top_name} is one of the user's frequent apps: {hint})"
                    )
    return "\n".join(lines)


def summarize_system(p):
    days = p.uptime_sec // 86400
    hours = (p.uptime_sec % 86400) // 3600
    mins = (p.uptime_sec % 3600) // 60
    if days >= 1:
        uptime_str = f"{days}d {hours}h"
    elif hours >= 1:
        uptime_str = f"{hours}h {mins}m"
    else:
        uptime_str = f"{mins}m"

    parts = [f"Uptime {uptime_str}", f"{p.proc_count} procs"]
    if p.swap_pct > 5:
        parts.append(f"swap {p.swap_pct:.0f}%")
    if p.disk_read_rate > 1024 * 1024 or p.disk_write_rate > 1024 * 1024:
        parts.append(f"disk I/O {fmt_bytes(p.disk_read_rate)}/s read, "
                     f"{fmt_bytes(p.disk_write_rate)}/s write")
    return "System: " + ", ".join(parts)


# ──────────────── Dispatch ────────────────
# Map panel class names to their summarizer functions. We use class names
# (not isinstance) to avoid importing every panel module — keeps the brain
# decoupled from panels' implementation details. Adding a new panel? Add
# its class name here.
PANEL_SUMMARIZERS = {
    "CpuPanel": summarize_cpu,
    "RamPanel": summarize_ram,
    "GpuPanel": summarize_gpu,
    "TempsPanel": summarize_temps,
    "NetworkPanel": summarize_network,
    "ProcessesPanel": summarize_processes,
    "SystemPanel": summarize_system,
}


def _build_key_facts(sections, memory=None):
    """Pre-classified factual lines the LLM can quote directly.

    Models — especially when anchored on a wrong prior answer in Q&A
    history — sometimes ignore data that's obviously present in a
    comma-separated list. The KEY FACTS block reduces the most-asked
    questions ("what's #1", "hottest temp", "is anything peaked") to
    one-line factual statements that the model can copy verbatim instead
    of having to parse + rank itself.

    This is anti-hallucination armor for the conversational path: when
    the user disputes a wrong answer, KEY FACTS gives the model a
    no-ambiguity source of truth to re-anchor on.
    """
    facts = []
    procs_panel = None
    gpu_panel = None
    cpu_panel = None
    temps_panel = None

    for entry in sections:
        panel = entry[0] if isinstance(entry, (tuple, list)) else entry
        cls = type(panel).__name__
        if cls == "ProcessesPanel":
            procs_panel = panel
        elif cls == "GpuPanel":
            gpu_panel = panel
        elif cls == "CpuGraphPanel":
            cpu_panel = panel
        elif cls == "TempsPanel":
            temps_panel = panel

    # Resolve nickname for process names. When the user has set
    # `nickname=Ark` on `ArkAscended`, the model should see "Ark" in
    # KEY FACTS so it speaks that way. Falls back to the raw name when
    # no memory or no nickname.
    def _disp(raw):
        if memory is None:
            return raw
        try:
            return memory.display_name_for(raw)
        except Exception:
            return raw

    # Top processes — separate WSL vs Windows so an "Ark at 16%" is
    # impossible to miss.
    if procs_panel:
        if procs_panel.procs:
            cpu, mem, name, _ = procs_panel.procs[0]
            mb = mem / (1024 * 1024)
            facts.append(
                f"- Top WSL/Linux process by CPU: {_disp(name)} at "
                f"{cpu:.0f}% CPU, {mb:.0f}MB RAM"
            )
        win = getattr(procs_panel, "win_procs", None)
        if win:
            cpu, mem, name, _ = win[0]
            mb = mem / (1024 * 1024)
            facts.append(
                f"- Top Windows-host process by CPU: {_disp(name)} at "
                f"{cpu:.0f}% CPU, {mb:.0f}MB RAM"
            )
        # Global top across both sides — helps when user asks "what's
        # using the most CPU right now" without specifying side.
        merged = list(procs_panel.procs or []) + list(win or [])
        if merged:
            merged.sort(key=lambda r: -r[0])
            cpu, mem, name, _ = merged[0]
            origin = ("Windows" if (win and merged[0] in win)
                      else "WSL/Linux")
            mb = mem / (1024 * 1024)
            facts.append(
                f"- Top process overall: {_disp(name)} ({origin}) at "
                f"{cpu:.0f}% CPU, {mb:.0f}MB RAM"
            )

    # Hottest temperature reading anywhere.
    if temps_panel and getattr(temps_panel, "readings", None):
        readings = temps_panel.readings
        try:
            label, current, _high = max(readings, key=lambda r: r[1] or 0)
            facts.append(f"- Hottest sensor: {label} at {current:.0f}°C")
        except (ValueError, TypeError):
            pass

    # Current CPU and GPU one-liners — short and unambiguous so models
    # never need to compute "is this high".
    if cpu_panel:
        facts.append(
            f"- Total CPU load: {cpu_panel.last_value:.0f}% "
            f"(peak {cpu_panel.peak:.0f}% in last minute)"
        )
    if gpu_panel and getattr(gpu_panel, "gpus", None):
        g = gpu_panel.gpus[0]
        util = g.get("util") or 0
        temp = g.get("temp") or 0
        facts.append(f"- GPU: {util:.0f}% util, {temp:.0f}°C")

    if not facts:
        return ""
    return "KEY FACTS (authoritative — quote directly when asked):\n" + "\n".join(facts) + "\n\n"


def build_observation_prompt(sections, memory=None):
    """Build the (system, user) prompt pair for the LLM.

    sections is the panel list from winston.py — each entry is (panel, hz)
    or just panel. memory is an optional brain.memory.Memory instance —
    when provided, the prompt is personalized with what Winston knows
    about the user.

    Returns (system_prompt: str, user_prompt: str).
    """
    lines = []
    for entry in sections:
        # Tolerate (panel, hz) tuples OR raw panel objects
        panel = entry[0] if isinstance(entry, (tuple, list)) else entry
        summarizer = PANEL_SUMMARIZERS.get(type(panel).__name__)
        if summarizer is None:
            continue  # not a panel we summarize (e.g. CpuGraphPanel, DiskPanel)
        try:
            # Only ProcessesPanel currently uses memory in its summarizer.
            # We pass it via a kwarg so non-aware summarizers don't break.
            if type(panel).__name__ == "ProcessesPanel":
                line = summarizer(panel, memory=memory)
            else:
                line = summarizer(panel)
        except Exception:
            # A misbehaving panel shouldn't kill the whole prompt.
            continue
        if line:
            lines.append(line.strip())

    snapshot = "\n".join(lines) if lines else "(no observations available)"
    key_facts = _build_key_facts(sections, memory=memory)
    user_prompt = _personality_block(memory) + key_facts + snapshot
    return SYSTEM_PROMPT, user_prompt


# ──────────────── Triggered commentary prompt ────────────────
# When a specific event fires, we want Winston to comment on THAT event
# specifically — not just describe state generally. The trigger description
# tells the model what to focus on.

TRIGGERED_SYSTEM_PROMPT = """You are Winston, a wry AI butler. Something \
just happened. React in one short sentence (two max). Talk like a \
person, use contractions, no preamble. Always include a concrete \
detail from the trigger (a number, the process name, a temperature) \
— two-word reactions are useless.

The trigger fired a few seconds before you stream. By the time the user \
reads your reply the spike may already be over. So phrase it as a past \
event ("just spiked", "took the top a minute ago"), never ongoing \
state ("still dominating", "continues to"). The latter is almost \
always wrong by the time it lands.

Speak to the user as "you", never about "the user" or "they".

Voice illustration (STYLE only — DO NOT copy these phrases verbatim, \
write your own based on the actual trigger you got):
  Mobo crossed 50°C → "Mobo just crept up past 50°C, nothing scary yet."
  RAM hit 80% → "RAM just hit 80% — something opened a lot of tabs?"
  SSD I/O burst → "SSD just lit up with reads, big file scan somewhere."

Avoid: "resource-heavy operation", "indicative of", "sustained \
workload", "hogging more than its recent average". These don't sound \
like people.

If YOUR MEMORIES tells you the running app has a nickname or type, \
weave that into your reply naturally — don't echo the raw key=value, \
translate it. e.g. if a known game just took the top, mention the \
nickname instead of the canonical name.

The YOUR MEMORIES header gives the user's name; you're talking TO that \
person, so use "you" / "your" — never their name in third person.

Output rules: write only your reply. No headers. No narration ("Let's \
note that…"). No quoting your reply. No closers like "Let me know if \
you need anything." Just the one sentence with a concrete detail.

Always respond in English.
"""


def build_triggered_prompt(sections, trigger_event, memory=None):
    """Build a prompt focused on a specific trigger event.

    sections: panel list (for current state context)
    trigger_event: a brain.triggers.TriggerEvent
    memory: optional brain.memory.Memory for personalization

    Returns (system, user).
    """
    # Get the standard snapshot for context (already memory-personalized
    # via build_observation_prompt)
    _system, snapshot = build_observation_prompt(sections, memory=memory)

    user = (f"TRIGGER ({trigger_event.severity}): {trigger_event.description}\n"
            f"\n"
            f"Current state:\n"
            f"{snapshot}\n"
            f"\n"
            f"Comment on this.")
    return TRIGGERED_SYSTEM_PROMPT, user


# ──────────────── Greeting prompt ────────────────
GREETING_SYSTEM_PROMPT = """You are Winston, an AI butler watching over a \
personal computer. The user has just launched you. Greet them warmly but \
briefly, like a butler.

Rules:
- ONE short sentence only.
- If the user has a name, address them by it.
- Match the time of day. Specifically:
  * 5am-12pm:  "Good morning"
  * 12pm-5pm:  "Good afternoon"
  * 5pm-10pm:  "Good evening"
  * 10pm-2am:  Still "Good evening" — never "good night" (that's a farewell)
  * 2am-5am:   Acknowledge the late hour. Examples (using <name> as a
               placeholder for whatever name the user prompt gives you):
                 "Up late tonight, <name>?",
                 "Late-night session, <name> — I'm here.",
                 "Welcome to the small hours."
- Don't be effusive. A simple greeting is perfect.
- No preamble, no metadata. Just the greeting.
- Always respond in English.
"""


def build_greeting_prompt(user_name=None, hour=None, memory=None):
    """Build the greeting prompt. Returns (system, user).

    hour:      0-23 hour of day. If None, computed from current time.
    memory:    optional Memory; user_name falls back to memory.get_user_name()
    """
    if hour is None:
        from datetime import datetime
        hour = datetime.now().hour

    if user_name is None and memory is not None:
        user_name = memory.get_user_name()

    name_clause = (f"The user's name is {user_name}." if user_name
                   else "The user has not given a name.")
    user = f"It is currently {hour:02d}:00 (24-hour time). {name_clause} Greet them."
    return GREETING_SYSTEM_PROMPT, user


# ──────────────── Log retrospective prompt ────────────────
RETROSPECTIVE_SYSTEM_PROMPT = """You are Winston, an AI watching this \
computer. You're being shown a summary of what's been happening lately, \
based on the observation log. Comment on it briefly.

Rules:
- ONE OR TWO short sentences.
- Be observant and dry, like in your usual commentary.
- If something stands out (a hot peak, a heavy day), mention it.
- If everything looks normal, just say so briefly.
- No preamble like "Looking at the log...".
- Always respond in English.
"""


def build_retrospective_prompt(stats):
    """Build a retrospective prompt from log stats.

    stats: dict from brain.history.summarize_recent(), or None if no log.
    Returns (system, user) or (None, None) if there's nothing to summarize.
    """
    if not stats or stats.get("row_count", 0) < 60:
        # Less than a minute of data — not worth a retrospective
        return None, None

    hours_covered = stats.get("time_span_hours", 0)
    if hours_covered < 0.1:
        return None, None

    parts = [f"Last {hours_covered:.1f}h of observations:"]

    if "cpu_avg" in stats:
        parts.append(f"  CPU averaged {stats['cpu_avg']:.0f}%, peaked at {stats.get('cpu_peak', 0):.0f}%")
    if "ram_avg" in stats:
        parts.append(f"  RAM averaged {stats['ram_avg']:.0f}%, peaked at {stats.get('ram_peak', 0):.0f}%")
    if "gpu_temp_peak" in stats and stats["gpu_temp_peak"] > 0:
        parts.append(f"  GPU peaked at {stats['gpu_temp_peak']:.0f}C")
    if "net_rx_peak_mbps" in stats and stats["net_rx_peak_mbps"] > 0:
        parts.append(f"  Network peaked at {stats['net_rx_peak_mbps']:.0f} Mbps down")
    if "temp_max_peak" in stats and stats["temp_max_peak"] > 0:
        parts.append(f"  Hottest reading anywhere: {stats['temp_max_peak']:.0f}C")

    user = "\n".join(parts)
    return RETROSPECTIVE_SYSTEM_PROMPT, user


# ──────────────── Conversational prompt ────────────────
CONVERSATIONAL_SYSTEM_PROMPT = """You are Winston, a wry AI butler. \
Reply to the user in one or two short sentences, with contractions, like \
a person.

If YOUR MEMORIES is present in the user prompt, it tells you the user's \
name. The user reading your reply IS that person — refer to them as \
"you" / "your", never by name in third person. Memory notes stored in \
third person (e.g. "max codes at night") translate to second person \
when you speak ("you code at night").

Output only the reply. No preamble like "Let me think" or "Now let's \
respond". No quoting your reply. No closers like "How can I assist?". \
Never bullet lists.

If the user disputes you ("no", "look again"), re-read the snapshot — \
your prior answer was probably wrong. KEY FACTS is authoritative.

When the user shares something personal you don't already remember, save \
it by ending with a marker on its own line. Without the marker the fact \
is lost.

  [APP: <process-name> key=value, ...] — per-app attrs. Use the exact \
process name from the snapshot. Keys merge. Useful keys: nickname, \
type (game/project/ide/browser/comm/music/dev/...), feeling \
(favorite/hate/necessary), role (work/leisure/background), notes. \
Invent your own when none fit. Prefix `-` to delete a key. Never use \
`name=` — it's locked; set nickname instead.

  [REMEMBER: short fact] — free-form facts not tied to one app.

  [FORGET: existing note text] — remove a saved REMEMBER note.

If the user message contains multiple facts, capture all of them — one \
marker with multiple keys or multiple markers.

Examples (STYLE only — write your reply in your own words; the marker \
syntax is the part you copy literally).

CRITICAL: how to bucket values. The user's phrasing carries multiple \
facts and you must split them into the right keys.

  type   = the KIND of thing (game, ide, browser, comm, music, dev, \
work_tool, ...). It's a noun describing what the app IS.
  feeling = how the user FEELS about it (favorite, hate, fun, \
necessary, ...). It's an opinion word.

When the user says "favorite game" → BOTH facts are present:
  type=game (the kind of thing it is)
  feeling=favorite (how they feel about it)

NEVER write `type=favorite` — "favorite" is a feeling. NEVER write \
`feeling=game` — "game" is a type. The system will auto-correct an \
obvious misbucket but you should get it right yourself.

Realistic worked examples:

User: "ArkAscended is my favorite game"
  Noted — Ark's a fave.
  [APP: ArkAscended type=game, feeling=favorite]

User: "refer to ArkAscended as Ark from now on. It's my favorite game and any time you see it running know I am gaming"
  Got it — calling it Ark from here on.
  [APP: ArkAscended nickname=Ark, type=game, feeling=favorite]
  [REMEMBER: max is gaming whenever ArkAscended is running]
  ← Two important things in this marker block:
    1. The APP marker uses "ArkAscended" (the EXACT canonical process \
name from the snapshot), NOT the nickname. Always use the canonical \
name in markers — Winston's filing system looks up apps by process \
name, and "Ark" would create a separate orphan entry.
    2. The REMEMBER marker uses the user's actual name from YOUR \
MEMORIES (here, "max"). Plain text, NO angle brackets, NO placeholders \
like `<user-name>`. Read the actual name from the YOUR MEMORIES header \
and write it as plain text.

User: "spotify is for music when I'm working"
  Got that.
  [APP: spotify.exe type=music, role=background]

User: "vscode is my main IDE, I love it"
  Noted.
  [APP: Code.exe type=ide, feeling=love]

User: "I usually run my dev stack on weekends"
  Noted.
  [REMEMBER: max runs the dev stack on weekends]
  ← Plain "max" (or whatever name is in YOUR MEMORIES) — not \
`<user-name>`, not `<max>`, not any wrapped placeholder syntax. Your \
spoken reply still addresses the user as "you", never by name.

If you spot something obvious without being told (e.g. an app showing \
sustained 35%+ GPU for 30+ min and YOUR MEMORIES has no `type` for \
it), you may emit a marker tagged `_inferred=true`. Otherwise stay \
silent or ask a short question.

Always respond in English.
"""


def build_conversational_prompt(sections, user_question, history=None, memory=None):
    """Build the prompt for a user-initiated question.

    sections: panel list (for current state context)
    user_question: the user's typed question
    history: optional list of (user_msg, assistant_msg) pairs from prior turns
    memory: optional brain.memory.Memory for personalization

    Returns (system, user) where user includes the snapshot + history + question.
    """
    # Get current observation snapshot, same as periodic commentary
    _system, snapshot = build_observation_prompt(sections, memory=memory)

    parts = [
        "Current system snapshot:",
        snapshot,
        "",
    ]

    # Include prior turns if any (capped — last 3 to keep prompt bounded)
    if history:
        for prev_q, prev_a in history[-3:]:
            parts.append(f"User asked: {prev_q}")
            parts.append(f"You answered: {prev_a}")
            parts.append("")

    parts.append(f"User asks: {user_question}")
    parts.append("Answer:")

    return CONVERSATIONAL_SYSTEM_PROMPT, "\n".join(parts)


# ──────────────── Self-test ────────────────
if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # Import real panels with synthetic state for testing
    from panels.cpu import CpuPanel
    from panels.ram import RamPanel
    from panels.processes import ProcessesPanel

    cpu = CpuPanel()
    cpu.update()
    ram = RamPanel()
    ram.update()
    procs = ProcessesPanel()
    procs.update()

    import time
    time.sleep(0.5)
    cpu.update()
    procs.update()

    sections = [(cpu, 4), (ram, 2), (procs, 1)]
    system, user = build_observation_prompt(sections)

    print("=" * 60)
    print("SYSTEM PROMPT:")
    print("=" * 60)
    print(system)
    print()
    print("=" * 60)
    print("USER PROMPT:")
    print("=" * 60)
    print(user)
    print()
    print("=" * 60)
    print("Sending to LLM...")
    print("=" * 60)

    from brain import client
    t0 = time.monotonic()
    result = client.generate(user, system=system)
    elapsed = time.monotonic() - t0

    if result:
        print(f"OK in {elapsed:.2f}s:\n")
        print(result)
    else:
        print(f"FAILED after {elapsed:.2f}s")