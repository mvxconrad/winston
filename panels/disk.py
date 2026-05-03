import re
import psutil
from rich.text import Text

from panels.base import (health_for, fit_bar_width, fmt_bytes,
                          empty_color, heatmap_color, FILLED, EMPTY)
from theme import LABEL, SECONDARY, BRIGHT, MEDIUM, DIM


_WSL_MOUNT_RE = re.compile(r"^/mnt/([a-z])/?$")


def _classify(mount):
    """Return (label, kind) for a mount.
    kind = 'windows' for /mnt/c etc, 'wsl' for /, 'other' otherwise.
    """
    m = _WSL_MOUNT_RE.match(mount)
    if m:
        return (m.group(1).upper() + ":", "windows")
    if mount == "/":
        return ("WSL", "wsl")
    return (mount, "other")


class DiskPanel:
    def __init__(self):
        self.disks = []  # list of (label, kind, pct, used, total)

    def update(self):
        results = []
        try:
            partitions = psutil.disk_partitions(all=True)
        except Exception:
            partitions = []

        seen_mounts = set()
        for part in partitions:
            mount = part.mountpoint
            if mount in seen_mounts:
                continue
            if part.fstype in ("", "tmpfs", "devtmpfs", "proc", "sysfs", "cgroup",
                                "cgroup2", "pstore", "bpf", "tracefs", "debugfs",
                                "configfs", "fusectl", "mqueue", "hugetlbfs",
                                "binfmt_misc", "autofs", "ramfs", "rpc_pipefs",
                                "fuse.snapfuse", "squashfs", "overlay"):
                continue
            if mount.startswith(("/snap", "/proc", "/sys", "/dev", "/run", "/boot",
                                  "/usr/lib/wsl", "/init", "/mnt/wslg", "/mnt/wsl")):
                continue
            # Skip the WSL root (/) — it's not the disk that fills up,
            # the underlying C: drive is. Avoids confusion.
            if mount == "/":
                continue
            try:
                u = psutil.disk_usage(mount)
            except (PermissionError, OSError):
                continue
            if u.total < 100 * 1024 * 1024:
                continue
            seen_mounts.add(mount)
            label, kind = _classify(mount)
            results.append((label, kind, u.percent, u.used, u.total, mount))

        # Order: windows first, then other, then wsl
        def sort_key(d):
            label, kind, _pct, _used, _total, mount = d
            kind_order = {"windows": 0, "other": 1, "wsl": 2}.get(kind, 3)
            return (kind_order, label)
        results.sort(key=sort_key)

        self.disks = [(lbl, kind, p, u, t) for lbl, kind, p, u, t, _ in results[:5]]

    @property
    def title(self):
        """Dynamic panel title — singular vs plural based on disk count."""
        n = len(self.disks)
        return "DISK" if n == 1 else "DISKS"

    def _c_drive_free(self):
        """How much free space is on C: (where WSL's vhdx file lives).
        Returns bytes free, or None if we can't tell."""
        try:
            u = psutil.disk_usage("/mnt/c")
            return u.free
        except (FileNotFoundError, OSError):
            return None

    def render(self, width=None):
        if width is None:
            width = 30

        text = Text()
        if not self.disks:
            text.append("(no disks)", style=SECONDARY)
            return text

        label_w = max(len(d[0]) for d in self.disks)
        prefix_chars = label_w + 1
        suffix_chars = 5
        bar_width = fit_bar_width(width, prefix_chars, suffix_chars,
                                   min_width=6, max_width=80)

        for i, (label, kind, pct, used, total) in enumerate(self.disks):
            fg = heatmap_color(pct)
            bg = empty_color(pct)
            filled_count = int((pct / 100) * bar_width)
            empty_count = bar_width - filled_count

            text.append(f"{label:<{label_w}} ", style=LABEL)
            text.append(FILLED * filled_count, style=fg)
            text.append(EMPTY * empty_count, style=bg)
            text.append(f" {int(round(pct)):3d}%\n", style=fg)

            text.append(f"{' ' * (label_w + 1)}", style=DIM)
            text.append(f"{fmt_bytes(used)}", style=BRIGHT)
            text.append(" of ", style=SECONDARY)
            text.append(f"{fmt_bytes(total)}", style=MEDIUM)

            if i < len(self.disks) - 1:
                text.append("\n\n")
            else:
                text.append("\n")

        return text

    def csv_headers(self):
        return ["disk_main_pct"]

    def csv_columns(self):
        for label, kind, pct, _used, _total in self.disks:
            if kind != "wsl":
                return [pct]
        if self.disks:
            return [self.disks[0][2]]
        return [0.0]