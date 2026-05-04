"""Network panel.

Two data sources:
- WSL/Linux psutil counters (only sees traffic INSIDE WSL — Chrome on
  Windows is invisible)
- Windows Get-NetAdapterStatistics via PowerShell (sees everything)

When running under WSL, prefer the Windows source. The PowerShell call
takes ~50ms which is too long to do synchronously on the UI thread at
2Hz — would visibly stall every render. So we run it in a background
thread that polls every 0.5s and the panel reads the latest cached
values.

Smoothing: rates are computed as the average over a 2-second rolling
window of (timestamp, total_bytes) samples. Avoids the spikes/zeros
you'd get from naive "delta since last call" math.
"""
import os
import platform
import shutil
import subprocess
import threading
import time
from collections import deque

import psutil
from rich.text import Text

from panels.base import braille_graph, fmt_rate, fmt_bytes
from theme import LABEL, SECONDARY, BRIGHT, MEDIUM, DIM


# Rolling window for rate smoothing
RATE_WINDOW_SEC = 2.0

# How often the background thread polls the data source.
#
# Why 5 seconds and not faster: each poll spawns a new PowerShell process
# (Get-NetAdapterStatistics) over the WSL→Windows boundary. Process
# creation alone is 50-200ms, and even though the work happens on a daemon
# thread (with the GIL released during the subprocess wait), the GIL
# handoffs around subprocess return are still enough to disrupt stdin
# reading on the asyncio loop. We tested 0.5s, 2s, and 5s on the same
# machine — 0.5s dropped ~1 in 20 keystrokes, 2s dropped occasional
# letters, 5s is clean.
#
# Why network is uniquely bad and other panels with threads aren't:
# pynvml (GPU) calls into a local Linux .so via ctypes — GIL releases
# cleanly, no process spawn. Network has to cross WSL→Windows for every
# sample, which is fundamentally heavier. The proper fix would be a long-
# lived PowerShell session piped over stdin, eliminating per-call churn.
POLL_INTERVAL_SEC = 5.0


def _is_wsl():
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/version", "r") as f:
            content = f.read().lower()
            return "microsoft" in content or "wsl" in content
    except OSError:
        return False


_POWERSHELL_FALLBACK_PATHS = [
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    "/mnt/c/Program Files/PowerShell/7/pwsh.exe",
    "/mnt/c/Windows/SysWOW64/WindowsPowerShell/v1.0/powershell.exe",
]


def _find_powershell():
    """Locate powershell.exe — PATH first, then known WSL fallback paths."""
    found = shutil.which("powershell.exe")
    if found:
        return found
    for candidate in _POWERSHELL_FALLBACK_PATHS:
        if os.path.exists(candidate):
            return candidate
    return None


def _fmt_mbps(bytes_per_sec):
    mbps = (bytes_per_sec * 8) / 1_000_000
    if mbps < 1:
        return f"{mbps:.2f} Mbps"
    elif mbps < 100:
        return f"{mbps:.1f} Mbps"
    else:
        return f"{mbps:.0f} Mbps"


