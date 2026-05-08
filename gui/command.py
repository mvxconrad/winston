"""Command tab — tactical overview for the Winston dashboard.

Green-on-black military palette. Three-column layout:
  Left:   Active triggers + hardware vitals + alert log
  Center: Winston HUD circle (concentric arcs + ticks + particles)
  Right:  Mini map placeholder + permissions + loaded tools

Performance notes:
  - HUD circle uses QPainter with pre-allocated colors and sin/cos LUTs.
  - Trigger/vitals refresh is gated to ~4Hz (every 8th frame tick at
    the default 30fps) since the underlying data only changes at 1-4Hz.
  - All animation math is pure Python floats — no allocations per frame.
  - State-aware: IDLE/LISTENING/THINKING/SPEAKING/ALERT change animation
    speed, tick spin rate, and color (green→red for ALERT).
"""
import math
import random
import time

from PyQt6.QtCore import Qt, QPointF, QTimer
from PyQt6.QtGui import (
    QFont, QPainter, QColor, QRadialGradient, QPen, QBrush,
    QLinearGradient,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QSizePolicy,
)

# ──────────────── Palette ────────────────
CMD_BG       = "#000000"
CMD_GREEN    = "#22c55e"
CMD_GREEN_DK = "#15803d"
CMD_GREEN_DM = "#0a3d1f"
CMD_CYAN     = "#0ea5e9"
CMD_RED      = "#ef4444"
CMD_AMBER    = "#f59e0b"
CMD_DIM      = "#6b7280"
CMD_BORDER   = "#1a3a2a"
CMD_PANEL_BG = "#050f08"

MONO_FAMILIES = ["JetBrains Mono", "Cascadia Mono", "DejaVu Sans Mono",
                 "Consolas", "Menlo", "monospace"]


def _mono(size=10):
    f = QFont()
    f.setFamilies(MONO_FAMILIES)
    f.setPointSize(size)
    return f


def _label(text, color=CMD_DIM, size=9, bold=False):
    lbl = QLabel(text)
    lbl.setFont(_mono(size))
    weight = "bold" if bold else "normal"
    lbl.setStyleSheet(f"color: {color}; font-weight: {weight};")
    lbl.setTextFormat(Qt.TextFormat.RichText)
    return lbl


# ──────────────── Panel frame ────────────────
class CmdPanel(QFrame):
    """Bordered panel container — command-tab palette."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(f"""
            CmdPanel {{
                border: 1px solid {CMD_BORDER};
                border-radius: 4px;
                background: {CMD_PANEL_BG};
            }}
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(2)

        self._title = QLabel(f"// {title}")
        self._title.setFont(_mono(8))
        self._title.setStyleSheet(f"color: {CMD_GREEN}; font-weight: bold;")
        outer.addWidget(self._title)

        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(2)
        outer.addLayout(self._body, stretch=1)

    def body(self):
        return self._body


# ──────────────── Trigger display ────────────────
class TriggersPanel(QWidget):
    """Shows all registered triggers with live status."""

    def __init__(self, trigger_config, sections, parent=None):
        super().__init__(parent)
        self._sections = sections
        self._runner = None
        self._config = trigger_config
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)

        self._rows = {}
        # Build rows immediately from config
        for name in trigger_config:
            row = _label("", CMD_DIM, 8)
            layout.addWidget(row)
            self._rows[name] = row
        layout.addStretch(1)

        # Create our own trigger runner for status display
        if trigger_config:
            try:
                from brain.triggers import TriggerRunner
                self._runner = TriggerRunner(trigger_config)
            except Exception:
                pass

    def refresh(self):
        if self._runner is None:
            return
        # Tick the runner to update baselines + evaluate
        try:
            event = self._runner.tick(self._sections)
            if event is not None:
                # Forward to alert log via parent
                parent = self.parent()
                while parent is not None:
                    if hasattr(parent, 'push_trigger_event'):
                        parent.push_trigger_event(event)
                        break
                    parent = parent.parent()
        except Exception:
            pass

        now = time.monotonic()
        for name, cfg in self._runner.config.items():
            lbl = self._rows.get(name)
            if lbl is None:
                continue
            enabled = cfg.get("enabled", True)
            last = self._runner._last_fired.get(name)
            cooldown = cfg.get("cooldown_sec", 60)
            severity = cfg.get("severity", "notable")
            pretty = name.replace("_", " ").upper()

            if not enabled:
                lbl.setText(
                    f"<span style='color:{CMD_DIM};'>[-] {pretty}</span>"
                )
                continue

            if last is not None and (now - last) < cooldown:
                remaining = int(cooldown - (now - last))
                lbl.setText(
                    f"<span style='color:{CMD_AMBER};'>[*] {pretty}</span>"
                    f" <span style='color:{CMD_DIM};'>{remaining}s</span>"
                )
            elif last is not None:
                ago = int(now - last)
                sev_color = CMD_RED if severity == "alert" else CMD_AMBER
                lbl.setText(
                    f"<span style='color:{sev_color};'>[!] {pretty}</span>"
                    f" <span style='color:{CMD_DIM};'>{ago}s ago</span>"
                )
            else:
                lbl.setText(
                    f"<span style='color:{CMD_GREEN};'>[+] {pretty}</span>"
                    f" <span style='color:{CMD_GREEN_DK};'>ARMED</span>"
                )


