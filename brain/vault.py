"""Markdown vault — Winston's persistent memory in human-readable form.

Why both vault and memory.json:
  memory.json  — canonical, machine-parseable, single file, atomic writes.
  vault/*.md   — human-readable mirror, easy to skim/edit/grep, organized
                 by topic.

The JSON is the source of truth. Editing the MD files won't change what
Winston remembers (yet — see `import_user_md()` for a future hook). The
vault is regenerated from memory.facts every time `Memory.save()` is
called, so it always matches what's actually in memory.

Layout:
    vault/
      index.md          — landing page: who/what/links to others
      user.md           — explicit user info (name, preferences)
      machine.md        — auto-detected hardware / OS facts
      apps.md           — top apps, ranked, with stats
      sessions/         — (future) one MD per session journal entry

Why MD specifically: it's the lingua franca of personal-knowledge tools
(Obsidian, Logseq, plain `cat`, `grep -r vault/`). Plain text future-
proofs everything; if Winston goes away tomorrow, the vault is still
readable as a folder of notes.
"""
import os
from datetime import datetime


VAULT_DIR = "vault"


def _safe_write(path, content):
    """Best-effort write. Vault is a derived view; corruption is non-fatal
    (the JSON is canonical). Never raises."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError:
        pass


def _ts():
    return datetime.now().isoformat(timespec="seconds")


# ──────────────── Page builders ────────────────
def render_index(facts):
    user = facts.get("user", {}) or {}
    machine = facts.get("machine", {}) or {}
    apps = facts.get("apps", {}) or {}
    last_learned = facts.get("last_learned") or "(never)"

    name = user.get("name") or "(unknown)"
    host = machine.get("host") or "(unknown)"
    n_apps = len(apps)

    return f"""# Winston :: vault index

> _Last updated: {_ts()}_
> _Last log learn: {last_learned}_

This is Winston's persistent memory in markdown form. The canonical store
is `logs/memory.json`; these pages are regenerated from it on every
`Memory.save()` call.

## Pages

- [user.md](user.md) — what Winston knows about **{name}**
- [machine.md](machine.md) — hardware on **{host}**
- [apps.md](apps.md) — top {n_apps} apps mined from the observation log

## Quick stats

