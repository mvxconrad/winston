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
from PyQt6.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt6.QtGui import QFont, QShortcut, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
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


class SystemView(QWidget):
    """Compact load/proc/thread/swap/IO/uptime summary — same data as
    the TUI's SystemPanel render, laid out as a small key/value grid."""
    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._label = QLabel("")
        self._label.setFont(_mono(9))
        self._label.setTextFormat(Qt.TextFormat.RichText)
        self._label.setStyleSheet(f"color: {MEDIUM};")
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._label, stretch=1)

    def refresh(self):
        from panels.base import fmt_bytes
        p = self.panel
        # Load color: scale by cpu_count so 100% load = bright.
        cpu_count = max(1, getattr(p, "cpu_count", 1))
        def lc(load):
            return heat_pct((load / cpu_count) * 100)
        # I/O dim when idle (<1KB/s).
        idle_io = (p.disk_read_rate < 1024 and p.disk_write_rate < 1024)
        io_color = DIM if idle_io else MEDIUM
        # Swap red if any.
        swap_color = heat_pct(p.swap_pct) if p.swap_pct > 0 else DIM
        # Uptime
        days = p.uptime_seconds // 86400
        hours = (p.uptime_seconds % 86400) // 3600
        mins = (p.uptime_seconds % 3600) // 60
        self._label.setText(
            f"<span style='color:{DIM};'>LOAD</span>  "
            f"<span style='color:{lc(p.load_1)}; font-weight:bold;'>{p.load_1:5.2f}</span> "
            f"<span style='color:{lc(p.load_5)};'>{p.load_5:5.2f}</span> "
            f"<span style='color:{lc(p.load_15)};'>{p.load_15:5.2f}</span>"
            f"<br><span style='color:{DIM};'>     1m    5m    15m</span>"
            f"<br><span style='color:{DIM};'>PROCS</span> "
            f"<span style='color:{MEDIUM};'>{p.proc_count}</span>  "
            f"<span style='color:{DIM};'>THR</span> "
            f"<span style='color:{MEDIUM};'>{p.thread_count}</span>"
            f"<br><span style='color:{DIM};'>SWAP</span>  "
            f"<span style='color:{swap_color};'>{p.swap_pct:.1f}%</span> "
            f"<span style='color:{DIM};'>{fmt_bytes(p.swap_used)} / {fmt_bytes(p.swap_total)}</span>"
            f"<br><span style='color:{DIM};'>I/O</span>   "
            f"<span style='color:{io_color};'>R {fmt_bytes(p.disk_read_rate)}/s</span>  "
            f"<span style='color:{io_color};'>W {fmt_bytes(p.disk_write_rate)}/s</span>"
            f"<br><span style='color:{DIM};'>UP</span>    "
            f"<span style='color:{MEDIUM};'>{int(days)}d {int(hours):02d}h {int(mins):02d}m</span>"
        )


class DiskView(QWidget):
    """Per-disk usage bars."""
    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(2)
        self._rows = []  # list of (label, bar, pct_label) per disk

    def refresh(self):
        from panels.base import fmt_bytes
        disks = getattr(self.panel, "disks", []) or []
        # Build/grow rows lazily; reuse on subsequent ticks.
        while len(self._rows) < len(disks):
            row = QHBoxLayout()
            row.setSpacing(6)
            lbl = QLabel("")
            lbl.setFont(_mono(9))
            lbl.setFixedWidth(28)
            bar = HeatBar()
            pct = QLabel("")
            pct.setFont(_mono(9))
            pct.setFixedWidth(40)
            pct.setAlignment(Qt.AlignmentFlag.AlignRight)
            row.addWidget(lbl)
            row.addWidget(bar, stretch=1)
            row.addWidget(pct)
            self._lay.addLayout(row)
            self._rows.append((lbl, bar, pct))
        # Hide extras if we got fewer disks than before.
        for i, (lbl, bar, pct) in enumerate(self._rows):
            if i < len(disks):
                label, kind, p, used, total = disks[i]
                lbl.setText(f"<span style='color:{BRIGHT};'>{label}</span>")
                lbl.setTextFormat(Qt.TextFormat.RichText)
                bar.setValue(p)
                color = heat_pct(p)
                pct.setText(f"<span style='color:{color};'>{int(round(p))}%</span>")
                pct.setTextFormat(Qt.TextFormat.RichText)
                lbl.show(); bar.show(); pct.show()
            else:
                lbl.hide(); bar.hide(); pct.hide()


