"""Event-driven trigger system for Winston commentary.

Replaces dumb 30-second polling with: "comment when something interesting
happens, otherwise stay quiet." Each trigger inspects current panel state
+ rolling baselines and decides if the current moment is noteworthy.

Architecture:
  TriggerEvent     — a thing worth commenting on. Has severity + description.
  TriggerRunner    — ticks at 1Hz. Pushes baselines. Runs all triggers.
                     Enforces per-trigger cooldowns. Returns highest-
                     priority event ready to fire (or None).
  trigger functions — pure functions: (sections, baselines, config) → event

Severity tiers (drives preemption rules in CommentaryPanel):
  routine   — every-N-min ambient updates. Slow typewriter, can be preempted.
  notable   — something changed. Medium typewriter. Preempts routine.
  alert     — concerning. Instant display. Preempts anything.

All thresholds and cooldowns come from config.py — see TRIGGERS dict.
"""
import time
from dataclasses import dataclass

from brain.baselines import BaselineRegistry


# ──────────────── Event type ────────────────
@dataclass
class TriggerEvent:
    name: str          # stable id matching the config key, e.g. "cpu_thermal"
    severity: str      # "routine" | "notable" | "alert"
    description: str   # one-line context fed to the LLM


SEVERITY_RANK = {"routine": 0, "notable": 1, "alert": 2}


# ──────────────── Helpers ────────────────
def _find_panel(sections, type_name):
    """Look up a panel by class name. None if not present.
    Tolerates (panel, hz) tuples or raw panel objects."""
    for entry in sections:
        panel = entry[0] if isinstance(entry, (tuple, list)) else entry
        if type(panel).__name__ == type_name:
            return panel
    return None


# ──────────────── Trigger functions ────────────────
# Each is a pure function: (sections, baselines, cfg) → TriggerEvent | None
# `cfg` is the per-trigger config dict from config.TRIGGERS[name].

def trigger_single_core_pegged(sections, baselines, cfg):
    """One core at 100% while average looks fine. Catches `yes > /dev/null`
    and runaway threads — the case where average CPU is misleading."""
    cpu = _find_panel(sections, "CpuPanel")
    if cpu is None or not cpu.values:
        return None

    avg = cpu.average
    if avg >= cfg["avg_threshold_pct"]:
        # If the average is already high, this isn't the "single core"
        # case — let cpu_sustained_high handle it.
        return None

    pegged = [(i, v) for i, v in enumerate(cpu.values) if v >= cfg["core_threshold_pct"]]
    if not pegged:
        return None

    # Build a description listing pegged cores
    if len(pegged) == 1:
        i, v = pegged[0]
        desc = f"Core {i} pegged at {v:.0f}% while overall CPU average is only {avg:.0f}%."
    else:
        cores_str = ", ".join(f"core {i} ({v:.0f}%)" for i, v in pegged[:3])
        desc = f"{len(pegged)} cores pegged ({cores_str}) while overall CPU average is {avg:.0f}%."

    return TriggerEvent(
        name="single_core_pegged",
        severity=cfg.get("severity", "notable"),
        description=desc,
    )


# State for cpu_sustained_high — needs to track "high for how long"
_cpu_high_since = [None]  # list-as-cell so we can mutate from inside the fn

def trigger_cpu_sustained_high(sections, baselines, cfg):
    """CPU average above threshold for sustained duration."""
    cpu = _find_panel(sections, "CpuPanel")
    if cpu is None or not cpu.values:
        _cpu_high_since[0] = None
        return None

    avg = cpu.average
    threshold = cfg["avg_threshold_pct"]
    duration = cfg["duration_sec"]
    now = time.monotonic()

    if avg < threshold:
        _cpu_high_since[0] = None
        return None

    # CPU is currently high. Has it been high long enough?
    if _cpu_high_since[0] is None:
        _cpu_high_since[0] = now
        return None  # just started — wait for duration to elapse

    elapsed = now - _cpu_high_since[0]
    if elapsed < duration:
        return None  # not sustained long enough yet

    # Sustained. Reset the timer so we don't re-fire continuously (cooldown
    # also helps but resetting the start makes the next firing require a
    # fresh sustained period after cpu drops + comes back up).
    _cpu_high_since[0] = None

    return TriggerEvent(
        name="cpu_sustained_high",
        severity=cfg.get("severity", "notable"),
        description=(f"CPU average has been at {avg:.0f}% for {elapsed:.0f}+ "
                     f"seconds. Something's working hard."),
    )


