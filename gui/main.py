"""WINSTON — PyQt6 desktop frontend.

The TUI (display.py) is the original render path; this is the GUI
alternative. Both consume the same panel data classes (panels/*) and
the same brain/* modules. Only the rendering layer differs.

Why a GUI: terminals get starved of redraws by Windows when the GPU
is hot from gaming, ASCII can only encode so much, and there's no
real scroll. PyQt6 + pyqtgraph give us a native window with GPU-
accelerated charts that hits 60fps even while gaming.

Architecture mirrors display.py closely:
- `WinstonGui` (QMainWindow) owns the master frame loop (a QTimer at
  FRAME_HZ). On each tick we walk the section list, fire `panel.update()`
  for any panel whose rate has elapsed, then refresh its widget.
- Each `*View` class is a small QWidget that wraps one panel data class
  and renders it with native Qt widgets / pyqtgraph plots.
- `theme.py` is reused as the single source of color truth — `heat_pct()`
  returns hex strings that Qt accepts directly in stylesheets.

Launch via `python3 winston_gui.py` (or eventually `python3 winston.py
--gui` once we unify the entry points).
"""
from collections import deque
from datetime import datetime
import platform
import socket

import psutil
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QProgressBar, QVBoxLayout, QWidget,
)

from theme import heat_pct, heat_temp


# ──────────────── Color palette (Qt-side bridge to theme.py) ────────────────
# Rich-style names ("bright_green", "grey50") aren't valid Qt CSS, so we
# bridge them to hex here. Heat colors come directly from theme.heat_pct/
# heat_temp which already return hex.
BG          = "#000000"
BRIGHT      = "#7CFC00"   # ~bright_green
MEDIUM      = "#33b033"   # ~green
DIM         = "#7f7f7f"   # ~grey50
ACCENT      = "#00d7d7"   # ~bright_cyan (used for ASK input + brain panel)
BORDER      = "#1f5f1f"   # subtle dark green for frames

MONO_FAMILIES = ["JetBrains Mono", "Cascadia Mono", "DejaVu Sans Mono",
                 "Consolas", "Menlo", "monospace"]


def _mono(size=10):
    f = QFont()
    f.setFamilies(MONO_FAMILIES)
    f.setPointSize(size)
    return f