class TempsView(QWidget):
    """Per-device temp bars — CPU / GPU / AIO / SSD / MOBO."""
    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(2)
        self._backend_lbl = QLabel("")
        self._backend_lbl.setFont(_mono(8))
        self._backend_lbl.setStyleSheet(f"color: {DIM};")
        self._lay.addWidget(self._backend_lbl)
        self._rows = []  # (label, bar, temp_label)

    def refresh(self):
        readings = getattr(self.panel, "readings", []) or []
        backend = getattr(self.panel, "backend", None)
        backend_label = {"native": "lm-sensors", "lhm": "LHM",
                         "wmi": "WMI"}.get(backend, "")
        self._backend_lbl.setText(f"via {backend_label}" if backend_label else "")

        while len(self._rows) < len(readings):
            row = QHBoxLayout()
            row.setSpacing(6)
            lbl = QLabel("")
            lbl.setFont(_mono(9))
            lbl.setFixedWidth(50)
            bar = HeatBar(max_val=110)
            tmp = QLabel("")
            tmp.setFont(_mono(9))
            tmp.setFixedWidth(50)
            tmp.setAlignment(Qt.AlignmentFlag.AlignRight)
            row.addWidget(lbl)
            row.addWidget(bar, stretch=1)
            row.addWidget(tmp)
            self._lay.addLayout(row)
            self._rows.append((lbl, bar, tmp))

        for i, (lbl, bar, tmp) in enumerate(self._rows):
            if i < len(readings):
                label, current, _high = readings[i]
                color = heat_temp(current)
                lbl.setTextFormat(Qt.TextFormat.RichText)
                lbl.setText(f"<span style='color:{color}; font-weight:bold;'>{label}</span>")
                bar.setValue(current)  # 0–110 is fine for typical sensors
                tmp.setTextFormat(Qt.TextFormat.RichText)
                tmp.setText(f"<span style='color:{color}; font-weight:bold;'>{int(round(current))}°C</span>")
                lbl.show(); bar.show(); tmp.show()
            else:
                lbl.hide(); bar.hide(); tmp.hide()


class NetworkView(QWidget):
    """DOWN / UP rates with mini live charts (one for each direction)."""
    HISTORY_POINTS = 120

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._down_text = QLabel("DOWN  ↓ 0.0 Mbps")
        self._down_text.setFont(_mono(9))
        self._down_text.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._down_text)

        self._down_plot = self._make_mini_plot()
        self._down_curve = self._down_plot.plot(
            [], pen=pg.mkPen(BRIGHT, width=1.5),
            fillLevel=0, brush=pg.mkBrush(124, 252, 0, 60),
        )
        layout.addWidget(self._down_plot)

        self._up_text = QLabel("UP    ↑ 0.0 Mbps")
        self._up_text.setFont(_mono(9))
        self._up_text.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._up_text)

        self._up_plot = self._make_mini_plot()
        self._up_curve = self._up_plot.plot(
            [], pen=pg.mkPen(BRIGHT, width=1.5),
            fillLevel=0, brush=pg.mkBrush(124, 252, 0, 60),
        )
        layout.addWidget(self._up_plot)

        self._totals = QLabel("TOTAL")
        self._totals.setFont(_mono(9))
        self._totals.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._totals)
        layout.addStretch(1)

    def _make_mini_plot(self):
        plot = pg.PlotWidget()
        plot.setBackground(BG)
        plot.setMouseEnabled(x=False, y=False)
        plot.hideAxis('bottom')
        plot.hideAxis('left')
        plot.setFixedHeight(28)
        return plot

    def refresh(self):
        from panels.base import fmt_bytes
        p = self.panel
        rx_mbps = (p.rx_rate * 8) / 1_000_000
        tx_mbps = (p.tx_rate * 8) / 1_000_000
        rx_peak = (p.peak_rx_rate * 8) / 1_000_000 if p.peak_rx_rate else 0
        tx_peak = (p.peak_tx_rate * 8) / 1_000_000 if p.peak_tx_rate else 0

        rx_hist = list(p.rx_history)[-self.HISTORY_POINTS:]
        tx_hist = list(p.tx_history)[-self.HISTORY_POINTS:]
        if rx_hist:
            self._down_curve.setData(list(range(len(rx_hist))), rx_hist)
        if tx_hist:
            self._up_curve.setData(list(range(len(tx_hist))), tx_hist)

        self._down_text.setText(
            f"<span style='color:{BRIGHT}; font-weight:bold;'>DOWN</span> "
            f"<span style='color:{BRIGHT};'>↓ {rx_mbps:.2f} Mbps</span>  "
            f"<span style='color:{DIM};'>peak {rx_peak:.0f} Mbps</span>"
        )
        self._up_text.setText(
            f"<span style='color:{BRIGHT}; font-weight:bold;'>UP</span>   "
            f"<span style='color:{BRIGHT};'>↑ {tx_mbps:.2f} Mbps</span>  "
            f"<span style='color:{DIM};'>peak {tx_peak:.0f} Mbps</span>"
        )
        self._totals.setText(
            f"<span style='color:{DIM};'>TOTAL</span>  "
            f"<span style='color:{MEDIUM};'>↓ {fmt_bytes(p.total_rx)}</span>  "
            f"<span style='color:{MEDIUM};'>↑ {fmt_bytes(p.total_tx)}</span>"
        )


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


