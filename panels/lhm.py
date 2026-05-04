"""Shared LHM (LibreHardwareMonitor) data poller.

Both the GPU panel (for hot-spot/memory junction temps) and the Temps panel
(for CPU/AIO/MOBO/SSD temps) need data from the LHM HTTP endpoint. Without
this module, each panel makes its own HTTP call from the UI thread —
about 10-50ms each, every few seconds. Two of them on the same tick can
freeze the UI for 100ms+, which feels like jank even though the per-panel
timing looks fine.

This module runs ONE background thread that fetches the LHM JSON every few
seconds and caches the result. Panels read the cache instantly — no
blocking, no duplicate requests, no jank.

If LHM isn't running, the poller silently retries on its interval. Panels
just see None and gracefully fall back to their other backends.
"""
import json
import os
import platform
import threading
import time
import urllib.error
import urllib.request


# How often the background poller hits LHM. LHM internally only updates
# its own sensors every ~1s, so polling faster than that is wasted work.
POLL_INTERVAL_SEC = 2.0
HTTP_TIMEOUT_SEC = 1.0


def _is_wsl():
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/version") as f:
            content = f.read().lower()
            return "microsoft" in content or "wsl" in content
    except OSError:
        return False


def _wsl_host_ip():
    """Find the Windows host's IP from inside WSL (None if not WSL)."""
    if not _is_wsl():
        return None
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    gw_hex = parts[2]
                    octets = [int(gw_hex[i:i+2], 16) for i in (6, 4, 2, 0)]
                    return ".".join(str(o) for o in octets)
    except (OSError, ValueError, IndexError):
        pass
    return None


# ──────────────── Singleton state ────────────────
# Module-global so all panels share one thread + cache. Started lazily on
# first get_data() call.
_lock = threading.Lock()
_thread = None
_stop_event = threading.Event()
_latest_data = None    # the parsed JSON tree from LHM, or None
_last_fetch_at = 0.0
_last_attempt_at = 0.0
_known_host = None     # IP that worked last time, try first


def _candidate_hosts():
    """In order of preference: known-good host, localhost, WSL gateway."""
    hosts = []
    if _known_host:
        hosts.append(_known_host)
    if "localhost" not in hosts:
        hosts.append("localhost")
    gw = _wsl_host_ip()
    if gw and gw not in hosts:
        hosts.append(gw)
    return hosts


def _fetch_once():
    """Try to fetch LHM JSON. Returns parsed dict or None.
    Side effect: updates _known_host if a host works."""
    global _known_host
    for host in _candidate_hosts():
        url = f"http://{host}:8085/data.json"
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SEC) as r:
                data = json.loads(r.read().decode())
                _known_host = host
                return data
        except (urllib.error.URLError, OSError,
                json.JSONDecodeError, TimeoutError):
            continue
    return None


def _poll_loop():
    global _latest_data, _last_fetch_at, _last_attempt_at
    while not _stop_event.is_set():
        _last_attempt_at = time.monotonic()
        data = _fetch_once()
        if data is not None:
            with _lock:
                _latest_data = data
                _last_fetch_at = time.monotonic()
        # Wait before next poll. Using event.wait so we can exit fast on stop.
        if _stop_event.wait(POLL_INTERVAL_SEC):
            return


def _ensure_started():
    """Start the polling thread on first call. Idempotent.

    WINSTON_NO_THREADS=1 or WINSTON_NO_LHM=1 disables it — kept for
    diagnosing input-drop issues. With no LHM thread, GPU hot-spot /
    memory-junction temps and the multi-device TEMPS panel will be empty,
    but the dashboard is otherwise unaffected.
    """
    if os.environ.get("WINSTON_NO_THREADS") or os.environ.get("WINSTON_NO_LHM"):
        return
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(
            target=_poll_loop,
            name="winston-lhm-poller",
            daemon=True,
        )
        _thread.start()


# ──────────────── Public API ────────────────
def get_data():
    """Return the latest LHM JSON tree, or None if not yet available.

    First call starts the background poller. Returns None if LHM isn't
    running yet (poller will keep trying). Subsequent calls return the
    cached value — the call is essentially free.
    """
    _ensure_started()
    with _lock:
        return _latest_data


def is_fresh(max_age_sec=10.0):
    """Has data been successfully fetched recently?"""
    with _lock:
        if _latest_data is None:
            return False
        return (time.monotonic() - _last_fetch_at) < max_age_sec


def shutdown():
    """Stop the polling thread (called from app shutdown if desired)."""
    _stop_event.set()