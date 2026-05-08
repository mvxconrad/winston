"""SensorHub — single source of truth for all hardware data.

Every panel's update() runs here, on one daemon thread. Both the orb
(gui/presence.py) and the dashboard (gui/main.py) read the same panel
objects — no duplicate polling, no second process.

Two tiers:
  essential   — panels needed for triggers + brain prompts. Always polled
                whenever Winston is running, even if no UI is visible.
  dashboard   — panels only useful when the dashboard is open. Activated
                on demand; deactivated when the dashboard closes.

Trigger-essential panels (always on):
  CpuPanel, RamPanel, SystemPanel, TempsPanel, GpuPanel,
  NetworkPanel, ProcessesPanel

Dashboard-only panels (on demand):
  CpuGraphPanel   — history for the load graph, no trigger reads it
  DiskPanel        — disk usage changes glacially, no trigger reads it

Usage:
  hub = SensorHub(sections, logger, config)
  hub.start()                 # begin polling essentials
  hub.activate_all()          # dashboard opened — poll everything
  hub.deactivate_extras()     # dashboard closed — drop to essentials
  hub.stop()                  # shutdown
"""

import threading
import time


# Panel class names that triggers / brain prompts need at all times.
_ESSENTIAL = frozenset({
    "CpuPanel",
    "RamPanel",
    "SystemPanel",      # swap_pct for memory_pressure trigger
    "TempsPanel",
    "GpuPanel",
    "NetworkPanel",
    "ProcessesPanel",
})


class SensorHub:
    """Central hardware poller. Owns the polling thread and the logger."""

    def __init__(self, sections, logger=None, config=None):
        """
        Args:
            sections: [(panel_instance, refresh_hz), ...] from winston.py
            logger:   Logger instance (optional)
            config:   config module (for LOGGER_HZ)
        """
        self._sections = sections
        self._panels = [p for p, _ in sections]
        self._logger = logger
        self._config = config

        # Rate tables
        self._intervals = {id(p): 1.0 / hz for p, hz in sections}
        self._due_at = {id(p): 0.0 for p, _ in sections}

        # Tier classification
        self._essential_ids = set()
        self._dashboard_ids = set()
        for p, _ in sections:
            cls_name = type(p).__name__
            if cls_name in _ESSENTIAL:
                self._essential_ids.add(id(p))
            else:
                self._dashboard_ids.add(id(p))

        # Active set — starts with essentials only
        self._active = set(self._essential_ids)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

        # Logger rate gating
        log_hz = getattr(config, "LOGGER_HZ", 1.0) if config else 1.0
        self._log_interval = 1.0 / max(0.1, log_hz)
        self._log_due_at = 0.0

    # ──────────────── Public accessors ────────────────
    @property
    def sections(self):
        """The (panel, hz) list — triggers, brain, and logger read this."""
        return self._sections

    @property
    def panels(self):
        """Flat list of panel instances."""
        return self._panels

    def panel_by_cls(self, cls_name):
        """Look up a panel by class name (e.g. 'CpuPanel')."""
        for p in self._panels:
            if type(p).__name__ == cls_name:
                return p
        return None

    # ──────────────── Lifecycle ────────────────
    def start(self):
        """Start the polling thread. Essentials-only by default."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="sensor-hub",
            daemon=True,
        )
        self._thread.start()

    def activate_all(self):
        """Dashboard opened — poll every panel."""
        with self._lock:
            self._active = self._essential_ids | self._dashboard_ids

    def deactivate_extras(self):
        """Dashboard closed — drop to essentials only."""
        with self._lock:
            self._active = set(self._essential_ids)

    def is_all_active(self):
        """True when all panels are being polled (dashboard open)."""
        with self._lock:
            return self._active == (self._essential_ids | self._dashboard_ids)

    def stop(self):
        """Signal the polling thread to exit."""
        self._stop.set()

    # ──────────────── Polling loop ────────────────
    def _poll_loop(self):
        """Daemon thread: poll active panels at their configured rates.

        Runs on its own thread so expensive calls (psutil.process_iter,
        PowerShell WMI) don't block the Qt event loop. Each panel update
        is followed by a 1ms GIL yield so PortAudio's callback thread
        (which needs the GIL for its Python-side speaker callback) can
        run without buffer underruns.
        """
        while not self._stop.is_set():
            now = time.monotonic()

            with self._lock:
                active = set(self._active)

            for panel, _hz in self._sections:
                pid = id(panel)
                if pid not in active:
                    continue
                if now < self._due_at[pid]:
                    continue
                self._due_at[pid] = now + self._intervals[pid]
                try:
                    panel.update()
                except Exception:
                    pass
                # Tiny GIL yield between panel updates. Without this,
                # a full panel sweep holds the GIL for 50-150ms and
                # PortAudio's callback thread can't grab it in time,
                # causing audio stutter.
                time.sleep(0.001)

            # Logger tick — writes CSV at LOGGER_HZ.
            if self._logger is not None and now >= self._log_due_at:
                self._log_due_at = now + self._log_interval
                try:
                    self._logger.log(self._panels)
                except Exception:
                    pass

            # Sleep just under the fastest panel's period so we don't
            # over-poll. 200ms is fine — even the snappiest panel
            # (CPU at 2Hz) only needs an update every 500ms.
            self._stop.wait(0.2)
