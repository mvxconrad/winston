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

Examples of good output:
- "Idle. CPU barely doing anything, GPU completely cold."
- "Chrome's eating 4GB. That's a lot of tabs."
- "GPU at 78°C — running hot. Probably a game."
- "Network's busy: 350 Mbps down. Big download somewhere."

Rules:
- Maximum two sentences.
- No preamble like "Looking at the data..." — just the observation.
- Don't list metrics back; assume the user can see the screen.
- If everything looks normal, say so briefly. Don't invent drama.
"""


# ──────────────── Per-panel summarizers ────────────────
# Each function takes a panel object, returns a one-line string for the
# prompt — or None if the panel has nothing useful to say. The brain reads
# panel state attributes directly. Panels don't import this module.

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


def summarize_processes(p):
    if not p.procs:
        return None
    parts = []
    for cpu, mem, name, _pid in p.procs[:5]:
        mem_mb = mem / (1024 * 1024)
        if mem_mb >= 100:
            parts.append(f"{name} ({cpu:.0f}% CPU, {mem_mb:.0f}MB)")
        else:
            parts.append(f"{name} ({cpu:.0f}% CPU)")
    return "Top procs: " + ", ".join(parts)


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


def build_observation_prompt(sections):
    """Build the (system, user) prompt pair for the LLM.

    sections is the panel list from winston.py — each entry is (panel, hz)
    or just panel. Returns (system_prompt: str, user_prompt: str).
    """
    lines = []
    for entry in sections:
        # Tolerate (panel, hz) tuples OR raw panel objects
        panel = entry[0] if isinstance(entry, (tuple, list)) else entry
        summarizer = PANEL_SUMMARIZERS.get(type(panel).__name__)
        if summarizer is None:
            continue  # not a panel we summarize (e.g. CpuGraphPanel, DiskPanel)
        try:
            line = summarizer(panel)
        except Exception:
            # A misbehaving panel shouldn't kill the whole prompt.
            continue
        if line:
            lines.append(line.strip())

    user_prompt = "\n".join(lines) if lines else "(no observations available)"
    return SYSTEM_PROMPT, user_prompt


# ──────────────── Triggered commentary prompt ────────────────
# When a specific event fires, we want Winston to comment on THAT event
# specifically — not just describe state generally. The trigger description
# tells the model what to focus on.

TRIGGERED_SYSTEM_PROMPT = """You are Winston, an AI watching this computer's \
vital signs. Something just happened that's worth commenting on. Below \
you'll see a TRIGGER (the specific thing that fired) and the current \
system snapshot for context.

Rules:
- ONE OR TWO short sentences max.
- Comment on the TRIGGER specifically. Don't restate every metric.
- Be observant and a little dry, like in your usual commentary.
- If the trigger description is enough info, you don't need to dig further.
- No preamble like "Looking at the data..." — just the observation.
"""


def build_triggered_prompt(sections, trigger_event):
    """Build a prompt focused on a specific trigger event.

    sections: panel list (for current state context)
    trigger_event: a brain.triggers.TriggerEvent

    Returns (system, user).
    """
    # Get the standard snapshot for context
    _system, snapshot = build_observation_prompt(sections)

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
"""


def build_greeting_prompt(user_name=None, hour=None):
    """Build the greeting prompt. Returns (system, user).

    hour: 0-23 hour of day. If None, computed from current time.
    """
    if hour is None:
        from datetime import datetime
        hour = datetime.now().hour

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

Rules:
- Be concise. 1-3 short sentences max.
- Be direct and a little dry, but helpful.
- Use the data you see — quote specific numbers where relevant.
- If you genuinely don't know or can't tell from the data, say so.
- No preamble like "Looking at your system..." — just answer.
- If the user asked a follow-up, treat the prior turns as context.
"""


def build_conversational_prompt(sections, user_question, history=None):
    """Build the prompt for a user-initiated question.

    sections: panel list (for current state context)
    user_question: the user's typed question
    history: optional list of (user_msg, assistant_msg) pairs from prior turns

    Returns (system, user) where user includes the snapshot + history + question.
    """
    # Get current observation snapshot, same as periodic commentary
    _system, snapshot = build_observation_prompt(sections)

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