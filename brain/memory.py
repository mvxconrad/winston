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
    # Display-enriched names ("python3 (winston.py) [self]") are not
    # real psutil process names — they used to leak into the CSV before
    # we moved enrichment to render-time. Older CSV rows still contain
    # them; skip those rows so they don't re-pollute the apps dict.
    if any(c in n for c in "()[]"):
        return True
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


# Common executable suffixes we strip when normalizing app dict keys.
# This is what stops `ArkAscended.exe` from creating a duplicate of an
# existing `arkascended` entry — they're the same app, the suffix is just
# the model being literal about Windows-side process names. Always store
# under the suffix-less key.
_EXE_SUFFIXES = (".exe", ".app", ".bin")


def _normalize_app_key(name):
    """Lower-case + strip common executable suffix. Use this everywhere
    we look up or insert into facts['apps'] so the same app never gets
    two entries (`arkascended` vs `arkascended.exe`)."""
    n = (name or "").strip().lower()
    for suffix in _EXE_SUFFIXES:
        if n.endswith(suffix):
            return n[: -len(suffix)]
    return n


# ──────────────── Attribute key sanity ────────────────
# Models occasionally bucket values into the wrong attribute key. The
# user says "ark is my favorite game" and the model writes
# `type=favorite, feeling=favorite` — the word "favorite" lives in BOTH
# slots, "game" gets dropped. The normalizer below catches that: when a
# value is clearly a feeling-word but appears under `type=`, we swap it
# to `feeling=`, and vice versa. The vocabularies are STARTER lists, not
# closed sets — Winston is still free to invent new values, but obvious
# misbucketed common words get auto-corrected.

# Words we'd expect under `feeling`. Lowercased, simple synonyms only.
FEELING_VOCAB = frozenset({
    "favorite", "fav", "hate", "love", "like", "dislike", "favored",
    "necessary", "essential", "fun", "boring", "annoying", "useful",
})

# Words we'd expect under `type`. Same starter set as the prompt suggests.
TYPE_VOCAB = frozenset({
    "game", "project", "work_tool", "ide", "browser", "comm", "music",
    "render", "dev", "system", "background", "tool", "app", "service",
    "media", "chat", "social",
})


