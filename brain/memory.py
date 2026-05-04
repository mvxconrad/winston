"""Persistent memory for Winston.

A small JSON-backed store that survives restarts. Two kinds of content:

  EXPLICIT FACTS — set by user or config: name, machine specs, preferences.
                   Stable; doesn't change unless the user/system changes.

  DERIVED FACTS  — mined from the observation log: most-used apps, behavior
                   fingerprints (does this app correlate with high GPU?
                   high disk?), time-of-day patterns. Refreshed periodically
                   from the CSV — Winston *learns* what's normal for you.

The point: prompts can include "what Winston knows about max" so the LLM
can make personalized inferences without a hardcoded taxonomy. When
ArkAscended.exe shows up as the top process and GPU is hot, the prompt
includes "max's most-played app: ArkAscended (avg 87% GPU when running)"
so the model connects the dots itself.

Storage format: single JSON file at logs/memory.json. Small (<10KB even
after months of use). Human-readable so you can inspect/edit. Atomic
writes so a crash never corrupts it.

We deliberately do NOT use a vector DB / embeddings here. The data is
structured numeric/categorical (process names, counts, percentages) —
exact querying via dict lookup beats fuzzy semantic search for this job.
"""
import json
import os
import platform
import socket
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta


# Where the memory blob lives. Same logs/ dir as the CSV — keeps all of
# Winston's persistent state in one inspectable place.
DEFAULT_MEMORY_PATH = "logs/memory.json"

# CSV log location (matches logger.py).
DEFAULT_LOG_PATH = "logs/raw/observations.csv"

# Cap log scan size — same as history.py. A normal week of 1Hz logging is
# ~50MB; this gives 4-day comfortable headroom.
MAX_LOG_SIZE_BYTES = 50 * 1024 * 1024


# ──────────────── Process noise filter ────────────────
# When ranking "what does max use most", we want to ignore Windows/Linux
# system processes that are always running but aren't apps in the meaningful
# sense. This list is conservative — we err on excluding rather than
# including, because a noisy process pollutes the "most used" ranking.
#
# Note we deliberately do NOT filter "python", "python3", "node", etc.
# An earlier version did, on the theory of "filter Winston's own process",
# but that nukes everything written in those interpreters — which on a
# dev machine is most of the user's real work. We rely on the noise list
# below being just OS plumbing.
#
# Match is case-insensitive exact (lowercased on both sides).
SYSTEM_NOISE_PROCESSES = frozenset({
    # Windows
    "system", "system idle process", "registry", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "svchost.exe", "fontdrvhost.exe", "dwm.exe", "runtimebroker.exe",
    "sihost.exe", "taskhostw.exe", "ctfmon.exe", "searchindexer.exe",
    "searchhost.exe", "searchapp.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "applicationframehost.exe",
    "wmiprvse.exe", "conhost.exe", "audiodg.exe", "spoolsv.exe",
    "msmpeng.exe", "mssense.exe", "securityhealthservice.exe",
    "memcompression",
    # Linux
    "kthreadd", "ksoftirqd", "rcu_sched", "migration", "watchdog",
    "systemd", "systemd-journal", "systemd-resolve", "systemd-udevd",
    "dbus-daemon", "kworker",
    # WSL plumbing
    "init", "wslservice.exe", "wsl.exe",
    # (no entry for python/python3/node — those are real workloads)
})


def _is_system_noise(name):
    """Is this process name something we should ignore for 'most used'?"""
    if not name:
        return True
    n = name.strip().lower()
    if not n:
        return True
    # Pure numeric values land here when CSV columns get out of order
    # somewhere upstream — strip them out so we don't end up with
    # entries like {"name": "43.0", "hours": ...} polluting memory.
    try:
        float(n)
        return True
    except ValueError:
        pass
    if n in SYSTEM_NOISE_PROCESSES:
        return True
    # kworker/* and similar kernel threads — match the prefix
    if n.startswith("kworker") or n.startswith("ksoftirq"):
        return True
    return False


def _safe_float(s):
    """Parse a CSV cell to float, returning None if blank/invalid."""
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


