"""System-level vitals: load avg, procs/threads, swap, disk I/O, uptime.

Designed to fit in a 9-line panel body. All sections inline-aligned.
"""
import os
import time
import psutil
from datetime import timedelta
from rich.text import Text

from panels.base import health_for, fmt_bytes, fmt_rate
from theme import LABEL, SECONDARY, BRIGHT, MEDIUM, DIM, heat_pct


class SystemPanel:
    def __init__(self):
        self.load_1 = 0.0
        self.load_5 = 0.0
        self.load_15 = 0.0
        self.proc_count = 0
        self.thread_count = 0
        self.swap_pct = 0.0
        self.swap_used = 0
        self.swap_total = 0
        self.disk_read_rate = 0
        self.disk_write_rate = 0
        self.uptime_seconds = 0
        self.cpu_count = psutil.cpu_count() or 1

        self._last_disk_io = None
        self._last_disk_time = None

    def update(self):
        try:
            self.load_1, self.load_5, self.load_15 = os.getloadavg()
        except (OSError, AttributeError):
            self.load_1 = self.load_5 = self.load_15 = 0.0

        count = 0
        threads = 0
        for p in psutil.process_iter(['num_threads']):
            count += 1
            try:
                threads += p.info['num_threads'] or 0
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        self.proc_count = count
        self.thread_count = threads

        try:
            sw = psutil.swap_memory()
            self.swap_pct = sw.percent
            self.swap_used = sw.used
            self.swap_total = sw.total
        except Exception:
            self.swap_pct = 0.0
            self.swap_used = 0
            self.swap_total = 0

        try:
            io = psutil.disk_io_counters()
            now = time.monotonic()
            if io and self._last_disk_io is not None and self._last_disk_time is not None:
                dt = now - self._last_disk_time
                if dt > 0:
                    dr = io.read_bytes - self._last_disk_io.read_bytes
                    dw = io.write_bytes - self._last_disk_io.write_bytes
                    self.disk_read_rate = max(0, dr / dt)
                    self.disk_write_rate = max(0, dw / dt)
            self._last_disk_io = io
            self._last_disk_time = now
        except Exception:
            self.disk_read_rate = 0
            self.disk_write_rate = 0

        self.uptime_seconds = time.time() - psutil.boot_time()

    def _load_color(self, load):
        ratio = (load / self.cpu_count) * 100
        return health_for(ratio)

    def render(self, width=None):
        text = Text()

        # ── Line 1: load averages ──
        text.append("LOAD ", style=LABEL)
        text.append(f"{self.load_1:5.2f}", style=f"bold {self._load_color(self.load_1)}")
        text.append(f"  {self.load_5:5.2f}", style=self._load_color(self.load_5))
        text.append(f"  {self.load_15:5.2f}\n", style=self._load_color(self.load_15))
        # Line 2: load labels
        text.append("     ", style=DIM)
        text.append(" 1m     5m    15m\n", style=DIM)

        # Line 3: procs / threads inline
        text.append("PROCS  ", style=LABEL)
        text.append(f"{self.proc_count:<5}", style=BRIGHT)
        text.append("THR  ", style=LABEL)
        text.append(f"{self.thread_count}\n", style=BRIGHT)

        # Line 4: SWAP (always show — if 0, that's still useful info)
        text.append("SWAP   ", style=LABEL)
        if self.swap_total > 0:
            swap_color = heat_pct(self.swap_pct)
            text.append(f"{self.swap_pct:4.1f}% ", style=f"bold {swap_color}")
            text.append(f"{fmt_bytes(self.swap_used)}/{fmt_bytes(self.swap_total)}\n",
                        style=MEDIUM)
        else:
            text.append("none\n", style=DIM)

        # Line 5: disk I/O (compact, both R and W on one line)
        text.append("I/O    ", style=LABEL)
        # If both rates are negligible (<1KB/s), fade to DIM
        active = (self.disk_read_rate + self.disk_write_rate) > 1024
        rw_style = MEDIUM if active else DIM
        text.append("R ", style=DIM)
        text.append(f"{fmt_rate(self.disk_read_rate):<10}", style=rw_style)
        text.append("W ", style=DIM)
        text.append(f"{fmt_rate(self.disk_write_rate)}\n", style=rw_style)

        # Line 6: uptime
        up = timedelta(seconds=int(self.uptime_seconds))
        days = up.days
        hours, rem = divmod(up.seconds, 3600)
        mins, _ = divmod(rem, 60)
        text.append("UP     ", style=LABEL)
        text.append(f"{days}d {hours:02d}h {mins:02d}m", style=BRIGHT)

        return text

    def csv_headers(self):
        return ["load_1", "load_5", "load_15", "proc_count",
                "swap_pct", "disk_read_bps", "disk_write_bps"]

    def csv_columns(self):
        return [self.load_1, self.load_5, self.load_15, self.proc_count,
                self.swap_pct, self.disk_read_rate, self.disk_write_rate]