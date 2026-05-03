import psutil
from collections import deque
from rich.text import Text

from panels.base import (health_for, fit_bar_width, fmt_bytes,
                          empty_color, heatmap_color, FILLED, EMPTY)
from theme import LABEL, SECONDARY, BRIGHT, MEDIUM


class RamPanel:
    def __init__(self, history_size=60):
        self.value = 0.0
        self.used = 0
        self.total = 0
        self.history = deque(maxlen=history_size)

    def update(self):
        mem = psutil.virtual_memory()
        self.value = mem.percent
        self.used = mem.used
        self.total = mem.total
        self.history.append(self.value)

    def render(self, width=None):
        if width is None:
            width = 30

        v = self.value
        fg = heatmap_color(v)
        bg = empty_color(v)
        bar_width = fit_bar_width(width, prefix_chars=0, suffix_chars=5,
                                   min_width=8, max_width=80)

        filled_count = int((v / 100) * bar_width)
        empty_count = bar_width - filled_count

        text = Text()
        text.append(FILLED * filled_count, style=fg)
        text.append(EMPTY * empty_count, style=bg)
        text.append(f" {int(round(v)):3d}%\n", style=fg)
        text.append(f"{fmt_bytes(self.used)}", style=BRIGHT)
        text.append(" of ", style=SECONDARY)
        text.append(f"{fmt_bytes(self.total)}", style=MEDIUM)
        return text

    def csv_headers(self):
        return ["ram_pct"]

    def csv_columns(self):
        return [self.value]