# ──────────────── Memory class ────────────────
class Memory:
    """Winston's persistent memory.

    Holds a dict of facts. Explicit facts are set by the caller; derived
    facts are produced by learn_from_log(). load() reads the JSON file
    on init; save() writes it atomically.

    Typical lifecycle:
      mem = Memory()                # load from disk
      mem.set_machine_facts()       # auto-detect host, OS, CPU, etc.
      mem.learn_from_log(hours=168) # scan last 7d of CSV, update derived
      mem.save()                    # write back to disk

    Then the prompt builder reads facts via mem.facts.
    """

    def __init__(self, path=DEFAULT_MEMORY_PATH):
        self.path = path
        # Default empty shape, in the order we want them written to disk:
        #   user → last_learned → machine → apps
        # apps is a name-keyed dict, the single source of truth. Top-N
        # ranking is computed at read time via get_top_apps().
        self.facts = {
            "user": {},          # name, preferences
            "last_learned": None,
            "machine": {},       # host, os, cpu, gpu, ram_gb
            "apps": {},          # {name_lower: {name, hours, avg_cpu, peak_cpu, avg_gpu_when_top}}
        }
        self._load()

    # ──────────────── Disk I/O ────────────────
    def _load(self):
        """Load from JSON. Missing file is fine — we start with defaults."""
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return

        # ── Migrate old schema (pre-v0.9): had separate `top_apps` list
        # and `behavior` dict storing the SAME per-app payload. Collapse
        # to a single `apps` dict.
        if "apps" not in data and ("top_apps" in data or "behavior" in data):
            apps = {}
            # Prefer behavior dict (already keyed by name) when present.
            for key, entry in (data.get("behavior") or {}).items():
                if isinstance(entry, dict) and entry.get("name"):
                    apps[entry["name"].lower()] = entry
            # Fall back to top_apps list for any names not in behavior.
            for entry in (data.get("top_apps") or []):
                if isinstance(entry, dict) and entry.get("name"):
                    k = entry["name"].lower()
                    apps.setdefault(k, entry)
            data["apps"] = apps
            data.pop("top_apps", None)
            data.pop("behavior", None)

        # Merge into defaults so a future schema addition doesn't break
        # old memory files.
        for k, v in data.items():
            self.facts[k] = v

    def save(self):
        """Atomic write: tempfile + rename. Crash-safe.

        Writes fields in a deterministic, human-readable order
        (user → last_learned → machine → apps) rather than alphabetic
        sort, so the file reads top-down like a profile.

        Also regenerates the markdown vault under `vault/` so the
        human-readable mirror stays in sync. JSON is canonical; vault
        is derived.
        """
        # Build an ordered dict containing only known top-level keys so
        # legacy fields can't sneak back in after migration. Python dicts
        # preserve insertion order.
        ordered = {
            "user":          self.facts.get("user", {}),
            "last_learned":  self.facts.get("last_learned"),
            "machine":       self.facts.get("machine", {}),
            "apps":          self.facts.get("apps", {}),
        }
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            dir_ = os.path.dirname(self.path) or "."
            with tempfile.NamedTemporaryFile(
                "w", dir=dir_, delete=False, suffix=".tmp", encoding="utf-8"
            ) as tf:
                json.dump(ordered, tf, indent=2)  # no sort_keys — order matters
                tmp_path = tf.name
            os.replace(tmp_path, self.path)
        except OSError:
            # Best-effort. A failed save is not fatal — we'll try again next
            # time the caller invokes save().
            pass

        # Regenerate the markdown vault. Best-effort, never raises — the
        # JSON above is the source of truth.
        try:
            from brain.vault import write_all
            write_all(self.facts)
        except Exception:
            pass

    # ──────────────── Explicit facts ────────────────
    def set_user(self, name=None, **kwargs):
        """Record user info. Call from config bootstrap."""
        if name is not None:
            self.facts["user"]["name"] = name
        for k, v in kwargs.items():
            if v is not None:
                self.facts["user"][k] = v

    def set_machine_facts(self, gpu_panel=None, ram_panel=None):
        """Auto-detect and record machine specs.

        gpu_panel/ram_panel are optional — if passed, we pull GPU name and
        total RAM from them (more accurate than redoing the detection).
        Otherwise we stick with what we can introspect with stdlib.
        """
        m = self.facts.setdefault("machine", {})
        m["host"] = socket.gethostname()
        m["os"] = platform.system()
        m["os_release"] = platform.release()

        # CPU model — psutil doesn't expose this portably. Try platform.
        cpu = platform.processor()
        if cpu and cpu != "x86_64":  # platform.processor() is often useless
            m["cpu"] = cpu
        else:
            # Fall back to /proc/cpuinfo on Linux/WSL
            try:
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if line.lower().startswith("model name"):
                            m["cpu"] = line.split(":", 1)[1].strip()
                            break
            except OSError:
                pass

        if gpu_panel and getattr(gpu_panel, "gpus", None):
            g = gpu_panel.gpus[0]
            m["gpu"] = g.get("name")
            mem_total = g.get("mem_total")
            if mem_total:
                m["gpu_vram_gb"] = round(mem_total / (1024 ** 3), 1)

        if ram_panel and getattr(ram_panel, "total", None):
            m["ram_gb"] = round(ram_panel.total / (1024 ** 3), 1)

    # ──────────────── Derived facts (mined from the log) ────────────────
    def learn_from_log(self, log_path=DEFAULT_LOG_PATH, hours=168):
        """Scan the CSV log to derive what max actually uses.

        Single pass, O(n) over rows. Accumulates:
          - per-process seen-time (in seconds, since one row = one second)
          - per-process avg/peak CPU + RAM when that process is "top"
          - co-occurrence with GPU load (was GPU busy when this app was top?)

        Updates self.facts["top_apps"] and self.facts["behavior"].

        hours: how far back to scan. Default 168 = 7 days.

        Returns a small status dict; safe to ignore.
        """
        result = {"rows_scanned": 0, "ranked_apps": 0, "log_missing": False}
        if not os.path.exists(log_path):
            result["log_missing"] = True
            return result
        try:
            if os.path.getsize(log_path) > MAX_LOG_SIZE_BYTES:
                result["log_missing"] = True  # too big — refuse to scan
                return result
        except OSError:
            result["log_missing"] = True
            return result

        cutoff = datetime.now() - timedelta(hours=hours)

        # Per-process accumulators. Keyed by lowercased process name (so
        # "Chrome.exe" and "chrome.exe" merge).
        seen = defaultdict(int)              # row count = seconds active
        cpu_sum = defaultdict(float)         # for avg-while-running
        cpu_count = defaultdict(int)
        cpu_peak = defaultdict(float)
        # Behavior co-occurrence: when this app was top-1, what was the
        # ambient state? Helps Winston infer category. A high avg-GPU-when-
        # I-was-top means I'm probably a GPU-hungry app (game, compute).
        gpu_sum_when_top = defaultdict(float)
        gpu_count_when_top = defaultdict(int)
        # Display name (preserve case of the most-recently-seen variant
        # so we render "Chrome" rather than "chrome.exe" if the data has
        # both forms).
        display_name = {}

        # Stream the CSV. Column names match logger.py: ProcessesPanel
        # writes `top_proc_name` / `top_proc_cpu`, GpuPanel writes
        # `gpu_util`. We don't have per-top-proc memory in the log right
        # now (only RAM panel total), so we leave that field out.
        import csv
        try:
            with open(log_path, "r", newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    return result

                for row in reader:
                    ts_str = row.get("timestamp", "")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str)
                    except ValueError:
                        continue
                    if ts < cutoff:
                        continue
                    result["rows_scanned"] += 1

                    name = (row.get("top_proc_name") or "").strip()
                    if not name or _is_system_noise(name):
                        continue

                    key = name.lower()
                    display_name[key] = name  # last seen wins; usually fine

                    seen[key] += 1

                    cpu = _safe_float(row.get("top_proc_cpu"))
                    if cpu is not None:
                        cpu_sum[key] += cpu
                        cpu_count[key] += 1
                        if cpu > cpu_peak[key]:
                            cpu_peak[key] = cpu

                    gpu = _safe_float(row.get("gpu_util"))
                    if gpu is not None:
                        gpu_sum_when_top[key] += gpu
                        gpu_count_when_top[key] += 1
        except (OSError, csv.Error):
            return result

        # Build the apps dict. Single source of truth — name-keyed,
        # capped at 25 entries (the prompt will only ever show the top
        # few). Ranking by hours is computed at read time in
        # get_top_apps() so we don't store the same payload twice.
        apps = {}
        for key, sec in sorted(seen.items(), key=lambda kv: -kv[1])[:25]:
            if sec < 60:
                # Less than a minute total in 7 days — not interesting.
                continue
            apps[key] = {
                "name": display_name[key],
                "hours": round(sec / 3600.0, 2),
                "avg_cpu": (round(cpu_sum[key] / cpu_count[key], 1)
                            if cpu_count[key] else None),
                "peak_cpu": round(cpu_peak[key], 1) if key in cpu_peak else None,
                "avg_gpu_when_top": (round(gpu_sum_when_top[key] / gpu_count_when_top[key], 1)
                                     if gpu_count_when_top[key] else None),
            }

        self.facts["apps"] = apps
        self.facts["last_learned"] = datetime.now().isoformat(timespec="seconds")
        result["ranked_apps"] = len(apps)
        return result

    # ──────────────── Convenience accessors for the prompt builder ────────
    def get_user_name(self):
        return self.facts.get("user", {}).get("name")

    def get_machine_summary(self):
        """One-liner of machine facts for prompts. Empty string if nothing
        is known yet."""
        m = self.facts.get("machine", {})
        if not m:
            return ""
        bits = []
        if m.get("os"):
            bits.append(m["os"])
        if m.get("cpu"):
            bits.append(m["cpu"])
        if m.get("gpu"):
            gpu_str = m["gpu"]
            if m.get("gpu_vram_gb"):
                gpu_str += f" ({m['gpu_vram_gb']}GB VRAM)"
            bits.append(gpu_str)
        if m.get("ram_gb"):
            bits.append(f"{m['ram_gb']}GB RAM")
        return ", ".join(bits)

    def get_top_apps(self, n=5):
        """Return the top N most-used apps as a list, ranked by hours
        descending. Computed at read time from the canonical `apps` dict
        — no separate ranked list is stored."""
        apps = self.facts.get("apps", {})
        ranked = sorted(apps.values(),
                        key=lambda a: float(a.get("hours") or 0),
                        reverse=True)
        return ranked[:n]

    def lookup_app(self, name):
        """Look up a process name in derived behavior. Returns None if
        unknown. Used by the prompt builder when a current process is
        running so it can say 'this is one of max's frequent apps'."""
        if not name:
            return None
        return self.facts.get("apps", {}).get(name.lower())


# ──────────────── Self-test ────────────────
if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MEMORY_PATH
    log = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_LOG_PATH

    print(f"Memory file: {path}")
    print(f"Log file:    {log}")
    print()

    m = Memory(path=path)
    m.set_user(name="max")
    m.set_machine_facts()
    print("Machine summary:", m.get_machine_summary() or "(unknown)")
    print()

    print("Scanning log...")
    info = m.learn_from_log(log_path=log, hours=168)
    print(f"  rows scanned: {info['rows_scanned']}")
    print(f"  ranked apps:  {info['ranked_apps']}")
    print(f"  log missing:  {info['log_missing']}")
    print()

    apps = m.get_top_apps(10)
    if apps:
        print(f"{'app':<30} {'hours':>7} {'avg_cpu':>8} {'avg_gpu':>8}")
        for a in apps:
            print(f"{a['name']:<30} {a['hours']:>7.2f}  "
                  f"{(a.get('avg_cpu') or 0):>7.1f}% "
                  f"{(a.get('avg_gpu_when_top') or 0):>7.1f}%")
    else:
        print("(no top apps yet — log empty or too short)")

    m.save()
    print()
    print(f"Saved to {path}")