def trigger_cpu_thermal(sections, baselines, cfg):
    """CPU temperature crossed warning thresholds."""
    temps = _find_panel(sections, "TempsPanel")
    if temps is None or not temps.readings:
        return None

    # Find CPU row
    cpu_temp = None
    for label, current, _high in temps.readings:
        if label == "CPU":
            cpu_temp = current
            break
    if cpu_temp is None:
        return None

    if cpu_temp >= cfg["alert_temp_c"]:
        return TriggerEvent(
            name="cpu_thermal",
            severity="alert",
            description=(f"CPU at {cpu_temp:.0f}°C — concerningly hot, likely "
                         f"thermal throttling territory."),
        )
    elif cpu_temp >= cfg["notable_temp_c"]:
        return TriggerEvent(
            name="cpu_thermal",
            severity="notable",
            description=f"CPU at {cpu_temp:.0f}°C — running warm.",
        )
    return None


def trigger_gpu_thermal(sections, baselines, cfg):
    """GPU temperature crossed warning thresholds."""
    gpu = _find_panel(sections, "GpuPanel")
    if gpu is None or not gpu.gpus:
        return None

    g = gpu.gpus[0]
    # Use the highest GPU temp we have (hot spot if available, else die)
    die = gpu.lhm_temps.get("core") or g.get("temp") or 0
    hot = gpu.lhm_temps.get("hot_spot") or 0
    peak = max(die, hot)
    if peak == 0:
        return None

    if peak >= cfg["alert_temp_c"]:
        return TriggerEvent(
            name="gpu_thermal",
            severity="alert",
            description=f"GPU hot-spot at {peak:.0f}°C — that's quite hot.",
        )
    elif peak >= cfg["notable_temp_c"]:
        return TriggerEvent(
            name="gpu_thermal",
            severity="notable",
            description=f"GPU at {peak:.0f}°C — running warm. Probably a game or compute load.",
        )
    return None


def trigger_memory_pressure(sections, baselines, cfg):
    """RAM near full or significant swap usage."""
    ram = _find_panel(sections, "RamPanel")
    sys_p = _find_panel(sections, "SystemPanel")
    if ram is None:
        return None

    ram_pct = ram.value
    swap_pct = sys_p.swap_pct if sys_p else 0

    # Alert: RAM critically full OR heavy swap usage
    if ram_pct >= cfg["alert_ram_pct"]:
        return TriggerEvent(
            name="memory_pressure",
            severity="alert",
            description=f"RAM at {ram_pct:.0f}% — running out of memory.",
        )
    if swap_pct >= cfg["alert_swap_pct"]:
        return TriggerEvent(
            name="memory_pressure",
            severity="alert",
            description=f"Swap at {swap_pct:.0f}% — heavy memory pressure, system may be slow.",
        )

    # Notable: RAM high or swap starting to build
    if ram_pct >= cfg["notable_ram_pct"]:
        return TriggerEvent(
            name="memory_pressure",
            severity="notable",
            description=f"RAM at {ram_pct:.0f}% — getting full.",
        )
    if swap_pct >= cfg["notable_swap_pct"]:
        return TriggerEvent(
            name="memory_pressure",
            severity="notable",
            description=f"Swap at {swap_pct:.0f}% — RAM is squeezed.",
        )
    return None


def trigger_network_burst(sections, baselines, cfg):
    """Network rate well above recent baseline (anomaly detection)."""
    net = _find_panel(sections, "NetworkPanel")
    if net is None:
        return None

    # Convert bytes/sec to Mbps for threshold comparison
    rx_mbps = (net.rx_rate * 8) / 1_000_000
    tx_mbps = (net.tx_rate * 8) / 1_000_000

    # Don't trigger on tiny bursts even if anomalous (10ms wifi blip)
    if rx_mbps < cfg["min_rate_mbps"] and tx_mbps < cfg["min_rate_mbps"]:
        return None

    rx_baseline = baselines.get("net_rx_mbps")
    tx_baseline = baselines.get("net_tx_mbps")

    rx_anomaly = rx_baseline and rx_baseline.is_anomaly(rx_mbps, sigma=cfg["sigma"])
    tx_anomaly = tx_baseline and tx_baseline.is_anomaly(tx_mbps, sigma=cfg["sigma"])

    if rx_anomaly:
        return TriggerEvent(
            name="network_burst",
            severity=cfg.get("severity", "notable"),
            description=(f"Network download burst: {rx_mbps:.0f} Mbps, well "
                         f"above recent baseline. Big download somewhere?"),
        )
    if tx_anomaly:
        return TriggerEvent(
            name="network_burst",
            severity=cfg.get("severity", "notable"),
            description=(f"Network upload burst: {tx_mbps:.0f} Mbps, well "
                         f"above recent baseline."),
        )
    return None


# State for new_heavy_process — track the most recent top-1 process name
_last_top_proc = [None]

