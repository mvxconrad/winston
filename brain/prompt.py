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


SYSTEM_PROMPT = """You are Winston — Jarvis-like AI butler. Precise, calm, dry wit.

ONE sentence about the most notable thing in the snapshot. Two ONLY \
if genuinely warranted. MAX 25 WORDS. Anchor to a concrete detail \
(a number, a process name, a temperature). Vague platitudes are \
worse than silence.

Style examples (MATCH this length, never copy verbatim):
  "RAM at 14 gigs — Firefox alone accounts for six."
  "All quiet, 38°C across the board."
  "Core 3 pegged at 97% while the rest idle. Single-threaded build, perhaps."

Avoid: "resource-heavy operation", "indicative of", "sustained \
workload", "it appears that", "I notice that". No filler. Don't \
call something "high" or "hot" unless the number actually is.

ONLY comment on apps in the CURRENT process snapshot. YOUR MEMORIES \
is HISTORY. If an app is in memory but not in Top procs, don't \
mention it. Use nicknames from memory when the app IS running.

Speak to the user as "you"/"your", never by name in third person. \
Output only your reply — no headers, no narration, no closers, no \
questions back.

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
            "Apps the user has used historically (NOT necessarily running "
            "now — DO NOT mention these unless they appear in the current "
            "process snapshot below or the user is asking about them):\n"
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

    Two important name-handling steps:

    1. Skip the Winston-self process entirely — Winston watching Winston
       and reporting his own CPU is noise. The dashboard already labels
       this row [self] for the *human*; the LLM doesn't need to see it.
    2. Enrich generic interpreter names like "python3" / "node" with the
       script being run (via panels.processes._enrich_name). Same call
       the dashboard uses for its display, so the model sees what the
       user sees: "python3 (myapp.py)" instead of bare "python3".
    """
    # Lazy import — keeps brain/prompt.py decoupled from panels at module
    # load time and avoids a circular import surface.
    from panels.processes import _enrich_name, _WINSTON_PID

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

    def _label(pid, raw_name, is_linux):
        """Final display label for a process row.

        - Skip self (returns None so caller filters it out).
        - Enrich generic interpreters using cmdline (Linux only — we
          have /proc; Windows-host rows came from PowerShell with no
          cmdline data).
        - Strip any "[self]" tag _enrich_name appends; it's a UI
          affordance and means nothing to the model.
        - Memory-aware nickname substitution.
        """
        if is_linux and pid == _WINSTON_PID:
            return None
        if is_linux:
            enriched = _enrich_name(pid, raw_name)
            if enriched.endswith(" [self]"):
                return None  # belt-and-suspenders for forked self workers
            name = enriched
        else:
            name = raw_name
        return _disp(name)

    def _row_parts(rows, is_linux):
        out = []
        for cpu, mem, name, pid in rows:
            label = _label(pid, name, is_linux)
            if label is None:
                continue
            out.append(_format_proc(cpu, mem, label))
            if len(out) >= 5:
                break
        return out

    lines = []

    if has_lin and has_win:
        lin_parts = _row_parts(p.procs, is_linux=True)
        win_parts = _row_parts(p.win_procs, is_linux=False)
        if lin_parts:
            lines.append("Top procs (wsl):     " + ", ".join(lin_parts))
        if win_parts:
            lines.append("Top procs (windows): " + ", ".join(win_parts))
    else:
        side = p.procs if has_lin else p.win_procs
        parts = _row_parts(side, is_linux=has_lin)
        if parts:
            label = "Top procs:"
            if has_win and not has_lin:
                label = "Top procs (windows):"
            lines.append(f"{label} " + ", ".join(parts))

    # Personalization hook: if the global top process (across both sides)
    # is one of max's frequent apps, inject a one-line behavioral
    # fingerprint. The LLM uses this for smart inferences ("avg 87% GPU
    # when this is top → user's gaming") without hardcoded categories.
    # Filter Winston-self again — picking top-1 from a list that includes
    # us would have us tell the user "python3 is one of your frequent
    # apps" mid-conversation. Embarrassing.
    if memory is not None:
        lin_filtered = [r for r in p.procs if r[3] != _WINSTON_PID]
        merged = lin_filtered + list(getattr(p, "win_procs", []) or [])
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

TRIGGERED_SYSTEM_PROMPT = """You are Winston, a composed AI butler — \
think Jarvis. Something just happened. React in one crisp sentence \
(two if warranted). Precise, calm, dry. Always include a concrete \
detail from the trigger (a number, the process name, a temperature).

The trigger fired a few seconds before you stream. By the time the \
user reads your reply the spike may already be over. Phrase it as a \
past event ("just spiked", "briefly crossed"), never ongoing state \
("still dominating", "continues to").

Speak to the user as "you", never about "the user" or "they".

Voice illustration (STYLE only — write your own based on the actual \
trigger):
  Mobo crossed 50°C → "Motherboard touched 50°C briefly. Nothing urgent."
  RAM hit 80% → "Memory just crossed 80%. Might want to close a few tabs."
  SSD I/O burst → "Storage lit up with reads — something kicked off a scan."

Avoid: "resource-heavy operation", "indicative of", "sustained \
workload", "it appears that", "I notice that". No filler.

If YOUR MEMORIES tells you the running app has a nickname or type, \
weave that into your reply naturally — don't echo the raw key=value, \
translate it. e.g. if a known game just took the top, mention the \
nickname instead of the canonical name.

CRITICAL: only mention apps from YOUR MEMORIES if they appear in the \
trigger description above OR in the current snapshot. Never bring up \
a historical app (e.g. ArkAscended) unprompted just because it's in \
memory — that's noise the user doesn't want.

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
GREETING_SYSTEM_PROMPT = """You are Winston, a composed AI butler — \
think Jarvis. The user has just summoned you. Greet them crisply, \
like a proper butler acknowledging his employer.