# ──────────────── Hardware vitals ────────────────
class VitalsPanel(QWidget):
    """Compact hardware stats — one line each."""

    def __init__(self, panels_by_cls, parent=None):
        super().__init__(parent)
        self._panels = panels_by_cls
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)

        self._cpu_lbl = _label("", CMD_GREEN, 9)
        self._ram_lbl = _label("", CMD_GREEN, 9)
        self._gpu_lbl = _label("", CMD_GREEN, 9)
        self._temp_lbl = _label("", CMD_GREEN, 9)
        self._net_lbl = _label("", CMD_GREEN, 9)

        for lbl in (self._cpu_lbl, self._ram_lbl, self._gpu_lbl,
                    self._temp_lbl, self._net_lbl):
            layout.addWidget(lbl)
        layout.addStretch(1)

    def refresh(self):
        p = self._panels

        cpu = p.get("CpuPanel")
        if cpu and cpu.values:
            avg = cpu.average
            self._cpu_lbl.setText(
                f"<span style='color:{CMD_DIM};'>CPU</span> "
                f"<span style='color:{self._heat(avg)}; font-weight:bold;'>"
                f"{avg:.0f}%</span>"
                f" <span style='color:{CMD_DIM};'>({len(cpu.values)}C)</span>"
            )

        ram = p.get("RamPanel")
        if ram:
            pct = ram.value
            used = ram.used / (1024**3) if hasattr(ram, 'used') else 0
            total = ram.total / (1024**3) if hasattr(ram, 'total') else 0
            self._ram_lbl.setText(
                f"<span style='color:{CMD_DIM};'>RAM</span> "
                f"<span style='color:{self._heat(pct)}; font-weight:bold;'>"
                f"{pct:.0f}%</span>"
                f" <span style='color:{CMD_DIM};'>{used:.1f}/{total:.1f}G</span>"
            )

        gpu = p.get("GpuPanel")
        if gpu and gpu.gpus:
            g = gpu.gpus[0]
            util = g.get("util", 0)
            vram_pct = (g.get("vram_used", 0) / g.get("vram_total", 1) * 100
                        ) if g.get("vram_total") else 0
            self._gpu_lbl.setText(
                f"<span style='color:{CMD_DIM};'>GPU</span> "
                f"<span style='color:{self._heat(util)}; font-weight:bold;'>"
                f"{util:.0f}%</span>"
                f" <span style='color:{CMD_DIM};'>VRAM {vram_pct:.0f}%</span>"
            )

        temps = p.get("TempsPanel")
        if temps and temps.readings:
            parts = []
            for label, current, _high in temps.readings[:3]:
                parts.append(
                    f"<span style='color:{CMD_DIM};'>{label}</span>"
                    f" <span style='color:{self._theat(current)};'>"
                    f"{current:.0f}C</span>"
                )
            self._temp_lbl.setText("  ".join(parts))

        net = p.get("NetworkPanel")
        if net:
            self._net_lbl.setText(
                f"<span style='color:{CMD_DIM};'>NET</span> "
                f"<span style='color:{CMD_CYAN};'>"
                f"{self._frate(net.rx_rate)}</span>"
                f"<span style='color:{CMD_DIM};'>/</span>"
                f"<span style='color:{CMD_GREEN};'>"
                f"{self._frate(net.tx_rate)}</span>"
            )

    @staticmethod
    def _heat(pct):
        if pct >= 90: return CMD_RED
        if pct >= 70: return CMD_AMBER
        return CMD_GREEN

    @staticmethod
    def _theat(t):
        if t >= 85: return CMD_RED
        if t >= 70: return CMD_AMBER
        return CMD_GREEN

    @staticmethod
    def _frate(bps):
        if bps >= 1_000_000: return f"{bps/1_000_000:.1f}MB/s"
        if bps >= 1_000: return f"{bps/1_000:.0f}KB/s"
        return f"{bps:.0f}B/s"