# ──────────────── Reusable widgets ────────────────
class PanelFrame(QFrame):
    """Bordered panel container — visual equivalent of the TUI's round
    panel borders. One titled box per data panel."""

    def __init__(self, title, parent=None, accent=False):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        border_color = ACCENT if accent else BORDER
        title_color = ACCENT if accent else BRIGHT
        self.setStyleSheet(f"""
            PanelFrame {{
                border: 1px solid {border_color};
                border-radius: 6px;
                background: {BG};
            }}
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 6)
        outer.setSpacing(2)

        self._title = QLabel(f"─ {title} ─")
        self._title.setFont(_mono(9))
        self._title.setStyleSheet(f"color: {title_color}; font-weight: bold;")
        outer.addWidget(self._title)

        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(2)
        outer.addLayout(self._body)

    def body(self):
        return self._body

    def setTitle(self, text):
        self._title.setText(f"─ {text} ─")


class HeatBar(QProgressBar):
    """Progress bar whose fill color is driven by the value — mirrors
    the TUI's heat-mapped bars. Single source of truth: theme.heat_pct."""

    def __init__(self, parent=None, max_val=100):
        super().__init__(parent)
        self.setRange(0, int(max_val))
        self.setTextVisible(False)
        self.setFixedHeight(10)
        self._max_val = max_val
        self._apply(0)

    def setValue(self, value):
        v = max(0, min(self._max_val, int(value)))
        super().setValue(v)
        self._apply(v / self._max_val * 100 if self._max_val else 0)

    def _apply(self, pct):
        color = heat_pct(pct)
        self.setStyleSheet(f"""
            QProgressBar {{
                background: #0a1a0a;
                border: 1px solid {BORDER};
                border-radius: 2px;
            }}
            QProgressBar::chunk {{
                background: {color};
                border-radius: 1px;
            }}
        """)


# ──────────────── Panel views ────────────────
class CpuGraphView(QWidget):
    """Live CPU LOAD chart via pyqtgraph. Replaces the TUI's hand-rolled
    braille graph with a real anti-aliased line, GPU-accelerated."""

    HISTORY_SECONDS = 120
    HISTORY_HZ = 4

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._readout = QLabel("0.0%   avg 0.0%   peak 0.0%")
        self._readout.setFont(_mono(10))
        self._readout.setStyleSheet(f"color: {BRIGHT}; font-weight: bold;")
        layout.addWidget(self._readout)

        # Plot config — solid black bg, minimal axes, 0–100 fixed Y.
        pg.setConfigOptions(antialias=True)
        self._plot = pg.PlotWidget()
        self._plot.setBackground(BG)
        self._plot.setYRange(0, 100, padding=0)
        self._plot.setMouseEnabled(x=False, y=False)
        self._plot.showGrid(x=False, y=True, alpha=0.15)
        self._plot.getAxis('left').setPen(DIM)
        self._plot.getAxis('left').setTextPen(DIM)
        self._plot.getAxis('bottom').setPen(DIM)
        self._plot.getAxis('bottom').setTextPen(DIM)
        self._plot.hideAxis('bottom')
        layout.addWidget(self._plot, stretch=1)

        # Two curves: filled area underneath in cool color, line on top
        # in heat-mapped color (recolored on each refresh).
        self._curve = self._plot.plot(
            [], pen=pg.mkPen(BRIGHT, width=2),
            fillLevel=0, brush=pg.mkBrush(124, 252, 0, 60),
        )

    def refresh(self):
        history = list(self.panel.history)[-self.HISTORY_SECONDS * self.HISTORY_HZ:]
        if not history:
            return
        x = list(range(len(history)))
        self._curve.setData(x, history)

        cur = self.panel.last_value
        avg = self.panel.average
        peak = self.panel.peak
        color = heat_pct(cur)
        # Line color tracks current load — same heat semantic as the bars.
        self._curve.setPen(pg.mkPen(color, width=2))

        self._readout.setText(
            f"<span style='color:{color}; font-weight:bold;'>{cur:5.1f}%</span>"
            f"   <span style='color:{DIM};'>avg</span> "
            f"<span style='color:{MEDIUM};'>{avg:.1f}%</span>"
            f"   <span style='color:{DIM};'>peak</span> "
            f"<span style='color:{heat_pct(peak)};'>{peak:.1f}%</span>"
        )


class CoresView(QWidget):
    """Per-core grid of horizontal bars."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self._grid = QVBoxLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(2)
        self._rows = []  # list of (label, bar, value_label) per core

    def refresh(self):
        values = self.panel.values
        # Build rows lazily on first refresh once we know core count.
        if not self._rows:
            for i in range(len(values)):
                row = QHBoxLayout()
                row.setSpacing(6)
                idx_label = QLabel(f"{i:>2}")
                idx_label.setFont(_mono(9))
                idx_label.setStyleSheet(f"color: {DIM};")
                idx_label.setFixedWidth(20)
                bar = HeatBar()
                val_label = QLabel("0%")
                val_label.setFont(_mono(9))
                val_label.setStyleSheet(f"color: {DIM};")
                val_label.setFixedWidth(36)
                val_label.setAlignment(Qt.AlignmentFlag.AlignRight)
                row.addWidget(idx_label)
                row.addWidget(bar, stretch=1)
                row.addWidget(val_label)
                self._grid.addLayout(row)
                self._rows.append((idx_label, bar, val_label))

        for (idx_label, bar, val_label), v in zip(self._rows, values):
            bar.setValue(v)
            color = heat_pct(v)
            val_label.setText(f"{int(round(v))}%")
            val_label.setStyleSheet(f"color: {color};")


class MemoryView(QWidget):
    """RAM bar with size text below."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._bar = HeatBar()
        self._bar.setFixedHeight(14)
        layout.addWidget(self._bar)

        self._label = QLabel("0.0GB of 0.0GB")
        self._label.setFont(_mono(10))
        self._label.setStyleSheet(f"color: {MEDIUM};")
        layout.addWidget(self._label)
        layout.addStretch(1)

    def refresh(self):
        pct = getattr(self.panel, "value", 0) or 0
        self._bar.setValue(pct)
        used = getattr(self.panel, "used", 0) or 0
        total = getattr(self.panel, "total", 0) or 0
        gb = lambda b: b / (1024 ** 3)
        self._label.setText(f"{gb(used):.1f}GB of {gb(total):.1f}GB")


class GpuView(QWidget):
    """GPU util bar + power text + temp summary."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._name = QLabel("…")
        self._name.setFont(_mono(10))
        self._name.setStyleSheet(f"color: {BRIGHT}; font-weight: bold;")
        layout.addWidget(self._name)

        self._util = HeatBar()
        layout.addWidget(self._util)
        self._util_text = QLabel("GPU 0%")
        self._util_text.setFont(_mono(9))
        self._util_text.setStyleSheet(f"color: {MEDIUM};")
        layout.addWidget(self._util_text)

        self._vram = HeatBar()
        layout.addWidget(self._vram)
        self._vram_text = QLabel("VRAM 0.0GB of 0.0GB")
        self._vram_text.setFont(_mono(9))
        self._vram_text.setStyleSheet(f"color: {MEDIUM};")
        layout.addWidget(self._vram_text)

        self._temps = QLabel("CORE -- · HOTSPOT -- · VRAM --")
        self._temps.setFont(_mono(9))
        layout.addWidget(self._temps)
        layout.addStretch(1)

    def refresh(self):
        if not getattr(self.panel, "gpus", None):
            return
        g = self.panel.gpus[0]
        self._name.setText(g.get("name", "?"))

        util = g.get("util") or 0
        self._util.setValue(util)
        power = g.get("power")
        power_limit = g.get("power_limit")
        power_str = (f"{power:.0f}W of {power_limit:.0f}W"
                     if power and power_limit
                     else (f"{power:.0f}W" if power else ""))
        self._util_text.setText(
            f"GPU {int(round(util))}%   {power_str}"
        )

        mem_u = g.get("mem_used") or 0
        mem_t = g.get("mem_total") or 1
        vram_pct = (mem_u / mem_t * 100)
        self._vram.setValue(vram_pct)
        gb = lambda b: b / (1024 ** 3)
        self._vram_text.setText(
            f"VRAM {int(round(vram_pct))}%   {gb(mem_u):.1f}GB of {gb(mem_t):.1f}GB"
        )

        # Temps line — color each reading by its own value.
        die = self.panel.lhm_temps.get("core") or g.get("temp")
        hot = self.panel.lhm_temps.get("hot_spot")
        mem = self.panel.lhm_temps.get("memory")

        def fmt(label, val):
            if val is None:
                return f"<span style='color:{DIM};'>{label} --</span>"
            c = heat_temp(val)
            return f"<span style='color:{c}; font-weight:bold;'>{label} {int(round(val))}°C</span>"

        self._temps.setText(
            "  ".join(fmt(l, v) for l, v in
                     [("CORE", die), ("HOTSPOT", hot), ("VRAM", mem)])
        )


class ProcessesView(QWidget):
    """Top-N process table — PID / NAME / CPU% / MEM."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = QLabel("   PID  NAME                       CPU%      MEM")
        header.setFont(_mono(9))
        header.setStyleSheet(f"color: {DIM};")
        layout.addWidget(header)

        self._rows_label = QLabel("")
        self._rows_label.setFont(_mono(9))
        self._rows_label.setTextFormat(Qt.TextFormat.RichText)
        self._rows_label.setStyleSheet(f"color: {MEDIUM};")
        layout.addWidget(self._rows_label, stretch=1)

    def refresh(self):
        from panels.base import fmt_bytes
        lines = []
        for cpu, mem, name, pid in getattr(self.panel, "procs", []):
            color = heat_pct(cpu) if cpu >= 1 else DIM
            name_disp = (name[:22] + "…") if len(name) > 23 else name
            lines.append(
                f"<span style='color:{DIM};'>{pid:>6}</span>  "
                f"<span style='color:{BRIGHT if cpu > 50 else MEDIUM};'>{name_disp:<23}</span>  "
                f"<span style='color:{color}; font-weight:bold;'>{cpu:5.1f}%</span>  "
                f"<span style='color:{DIM};'>{fmt_bytes(mem):>7}</span>"
            )
        self._rows_label.setText("<br>".join(lines))


class StatusBarLabel(QLabel):
    """Top-row status — host / OS / uptime / time. 1Hz update."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(_mono(10))
        self.setStyleSheet(f"color: {BRIGHT}; padding: 2px 8px;")
        self.setTextFormat(Qt.TextFormat.RichText)
        self.refresh()

    def refresh(self):
        host = socket.gethostname()
        boot = datetime.fromtimestamp(psutil.boot_time())
        up = datetime.now() - boot
        days = up.days
        hours, rem = divmod(up.seconds, 3600)
        mins, _ = divmod(rem, 60)
        uptime = f"{days}d {hours:02d}h {mins:02d}m"
        now = datetime.now().strftime("%H:%M:%S")
        os_name = platform.system().upper()
        self.setText(
            f"<b>◤ WINSTON</b> <span style='color:{DIM};'>v0.9</span>"
            f"   <span style='color:{DIM};'>HOST</span> {host}"
            f"   <span style='color:{DIM};'>OS</span> {os_name}"
            f"   <span style='color:{DIM};'>UP</span> {uptime}"
            f"   <span style='color:{DIM};'>TIME</span> {now}"
        )


# ──────────────── Main window ────────────────
class WinstonGui(QMainWindow):
    """Master window. Owns the frame loop and the per-panel due-time
    bookkeeping — same pattern as display.py:WinstonApp but using QTimer
    instead of Textual's set_interval."""

    def __init__(self, sections, logger, llm_config=None, memory=None):
        super().__init__()
        self.setWindowTitle("Winston")
        self.resize(1500, 950)
        self.setStyleSheet(f"QMainWindow, QWidget {{ background: {BG}; color: {BRIGHT}; }}")

        self.sections = [s[0] for s in sections]
        self.section_rates = [s[1] for s in sections]
        self.logger = logger
        self.llm_config = llm_config or {}
        self.memory = memory

        # Resolve panel objects by class name so layout binding is robust
        # to section-list ordering changes upstream.
        by_cls = {type(p).__name__: p for p in self.sections}
        self._panels_by_cls = by_cls

        # ── Layout ──
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        self._status = StatusBarLabel()
        root.addWidget(self._status)

        # CPU LOAD — full-width
        self._cpu_graph = CpuGraphView(by_cls["CpuGraphPanel"])
        cpu_frame = PanelFrame("CPU LOAD")
        cpu_frame.body().addWidget(self._cpu_graph)
        cpu_frame.setMinimumHeight(180)
        root.addWidget(cpu_frame)

        # Row 1: CORES (2fr) | MEMORY | (placeholder for SYSTEM later)
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        self._cores = CoresView(by_cls["CpuPanel"])
        cores_frame = PanelFrame("CORES")
        cores_frame.body().addWidget(self._cores)
        row1.addWidget(cores_frame, stretch=2)

        self._memory = MemoryView(by_cls["RamPanel"])
        mem_frame = PanelFrame("MEMORY")
        mem_frame.body().addWidget(self._memory)
        row1.addWidget(mem_frame, stretch=1)

        # GPU sits in row 1 for the MVP; full layout (SYSTEM/DISK/TEMPS/
        # NETWORK) gets built out in the next iteration.
        self._gpu = GpuView(by_cls["GpuPanel"])
        gpu_frame = PanelFrame("GPU")
        gpu_frame.body().addWidget(self._gpu)
        row1.addWidget(gpu_frame, stretch=2)

        root.addLayout(row1, stretch=2)

        # Row 2: PROCESSES (full-width for now)
        self._processes = ProcessesView(by_cls["ProcessesPanel"])
        proc_frame = PanelFrame("PROCESSES")
        proc_frame.body().addWidget(self._processes)
        root.addWidget(proc_frame, stretch=3)

        # Footer
        footer = QLabel(
            f"<span style='color:{BRIGHT}; font-weight:bold;'>Q</span>"
            f" <span style='color:{DIM};'>quit</span> · "
            f"<span style='color:{BRIGHT}; font-weight:bold;'>R</span>"
            f" <span style='color:{DIM};'>reset graphs</span> · "
            f"<span style='color:{DIM};'>(GUI v0 — TUI panels not yet ported: SYSTEM / DISK / TEMPS / NETWORK / COMMENTARY / BRAIN / ASK)</span>"
        )
        footer.setFont(_mono(9))
        footer.setStyleSheet(f"padding: 2px 8px;")
        root.addWidget(footer)

        # ── Per-panel rate gating (mirrors display.py frame loop) ──
        import time
        now = time.monotonic()
        self._panel_intervals = {}
        self._panel_due_at = {}
        for panel, hz in zip(self.sections, self.section_rates):
            iv = 1.0 / hz
            self._panel_intervals[id(panel)] = iv
            self._panel_due_at[id(panel)] = now + iv

        # Map panel id -> the View widget that needs refreshing
        self._panel_view = {
            id(by_cls["CpuGraphPanel"]): self._cpu_graph,
            id(by_cls["CpuPanel"]): self._cores,
            id(by_cls["RamPanel"]): self._memory,
            id(by_cls["GpuPanel"]): self._gpu,
            id(by_cls["ProcessesPanel"]): self._processes,
        }

        self._status_due_at = now + 1.0
        self._log_due_at = now + 1.0

        # Master frame loop. QTimer is millisecond-resolution — good
        # enough at 30fps (33ms intervals).
        import config as _cfg
        frame_hz = float(getattr(_cfg, "FRAME_HZ", 30.0))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._frame_tick)
        self._timer.start(int(1000 / frame_hz))

    def _frame_tick(self):
        """Master tick. For each panel that's due, run update() on its
        data class then refresh its View widget. Status bar + logger are
        gated separately."""
        import time
        now = time.monotonic()

        for panel in self.sections:
            if id(panel) not in self._panel_view:
                continue
            if now < self._panel_due_at.get(id(panel), 0):
                continue
            self._panel_due_at[id(panel)] = now + self._panel_intervals[id(panel)]
            try:
                panel.update()
            except Exception:
                continue
            try:
                self._panel_view[id(panel)].refresh()
            except Exception:
                pass

        if now >= self._status_due_at:
            self._status_due_at = now + 1.0
            self._status.refresh()

        if now >= self._log_due_at:
            self._log_due_at = now + 1.0
            try:
                self.logger.log(self.sections)
            except Exception:
                pass

    def keyPressEvent(self, event):
        # Q = quit, R = reset graph history.
        key = event.key()
        if key == Qt.Key.Key_Q:
            self.close()
            return
        if key == Qt.Key.Key_R:
            for s in self.sections:
                for attr in ("history", "rx_history", "tx_history"):
                    h = getattr(s, attr, None)
                    if hasattr(h, "clear"):
                        h.clear()
                if hasattr(s, "histories"):
                    for h in s.histories:
                        h.clear()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        try:
            self.logger.close()
        except Exception:
            pass
        super().closeEvent(event)


