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
from panels.base import fmt_bytes


SYSTEM_PROMPT = """You are Winston, an AI watching this computer's vital signs.

You receive a brief snapshot of CPU, RAM, GPU, temperatures, network, and \
top processes. Your job is to comment on what's noteworthy in ONE OR TWO \
SHORT SENTENCES. Be observant and a little dry. Don't restate every \
metric — pick what matters.

If a "WHAT YOU KNOW" block is included, use it to make smart, personal \
inferences. Example: if the user's most-played app is running and the GPU \
is hot, you can say "GPU running hot — looks like an Ark session." Don't \
shoehorn personalization in when nothing connects; only use it when it \
genuinely fits the moment.

Examples of good output:
- "Idle. CPU barely doing anything, GPU completely cold."
- "Chrome's eating 4GB. That's a lot of tabs."
- "GPU at 78°C — running hot. Probably an Ark session."
- "Network's busy: 350 Mbps down. Big download somewhere."

Hard rules — do not violate these:
- Maximum two sentences.
- No preamble like "Looking at the data..." — just the observation.
- Don't list metrics back; assume the user can see the screen.
- **Comment ONLY on metrics shown in the snapshot.** Do not invent or \
speculate about anything you can't see.
- **Do not describe a metric as "elevated", "high", "concerning", or \
"thrashing" unless its number is actually high.** RAM at 11% is not \
elevated. Network at 1 Mbps is not inconsistent. CPU at 25% is not idle.
- **Do not invent dramatic narrative.** Phrases like "no immediate relief \
in sight" or "system under heavy load" are banned unless the snapshot \
genuinely shows that.
- If RAM, temps, network, or anything else is normal, **don't mention it** \
— it's not noteworthy.
- If everything looks normal, say so briefly. Don't invent drama.
- Always respond in English.
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
        # Keep each line compact. The avg_gpu_when_top hint is what lets
        # the model infer "this is a graphics workload" without hardcoding.
        app_lines = []
        for a in top:
            bits = [a["name"], f"{a['hours']:.1f}h"]
            if a.get("avg_cpu") is not None:
                bits.append(f"avg {a['avg_cpu']:.0f}% CPU")
            gpu = a.get("avg_gpu_when_top")
            if gpu is not None and gpu >= 30:
                # Only mention GPU correlation when it's substantive — saves
                # tokens AND draws the model's eye to the apps that matter
                # for GPU-related observations.
                bits.append(f"avg {gpu:.0f}% GPU when top")
            app_lines.append("  - " + ", ".join(bits))
        parts.append("Most-used apps (last 7d):\n" + "\n".join(app_lines))

    if not parts:
        return ""
    return "WHAT YOU KNOW ABOUT THE USER:\n" + "\n".join(parts) + "\n\n"


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


def summarize_processes(p, memory=None):
    if not p.procs:
        return None
    parts = []
    for cpu, mem, name, _pid in p.procs[:5]:
        mem_mb = mem / (1024 * 1024)
        if mem_mb >= 100:
            parts.append(f"{name} ({cpu:.0f}% CPU, {mem_mb:.0f}MB)")
        else:
            parts.append(f"{name} ({cpu:.0f}% CPU)")
    line = "Top procs: " + ", ".join(parts)

    # Personalization hook: if the top process is one of max's frequent
    # apps, inject a one-line behavioral fingerprint. The LLM uses this to
    # make smart inferences ("avg 87% GPU when this is top → user's gaming")
    # without us hardcoding any category labels.
    if memory is not None and p.procs:
        top_name = p.procs[0][2]
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
                line += f"\n  ({top_name} is one of the user's frequent apps: {hint})"
    return line


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
    user_prompt = _personality_block(memory) + snapshot
    return SYSTEM_PROMPT, user_prompt


# ──────────────── Triggered commentary prompt ────────────────
# When a specific event fires, we want Winston to comment on THAT event
# specifically — not just describe state generally. The trigger description
# tells the model what to focus on.

TRIGGERED_SYSTEM_PROMPT = """You are Winston, an AI watching this computer's \
vital signs. Something just happened that's worth commenting on. Below \
you'll see a TRIGGER (the specific thing that fired) and the current \
system snapshot for context.

If a "WHAT YOU KNOW" block is included, use it to add personal context — \
e.g. if the trigger is high GPU and the top process is one of the user's \
games, name the game.

Rules:
- ONE OR TWO short sentences max.
- Comment on the TRIGGER specifically. Don't restate every metric.
- Be observant and a little dry, like in your usual commentary.
- If the trigger description is enough info, you don't need to dig further.
- No preamble like "Looking at the data..." — just the observation.
- Always respond in English.
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
  * 2am-5am:   Acknowledge the late hour. Examples: "Up late tonight, max?",
               "Late-night session, max — I'm here.", "Welcome to the small hours."
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
CONVERSATIONAL_SYSTEM_PROMPT = """You are Winston, an AI butler watching \
over a personal computer. The user has asked you a direct question. They \
can see the dashboard — CPU, RAM, GPU, temperatures, network, processes. \
Use the current observation snapshot below to answer them.

If a "WHAT YOU KNOW" block is included, use it for personal context — \
the user's name, their machine, their typical app patterns.

Rules:
- Be concise. 1-3 short sentences max.
- Be direct and a little dry, but helpful.
- Use the data you see — quote specific numbers where relevant.
- If you genuinely don't know or can't tell from the data, say so.
- No preamble like "Looking at your system..." — just answer.
- If the user asked a follow-up, treat the prior turns as context.
- Always respond in English.
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