# ──────────────── Alert log ────────────────
class AlertLog(QWidget):
    MAX_ENTRIES = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        self._labels = []
        for _ in range(self.MAX_ENTRIES):
            lbl = _label("", CMD_DIM, 7)
            layout.addWidget(lbl)
            self._labels.append(lbl)
        layout.addStretch(1)

    def push_event(self, event):
        from datetime import datetime
        self._entries.append((datetime.now(), event))
        if len(self._entries) > self.MAX_ENTRIES:
            self._entries = self._entries[-self.MAX_ENTRIES:]
        self._redraw()

    def _redraw(self):
        for i, lbl in enumerate(self._labels):
            if i < len(self._entries):
                ts, ev = self._entries[-(i + 1)]
                t = ts.strftime("%H:%M:%S")
                sc = (CMD_RED if ev.severity == "alert"
                      else CMD_AMBER if ev.severity == "notable"
                      else CMD_GREEN_DK)
                desc = ev.description[:55]
                lbl.setText(
                    f"<span style='color:{CMD_DIM};'>{t}</span> "
                    f"<span style='color:{sc};'>[{ev.severity[0].upper()}]</span> "
                    f"<span style='color:{CMD_DIM};'>{desc}</span>"
                )
            else:
                lbl.setText("")


# ──────────────── Winston HUD Circle Visualization ────────────────

# Pre-allocated colors — avoids QColor construction every frame.
_C_GREEN_HI  = QColor(34, 197, 94, 220)
_C_GREEN_200 = QColor(34, 197, 94, 200)
_C_GREEN_150 = QColor(34, 197, 94, 150)
_C_GREEN_100 = QColor(34, 197, 94, 100)
_C_GREEN_80  = QColor(34, 197, 94, 80)
_C_GREEN_50  = QColor(34, 197, 94, 50)
_C_GREEN_30  = QColor(34, 197, 94, 30)
_C_GREEN_20  = QColor(34, 197, 94, 20)
_C_GREEN_12  = QColor(34, 197, 94, 12)
_C_CLEAR     = QColor(0, 0, 0, 0)
_C_RED_HI    = QColor(239, 68, 68, 220)
_C_RED_100   = QColor(239, 68, 68, 100)
_C_RED_50    = QColor(239, 68, 68, 50)

_PEN_NONE    = Qt.PenStyle.NoPen
_BRUSH_NONE  = Qt.BrushStyle.NoBrush

# Lookup table for sin — avoids math.sin calls per particle per frame.
_SIN_LUT_N = 256
_SIN_LUT = [math.sin(2 * math.pi * i / _SIN_LUT_N) for i in range(_SIN_LUT_N)]
_COS_LUT = [math.cos(2 * math.pi * i / _SIN_LUT_N) for i in range(_SIN_LUT_N)]

def _fast_sin(x):
    """Fast sine from lookup table."""
    return _SIN_LUT[int(x * _SIN_LUT_N / (2 * math.pi)) % _SIN_LUT_N]

def _fast_cos(x):
    """Fast cosine from lookup table."""
    return _COS_LUT[int(x * _SIN_LUT_N / (2 * math.pi)) % _SIN_LUT_N]

# ── State definitions ──
# Each state: (arc_speed, tick_energy, tick_extend, pulse_rate, color_mode)
#   tick_energy: how much force the gravity-bounce tick system has
#   color_mode: 0=green, 1=red
_STATES = {
    "IDLE":         (0.20, 0.22, 0.0, 0.8, 0),
    "LISTENING":    (0.25, 0.35, 0.3, 1.0, 0),
    "TRANSCRIBING": (0.30, 0.50, 0.4, 1.3, 0),
    "THINKING":     (0.50, 1.0,  1.0, 1.8, 0),
    "SPEAKING":     (0.35, 0.40, 0.4, 2.5, 0),
    "ALERT":        (0.70, 0.90, 0.8, 3.5, 1),
    "ERROR":        (0.20, 0.20, 0.1, 1.0, 1),
}

# Arc segment definitions — (radius_frac, start_deg, sweep_deg, osc_amp, osc_freq, direction)
#   direction: +1 = CW drift, -1 = CCW drift
_ARC_DEFS = [
    # Inner ring
    (0.60, 0.0,   55, 0.15, 0.7,  +1),
    (0.60, 120.0, 70, 0.20, 0.9,  -1),
    (0.60, 250.0, 45, 0.12, 1.1,  +1),
    # Middle ring
    (0.72, 30.0,  80, 0.18, 0.5,  -1),
    (0.72, 160.0, 60, 0.22, 0.8,  +1),
    (0.72, 270.0, 50, 0.14, 1.0,  -1),
    # Outer ring
    (0.84, 10.0,  65, 0.20, 0.6,  +1),
    (0.84, 200.0, 55, 0.16, 1.2,  -1),
]

# Tick mark angles — 24 ticks at 15° spacing (was 36 × 10°).
# Wider spacing keeps ticks from visually crowding when the gravity-
# bounce offset rotates the ring and pulse extends the tips.
# Every other tick is flagged as "minor" and drawn shorter/dimmer.
_TICK_ANGLES = [(i * 15.0, i % 2 == 1) for i in range(24)]
_TICK_RADIUS_FRAC = 0.90


