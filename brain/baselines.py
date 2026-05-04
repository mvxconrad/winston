"""Rolling baselines for system metrics.

Triggers consult these to decide "is this current value unusual?". A
baseline tracks the last N samples of one metric and reports running mean
and standard deviation. Triggers compare current value against baseline:
"is current > 3 standard deviations above mean?" → unusual.

Why per-metric instead of one big stats blob? Different metrics have
different "interestingness" thresholds. CPU usage shifts wildly even at
idle (background processes). GPU temperature is much more stable. Having
each metric own its baseline lets the trigger logic stay simple.

This module only does math. It doesn't know what triggers exist or how
they decide things — that's brain/triggers.py.
"""
from collections import deque


class RollingBaseline:
    """Tracks the last N samples of a single metric.

    Provides running mean and stddev. We update on every observation but
    only compute stats lazily when asked — keeps push() fast (called at
    1Hz from the trigger loop) and stat queries cheap (only triggers
    that actually want them).
    """

    def __init__(self, window_size=300):
        # 300 samples at 1Hz = 5 minutes of history. That's enough to
        # establish "what's normal lately" without being so long that
        # legitimate state changes (started a game) take forever to
        # become the new baseline.
        self.window_size = window_size
        self._samples = deque(maxlen=window_size)

    def push(self, value):
        """Add a new sample. Drops the oldest if at capacity."""
        if value is None:
            return
        self._samples.append(float(value))

    def mean(self):
        """Running mean of samples in the window. None if no samples."""
        if not self._samples:
            return None
        return sum(self._samples) / len(self._samples)

    def stddev(self):
        """Running stddev. None if fewer than 2 samples."""
        if len(self._samples) < 2:
            return None
        m = self.mean()
        var = sum((x - m) ** 2 for x in self._samples) / (len(self._samples) - 1)
        return var ** 0.5

    def peak(self):
        """Highest value in the window. None if empty."""
        if not self._samples:
            return None
        return max(self._samples)

    def is_anomaly(self, value, sigma=3.0, min_samples=30):
        """Is `value` more than `sigma` stddevs above the running mean?

        Returns False if we don't have enough samples yet to be confident
        (default: need 30 samples, i.e. ~30 seconds of data at 1Hz).
        """
        if len(self._samples) < min_samples:
            return False
        m = self.mean()
        s = self.stddev()
        if s is None or s < 0.01:  # No variance — anomaly check meaningless
            return False
        return value > m + sigma * s

    def __len__(self):
        return len(self._samples)


class BaselineRegistry:
    """Container for all baselines we track. Keyed by metric name.

    Triggers ask the registry by name: registry.get('cpu_avg').is_anomaly(value).
    The trigger loop pushes samples on every tick: registry.push('cpu_avg', cpu).
    """

    def __init__(self, window_size=300):
        self.window_size = window_size
        self._baselines = {}

    def push(self, metric, value):
        """Add a sample. Creates the baseline lazily on first push."""
        if metric not in self._baselines:
            self._baselines[metric] = RollingBaseline(window_size=self.window_size)
        self._baselines[metric].push(value)

    def get(self, metric):
        """Return the baseline for a metric, or None if it's never been pushed."""
        return self._baselines.get(metric)