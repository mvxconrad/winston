"""Read the observation log to compute summary statistics.

The CSV log has every observation Winston has ever made, one row per second.
This module scans it efficiently and returns digested summaries — peaks,
averages over a time window — for the LLM prompt builder to use.

Why pre-digest? Feeding raw CSV to the LLM would blow context and confuse it.
A summary like "GPU peaked at 78C, RAM averaged 45%" is high-signal and
low-token. The LLM does NOT need 86,400 numbers to understand "yesterday
was a busy day."

Single-pass O(n) over the file. We never load everything into memory —
just stream rows and accumulate running stats.
"""
import csv
import os
from datetime import datetime, timedelta


# Cap the file scan at this size. At ~150 bytes/row that's ~350k rows
# = 4 days of 1Hz logging. Plenty for "last 24h" summaries.
MAX_LOG_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB


def _safe_float(s):
    """Parse a CSV cell to float, returning None if blank/invalid."""
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def summarize_recent(log_path="logs/raw/observations.csv", hours=24):
    """Return summary stats for the last N hours of observations.

    Output keys (any may be missing if the column wasn't logged that long ago):
      row_count        — number of rows in the window
      time_span_hours  — actual time span (may be < hours if log is younger)
      cpu_avg, cpu_peak — CPU% averages and peaks
      ram_avg, ram_peak — RAM% averages and peaks
      gpu_temp_peak    — peak GPU die temp C
      gpu_hot_peak     — peak GPU hot-spot C
      temp_max_peak    — peak max-of-anywhere temp C
      net_rx_peak_mbps — peak download rate
      net_tx_peak_mbps — peak upload rate

    Returns None if log can't be read.
    """
    if not os.path.exists(log_path):
        return None
    try:
        if os.path.getsize(log_path) > MAX_LOG_SIZE_BYTES:
            return None
    except OSError:
        return None

    cutoff = datetime.now() - timedelta(hours=hours)

    # Running accumulators
    cpu_sum, cpu_count, cpu_peak = 0.0, 0, 0.0
    ram_sum, ram_count, ram_peak = 0.0, 0, 0.0
    gpu_temp_peak = 0.0
    gpu_hot_peak = 0.0
    temp_max_peak = 0.0
    net_rx_peak = 0.0  # bytes/sec
    net_tx_peak = 0.0  # bytes/sec
    earliest_ts = None
    latest_ts = None
    row_count = 0

    try:
        with open(log_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                return None

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

                row_count += 1
                if earliest_ts is None or ts < earliest_ts:
                    earliest_ts = ts
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts

                cpu = _safe_float(row.get("cpu_avg"))
                if cpu is not None:
                    cpu_sum += cpu
                    cpu_count += 1
                    if cpu > cpu_peak:
                        cpu_peak = cpu

                ram = _safe_float(row.get("ram_pct"))
                if ram is not None:
                    ram_sum += ram
                    ram_count += 1
                    if ram > ram_peak:
                        ram_peak = ram

                gt = _safe_float(row.get("gpu_temp_die"))
                if gt is not None and gt > gpu_temp_peak:
                    gpu_temp_peak = gt

                gh = _safe_float(row.get("gpu_temp_hot"))
                if gh is not None and gh > gpu_hot_peak:
                    gpu_hot_peak = gh

                tm = _safe_float(row.get("temp_max_c"))
                if tm is not None and tm > temp_max_peak:
                    temp_max_peak = tm

                rx = _safe_float(row.get("net_rx_bps"))
                # Filter outliers (>10 Gbps = sensor glitch)
                if rx is not None and 0 < rx < 10_000_000_000 / 8 and rx > net_rx_peak:
                    net_rx_peak = rx

                tx = _safe_float(row.get("net_tx_bps"))
                if tx is not None and 0 < tx < 10_000_000_000 / 8 and tx > net_tx_peak:
                    net_tx_peak = tx
    except (OSError, csv.Error):
        return None

    if row_count == 0:
        return {"row_count": 0, "time_span_hours": 0.0}

    span_seconds = (latest_ts - earliest_ts).total_seconds() if earliest_ts and latest_ts else 0
    span_hours = span_seconds / 3600.0

    out = {
        "row_count": row_count,
        "time_span_hours": span_hours,
    }
    if cpu_count > 0:
        out["cpu_avg"] = cpu_sum / cpu_count
        out["cpu_peak"] = cpu_peak
    if ram_count > 0:
        out["ram_avg"] = ram_sum / ram_count
        out["ram_peak"] = ram_peak
    if gpu_temp_peak > 0:
        out["gpu_temp_peak"] = gpu_temp_peak
    if gpu_hot_peak > 0:
        out["gpu_hot_peak"] = gpu_hot_peak
    if temp_max_peak > 0:
        out["temp_max_peak"] = temp_max_peak
    if net_rx_peak > 0:
        out["net_rx_peak_mbps"] = (net_rx_peak * 8) / 1_000_000
    if net_tx_peak > 0:
        out["net_tx_peak_mbps"] = (net_tx_peak * 8) / 1_000_000

    return out


if __name__ == "__main__":
    # Quick test from project root
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "logs/raw/observations.csv"
    hours = float(sys.argv[2]) if len(sys.argv) > 2 else 24
    stats = summarize_recent(log_path=path, hours=hours)
    if stats is None:
        print(f"Log not found or unreadable: {path}")
    else:
        print(f"Last {hours}h summary from {path}:")
        for k, v in stats.items():
            if isinstance(v, float):
                print(f"  {k:<22} {v:.2f}")
            else:
                print(f"  {k:<22} {v}")