class WinstonCore(QWidget):
    """Sci-fi HUD circle — concentric arc segments that oscillate,
    perpendicular tick marks that spin when thinking, breathing core,
    orbital particles. State-aware animation.

    Performance design:
      - All colors pre-allocated as module-level constants.
      - sin/cos use 256-entry lookup tables.
      - Arc/tick/particle state mutated in-place (no allocation per frame).
      - ~8 arcs, ~36 ticks, ~30 particles, 1 core glow = ~80 draw calls.
      - QPainter handles this trivially at 30fps.
    """

    def __init__(self, winston_state=None, show_label=True, parent=None):
        super().__init__(parent)
        self.setMinimumSize(60, 60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._t = 0.0
        self._state = "IDLE"
        self._state_params = _STATES["IDLE"]
        self._winston_state = winston_state

        # Smooth state transitions — lerp current params toward target
        self._cur_arc_speed = 0.12
        self._cur_tick_energy = 0.15
        self._cur_tick_extend = 0.0
        self._cur_pulse_rate = 0.6
        self._cur_color_mode = 0.0  # 0=green, 1=red (lerped)

        # ── Gravity-bounce tick physics ──
        # The tick ring has angular velocity + acceleration that simulates
        # a bouncing pendulum: drifts CW, decelerates, pauses, snaps back
        # CCW, decelerates again. Like gravity pulling it back and forth.
        self._tick_angle = 0.0       # current angle offset (degrees)
        self._tick_vel = 0.0         # angular velocity (deg/s)
        self._tick_phase = 0.0       # phase in the bounce cycle
        self._tick_bounce_amp = 25.0 # max angle swing (degrees)

        # Smoothed amplitude (EMA) for voice reactivity
        self._displayed_amp = 0.0

        # Pre-built color palettes (256 entries each) — avoids creating
        # QColor objects in paintEvent. One-time cost at init.
        self._pal_green = [QColor(70, 210, 150, a) for a in range(256)]
        self._pal_red = [QColor(239, 68, 68, a) for a in range(256)]

        # Particles — [angle, radius_frac, angular_speed, radial_osc_phase]
        rng = random.Random(7)
        self._particles = []
        for _ in range(18):
            self._particles.append([
                rng.uniform(0, 2 * math.pi),       # angle
                rng.uniform(0.35, 0.95),            # radius fraction
                rng.uniform(0.1, 0.5),              # angular speed (rad/s)
                rng.uniform(0, 2 * math.pi),        # radial oscillation phase
            ])

        # Optional state label at bottom (hidden for compact/orb modes)
        self._show_label = show_label
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if show_label:
            self._state_label = _label("IDLE", CMD_GREEN, 10, bold=True)
            layout.addStretch(1)
            layout.addWidget(self._state_label,
                             alignment=Qt.AlignmentFlag.AlignCenter)
            layout.addStretch(0)
        else:
            self._state_label = None

        # Subscribe to unified state if provided
        if winston_state is not None:
            winston_state.state_changed.connect(self.set_state)
            # Sync to current state immediately
            self.set_state(winston_state.state)

        # Self-driving animation timer at WINSTON_FPS from config.
        # Each WinstonCore instance runs its own timer — independent
        # of the dashboard's data-refresh frame loop.
        try:
            import config as _cfg
            fps = float(getattr(_cfg, "WINSTON_FPS", 60))
        except Exception:
            fps = 60.0
        self._anim_interval = max(1, int(1000 / fps))
        self._anim_dt = 1.0 / fps
        self._anim_timer = QTimer(self)
        self._anim_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim_timer.timeout.connect(self._auto_tick)
        self._anim_timer.start(self._anim_interval)

    def set_state(self, state_text):
        state_text = state_text.upper()
        if state_text in _STATES:
            self._state = state_text
            self._state_params = _STATES[state_text]
        if self._state_label is not None:
            color = CMD_RED if state_text == "ALERT" else CMD_GREEN
            self._state_label.setText(
                f"<span style='color:{color}; font-weight:bold;'>"
                f"{state_text}</span>"
            )

    def _auto_tick(self):
        """Called by the internal QTimer — delegates to tick().
        Skips if the widget is hidden (tab not active / orb hidden)."""
        if not self.isVisible():
            return
        self.tick(self._anim_dt)

    def tick(self, dt=0.017):
        self._t += dt
        sp = self._state_params

        # Lerp current params toward target (smooth transitions ~200ms)
        lerp = min(1.0, dt * 5.0)
        self._cur_arc_speed += (sp[0] - self._cur_arc_speed) * lerp
        self._cur_tick_energy += (sp[1] - self._cur_tick_energy) * lerp
        self._cur_tick_extend += (sp[2] - self._cur_tick_extend) * lerp
        self._cur_pulse_rate += (sp[3] - self._cur_pulse_rate) * lerp
        self._cur_color_mode += (sp[4] - self._cur_color_mode) * lerp

        # ── Amplitude (voice reactivity) ──
        target_amp = 0.0
        if self._winston_state is not None:
            target_amp = self._winston_state.amplitude
        # EMA: fast attack, slow decay
        if target_amp > self._displayed_amp:
            self._displayed_amp = 0.6 * self._displayed_amp + 0.4 * target_amp
        else:
            self._displayed_amp = 0.92 * self._displayed_amp + 0.08 * target_amp

        # ── Gravity-bounce tick physics ──
        # Think of a pendulum: swings CW, gravity pulls it back,
        # it overshoots CCW, gravity pulls it back again. The energy
        # parameter controls how far it swings.
        #
        # Phase drives a damped sine wave. At low energy (IDLE),
        # the swing is tiny and slow. At high energy (THINKING),
        # it's wide and fast with sharp reversals.
        energy = self._cur_tick_energy
        self._tick_phase += dt * (0.8 + energy * 2.5)

        # Base swing: sine with asymmetry (faster snap-back)
        phase = self._tick_phase
        raw_sin = _fast_sin(phase)
        # Sharpen the waveform: when positive (CW), ease out slowly;
        # when negative (CCW snap-back), move faster
        if raw_sin >= 0:
            swing = raw_sin * raw_sin  # slow ease CW
        else:
            swing = -(raw_sin * raw_sin)  # fast snap CCW

        swing_range = self._tick_bounce_amp * (0.3 + energy * 1.5)
        self._tick_angle = swing * swing_range

        # In THINKING state, add a fast jitter on top
        if energy > 0.7:
            jitter = _fast_sin(self._t * 18.0) * 3.0 * energy
            self._tick_angle += jitter

        # Update particles — in-place mutation
        amp_boost = 1.0 + self._displayed_amp * 3.0
        for p in self._particles:
            p[0] += p[2] * dt * (0.5 + self._cur_arc_speed) * amp_boost
            p[3] += dt * 1.5

        self.update()

    def paintEvent(self, event):
        w, h = self.width(), self.height()
        if w < 10 or h < 10:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # In floating orb mode, explicitly clear the backing store to
        # transparent BEFORE painting anything. Without this, Windows
        # DWM may retain stale/default grey pixels in the backing store
        # — the "grey rectangle" bug. CompositionMode_Clear writes
        # RGBA(0,0,0,0) regardless of the current brush/pen.
        if not self._show_label:
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(0, 0, w, h, Qt.GlobalColor.transparent)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver)

        # In floating orb mode, paint a soft dark disc behind the HUD.
        # Uses a radial gradient: dark center fading to transparent at the
        # edges for a softer, less harsh look. Not pure black — slightly
        # translucent so the orb blends gently into the desktop.
        if not self._show_label:
            disc_cx, disc_cy = w * 0.5, h * 0.5
            disc_r = min(w, h) * 0.46   # slightly larger than HUD elements
            disc_grad = QRadialGradient(QPointF(disc_cx, disc_cy), disc_r)
            disc_grad.setColorAt(0.0, QColor(5, 8, 5, 230))
            disc_grad.setColorAt(0.75, QColor(5, 8, 5, 220))
            disc_grad.setColorAt(0.92, QColor(5, 8, 5, 160))
            disc_grad.setColorAt(1.0, QColor(5, 8, 5, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(disc_grad))
            painter.drawEllipse(QPointF(disc_cx, disc_cy), disc_r, disc_r)

        cx, cy = w * 0.5, h * 0.5
        base_r = min(w, h) * 0.42  # base radius for scaling
        t = self._t
        cm = self._cur_color_mode  # 0=green, 1=red

        # Pre-built color palette — avoids creating QColor objects every
        # frame. 256 entries covering alpha 0-255 for the current color
        # mode. One allocation per frame instead of ~50.
        is_red = cm >= 0.5
        if is_red:
            _pal = self._pal_red
        else:
            _pal = self._pal_green

        def _clr(alpha, _unused=None):
            a = max(1, min(255, int(alpha)))
            return _pal[a]

        amp = self._displayed_amp
        amp_boost = 1.0 + amp * 5.0  # amplitude brightens everything

        # ── 0. Outer halo — big soft glow that gives "presence" ──
        breath = 0.5 + 0.5 * _fast_sin(t * self._cur_pulse_rate)
        halo_r = base_r * 1.3 * (1.0 + amp * 0.4)
        halo = QRadialGradient(QPointF(cx, cy), halo_r)
        halo_a = int((25 + 40 * breath + amp * 100))
        halo.setColorAt(0.0, _clr(halo_a))
        halo.setColorAt(0.4, _clr(int(halo_a * 0.4)))
        halo.setColorAt(1.0, _C_CLEAR)
        painter.setPen(_PEN_NONE)
        painter.setBrush(QBrush(halo))
        painter.drawEllipse(QPointF(cx, cy), halo_r, halo_r)

        # ── 1. Central breathing core (BIG, reactive, friendly) ──
        # The core is Winston's "eye" — warm, inviting, like a calm
        # glowing orb rather than a menacing surveillance dot.
        core_breath = 0.85 + 0.15 * breath + amp * 1.5
        core_r = base_r * 0.32 * core_breath

        # Layered glow: outer soft haze — bigger, warmer
        core_haze_r = core_r * 2.8
        core_haze = QRadialGradient(QPointF(cx, cy), core_haze_r)
        core_haze.setColorAt(0.0, _clr(int(min(255, 55 * core_breath))))
        core_haze.setColorAt(0.5, _clr(int(min(255, 20 * core_breath))))
        core_haze.setColorAt(1.0, _C_CLEAR)
        painter.setBrush(QBrush(core_haze))
        painter.drawEllipse(QPointF(cx, cy), core_haze_r, core_haze_r)

        # Inner glow — denser, brighter
        core_glow = QRadialGradient(QPointF(cx, cy), core_r * 1.5)
        core_glow.setColorAt(0.0, _clr(int(min(255, 120 * core_breath))))
        core_glow.setColorAt(0.6, _clr(int(min(255, 45 * core_breath))))
        core_glow.setColorAt(1.0, _C_CLEAR)
        painter.setBrush(QBrush(core_glow))
        painter.drawEllipse(QPointF(cx, cy), core_r * 1.5, core_r * 1.5)

        # Solid bright center — warm white-green blend
        bright_r = core_r * 0.55
        core_solid = QRadialGradient(QPointF(cx, cy), bright_r)
        center_a = int(min(255, (160 + amp * 300) * core_breath))
        # White-warm center fading to green — friendlier than pure green
        core_solid.setColorAt(0.0, QColor(180, 255, 210,
                                          min(255, center_a + 80)))
        core_solid.setColorAt(0.5, _clr(min(255, center_a + 30)))
        core_solid.setColorAt(1.0, _clr(int(center_a * 0.3)))
        painter.setBrush(QBrush(core_solid))
        painter.drawEllipse(QPointF(cx, cy), bright_r, bright_r)

        # Soft white glow when speaking (warm, not harsh)
        if amp > 0.02:
            pip_r = bright_r * 0.4 * min(1.0, amp * 5)
            painter.setBrush(QBrush(QColor(220, 255, 235,
                                           int(min(255, 100 + amp * 400)))))
            painter.drawEllipse(QPointF(cx, cy), pip_r, pip_r)

        # ── 2. Arc segments (different directions, BRIGHTER) ──
        for rf, start_deg, sweep_deg, osc_amp, osc_freq, direction in _ARC_DEFS:
            r = base_r * rf
            osc = osc_amp * _fast_sin(t * osc_freq * self._cur_arc_speed * 6.0)
            actual_start = start_deg + osc * 60.0
            actual_start += t * self._cur_arc_speed * 30.0 * direction

            # Brighter arcs — base alpha much higher
            tier_alpha = int(min(255, (120 + 80 * rf) * amp_boost))

            seg_count = max(3, int(sweep_deg / 10))
            seg_sweep = sweep_deg / seg_count
            for si in range(seg_count):
                frac = si / max(1, seg_count - 1)
                fade = 1.0 - abs(frac - 0.5) * 2.0
                fade = max(0.0, fade)
                a = int(min(255, tier_alpha * (0.15 + 0.85 * fade)))
                pen = QPen(_clr(a), 2.5)
                painter.setPen(pen)
                painter.setBrush(_BRUSH_NONE)
                rect_x = cx - r
                rect_y = cy - r
                arc_start = int((actual_start + si * seg_sweep) * 16)
                arc_span = int(seg_sweep * 16) + 1
                painter.drawArc(int(rect_x), int(rect_y),
                                int(2 * r), int(2 * r),
                                arc_start, arc_span)

        # ── 3. Perpendicular tick marks (gravity-bounce, BRIGHTER) ──
        tick_r = base_r * _TICK_RADIUS_FRAC
        tick_base_len = base_r * 0.10
        tick_extend_len = base_r * 0.16 * self._cur_tick_extend
        tick_amp_extend = base_r * 0.10 * amp
        offset = self._tick_angle

        _DEG2RAD = math.pi / 180.0
        for i, (base_angle, is_minor) in enumerate(_TICK_ANGLES):
            angle_rad = (base_angle + offset) * _DEG2RAD

            pulse = _fast_sin(t * self._cur_pulse_rate * 2.0 + i * 0.5)

            # Minor ticks are shorter and dimmer — keeps spacing clean
            if is_minor:
                tick_len = (tick_base_len * 0.5
                            + tick_extend_len * 0.3 * (0.5 + 0.5 * pulse)
                            + tick_amp_extend * 0.4)
            else:
                tick_len = (tick_base_len
                            + tick_extend_len * (0.5 + 0.5 * pulse)
                            + tick_amp_extend)

            dx = _fast_cos(angle_rad)
            dy = -_fast_sin(angle_rad)

            inner_x = cx + dx * (tick_r - tick_len * 0.3)
            inner_y = cy + dy * (tick_r - tick_len * 0.3)
            outer_x = cx + dx * (tick_r + tick_len * 0.7)
            outer_y = cy + dy * (tick_r + tick_len * 0.7)

            # Minor ticks dimmer; major ticks bright
            if is_minor:
                a = int(min(255, (50 + 60 * (0.5 + 0.5 * pulse)) * amp_boost))
                pen = QPen(_clr(a), 1.2)
            else:
                a = int(min(255, (100 + 100 * (0.5 + 0.5 * pulse)) * amp_boost))
                pen = QPen(_clr(a), 2.0)
            painter.setPen(pen)
            painter.drawLine(QPointF(inner_x, inner_y),
                             QPointF(outer_x, outer_y))

        # ── 4. Orbital particles (brighter, bigger) ──
        painter.setPen(_PEN_NONE)
        for angle, r_frac, _aspd, phase in self._particles:
            pr = base_r * (r_frac + 0.04 * _fast_sin(phase))
            px = cx + pr * _fast_cos(angle)
            py = cy - pr * _fast_sin(angle)
            a = int(min(255, (60 + 70 * (0.5 + 0.5 * _fast_sin(phase * 1.3)))
                        * amp_boost))
            painter.setBrush(QBrush(_clr(a)))
            painter.drawEllipse(QPointF(px, py), 2.2, 2.2)

        # ── 5. Guide rings (subtle but visible) ──
        ring_pen = QPen(_clr(25), 0.7)
        painter.setPen(ring_pen)
        painter.setBrush(_BRUSH_NONE)
        for rf in (0.60, 0.72, 0.84, _TICK_RADIUS_FRAC):
            r = base_r * rf
            painter.drawEllipse(QPointF(cx, cy), r, r)

        painter.end()


# ──────────────── Permissions panel ────────────────
class PermissionsPanel(QWidget):
    PERMISSIONS = [
        ("SYSTEM MONITOR", True),
        ("VOICE INPUT", True),
        ("VOICE OUTPUT", True),
        ("NETWORK ACCESS", False),
        ("FILE SYSTEM", False),
        ("PROCESS CTRL", False),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        for name, enabled in self.PERMISSIONS:
            color = CMD_GREEN if enabled else CMD_DIM
            icon = "+" if enabled else "-"
            lbl = _label(
                f"<span style='color:{color};'>[{icon}] {name}</span>",
                color, 8)
            layout.addWidget(lbl)
        layout.addStretch(1)


# ──────────────── Tools panel ────────────────
class ToolsPanel(QWidget):
    TOOLS = [
        ("SensorHub", "ACTIVE"),
        ("TriggerEngine", "ACTIVE"),
        ("Commentary", "ACTIVE"),
        ("VoiceInput", "ACTIVE"),
        ("VoiceOutput", "ACTIVE"),
        ("BrainPanel", "ACTIVE"),
        ("RECON", "STANDBY"),
        ("Claude API", "STANDBY"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        for name, status in self.TOOLS:
            color = CMD_GREEN if status == "ACTIVE" else CMD_AMBER
            lbl = _label(
                f"<span style='color:{CMD_DIM};'>></span> "
                f"<span style='color:{color};'>{name}</span> "
                f"<span style='color:{CMD_DIM};'>[{status}]</span>",
                CMD_DIM, 8)
            layout.addWidget(lbl)
        layout.addStretch(1)


# ──────────────── Map placeholder ────────────────
class MapPlaceholder(QWidget):
    """Simple grid/crosshair placeholder — replaces the ugly wireframe
    globe. Lightweight: just a few lines per paint, no trig."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(120, 100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def paintEvent(self, event):
        w, h = self.width(), self.height()
        if w < 10 or h < 10:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = w / 2, h / 2

        # Grid lines
        grid_pen = QPen(QColor(34, 197, 94, 15), 0.5)
        painter.setPen(grid_pen)
        step = 30
        for x in range(0, w, step):
            painter.drawLine(x, 0, x, h)
        for y in range(0, h, step):
            painter.drawLine(0, y, w, y)

        # Crosshair
        ch_pen = QPen(QColor(34, 197, 94, 50), 1.0)
        painter.setPen(ch_pen)
        painter.drawLine(int(cx) - 20, int(cy), int(cx) + 20, int(cy))
        painter.drawLine(int(cx), int(cy) - 20, int(cx), int(cy) + 20)
        painter.drawEllipse(QPointF(cx, cy), 15, 15)

        # "STANDBY" text
        painter.setPen(QPen(QColor(107, 114, 128, 80)))
        painter.setFont(_mono(8))
        painter.drawText(int(cx) - 30, int(cy) + 35, "// STANDBY")

        painter.end()


# ──────────────── Main Command Tab ────────────────
class CommandTab(QWidget):
    """Full Command tab widget — three-column layout."""

    def __init__(self, panels_by_cls, trigger_config=None,
                 winston_state=None, parent=None):
        super().__init__(parent)
        self._panels = panels_by_cls
        self._sections = list(panels_by_cls.values())
        self._refresh_counter = 0  # throttle vitals/triggers refresh
        self._winston_state = winston_state
        self.setStyleSheet(f"background: {CMD_BG};")

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(4)

        # ── Header ──
        header = QLabel(
            f"<span style='color:{CMD_GREEN}; font-weight:bold;'>WINSTON</span>"
            f"<span style='color:{CMD_DIM};'> // COMMAND CENTER</span>"
            f"<span style='color:{CMD_GREEN_DK};'> v0.9</span>"
        )
        header.setFont(_mono(10))
        root.addWidget(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {CMD_BORDER};")
        sep.setFixedHeight(1)
        root.addWidget(sep)

        # ── Three-column body ──
        body = QHBoxLayout()
        body.setSpacing(4)

        # ── LEFT COLUMN ──
        left = QVBoxLayout()
        left.setSpacing(4)

        triggers_frame = CmdPanel("TRIGGERS")
        self._triggers = TriggersPanel(
            trigger_config or {}, self._sections)
        triggers_frame.body().addWidget(self._triggers)
        left.addWidget(triggers_frame, stretch=2)

        vitals_frame = CmdPanel("VITALS")
        self._vitals = VitalsPanel(panels_by_cls)
        vitals_frame.body().addWidget(self._vitals)
        left.addWidget(vitals_frame, stretch=1)

        alerts_frame = CmdPanel("ALERT LOG")
        self._alert_log = AlertLog()
        alerts_frame.body().addWidget(self._alert_log)
        left.addWidget(alerts_frame, stretch=2)

        left_w = QWidget()
        left_w.setLayout(left)
        body.addWidget(left_w, stretch=1)

        # ── CENTER COLUMN ──
        center = QVBoxLayout()
        center.setSpacing(4)

        self._core = WinstonCore(winston_state=winston_state, show_label=True)
        center.addWidget(self._core, stretch=1)

        center_w = QWidget()
        center_w.setLayout(center)
        body.addWidget(center_w, stretch=2)

        # ── RIGHT COLUMN ──
        right = QVBoxLayout()
        right.setSpacing(4)

        map_frame = CmdPanel("RECON MAP")
        self._map = MapPlaceholder()
        map_frame.body().addWidget(self._map)
        right.addWidget(map_frame, stretch=2)

        perm_frame = CmdPanel("PERMISSIONS")
        self._permissions = PermissionsPanel()
        perm_frame.body().addWidget(self._permissions)
        right.addWidget(perm_frame, stretch=1)

        tools_frame = CmdPanel("MODULES")
        self._tools = ToolsPanel()
        tools_frame.body().addWidget(self._tools)
        right.addWidget(tools_frame, stretch=1)

        right_w = QWidget()
        right_w.setLayout(right)
        body.addWidget(right_w, stretch=1)

        root.addLayout(body, stretch=1)

        # ── Footer ──
        footer = QLabel(
            f"<span style='color:{CMD_GREEN}; font-weight:bold;'>TAB</span>"
            f" <span style='color:{CMD_DIM};'>switch</span>"
            f" <span style='color:{CMD_DIM};'>|</span> "
            f"<span style='color:{CMD_GREEN}; font-weight:bold;'>/</span>"
            f" <span style='color:{CMD_DIM};'>ask</span>"
            f" <span style='color:{CMD_DIM};'>|</span> "
            f"<span style='color:{CMD_GREEN}; font-weight:bold;'>F11</span>"
            f" <span style='color:{CMD_DIM};'>fullscreen</span>"
        )
        footer.setFont(_mono(8))
        root.addWidget(footer)

    def frame_tick(self, dt=0.033):
        """Called from the parent's frame loop for data refresh only.
        WinstonCore self-animates via its own QTimer at WINSTON_FPS."""
        # Throttle data refresh to ~4Hz (every 8th frame at 30fps)
        self._refresh_counter += 1
        if self._refresh_counter >= 8:
            self._refresh_counter = 0
            self._triggers.refresh()
            self._vitals.refresh()

    def push_trigger_event(self, event):
        self._alert_log.push_event(event)