def trigger_new_heavy_process(sections, baselines, cfg):
    """Top-1 process changed AND new top is using significant CPU."""
    procs = _find_panel(sections, "ProcessesPanel")
    if procs is None or not procs.procs:
        return None

    # procs.procs is sorted descending by CPU, each tuple is (cpu, mem, name, pid)
    top = procs.procs[0]
    cpu_pct, mem_bytes, name, pid = top

    if cpu_pct < cfg["min_cpu_pct"]:
        # Not heavy enough to be interesting — reset memory and skip
        _last_top_proc[0] = name
        return None

    if _last_top_proc[0] is None:
        _last_top_proc[0] = name
        return None  # first observation, no comparison possible

    if name == _last_top_proc[0]:
        return None  # same process as before, not new

    # New top process AND it's using significant CPU
    prev = _last_top_proc[0]
    _last_top_proc[0] = name
    mem_mb = mem_bytes / (1024 * 1024)
    return TriggerEvent(
        name="new_heavy_process",
        severity=cfg.get("severity", "notable"),
        description=(f"New top process: {name} using {cpu_pct:.0f}% CPU "
                     f"and {mem_mb:.0f}MB RAM (was {prev})."),
    )


# ──────────────── Trigger registry ────────────────
# Maps trigger config keys → trigger functions. Adding a new trigger:
#   1. Write a function above following the (sections, baselines, cfg) pattern
#   2. Add it here keyed by config name
#   3. Add the config block in config.py
TRIGGER_FUNCTIONS = {
    "single_core_pegged":   trigger_single_core_pegged,
    "cpu_sustained_high":   trigger_cpu_sustained_high,
    "cpu_thermal":          trigger_cpu_thermal,
    "gpu_thermal":          trigger_gpu_thermal,
    "memory_pressure":      trigger_memory_pressure,
    "network_burst":        trigger_network_burst,
    "new_heavy_process":    trigger_new_heavy_process,
}


# ──────────────── Runner ────────────────
class TriggerRunner:
    """Ticks at 1Hz. Updates baselines from current panel state. Evaluates
    all enabled triggers. Returns the highest-priority event ready to fire,
    or None if nothing's worth saying.

    Owns:
      - A baseline registry (rolling means/stddevs per metric)
      - Last-fire timestamps per trigger (for cooldown enforcement)
      - The trigger config from config.TRIGGERS
    """

    def __init__(self, trigger_config):
        # config.TRIGGERS — dict of {trigger_name: {enabled, cooldown_sec, ...}}
        self.config = trigger_config
        self.baselines = BaselineRegistry(window_size=300)  # 5 min @ 1Hz
        # When each trigger last fired (monotonic time). None = never.
        self._last_fired = {name: None for name in self.config}

    def push_baselines(self, sections):
        """Update rolling baselines from current panel state. Call every tick."""
        cpu = _find_panel(sections, "CpuPanel")
        if cpu and cpu.values:
            self.baselines.push("cpu_avg", cpu.average)

        ram = _find_panel(sections, "RamPanel")
        if ram:
            self.baselines.push("ram_pct", ram.value)

        gpu = _find_panel(sections, "GpuPanel")
        if gpu and gpu.gpus:
            self.baselines.push("gpu_util", gpu.gpus[0]["util"])

        net = _find_panel(sections, "NetworkPanel")
        if net:
            self.baselines.push("net_rx_mbps", (net.rx_rate * 8) / 1_000_000)
            self.baselines.push("net_tx_mbps", (net.tx_rate * 8) / 1_000_000)

    def evaluate(self, sections):
        """Run all enabled triggers. Return the highest-priority event ready
        to fire (cooldowns respected), or None."""
        now = time.monotonic()
        candidates = []

        for name, cfg in self.config.items():
            if not cfg.get("enabled", True):
                continue
            fn = TRIGGER_FUNCTIONS.get(name)
            if fn is None:
                continue

            # Cooldown check
            last = self._last_fired.get(name)
            cooldown = cfg.get("cooldown_sec", 60)
            if last is not None and (now - last) < cooldown:
                continue

            try:
                event = fn(sections, self.baselines, cfg)
            except Exception:
                # A trigger bug should not crash the loop.
                event = None

            if event is not None:
                candidates.append(event)

        if not candidates:
            return None

        # Pick the highest-severity event. Ties broken by config order.
        candidates.sort(key=lambda e: SEVERITY_RANK.get(e.severity, 0), reverse=True)
        chosen = candidates[0]

        # Mark the chosen trigger as fired (cooldown starts now)
        self._last_fired[chosen.name] = now
        return chosen

    def tick(self, sections):
        """Convenience: push baselines THEN evaluate. Call from a 1Hz timer.
        Returns event or None."""
        self.push_baselines(sections)
        return self.evaluate(sections)