# ──────────────── LLM commentary + ASK input ────────────────
class LlmBridge(QObject):
    """Bridges brain.client's worker-thread callbacks to Qt signals.

    brain.client streams chunks from a daemon thread (`winston-llm-worker`).
    Calling Qt widget methods directly from a non-UI thread is unsafe — but
    pyqtSignals are auto-marshaled to the receiver's thread via Qt's queued
    connections. So the worker just emits these signals; the slots run on
    the UI thread. Same role as Textual's `call_from_thread` in cli/display.py.
    """
    chunk = pyqtSignal(str)
    done = pyqtSignal(str)
    error = pyqtSignal()


class CommentaryView(QWidget):
    """Qt frontend for `brain.commentary_engine.CommentaryEngine`.

    All orchestration logic — state machine, Q&A history, trigger
    evaluation, heartbeat / stale-quiet, prompt building, model tiering —
    lives in the engine. This class only does:
      - Render the engine's state into HTML
      - Drive the engine's timers (typewriter, cursor blink, 1Hz triggers)
      - Marshal stream chunks from worker thread → engine via Qt signals
      - Translate engine "fire this prompt" results into actual
        `generate_stream_async` calls (with the bridge as the callback
        target)

    Same engine is shared with cli/display.py — see brain/commentary_engine.py
    for the contract.
    """
    CURSOR_BLINK_HZ = 2.5

    # Color-fade palette — newest message bright, oldest dim.
    FADE = ["#7CFC00", "#5fc05f", "#3aa83a", "#1a8c1a", "#0a5a0a", "#7f7f7f"]

    def __init__(self, llm_config, memory, sections, parent=None):
        super().__init__(parent)

        from brain.commentary_engine import CommentaryEngine
        self.engine = CommentaryEngine(sections, llm_config or {}, memory)

        # ── UI ──
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._text = QLabel("")
        self._text.setFont(_mono(10))
        self._text.setTextFormat(Qt.TextFormat.RichText)
        self._text.setWordWrap(True)
        self._text.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._text.setStyleSheet(f"color: {BRIGHT};")
        layout.addWidget(self._text, stretch=1)

        # Cursor blink for paint loop — purely a render concern.
        self._cursor_visible = True

        # ── LLM bridge ──
        # Stream callbacks fire from brain.client's worker thread; the
        # signals queue them onto the UI thread before they reach the
        # engine.
        self._bridge = LlmBridge()
        self._bridge.chunk.connect(self._on_chunk)
        self._bridge.done.connect(self._on_done)
        self._bridge.error.connect(self._on_error)

        # ── Lazy stream timers ──
        # Started in _begin_streaming, stopped in _on_message_finalized.
        self._cursor_timer = QTimer(self)
        self._cursor_timer.timeout.connect(self._toggle_cursor)
        self._typewriter_timer = QTimer(self)
        self._typewriter_timer.timeout.connect(self._typewriter_tick)

        # ── 1Hz trigger tick ──
        # Started after the startup ritual completes (or immediately if
        # startup_greeting is False). Drives engine.evaluate_triggers().
        self._trigger_timer = QTimer(self)
        self._trigger_timer.timeout.connect(self._trigger_tick)

        self._paint()

    # ──────────────── Public API ────────────────
    def start_startup_ritual(self):
        """Begin greeting → retrospective → regular loop. Called once by
        WinstonGui shortly after the window appears."""
        if self.engine.state == "DISABLED":
            return
        if self.engine.config.get("startup_greeting", True):
            self.engine.startup_step = "greeting"
            self._fire_step("greeting")
        else:
            self._begin_regular_loop()

    def ask_user(self, question):
        """User submitted a question via the ASK input. Routes it to the
        engine + fires a conversational prompt (quality tier)."""
        recorded = self.engine.handle_user_question(question)
        if recorded is None:
            return
        system, prompt, tier = self.engine.build_conversational(recorded)
        if system is None:
            self.engine.state = "ERROR"
            self._paint()
            return
        self._fire_stream(system, prompt, tier)

    # ──────────────── Internal: fire LLM streams ────────────────
    def _fire_stream(self, system, prompt, tier):
        """Common path for every LLM call. Engine begins the stream;
        we hand brain.client our bridge as the chunk/done/error sink."""
        from brain.client import generate_stream_async
        self.engine.begin_streaming()
        self._start_stream_timers()
        self._paint()
        model, keep_alive = self.engine.pick_model(tier)
        generate_stream_async(
            prompt, system=system, model=model, keep_alive=keep_alive,
            on_chunk=self._bridge.chunk.emit,
            on_done=self._bridge.done.emit,
            on_error=self._bridge.error.emit,
        )

    def _fire_step(self, step):
        """Fire the prompt for whichever startup step we're on."""
        if step == "greeting":
            system, prompt, tier = self.engine.build_greeting()
        else:  # retrospective
            system, prompt, tier = self.engine.build_retrospective()
        if system is None:
            # Skip this step — advance immediately.
            self._on_startup_step_done()
            return
        self._fire_stream(system, prompt, tier)

    def _begin_regular_loop(self):
        """Mirrors cli/display.py:CommentaryPanel._begin_regular_loop —
        init the trigger runner, fire one routine right away so the
        panel has fresh content, then start the 1Hz tick."""
        self.engine.startup_step = None
        self.engine.init_triggers()
        # Fire one observation as the "startup commentary" for parity
        # with the TUI behavior.
        system, prompt, tier = self.engine.build_observation()
        if system is not None:
            self._fire_stream(system, prompt, tier)
        # Start the 1Hz tick regardless. evaluate_triggers handles busy
        # gating internally.
        self._trigger_timer.start(1000)

    # ──────────────── Stream timers ────────────────
    def _start_stream_timers(self):
        if not self._cursor_timer.isActive():
            self._cursor_timer.start(int(1000 / self.CURSOR_BLINK_HZ))
        if not self._typewriter_timer.isActive():
            tps = self.engine.config.get("typewriter_tps", 25)
            self._typewriter_timer.start(int(1000 / tps))

    def _stop_stream_timers(self):
        self._cursor_timer.stop()
        self._typewriter_timer.stop()

    def _typewriter_tick(self):
        result = self.engine.typewriter_advance()
        if result == "advanced":
            self._paint()
        elif result == "finalize":
            self._on_message_finalized()

    def _toggle_cursor(self):
        if self.engine.state in ("THINKING", "STREAMING"):
            self._cursor_visible = not self._cursor_visible
            self._paint()

    def _on_message_finalized(self):
        """Stream finished and typewriter caught up. Stop blink/typewriter,
        repaint, and either advance the startup ritual or kick off the
        cooldown timer that lets the next event-driven message fire."""
        self._stop_stream_timers()
        self._paint()
        if self.engine.startup_step is not None:
            self._on_startup_step_done()
        else:
            pause_ms = int(self.engine.config.get(
                "inter_message_pause_sec", 2.0) * 1000)
            QTimer.singleShot(pause_ms, self.engine.end_cooldown)

    def _on_startup_step_done(self):
        if self.engine.startup_step == "greeting":
            self.engine.startup_step = "retrospective"
            pause_ms = int(self.engine.config.get(
                "inter_message_pause_sec", 2.0) * 1000)
            QTimer.singleShot(pause_ms, lambda: self._fire_step("retrospective"))
        else:  # "retrospective" (or anything else) → done with ritual
            pause_ms = int(self.engine.config.get(
                "inter_message_pause_sec", 2.0) * 1000)
            QTimer.singleShot(pause_ms, self._begin_regular_loop)

    # ──────────────── 1Hz trigger tick ────────────────
    def _trigger_tick(self):
        """Engine evaluates triggers + heartbeat + stale-quiet. We just
        fire whichever prompt it tells us to fire."""
        result = self.engine.evaluate_triggers()
        if result is None:
            return
        kind, payload = result
        if kind == "event":
            system, prompt, tier = self.engine.build_triggered(payload)
        else:  # "heartbeat" or "stale" — both fire a routine observation
            system, prompt, tier = self.engine.build_observation()
        if system is None:
            return
        self._fire_stream(system, prompt, tier)

    # ──────────────── Bridge slots (UI thread) ────────────────
    def _on_chunk(self, chunk):
        self.engine.on_chunk(chunk)
        self._paint()

    def _on_done(self, full_text):
        self.engine.on_done(full_text)

    def _on_error(self):
        self.engine.on_error()
        self._stop_stream_timers()
        self._paint()
        # If the failing call was part of the startup ritual, advance
        # anyway so the rest of the ritual can run.
        if self.engine.startup_step is not None:
            QTimer.singleShot(1500, self._on_startup_step_done)

    # ──────────────── Render ────────────────
    def _paint(self):
        e = self.engine
        if e.state == "DISABLED":
            self._text.setText(
                f"<span style='color:{DIM};'>analysis subsystem :: disabled "
                f"(LLM_ENABLED = False in config.py)</span>"
            )
            return

        lines = []
        history_count = len(e.history)
        for idx, entry in enumerate(e.history):
            if len(entry) == 3:
                ts, msg, kind = entry
            else:
                ts, msg = entry
                kind = "winston"
            distance = (history_count - 1) - idx
            color = self.FADE[min(distance + 1, len(self.FADE) - 1)]
            safe = self._html_escape(msg)
            if kind == "user":
                lines.append(
                    f"<span style='color:{DIM};'>{ts}</span>  "
                    f"<span style='color:{ACCENT}; font-weight:bold;'>?</span> "
                    f"<span style='color:#3a8a9c;'>{safe}</span>"
                )
            else:
                lines.append(
                    f"<span style='color:{DIM};'>{ts}</span>  "
                    f"<span style='color:{color}; font-weight:bold;'>&gt;</span> "
                    f"<span style='color:{color};'>{safe}</span>"
                )

        if e.state == "THINKING":
            cursor = "█" if self._cursor_visible else "&nbsp;"
            lines.append(
                f"<span style='color:{DIM};'>--:--:--</span>  "
                f"<span style='color:{BRIGHT}; font-weight:bold;'>&gt;</span> "
                f"<span style='color:{DIM};'>thinking…</span> "
                f"<span style='color:{BRIGHT};'>{cursor}</span>"
            )
        elif e.state == "STREAMING":
            ts = datetime.now().strftime("%H:%M:%S")
            visible = e.streaming_buffer[:e.typed_chars]
            safe = self._html_escape(visible)
            cursor = "█" if self._cursor_visible else "&nbsp;"
            lines.append(
                f"<span style='color:{DIM};'>{ts}</span>  "
                f"<span style='color:{BRIGHT}; font-weight:bold;'>&gt;</span> "
                f"<span style='color:{BRIGHT};'>{safe}</span>"
                f"<span style='color:{BRIGHT};'>{cursor}</span>"
            )
        elif e.state == "ERROR":
            lines.append(
                f"<span style='color:{DIM};'>--:--:--</span>  "
                f"<span style='color:{BRIGHT}; font-weight:bold;'>&gt;</span> "
                f"<span style='color:#cc1a1a;'>analysis error</span> "
                f"<span style='color:{DIM};'>(LLM unreachable)</span>"
            )

        self._text.setText("<br>".join(lines))

    @staticmethod
    def _html_escape(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;"))


class AskInput(QLineEdit):
    """Conversational input. `/` from anywhere in the window focuses it
    (handled by WinstonGui). Enter submits."""
    submitted = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("ask Winston something… (press / to focus)")
        self.setFont(_mono(10))
        self.setStyleSheet(f"""
            QLineEdit {{
                background: {BG};
                color: {ACCENT};
                border: 1px solid {BORDER};
                border-radius: 4px;
                padding: 6px 10px;
            }}
            QLineEdit:focus {{
                border: 1px solid {ACCENT};
            }}
        """)
        self.returnPressed.connect(self._on_return)

    def _on_return(self):
        text = self.text().strip()
        self.clear()
        if text:
            self.submitted.emit(text)


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

        # ── Row 1: CORES (2fr) | MEMORY (1fr) | SYSTEM (1fr) ──
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

        self._system = SystemView(by_cls["SystemPanel"])
        sys_frame = PanelFrame("SYSTEM")
        sys_frame.body().addWidget(self._system)
        row1.addWidget(sys_frame, stretch=1)

        root.addLayout(row1, stretch=2)

        # ── Row 2: DISK (1fr) | TEMPS (2fr) | GPU (2fr) ──
        row2 = QHBoxLayout()
        row2.setSpacing(6)

        self._disk = DiskView(by_cls["DiskPanel"])
        disk_frame = PanelFrame("DISK")
        disk_frame.body().addWidget(self._disk)
        row2.addWidget(disk_frame, stretch=1)

        self._temps = TempsView(by_cls["TempsPanel"])
        temps_frame = PanelFrame("TEMPS")
        temps_frame.body().addWidget(self._temps)
        row2.addWidget(temps_frame, stretch=2)

        self._gpu = GpuView(by_cls["GpuPanel"])
        gpu_frame = PanelFrame("GPU")
        gpu_frame.body().addWidget(self._gpu)
        row2.addWidget(gpu_frame, stretch=2)

        root.addLayout(row2, stretch=2)

        # ── Row 3: NETWORK (1fr) | PROCESSES (2fr) ──
        row3 = QHBoxLayout()
        row3.setSpacing(6)

        self._network = NetworkView(by_cls["NetworkPanel"])
        net_frame = PanelFrame("NETWORK")
        net_frame.body().addWidget(self._network)
        row3.addWidget(net_frame, stretch=1)

        self._processes = ProcessesView(by_cls["ProcessesPanel"])
        proc_frame = PanelFrame("PROCESSES")
        proc_frame.body().addWidget(self._processes)
        row3.addWidget(proc_frame, stretch=2)

        root.addLayout(row3, stretch=2)

        # ── COMMENTARY (LLM streaming) ──
        # Reuses brain.client / brain.prompt / brain.memory unchanged. The
        # CommentaryView wraps the same state machine we have in the TUI;
        # only the renderer differs.
        self._commentary = CommentaryView(
            llm_config=self.llm_config,
            memory=self.memory,
            sections=self.sections,
        )
        commentary_frame = PanelFrame("COMMENTARY")
        commentary_frame.body().addWidget(self._commentary)
        commentary_frame.setMinimumHeight(140)
        root.addWidget(commentary_frame, stretch=2)

        # ── ASK input ──
        # Press `/` from anywhere to focus (matches the TUI binding).
        self._ask = AskInput()
        self._ask.submitted.connect(self._commentary.ask_user)
        ask_frame = PanelFrame("ASK", accent=True)
        ask_frame.body().addWidget(self._ask)
        root.addWidget(ask_frame)

        # `/` focus is handled in keyPressEvent so the QLineEdit can still
        # receive `/` as literal input when it already has focus.

        # Footer
        footer = QLabel(
            f"<span style='color:{BRIGHT}; font-weight:bold;'>Q</span>"
            f" <span style='color:{DIM};'>quit</span> · "
            f"<span style='color:{BRIGHT}; font-weight:bold;'>R</span>"
            f" <span style='color:{DIM};'>reset</span> · "
            f"<span style='color:{BRIGHT}; font-weight:bold;'>/</span>"
            f" <span style='color:{DIM};'>ask</span> · "
            f"<span style='color:{BRIGHT}; font-weight:bold;'>F11</span>"
            f" <span style='color:{DIM};'>fullscreen</span> · "
            f"<span style='color:{BRIGHT}; font-weight:bold;'>Ctrl+↑/↓/←/→</span>"
            f" <span style='color:{DIM};'>snap</span>"
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
            id(by_cls["SystemPanel"]): self._system,
            id(by_cls["DiskPanel"]): self._disk,
            id(by_cls["TempsPanel"]): self._temps,
            id(by_cls["GpuPanel"]): self._gpu,
            id(by_cls["NetworkPanel"]): self._network,
            id(by_cls["ProcessesPanel"]): self._processes,
        }

        self._status_due_at = now + 1.0
        self._log_due_at = now + 1.0

        # ── GPU-busy throttle (mirrors cli/display.py) ──
        # When the GPU is hot from a game, we drop the dashboard's effective
        # refresh rate so the WSL2 process isn't fighting Windows for cycles.
        # Hysteresis prevents oscillation around the threshold.
        self._gpu_busy = False
        self._gpu_busy_since = None
        self._gpu_calm_since = None
        self._busy_skip_counter = 0
        self._gpu_panel = by_cls.get("GpuPanel")

        # Master frame loop. QTimer is millisecond-resolution — good
        # enough at 30fps (33ms intervals).
        import config as _cfg
        frame_hz = float(getattr(_cfg, "FRAME_HZ", 30.0))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._frame_tick)
        self._timer.start(int(1000 / frame_hz))

        # ── Startup ritual ──
        # Fire greeting + retrospective once Qt has finished laying out the
        # window. QTimer.singleShot(0, ...) defers to the next event-loop
        # iteration so the user sees the dashboard before the ritual starts.
        if self.llm_config.get("enabled") and self.llm_config.get("startup_greeting", True):
            QTimer.singleShot(300, self._commentary.start_startup_ritual)

    def _update_gpu_busy_state(self, now):
        """Return True if we should skip most frames this tick."""
        import config as _cfg
        busy_pct = getattr(_cfg, "GPU_BUSY_PCT", 50)
        busy_hold = getattr(_cfg, "GPU_BUSY_HOLD_SEC", 3.0)
        calm_hold = getattr(_cfg, "GPU_CALM_HOLD_SEC", 5.0)

        if self._gpu_panel is None or not getattr(self._gpu_panel, "gpus", None):
            return False
        util = self._gpu_panel.gpus[0].get("util") or 0

        if util > busy_pct:
            self._gpu_calm_since = None
            if self._gpu_busy_since is None:
                self._gpu_busy_since = now
            elif (not self._gpu_busy
                  and (now - self._gpu_busy_since) >= busy_hold):
                self._gpu_busy = True
        else:
            self._gpu_busy_since = None
            if self._gpu_calm_since is None:
                self._gpu_calm_since = now
            elif (self._gpu_busy
                  and (now - self._gpu_calm_since) >= calm_hold):
                self._gpu_busy = False
        return self._gpu_busy

    def _frame_tick(self):
        """Master tick. For each panel that's due, run update() on its
        data class then refresh its View widget. Status bar + logger are
        gated separately."""
        import time
        now = time.monotonic()

        # GPU-busy throttle: skip ~5 of every 6 frames when a game is hot.
        # Critical 1Hz items (status, logger) still fire because their own
        # due-time gating is below this skip — but they only ever land on
        # frames we DON'T skip, which is fine at this rate.
        if self._update_gpu_busy_state(now):
            self._busy_skip_counter += 1
            if self._busy_skip_counter % 6 != 0:
                return
            self._busy_skip_counter = 0

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
        # When the ASK input has focus, hand all keys back to it so typing
        # works normally (otherwise pressing 'q' inside the input would
        # close the window).
        if self._ask is not None and self._ask.hasFocus():
            super().keyPressEvent(event)
            return

        key = event.key()
        if event.text() == "/":
            self._ask.setFocus()
            return
        if key == Qt.Key.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            return

        # Snap shortcuts — substitute for Windows Aero Snap, which WSLg
        # windows don't get because they're not "real" Windows apps from
        # the OS's point of view. Ctrl+↑/↓/←/→ as window-manager parity.
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            screen = self.screen() or QApplication.primaryScreen()
            geo = screen.availableGeometry()
            half_w = geo.width() // 2
            if key == Qt.Key.Key_Up:
                self.showMaximized()
                return
            if key == Qt.Key.Key_Down:
                self.showNormal()
                self.resize(geo.width() * 3 // 4, geo.height() * 3 // 4)
                self.move(geo.x() + geo.width() // 8,
                          geo.y() + geo.height() // 8)
                return
            if key == Qt.Key.Key_Left:
                self.showNormal()
                self.setGeometry(geo.x(), geo.y(), half_w, geo.height())
                return
            if key == Qt.Key.Key_Right:
                self.showNormal()
                self.setGeometry(geo.x() + half_w, geo.y(), half_w, geo.height())
                return

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
    # Open maximized so the dashboard fills the screen by default. F11
    # toggles true fullscreen (no title bar) once running.
    win.showMaximized()
    app.exec()