class NetworkPanel:
    def __init__(self, history_size=120, prefer_windows=None,
                 log_path="logs/raw/observations.csv"):
        self.rx_rate = 0
        self.tx_rate = 0
        self.total_rx = 0
        self.total_tx = 0
        self.rx_history = deque(maxlen=history_size)
        self.tx_history = deque(maxlen=history_size)

        # (timestamp, total_bytes) sample buffers for smoothed rate calc
        self._rx_samples = deque(maxlen=20)
        self._tx_samples = deque(maxlen=20)

        # Latest counters from background thread (or psutil for non-WSL)
        # Access guarded by _lock since the thread writes and update() reads.
        self._lock = threading.Lock()
        self._latest = None  # (timestamp, rx_bytes, tx_bytes) or None

        # Observed peak rates (bytes/sec). Graphs scale to these so we can see
        # current usage as a fraction of the best we've ever achieved on this
        # network. Seeded from the CSV log on init so peaks persist across
        # sessions — first time you run a speedtest, the peak gets captured
        # forever and routine traffic stays visually small in comparison.
        self.peak_rx_rate, self.peak_tx_rate = self._load_peaks_from_log(log_path)

        # Powershell path — resolved once in init
        self._ps_path = None

        # Source selection
        if prefer_windows is None:
            prefer_windows = self._can_use_windows()
        self.prefer_windows = prefer_windows
        self.source = "windows" if prefer_windows else "wsl"

        # Start background polling thread for Windows source.
        # WINSTON_NO_THREADS=1 or WINSTON_NO_NET=1 disables it — kept for
        # diagnosing input-drop issues. Without the thread, network rates
        # stay frozen at zero, but the rest of the dashboard is unaffected.
        self._stop_event = threading.Event()
        self._thread = None
        _no_thread = (os.environ.get("WINSTON_NO_THREADS")
                      or os.environ.get("WINSTON_NO_NET"))
        if self.source == "windows" and not _no_thread:
            self._start_polling_thread()
        else:
            # For WSL/native source, prime with a first reading so the rate
            # buffer has data right away
            self._poll_once_local()

    def __del__(self):
        # Best-effort cleanup if Python collects us
        try:
            self._stop_event.set()
        except Exception:
            pass

    @property
    def title(self):
        if self.source == "windows":
            return "NETWORK (host)"
        return "NETWORK (wsl)"

    # ──────────────── Persistent peak tracking ────────────────
    @staticmethod
    def _load_peaks_from_log(log_path):
        """Find historical peak RX/TX rates by scanning the CSV log.

        Single-pass O(n) max — we don't need a sort. Just walk every row
        and track the running maximum for each column. Way faster than
        sorting the whole file just to grab the largest value.

        Filters out:
        - Malformed rows (bad parse → skip the row)
        - Missing columns (older logs predate this column → return zeros)
        - Outliers above MAX_PLAUSIBLE_BPS (sensor glitches)

        Skips entirely if the file is over MAX_LOG_SIZE_BYTES — at that
        point the CSV needs SQLite anyway and we shouldn't pay a multi-
        second startup cost.

        Returns (peak_rx_rate, peak_tx_rate) in bytes/sec.
        """
        # 10 Gbps = ~1.25 GB/sec. Anything bigger is sensor noise, not a
        # legit peak. Filters out the occasional crazy reading that would
        # otherwise dominate the graph scale forever.
        MAX_PLAUSIBLE_BPS = 10_000_000_000 / 8

        # Cap file scan size — at ~150 bytes/row that's ~350k rows = 4 days
        # of 1Hz logging. Plenty before SQLite migration is warranted.
        MAX_LOG_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB

        if not os.path.exists(log_path):
            return 0, 0
        try:
            if os.path.getsize(log_path) > MAX_LOG_SIZE_BYTES:
                return 0, 0  # file's too big — start fresh, log will rotate later
        except OSError:
            return 0, 0

        peak_rx = 0
        peak_tx = 0
        try:
            import csv as _csv
            with open(log_path, "r", newline="") as f:
                reader = _csv.DictReader(f)
                # If header doesn't have our columns, no peaks to find.
                # (Older log format predates net_rx_bps / net_tx_bps.)
                if reader.fieldnames is None:
                    return 0, 0
                if "net_rx_bps" not in reader.fieldnames:
                    return 0, 0

                for row in reader:
                    # Bad rows just get skipped — no exceptions bubble up.
                    try:
                        rx = float(row.get("net_rx_bps", 0) or 0)
                        tx = float(row.get("net_tx_bps", 0) or 0)
                    except (ValueError, TypeError):
                        continue
                    if 0 < rx < MAX_PLAUSIBLE_BPS and rx > peak_rx:
                        peak_rx = rx
                    if 0 < tx < MAX_PLAUSIBLE_BPS and tx > peak_tx:
                        peak_tx = tx
        except (OSError, _csv.Error):
            return 0, 0

        return peak_rx, peak_tx

    # ──────────────── Source detection ────────────────
    def _can_use_windows(self):
        """Decide if we can use the Windows host as the data source.
        Tests powershell availability AND that the cmdlet returns data.
        """
        if not _is_wsl():
            return False
        self._ps_path = _find_powershell()
        if not self._ps_path:
            return False
        # Try a real call. If it works, we're good.
        sample = self._read_windows_counters_sync()
        return sample is not None

    # ──────────────── Background polling thread ────────────────
    def _start_polling_thread(self):
        # Seed an initial sample synchronously so the panel has data
        # immediately on first render (no "0.0 Mbps" startup state).
        self._poll_once_windows()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="winston-net-poller",
            daemon=True,
        )
        self._thread.start()

    def _poll_loop(self):
        while not self._stop_event.is_set():
            self._poll_once_windows()
            # Wait before next poll. Using event.wait so we can exit fast on stop.
            if self._stop_event.wait(POLL_INTERVAL_SEC):
                return

    def _poll_once_windows(self):
        result = self._read_windows_counters_sync()
        if result is None:
            return
        rx, tx = result
        now = time.monotonic()
        with self._lock:
            self._latest = (now, rx, tx)

    def _poll_once_local(self):
        try:
            counters = psutil.net_io_counters()
        except Exception:
            return
        now = time.monotonic()
        with self._lock:
            self._latest = (now, counters.bytes_recv, counters.bytes_sent)

    # ──────────────── PowerShell call (synchronous, slow) ────────────────
    def _read_windows_counters_sync(self):
        """Pull RX/TX byte totals via PowerShell. Returns (rx, tx) or None.
        ~50ms per call — never call from the UI thread."""
        ps_path = self._ps_path or _find_powershell()
        if not ps_path:
            return None
        self._ps_path = ps_path

        ps_cmd = (
            "Get-NetAdapterStatistics -ErrorAction SilentlyContinue | "
            "Where-Object {$_.ReceivedBytes -gt 0} | "
            "Measure-Object -Property ReceivedBytes,SentBytes -Sum | "
            "ForEach-Object { $_.Property + '=' + $_.Sum }"
        )
        try:
            result = subprocess.run(
                [ps_path, "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=2.0,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None

        rx, tx = None, None
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            try:
                v = int(val.strip())
                if key.strip() == "ReceivedBytes":
                    rx = v
                elif key.strip() == "SentBytes":
                    tx = v
            except ValueError:
                continue
        if rx is None or tx is None:
            return None
        return rx, tx

    # ──────────────── Per-tick update (called by display) ────────────────
    def update(self):
        """Read the latest counter sample from the background thread,
        feed it into the rolling smoothing buffer, and recompute rates.
        """
        # For WSL/native source, we don't have a thread — poll directly here
        # (psutil is fast, ~10us, no need for a thread)
        if self.source != "windows":
            self._poll_once_local()

        with self._lock:
            latest = self._latest

        if latest is None:
            return

        ts, rx, tx = latest
        self.total_rx = rx
        self.total_tx = tx

        # Add to rolling buffers (with dedup against most recent timestamp
        # — same sample arriving multiple ticks shouldn't spam the buffer)
        if not self._rx_samples or self._rx_samples[-1][0] != ts:
            self._rx_samples.append((ts, rx))
            self._tx_samples.append((ts, tx))

        # Compute smoothed rates
        self.rx_rate = self._smoothed_rate(self._rx_samples)
        self.tx_rate = self._smoothed_rate(self._tx_samples)

        # Update peak if current rate exceeds historical max. Apply same
        # outlier filter as the log loader so a sensor glitch can't define
        # the ceiling permanently.
        MAX_PLAUSIBLE_BPS = 10_000_000_000 / 8
        if 0 < self.rx_rate < MAX_PLAUSIBLE_BPS and self.rx_rate > self.peak_rx_rate:
            self.peak_rx_rate = self.rx_rate
        if 0 < self.tx_rate < MAX_PLAUSIBLE_BPS and self.tx_rate > self.peak_tx_rate:
            self.peak_tx_rate = self.tx_rate

        self.rx_history.append(self.rx_rate)
        self.tx_history.append(self.tx_rate)

    def _smoothed_rate(self, samples):
        """Average bytes/sec over the rolling window."""
        if len(samples) < 2:
            return 0
        # Find the oldest sample within the window
        latest_t = samples[-1][0]
        cutoff = latest_t - RATE_WINDOW_SEC
        window = [s for s in samples if s[0] >= cutoff]
        if len(window) < 2:
            window = list(samples)
        t0, b0 = window[0]
        t1, b1 = window[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0
        delta = max(0, b1 - b0)
        return delta / dt

    # ──────────────── Render ────────────────
    def _graph_max(self, peak_rate, history):
        """Y-axis max for a rate graph.

        Prefer the all-time observed peak (read from log on init, updated
        live as we go). Falls back to recent peak if we don't have a peak
        yet — first run with no log history.
        """
        if peak_rate > 0:
            return peak_rate
        # Cold-start fallback: scale to recent peak so graph isn't flat.
        if not history:
            return 64 * 1024
        return max(max(history), 64 * 1024)

    @staticmethod
    def _fmt_peak_label(bytes_per_sec):
        """Format a peak rate as 'XX Mbps' for inline display.
        Returns None if peak is unset (no historical data yet)."""
        if not bytes_per_sec:
            return None
        return _fmt_mbps(bytes_per_sec)

    def render(self, width=None):
        if width is None:
            width = 40

        text = Text()
        rx_peak_label = self._fmt_peak_label(self.peak_rx_rate)
        tx_peak_label = self._fmt_peak_label(self.peak_tx_rate)
        graph_w = max(20, width - 6)

        # ── DOWN row ──
        text.append("DOWN  ", style=LABEL)
        text.append("↓ ", style=SECONDARY)
        text.append(f"{_fmt_mbps(self.rx_rate):<12}", style=BRIGHT)
        text.append(" ", style=DIM)
        text.append(f"{fmt_rate(self.rx_rate):<12}", style=MEDIUM)
        # Capacity context: best download rate we've ever observed
        if rx_peak_label:
            text.append("peak ", style=DIM)
            text.append(rx_peak_label, style=DIM)
        text.append("\n")
        # Graph scaled to observed peak — saturating the wire fills the bar
        rx_max = self._graph_max(self.peak_rx_rate, self.rx_history)
        rx_row = braille_graph(self.rx_history, width=graph_w, height=1, max_val=rx_max)[0]
        text.append("      ", style=DIM)
        text.append(rx_row, style=BRIGHT)
        text.append("\n")

        text.append("\n")

        # ── UP row ──
        text.append("UP    ", style=LABEL)
        text.append("↑ ", style=SECONDARY)
        text.append(f"{_fmt_mbps(self.tx_rate):<12}", style=BRIGHT)
        text.append(" ", style=DIM)
        text.append(f"{fmt_rate(self.tx_rate):<12}", style=MEDIUM)
        if tx_peak_label:
            text.append("peak ", style=DIM)
            text.append(tx_peak_label, style=DIM)
        text.append("\n")
        tx_max = self._graph_max(self.peak_tx_rate, self.tx_history)
        tx_row = braille_graph(self.tx_history, width=graph_w, height=1, max_val=tx_max)[0]
        text.append("      ", style=DIM)
        text.append(tx_row, style=BRIGHT)
        text.append("\n")

        # ── TOTAL ──
        text.append("\n")
        text.append("TOTAL ", style=LABEL)
        text.append("↓", style=SECONDARY)
        text.append(f"{fmt_bytes(self.total_rx):>9}", style=MEDIUM)
        text.append("   ↑", style=SECONDARY)
        text.append(f"{fmt_bytes(self.total_tx):>9}", style=MEDIUM)

        return text

    def csv_headers(self):
        return ["net_rx_bps", "net_tx_bps"]

    def csv_columns(self):
        return [self.rx_rate, self.tx_rate]