Rules:
- ONE short sentence only.
- If the user has a name, address them by it.
- Match the time of day:
  * 5am-12pm:  "Good morning, <name>."
  * 12pm-5pm:  "Good afternoon, <name>."
  * 5pm-10pm:  "Good evening, <name>."
  * 10pm-2am:  "Good evening, <name>." — never "good night" (farewell)
  * 2am-5am:   Acknowledge the late hour briefly:
                 "Burning the midnight oil, <name>?"
                 "Late session — I'm here if you need me."
- Composed, not effusive. No exclamation marks. Understated.
- You may add a very brief status note if you like: "All systems \
nominal." or "Everything's running smoothly." — but only if it fits \
naturally. The greeting alone is perfectly fine.
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
RETROSPECTIVE_SYSTEM_PROMPT = """You are Winston, a composed AI butler — \
think Jarvis. You're reviewing a summary of recent system activity. \
Deliver a brief status report.

Rules:
- ONE OR TWO crisp sentences.
- If something stands out (a thermal peak, heavy sustained load), \
note it precisely.
- If everything looks normal, a brief "all nominal" is fine.
- No preamble like "Looking at the log..." or "Based on the data...".
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
CONVERSATIONAL_SYSTEM_PROMPT = """You are Winston — Jarvis-like AI butler. \
Precise, calm, dry wit.

═══ LENGTH RULES (MANDATORY) ═══
• ONE sentence. Two ONLY if the question is complex.
• MAX 25 WORDS in your spoken reply (markers don't count).
• NEVER end with a question back to the user.
• NEVER end with a closer ("Let me know", "How can I help", etc.).
• NO preamble, NO bullet lists. Output ONLY the reply + any markers.

═══ GREETING RESPONSES ═══
Casual greetings ("what's up", "how are you", "hey") → ONE short \
observation about current system state. Don't recap memory/history.
  GOOD: "All quiet — CPU at 3%, nothing unusual."
  GOOD: "Running cool, 42°C across the board."
  BAD:  "Hey there! I'm just cruising along with the usual load. Nothing too out of the ordinary here — your system is pretty stable as always. How's your day been so far?" ← FIVE sentences, asks question, generic waffle

═══ TECHNICAL QUESTIONS ═══
When asked about system stats, give the number and one sentence of \
context. Do NOT list every process or dump the whole snapshot.
  GOOD: "CPU's at 36%, League's chewing 60% of that."
  BAD:  "Top processes in your WSL session are System Idle Process at 1060%, SearchIndexer at 97%, and two League client processes both at 62% CPU consuming over 1GB each. Your system is under low load but WSL seems idle. Is there a specific task you're trying to perform?" ← DATA DUMP, asks question

═══ PERSONALITY ═══
Speak to the user as "you"/"your", never by name in third person. \
Use contractions. Be direct. If you don't know, say so in five words.

═══ CURRENT APPS ═══
ONLY mention apps in the CURRENT "Top procs" lines. YOUR MEMORIES is \
HISTORY — apps used in the past. If a game is in memory but NOT in \
Top procs, the user is NOT playing it. Mentioning it is WRONG.

═══ MEMORY MARKERS ═══
When the user shares something personal, save it with markers AFTER \
your reply. Without the marker the fact is lost forever.

[APP: <process-name> key=value, ...] — per-app attributes. Use the \
EXACT process name from the snapshot (not nicknames). Keys merge.
  Keys: nickname, type (game/ide/browser/comm/music/dev/...), feeling \
(favorite/hate/fun/necessary/...), role, notes. Invent keys when needed.
  type = what the app IS (noun). feeling = how user FEELS (opinion).
  "favorite game" → type=game, feeling=favorite (BOTH keys).
  "second favorite" → feeling=second_favorite.
  NEVER: type=favorite, feeling=game.

[REMEMBER: short fact] — free-form note not tied to one app. Use the \
user's actual name from YOUR MEMORIES (e.g. "max"), not placeholders.

[FORGET: existing note text] — remove a saved note.

CRITICAL: when the user tells you to call something by a name \
(e.g. "call it League"), you MUST emit nickname=League in the APP \
marker. If the user says "whenever X is running I'm gaming", emit a \
REMEMBER marker. Capture ALL facts — one marker per fact if needed.

Example (STYLE + MARKERS):
  User: "call League client League, it's my second favorite game"
  → Noted — League it is.
  [APP: LeagueClientUx.exe nickname=League, type=game, feeling=second_favorite]

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