| Field          | Value |
| -------------- | ----- |
| User           | `{name}` |
| Host           | `{host}` |
| Tracked apps   | `{n_apps}` |
| Last learned   | `{last_learned}` |
"""


def render_user(facts):
    user = facts.get("user", {}) or {}
    name = user.get("name") or "(unknown)"

    lines = [
        "# user",
        "",
        f"> _Updated {_ts()}_",
        "",
        "## Identity",
        "",
        f"- **name**: `{name}`",
    ]
    # Any extra fields the user has set (preferences, role, etc).
    extras = {k: v for k, v in user.items() if k != "name"}
    if extras:
        lines += ["", "## Other facts", ""]
        for k, v in extras.items():
            lines.append(f"- **{k}**: `{v}`")
    else:
        lines += [
            "",
            "## Other facts",
            "",
            "_Nothing else recorded. Use `Memory.set_user(key=value)` to add._",
        ]
    return "\n".join(lines) + "\n"


def render_machine(facts):
    m = facts.get("machine", {}) or {}

    lines = [
        "# machine",
        "",
        f"> _Updated {_ts()}_",
        "",
        "## Hardware",
        "",
    ]
    if not m:
        lines.append("_Nothing detected yet — call `Memory.set_machine_facts()`._")
        return "\n".join(lines) + "\n"

    fields_in_order = [
        ("host",          "host"),
        ("os",            "OS"),
        ("os_release",    "OS release"),
        ("cpu",           "CPU"),
        ("gpu",           "GPU"),
        ("gpu_vram_gb",   "GPU VRAM (GB)"),
        ("ram_gb",        "RAM (GB)"),
    ]
    for key, label in fields_in_order:
        if m.get(key) is not None:
            lines.append(f"- **{label}**: `{m[key]}`")

    # Anything else stored under machine that we haven't explicitly listed.
    rest = {k: v for k, v in m.items()
            if k not in {f[0] for f in fields_in_order}}
    if rest:
        lines += ["", "## Extra fields", ""]
        for k, v in rest.items():
            lines.append(f"- **{k}**: `{v}`")

    return "\n".join(lines) + "\n"


def render_apps(facts):
    apps = facts.get("apps", {}) or {}
    last_learned = facts.get("last_learned") or "(never)"

    if not apps:
        return ("# apps\n\n"
                f"> _Updated {_ts()}_\n"
                f"> _Last log learn: {last_learned}_\n\n"
                "_No apps tracked yet. The CSV log needs at least a few "
                "minutes of data before `Memory.learn_from_log()` produces "
                "rankings._\n")

    ranked = sorted(apps.values(),
                    key=lambda a: float(a.get("hours") or 0),
                    reverse=True)

    lines = [
        "# apps",
        "",
        f"> _Updated {_ts()}_",
        f"> _Last log learn: {last_learned}_",
        "",
        f"Top **{len(ranked)}** apps mined from `logs/raw/observations.csv` "
        f"over the last 7 days. Ranked by total time at the top of the "
        f"process list (≈ active foreground time).",
        "",
        "| Rank | App | Hours | Avg CPU% | Peak CPU% | Avg GPU% (when top) |",
        "| ---: | --- | ----: | -------: | --------: | ------------------: |",
    ]
    for i, a in enumerate(ranked, 1):
        name = a.get("name") or "?"
        hours = float(a.get("hours") or 0)
        avg_cpu = a.get("avg_cpu")
        peak_cpu = a.get("peak_cpu")
        avg_gpu = a.get("avg_gpu_when_top")
        lines.append(
            f"| {i} | `{name}` | {hours:.2f} | "
            f"{(f'{avg_cpu:.1f}' if avg_cpu is not None else '—')} | "
            f"{(f'{peak_cpu:.1f}' if peak_cpu is not None else '—')} | "
            f"{(f'{avg_gpu:.1f}' if avg_gpu is not None else '—')} |"
        )
    return "\n".join(lines) + "\n"


# ──────────────── Public entry point ────────────────
def write_all(facts, vault_dir=VAULT_DIR):
    """Regenerate every vault page from `facts` (typically `memory.facts`).

    Idempotent — safe to call many times. Returns dict of {filename: bytes_written}
    so callers can verify, but most callers ignore the return value.
    """
    pages = {
        "index.md":   render_index(facts),
        "user.md":    render_user(facts),
        "machine.md": render_machine(facts),
        "apps.md":    render_apps(facts),
    }
    written = {}
    for name, content in pages.items():
        path = os.path.join(vault_dir, name)
        _safe_write(path, content)
        written[name] = len(content)
    return written


def vault_summary(vault_dir=VAULT_DIR):
    """Return a small dict describing the vault on disk — for the BRAIN
    panel to show. Best-effort; returns empty dict if the vault doesn't
    exist yet.

    Shape:
        {
          "exists": bool,
          "path": str,
          "pages": [{"name": "apps.md", "size": 1234, "lines": 42}, ...],
          "total_bytes": int,
        }
    """
    out = {"exists": False, "path": vault_dir, "pages": [], "total_bytes": 0}
    if not os.path.isdir(vault_dir):
        return out
    out["exists"] = True
    try:
        for name in sorted(os.listdir(vault_dir)):
            full = os.path.join(vault_dir, name)
            if not os.path.isfile(full) or not name.endswith(".md"):
                continue
            try:
                size = os.path.getsize(full)
                with open(full, "r", encoding="utf-8") as f:
                    lines = sum(1 for _ in f)
                out["pages"].append({"name": name, "size": size, "lines": lines})
                out["total_bytes"] += size
            except OSError:
                continue
    except OSError:
        pass
    return out


# ──────────────── Self-test ────────────────
if __name__ == "__main__":
    from brain.memory import Memory
    m = Memory()
    m.set_user(name="max")
    m.set_machine_facts()
    info = write_all(m.facts)
    print("Wrote:")
    for k, v in info.items():
        print(f"  vault/{k}  ({v} bytes)")
    summary = vault_summary()
    print()
    print("Vault summary:", summary)