# ──────────────── Entry point ────────────────
def run(sections, logger, config=None):
    """Same signature as display.run() so winston.py can pick either
    frontend without further plumbing.

    Primes panels synchronously (so the first frame has real data),
    then hands off to the Qt event loop.
    """
    if config is None:
        import config as default_config
        config = default_config

    print("WINSTON :: priming sensors...", end=" ", flush=True)

    psutil.cpu_percent(percpu=True)
    psutil.cpu_percent()
    for p in psutil.process_iter():
        try:
            p.cpu_percent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    for panel, _hz in sections:
        try:
            panel.update()
        except Exception:
            pass

    print("ready.")

    # Mirror display.run()'s llm_config + memory bootstrap so the GUI
    # path has the same parameters available even though we don't wire
    # the LLM pieces in MVP yet.
    llm_config = {
        "enabled":                 config.LLM_ENABLED,
        "model":                   config.LLM_MODEL,
        "use_tiered":              getattr(config, "LLM_USE_TIERED", False),
        "model_fast":              getattr(config, "LLM_MODEL_FAST", config.LLM_MODEL),
        "model_quality":           getattr(config, "LLM_MODEL_QUALITY", config.LLM_MODEL),
        "fast_keep_alive_sec":     getattr(config, "LLM_FAST_KEEP_ALIVE_SEC", 0),
        "quality_keep_alive_sec":  getattr(config, "LLM_QUALITY_KEEP_ALIVE_SEC", 0),
        "show_brain_panel":        getattr(config, "SHOW_BRAIN_PANEL", True),
        "user_name":               config.USER_NAME,
        "startup_greeting":        config.STARTUP_GREETING,
        "typewriter_tps":          config.TYPEWRITER_TPS,
        "inter_message_pause_sec": config.INTER_MESSAGE_PAUSE_SEC,
        "lines":                   config.COMMENTARY_LINES,
        "heartbeat_interval_sec":  config.HEARTBEAT_INTERVAL_SEC,
        "stale_quiet_threshold_sec": config.STALE_QUIET_THRESHOLD_SEC,
        "triggers":                config.TRIGGERS,
    }

    memory = None
    if config.LLM_ENABLED:
        try:
            from brain.memory import Memory
            memory = Memory()
            memory.set_user(name=config.USER_NAME)
            gpu_panel = next((p for p, _ in sections
                              if type(p).__name__ == "GpuPanel"), None)
            ram_panel = next((p for p, _ in sections
                              if type(p).__name__ == "RamPanel"), None)
            memory.set_machine_facts(gpu_panel=gpu_panel, ram_panel=ram_panel)
            memory.learn_from_log(hours=168)
            memory.save()
        except Exception as e:
            print(f"(memory init failed: {e!r} — continuing without it)")
            memory = None

    app = QApplication.instance() or QApplication([])
    win = WinstonGui(sections, logger, llm_config=llm_config, memory=memory)
    win.show()
    app.exec()
