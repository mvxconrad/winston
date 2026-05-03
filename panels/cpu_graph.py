"""Aggregate CPU graph — data only. The plotext widget lives in display.py
and reads from this panel's history."""
import psutil
from collections import deque


class CpuGraphPanel:
    """Stores CPU history. The display layer uses it to build a scrolling graph."""
    HISTORY_SIZE = 480   # 8 minutes at 1Hz, plenty to support wider graphs

    def __init__(self):
        self.history = deque(maxlen=self.HISTORY_SIZE)
        self.last_value = 0.0

    def update(self):
        v = psutil.cpu_percent()
        self.last_value = v
        self.history.append(v)

    @property
    def average(self):
        if not self.history:
            return 0.0
        return sum(self.history) / len(self.history)

    @property
    def peak(self):
        if not self.history:
            return 0.0
        return max(self.history)

    def moving_average(self, window=10):
        """Return list of moving averages, one per data point."""
        live = list(self.history)
        if not live:
            return []
        result = []
        for i in range(len(live)):
            start = max(0, i - window + 1)
            segment = live[start:i + 1]
            result.append(sum(segment) / len(segment))
        return result

    # CSV logger compatibility
    def csv_headers(self):
        return ["cpu_aggregate"]

    def csv_columns(self):
        return [self.last_value]