def _normalize_attr_kv(key, value):
    """Auto-correct obvious misbucketed (key, value) pairs.

    If the value clearly belongs in a different attribute slot, swap to
    the correct key. e.g. `type=favorite` → `feeling=favorite`. Pass-
    through for unknown words so Winston's freedom to invent new values
    isn't restricted.

    Returns (corrected_key, value). Both key and value lowercased.
    """
    if value in (None, ""):
        return key, value  # deletion sentinel — leave alone
    k = (key or "").strip().lower()
    v_lower = str(value).strip().lower()

    if k == "type" and v_lower in FEELING_VOCAB:
        return "feeling", v_lower
    if k == "feeling" and v_lower in TYPE_VOCAB:
        return "type", v_lower
    return k, value


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
        #   user → last_learned → machine → apps → notes
        #
        # apps:  name-keyed dict of per-app facts. Each entry is a flat
        #        dict laid out as:
        #
        #          name:            display name. AUTO from CSV initially,
        #                           but user-renamable via [APP: x name=Y].
        #                           Once user-set, learn_from_log preserves it.
        #          hours:           AUTO — total time at top of process list
        #          avg_cpu:         AUTO — average CPU when this app was top
        #          peak_cpu:        AUTO — highest CPU we ever saw
        #          avg_gpu_when_top: AUTO — co-occurring GPU load
        #
        #          <user-added keys appear here, in insertion order>
        #          category:        e.g. "game", "browser", "IDE"
        #          feeling:         e.g. "favorite", "necessary"
        #          nickname:        short name (kept for reference even if
        #                           user already renamed via name=)
        #          ...anything else Winston feels like inventing
        #
        #        Multi-key merging: setting one key doesn't erase others.
        #        That was the bug with the old single-string user_label.
        #        Dict insertion order is preserved on save() so the JSON
        #        always reads top-down: AUTO_KEYS first, user keys after.
        # notes: list of free-form things Winston has learned about the
        #        user, written via [REMEMBER:] markers parsed out of LLM
        #        responses. Each entry: {ts, text, source}. Capped so the
        #        prompt stays bounded.
        self.facts = {
            "user": {},
            "last_learned": None,
            "machine": {},
            "apps": {},
            "notes": [],
        }
        self._load()

    # Notes cap — keep the personality block bounded. When we exceed this,
    # oldest notes drop off the front of the list. Picked as a balance
    # between "rich enough to feel personal" and "doesn't blow the prompt
    # token budget".
    MAX_NOTES = 30

    # Auto-managed keys per app entry. learn_from_log owns these. Anything
    # else in an app entry is user-added (via [APP:] markers) and gets
    # preserved across CSV scans. Order here defines the order they
    # appear in the JSON when an entry is rebuilt.
    AUTO_APP_KEYS = ("name", "hours", "avg_cpu", "peak_cpu", "avg_gpu_when_top")

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

        # ── Drop polluted app entries: enriched display names like
        # "python3 (winston.py) [self]" used to leak into the CSV → memory.
        # Keys with parens / brackets are never real psutil names; drop them.
        apps_raw = data.get("apps") or {}
        cleaned_apps = {
            k: v for k, v in apps_raw.items()
            if not any(c in k for c in "()[]")
        }
        data["apps"] = cleaned_apps

        # ── Migrate old schemas:
        #     user_label (string)     → top-level "feeling" key (best guess)
        #     user_attrs (dict)       → flatten dict into entry top-level
        # Both old shapes get dissolved so the entry becomes one flat dict
        # with auto stats first, user-added keys after.
        for entry in cleaned_apps.values():
            if not isinstance(entry, dict):
                continue
            # Step 1: flatten user_attrs dict into the entry, then drop it.
            attrs = entry.pop("user_attrs", None)
            if isinstance(attrs, dict):
                for k, v in attrs.items():
                    if k and k not in entry:
                        entry[k] = v
            # Step 2: turn old user_label string into a "label" key.
            old_label = entry.pop("user_label", None)
            if old_label and "label" not in entry:
                entry["label"] = str(old_label)

        # ── Auto-correct misbucketed user attrs (e.g. `type=favorite` →
        # `feeling=favorite`). If the corrected slot already has a value
        # we keep the existing one (the model can re-tell us if needed)
        # but always remove the misbucketed key.
        for entry in cleaned_apps.values():
            if not isinstance(entry, dict):
                continue
            for k in list(entry.keys()):
                if k in self.AUTO_APP_KEYS or k.startswith("_"):
                    continue
                fixed_k, fixed_v = _normalize_attr_kv(k, entry[k])
                if fixed_k != k:
                    if fixed_k not in entry:
                        entry[fixed_k] = fixed_v
                    del entry[k]

        # ── Merge `.exe`/`.app`/`.bin` duplicates into the suffix-less key.
        # Winston sometimes wrote markers using "ArkAscended.exe" while
        # CSV-derived entries used "arkascended" — same app, two entries.
        # Walk the dict, find suffixed keys, fold them into the bare-key
        # entry (auto-stats from CSV win; user attrs merge in).
        merged_apps = {}
        for k, entry in cleaned_apps.items():
            norm = _normalize_app_key(k)
            if not norm:
                continue
            if norm in merged_apps:
                # Merge `entry` into the existing merged_apps[norm].
                # Strategy: existing wins on auto stats (CSV-derived,
                # accurate) but new entry's user-added keys fill in
                # anything the existing didn't have.
                target = merged_apps[norm]
                if isinstance(entry, dict):
                    for ek, ev in entry.items():
                        if ek not in target:
                            target[ek] = ev
            else:
                # Re-key under the normalized form.
                if isinstance(entry, dict):
                    # Display name: prefer the suffix-less form so it
                    # reads cleanly in the prompt.
                    if entry.get("name", "").lower().endswith(_EXE_SUFFIXES):
                        bare = entry["name"]
                        for sfx in _EXE_SUFFIXES:
                            if bare.lower().endswith(sfx):
                                bare = bare[: -len(sfx)]
                                break
                        entry["name"] = bare
                merged_apps[norm] = entry
        data["apps"] = merged_apps

        # ── Fold orphan short-name entries into a unique-prefix-matching
        # canonical entry. e.g. memory has `arkascended` from CSV AND
        # `ark` from a Winston [APP: Ark ...] marker — same app, two
        # entries. If the short key has ONLY user-added attrs (no auto
        # stats from CSV), it's almost certainly a stand-in for a longer
        # canonical name. Find the longer key by unique prefix match
        # and merge.
        consumed = set()
        for short_key, short_entry in list(merged_apps.items()):
            if short_key in consumed:
                continue
            if not isinstance(short_entry, dict):
                continue
            has_auto = any(short_entry.get(k) is not None
                           for k in ("hours", "avg_cpu", "peak_cpu",
                                     "avg_gpu_when_top"))
            if has_auto:
                continue  # CSV-real entry, not a stand-in
            if len(short_key) < 3:
                continue
            candidates = [k for k in merged_apps
                          if k != short_key and k.startswith(short_key)
                          and k not in consumed]
            if len(candidates) != 1:
                continue
            target_key = candidates[0]
            target = merged_apps[target_key]
            if not isinstance(target, dict):
                continue
            # Move user-added attrs from short → target (target wins on
            # auto stats; short fills in anything missing).
            for ek, ev in short_entry.items():
                if ek == "name":
                    # Treat short_entry's `name` as a nickname hint.
                    if "nickname" not in target and ev:
                        target["nickname"] = ev
                    continue
                if ek not in target:
                    target[ek] = ev
            consumed.add(short_key)
        for k in consumed:
            merged_apps.pop(k, None)
        data["apps"] = merged_apps

        # Merge into defaults so a future schema addition doesn't break
        # old memory files.
        for k, v in data.items():
            self.facts[k] = v

        # ── Clean angle-bracket placeholders from saved notes (run AFTER
        # the merge so _clean_note_text can resolve <user-name> against
        # the actual user data we just loaded).
        notes_in = self.facts.get("notes") or []
        if isinstance(notes_in, list):
            for n in notes_in:
                if isinstance(n, dict) and n.get("text"):
                    n["text"] = self._clean_note_text(n["text"])

    def save(self):
        """Atomic write: tempfile + rename. Crash-safe.

        Writes fields in a deterministic, human-readable order
        (user → last_learned → machine → apps → notes) rather than
        alphabetic sort, so the file reads top-down like a profile.
        """
        # Build an ordered dict containing only known top-level keys so
        # legacy fields can't sneak back in after migration. Python dicts
        # preserve insertion order.
        ordered = {
            "user":          self.facts.get("user", {}),
            "last_learned":  self.facts.get("last_learned"),
            "machine":       self.facts.get("machine", {}),
            "apps":          self.facts.get("apps", {}),
            "notes":         self.facts.get("notes", []),
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

        # Stream the CSV. Column names match logger.py:
        #   top_proc_name / top_proc_cpu       — global top across both sides
        #   top_winproc_name / top_winproc_cpu — Windows-host top specifically
        #   gpu_util                            — for behavior co-occurrence
        #
        # We learn from BOTH proc columns so games / browsers running on
        # the Windows host (Ark, Discord, Chrome) end up ranked alongside
        # WSL processes. Old logs predate top_winproc_* — DictReader
        # returns None for missing columns, which the noise filter drops,
        # so the older rows just contribute their top_proc_* and we move on.
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

                    gpu = _safe_float(row.get("gpu_util"))

                    # Walk both proc columns. Dedupe by lowercased name
                    # within the row so one app dominating both sides
                    # (rare — would mean same process showed up twice)
                    # only counts once.
                    seen_in_row = set()
                    for name_col, cpu_col in (
                            ("top_proc_name",    "top_proc_cpu"),
                            ("top_winproc_name", "top_winproc_cpu")):
                        name = (row.get(name_col) or "").strip()
                        if not name or _is_system_noise(name):
                            continue
                        key = name.lower()
                        if key in seen_in_row:
                            continue
                        seen_in_row.add(key)

                        display_name[key] = name  # last seen wins
                        seen[key] += 1

                        cpu = _safe_float(row.get(cpu_col))
                        if cpu is not None:
                            cpu_sum[key] += cpu
                            cpu_count[key] += 1
                            if cpu > cpu_peak[key]:
                                cpu_peak[key] = cpu

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
        # Existing apps dict — we want to PRESERVE user-added keys
        # (anything not in AUTO_APP_KEYS) and the user-set `name` if
        # it differs from the CSV's display_name (i.e. user renamed it).
        existing_apps = self.facts.get("apps") or {}

        for key, sec in sorted(seen.items(), key=lambda kv: -kv[1])[:25]:
            if sec < 60:
                # Less than a minute total in 7 days — not interesting.
                continue

            existing = existing_apps.get(key) or {}
            # Build the rebuilt entry in canonical order:
            #   AUTO_APP_KEYS first, then user-added keys.
            # `name`: prefer existing if it was user-renamed (i.e. doesn't
            # match the CSV display name AND there's evidence of user
            # involvement, indicated by ANY non-auto key being present).
            csv_name = display_name[key]
            user_added = {k: v for k, v in existing.items()
                          if k not in self.AUTO_APP_KEYS}
            existing_name = existing.get("name")
            user_renamed = (
                existing_name and existing_name != csv_name and user_added
            )
            final_name = existing_name if user_renamed else csv_name

            entry = {
                "name": final_name,
                "hours": round(sec / 3600.0, 2),
                "avg_cpu": (round(cpu_sum[key] / cpu_count[key], 1)
                            if cpu_count[key] else None),
                "peak_cpu": round(cpu_peak[key], 1) if key in cpu_peak else None,
                "avg_gpu_when_top": (round(gpu_sum_when_top[key] / gpu_count_when_top[key], 1)
                                     if gpu_count_when_top[key] else None),
            }
            # Append user-added keys after the auto stats.
            for k, v in user_added.items():
                entry[k] = v
            apps[key] = entry

        # Carry over apps that the CSV didn't see this scan but that
        # the user has already labelled — so a [APP:] marker sticks
        # even when the app hasn't appeared in the log yet.
        for key, existing in existing_apps.items():
            if key in apps:
                continue
            user_added = {k: v for k, v in existing.items()
                          if k not in self.AUTO_APP_KEYS}
            if user_added:
                # Preserve as-is — keeps user attrs alive.
                apps[key] = existing

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
        """Look up a process by name. Returns None if unknown.

        Resolution is suffix-aware (`.exe` etc.) and nickname-aware so
        callers can pass any of "ArkAscended", "ArkAscended.exe", "Ark",
        or "ark" and find the same entry.
        """
        if not name:
            return None
        key = self._resolve_app_key(name)
        return self.facts.get("apps", {}).get(key)

    def display_name_for(self, name):
        """Return what Winston should call this app — the user's nickname
        if one is set, otherwise the canonical display name, falling back
        to the input name if there's no entry. Use this when rendering
        process names in the prompt so the LLM sees "Ark" instead of
        "ArkAscended" when the user has nicknamed it."""
        entry = self.lookup_app(name) or {}
        nick = (entry.get("nickname") or "").strip()
        if nick:
            return nick
        # Fall back to entry's canonical name, then the input.
        return entry.get("name") or name

    # ──────────────── Self-learning hooks ────────────────
    # These let Winston grow his own memory in response to either user
    # statements ("ark is my favorite game") or detected patterns. The
    # CommentaryEngine parses [REMEMBER: …] / [LABEL: name = …] markers
    # out of LLM responses and routes them here.

    def _clean_note_text(self, text):
        """Strip angle-bracket placeholder syntax that models sometimes
        leak into note text. `<max>` → `max`, `<user-name>` → the actual
        user's name (or stripped if no name in memory).

        Driven entirely by what's in memory — no hardcoded names."""
        if not text:
            return text
        import re as _re
        s = str(text).strip()
        user_name = self.get_user_name() or ""
        # Replace any `<user-name>` / `<username>` / `<user>` with the
        # actual user name (or empty if none).
        for pat in (r"<\s*user[-_ ]?name\s*>",
                    r"<\s*user\s*>",
                    r"<\s*name\s*>"):
            s = _re.sub(pat, user_name, s, flags=_re.IGNORECASE)
        # Strip any other `<word>` wrappers around what's likely the
        # actual content. e.g. `<max>` → `max`. We keep the inner text.
        s = _re.sub(r"<\s*([^<>\n]{1,40}?)\s*>", r"\1", s)
        # Tidy up double spaces and stray leading/trailing whitespace.
        s = _re.sub(r"\s+", " ", s).strip()
        return s

    def add_note(self, text, source="user"):
        """Append a free-form note about the user to memory.

        text:    the fact in plain English. Concise — one fact per note.
                 Example: "Ark is max's favorite game", "Codes mostly
                 at night", "Hates browser tabs over 50".
        source:  who/what wrote this note. "user" = parsed from a
                 [REMEMBER: …] marker after a user statement.
                 "observation" = derived from a pattern Winston noticed.
                 "manual" = added programmatically.

        Sanitizes angle-bracket placeholder syntax that the model
        sometimes leaks: `<max>` → `max`, `<user-name>` → the actual
        user's name from memory (or just gets stripped if no name).
        That way notes don't end up with literal `<placeholder>` text.

        Dedupes case-insensitively against existing notes so the same
        fact doesn't pile up across sessions if the user tells Winston
        the same thing twice.

        Returns True if the note was added, False if it was a duplicate.
        """
        text = self._clean_note_text(text)
        if not text or len(text) > 280:
            # 280 chars: single tweet-sized fact. Anything bigger is
            # probably the model running off the rails inside a marker.
            return False

        notes = self.facts.setdefault("notes", [])
        norm = text.lower()
        for existing in notes:
            if (existing.get("text") or "").lower() == norm:
                return False  # already remember this

        notes.append({
            "ts":     datetime.now().isoformat(timespec="seconds"),
            "text":   text,
            "source": source,
        })
        # Cap — drop oldest first.
        if len(notes) > self.MAX_NOTES:
            self.facts["notes"] = notes[-self.MAX_NOTES:]
        return True

    def get_notes(self, n=None):
        """Return the most-recent N notes (or all if n is None)."""
        notes = self.facts.get("notes") or []
        if n is None:
            return list(notes)
        return notes[-n:]

    def forget_note(self, text):
        """Remove notes whose text matches (case-insensitive). Used when
        Winston emits [FORGET: …] in response to the user correcting him.
        Returns the count removed."""
        text = (text or "").strip().lower()
        if not text:
            return 0
        notes = self.facts.get("notes") or []
        before = len(notes)
        self.facts["notes"] = [
            n for n in notes if (n.get("text") or "").lower() != text
        ]
        return before - len(self.facts["notes"])

    def _resolve_app_key(self, name):
        """Find the canonical apps-dict key for an incoming process name.

        Lookup order:
          1. Exact normalized match (lower + strip .exe/.app/.bin).
          2. Legacy keys with executable suffix.
          3. Nickname match: any existing entry whose `nickname` equals
             the incoming name (case-insensitive).
          4. Prefix match: incoming name is a UNIQUE strict-prefix of an
             existing canonical key (e.g. "Ark" → "arkascended"). Only
             fires when exactly one existing app starts with the
             incoming string AND the incoming is at least 3 chars to
             avoid collisions on tiny tokens.

        Returns the resolved key (suitable for `apps[key]`) or the
        freshly-normalized key for a new entry if nothing matches.
        """
        apps = self.facts.get("apps") or {}
        norm = _normalize_app_key(name)
        if not norm:
            return norm
        # 1: direct hit
        if norm in apps:
            return norm
        # 2: legacy keys with suffix
        for suffix in _EXE_SUFFIXES:
            if (norm + suffix) in apps:
                return norm + suffix
        # 3: nickname match
        target = (name or "").strip().lower()
        for k, entry in apps.items():
            if isinstance(entry, dict):
                nick = (entry.get("nickname") or "").strip().lower()
                if nick and nick == target:
                    return k
        # 4: unique prefix match — catches Winston using a short form
        # like "Ark" when the canonical key is "arkascended". Required
        # length floor of 3 chars avoids false matches on tiny strings
        # like "no" matching "node". Only one candidate must exist for
        # the merge to be safe — ambiguous prefixes get a new entry.
        if len(norm) >= 3:
            candidates = [k for k in apps if k.startswith(norm) and k != norm]
            if len(candidates) == 1:
                return candidates[0]
        # No existing match — return the normalized form for a new entry.
        return norm

    def set_app_attrs(self, name, attrs):
        """Merge structured fields into an app entry as TOP-LEVEL keys.

        name:  app name. Used for case-insensitive dict-key lookup AND
               (if attrs has no `name` key) as the display name on first
               creation. Pass the app's actual process name — `set_app_attrs`
               does NOT rename a known app unless attrs carries `name=...`.
        attrs: dict of {key: value}. Special keys:
                 name      — sets the user-visible display name. After
                             this, learn_from_log preserves it on rescans.
                 -<key>    — pseudo-key; calling code (the marker parser)
                             passes None as the value to mean "delete".
               Other keys: anything goes ("category", "feeling", "nickname",
               "mood", whatever Winston wants to track).

        Ordering: AUTO_APP_KEYS first in their canonical order, then any
        user-added keys in the order they were first set. This is what
        you see when reading memory.json top-down — auto stats, then
        the user's stuff. Achieved by rebuilding the entry dict on every
        call so insertion order stays predictable.

        Returns True if anything actually changed, False if no-op.
        """
        if not name or not isinstance(attrs, dict):
            return False
        # Resolve to existing entry if we have one — strips .exe and
        # checks nicknames so "ArkAscended.exe" / "Ark" / "ArkAscended"
        # all map to the same canonical key.
        key = self._resolve_app_key(name)
        apps = self.facts.setdefault("apps", {})
        entry = dict(apps.get(key) or {})
        # Display name preserved if the entry already has one (don't
        # overwrite "ArkAscended" with "ArkAscended.exe" just because
        # Winston spelled it differently).
        entry.setdefault("name", name)

        changed = False
        for k, v in attrs.items():
            k = (k or "").strip().lower()
            if not k:
                continue
            # Canonical `name` is locked. The marker parser redirects
            # `name=` → `nickname=`; enforce it here too.
            if k == "name":
                continue
            # Auto-stat keys belong to learn_from_log.
            if k in self.AUTO_APP_KEYS:
                continue
            # Auto-correct misbucketed values (type=favorite → feeling=favorite).
            # Models often confuse the slots when a phrase like "favorite
            # game" gets parsed; the normalizer puts the value where it
            # makes sense.
            k, v = _normalize_attr_kv(k, v)
            if v in (None, ""):
                if k in entry:
                    del entry[k]
                    changed = True
            else:
                v = str(v).strip()
                if entry.get(k) != v:
                    entry[k] = v
                    changed = True

        if not changed:
            return False

        # Rebuild with canonical ordering: AUTO_APP_KEYS first (only the
        # ones present in the entry), then user-added keys.
        ordered = {}
        for ak in self.AUTO_APP_KEYS:
            if ak in entry:
                ordered[ak] = entry[ak]
        for k, v in entry.items():
            if k not in ordered:
                ordered[k] = v
        apps[key] = ordered
        return True

    def get_app_attrs(self, name):
        """Read user-added (non-auto) keys for a named app. Returns {}
        if app doesn't exist. Useful for prompt builders / display."""
        if not name:
            return {}
        entry = self.facts.get("apps", {}).get(name.lower()) or {}
        return {k: v for k, v in entry.items()
                if k not in self.AUTO_APP_KEYS}


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