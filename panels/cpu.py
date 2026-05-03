import psutil
from collections import deque
from rich.text import Text

from panels.base import bar_gauge, heatmap_color, empty_color, fit_bar_width, FILLED, EMPTY
from theme import DIM, MEDIUM, SECONDARY


class CpuPanel:
    """Per-core CPU panel — bars fill the panel."""
    def __init__(self, history_size=60, columns=2):
        self.values = []
        self.histories = []
        self.history_size = history_size
        self.columns = columns

    def update(self):
        self.values = psutil.cpu_percent(percpu=True)
        if not self.histories:
            self.histories = [deque(maxlen=self.history_size) for _ in self.values]
        for i, v in enumerate(self.values):
            self.histories[i].append(v)

    @property
    def average(self):
        return sum(self.values) / len(self.values) if self.values else 0.0

    def _smoothed(self, core_idx, window=8):
        """Moving average of the last `window` samples for a core.
        Smooths out kernel scheduler-driven flicker between cores.
        At 4Hz this is ~2 seconds of data."""
        if core_idx >= len(self.histories):
            return 0.0
        h = self.histories[core_idx]
        if not h:
            return 0.0
        recent = list(h)[-window:]
        return sum(recent) / len(recent)

    def render(self, width=None):
        text = Text()
        n = len(self.values)
        if n == 0:
            text.append("(no data)", style=SECONDARY)
            return text

        if width is None:
            width = 60

        gap = 2
        col_width = (width - gap * (self.columns - 1)) // self.columns

        # Layout per cell:  "NN ▓▓▓▓▓▓▓▓▓·············  100%"
        # prefix: "NN " = 3 chars
        # suffix: " 100%" = 5 chars (no decimal — saves space, lets bars be longer)
        prefix_chars = 3
        suffix_chars = 5
        bar_width = fit_bar_width(col_width, prefix_chars, suffix_chars,
                                   min_width=4, max_width=80)

        rows = (n + self.columns - 1) // self.columns
        for r in range(rows):
            for c in range(self.columns):
                idx = c * rows + r
                if idx >= n:
                    cell_w = prefix_chars + bar_width + suffix_chars
                    text.append(" " * cell_w)
                else:
                    v = self.values[idx]
                    # Bar uses a smoothed value over the last few samples so
                    # cores don't blip in and out. At 4Hz this averages 4-8
                    # samples = ~1-2 seconds. The displayed % is still LIVE
                    # so you see real-time changes, but the bar stays coherent.
                    smoothed = self._smoothed(idx)
                    fg = heatmap_color(smoothed)
                    bg = empty_color(smoothed)

                    # Bar reflects the smoothed value (avoids the jittery look)
                    filled_count = int((smoothed / 100) * bar_width)
                    empty_count = bar_width - filled_count

                    text.append(f"{idx:>2} ", style=SECONDARY)
                    text.append(FILLED * filled_count, style=fg)
                    text.append(EMPTY * empty_count, style=bg)
                    # The number on the right is the LIVE value, color from smoothed
                    text.append(f" {int(round(v)):3d}%", style=fg)
                if c < self.columns - 1:
                    text.append(" " * gap)
            text.append("\n")
        return text

    def csv_headers(self):
        headers = ["cpu_avg"]
        for i in range(len(self.values)):
            headers.append(f"core_{i}")
        return headers

    def csv_columns(self):
        cols = [self.average]
        cols.extend(self.values)
        return cols