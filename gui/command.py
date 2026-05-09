"""Command tab — tactical overview for the Winston dashboard.

Green-on-black military palette. Three-column layout:
  Left:   Active triggers + hardware vitals + alert log
  Center: Winston HUD circle (concentric arcs + ticks + particles)
  Right:  Mini map placeholder + permissions + loaded tools

Performance notes:
  - HUD circle uses QOpenGLWidget + GLSL shaders for GPU rendering.
    Falls back to QPainter if PyOpenGL is unavailable.
  - Trigger/vitals refresh is gated to ~4Hz (every 8th frame tick at
    the default 30fps) since the underlying data only changes at 1-4Hz.
  - GPU path: Python uploads ~10 uniforms per frame, shader does all math.
  - QPainter fallback: sin/cos LUTs, pre-allocated palettes, ~80 draw calls.
  - State-aware: IDLE/LISTENING/THINKING/SPEAKING/ALERT change animation
    speed, tick spin rate, and color (green→red for ALERT).
"""
import math
import random
import time

from PyQt6.QtCore import Qt, QPointF, QTimer
from PyQt6.QtGui import (
    QFont, QPainter, QColor, QRadialGradient, QPen, QBrush,
    QLinearGradient, QSurfaceFormat,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QSizePolicy,
)

# OpenGL imports — GPU-accelerated rendering for WinstonCore + GlobeWidget
try:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    from OpenGL.GL import (
        glClear, glClearColor, glViewport, glEnable, glDisable, glBlendFunc,
        glGenVertexArrays, glBindVertexArray, glGenBuffers, glBindBuffer,
        glBufferData, glEnableVertexAttribArray, glVertexAttribPointer,
        glDrawArrays, glUseProgram,
        glGetUniformLocation, glUniform1f, glUniform2f, glUniform1i,
        glCreateShader, glShaderSource, glCompileShader, glGetShaderiv,
        glGetShaderInfoLog, glCreateProgram, glAttachShader, glLinkProgram,
        glGetProgramiv, glGetProgramInfoLog, glDeleteShader,
        GL_COLOR_BUFFER_BIT, GL_BLEND, GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA,
        GL_FLOAT, GL_FALSE, GL_TRIANGLE_STRIP, GL_ARRAY_BUFFER,
        GL_STATIC_DRAW, GL_VERTEX_SHADER, GL_FRAGMENT_SHADER,
        GL_COMPILE_STATUS, GL_LINK_STATUS, GL_TRUE,
    )
    import numpy as np
    _HAS_OPENGL = True
except ImportError:
    _HAS_OPENGL = False

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
    """Shows triggers with colored dots — green=armed, red=fired, dim=idle.
    Custom paintEvent matches the SVG render with dot indicators + status
    badges + summary footer line."""

    def __init__(self, trigger_config, sections, parent=None):
        super().__init__(parent)
        self._sections = sections
        self._runner = None
        self._config = trigger_config
        self._trigger_states = {}  # name -> ("armed"|"fired"|"cooldown"|"idle", extra)

        for name in trigger_config:
            self._trigger_states[name] = ("armed", 0)

        if trigger_config:
            try:
                from brain.triggers import TriggerRunner
                self._runner = TriggerRunner(trigger_config)
            except Exception:
                pass

        self.setMinimumHeight(max(60, len(trigger_config) * 18 + 28))

    def refresh(self):
        if self._runner is None:
            return
        try:
            event = self._runner.tick(self._sections)
            if event is not None:
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
            enabled = cfg.get("enabled", True)
            last = self._runner._last_fired.get(name)
            cooldown = cfg.get("cooldown_sec", 60)

            if not enabled:
                self._trigger_states[name] = ("idle", 0)
            elif last is not None and (now - last) < cooldown:
                self._trigger_states[name] = ("cooldown", int(cooldown - (now - last)))
            elif last is not None:
                self._trigger_states[name] = ("fired", int(now - last))
            else:
                self._trigger_states[name] = ("armed", 0)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        y = 4
        row_h = 17
        armed = fired = idle = 0

        for name in self._config:
            state, extra = self._trigger_states.get(name, ("armed", 0))
            pretty = name.replace("_", " ").upper()

            if state == "armed":
                dot_clr = QColor(34, 197, 94, 200)
                txt_clr = QColor(200, 214, 206)
                stat = "ARMED"
                stat_clr = QColor(34, 197, 94)
                armed += 1
            elif state in ("fired", "cooldown"):
                dot_clr = QColor(239, 68, 68, 230)
                txt_clr = QColor(239, 68, 68)
                stat = f"FIRED {extra}s" if state == "cooldown" else f"{extra}s AGO"
                stat_clr = QColor(239, 68, 68)
                fired += 1
            else:  # idle
                dot_clr = QColor(26, 46, 36)
                txt_clr = QColor(42, 74, 56)
                stat = "IDLE"
                stat_clr = QColor(42, 74, 56)
                idle += 1

            # Colored dot
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(dot_clr))
            painter.drawEllipse(QPointF(10, y + row_h * 0.5), 3.0, 3.0)

            # Trigger name
            painter.setPen(QPen(txt_clr))
            painter.setFont(_mono(8))
            painter.drawText(20, int(y + row_h - 4), pretty)

            # Status badge — right-aligned
            painter.setPen(QPen(stat_clr))
            painter.setFont(_mono(7))
            fm = painter.fontMetrics()
            sw = fm.horizontalAdvance(stat)
            painter.drawText(w - sw - 6, int(y + row_h - 4), stat)

            y += row_h

        # Separator + summary footer
        if self._config:
            painter.setPen(QPen(QColor(15, 31, 24), 0.5))
            painter.drawLine(6, int(y + 2), w - 6, int(y + 2))
            painter.setPen(QPen(QColor(26, 51, 40)))
            painter.setFont(_mono(6))
            summary = f"{armed} ARMED · {fired} FIRED · {idle} IDLE"
            painter.drawText(6, int(y + 14), summary)

        painter.end()


# ──────────────── Hardware vitals ────────────────

# Pre-allocated bar colors
_BAR_TRACK = QColor(10, 20, 16)
_BAR_GREEN = QColor(34, 197, 94, 190)
_BAR_AMBER = QColor(245, 158, 11, 190)
_BAR_RED   = QColor(239, 68, 68, 190)
_VIT_LABEL = QColor(63, 111, 80)
_VIT_VALUE = QColor(224, 239, 229)
_VIT_DETAIL = QColor(26, 51, 40)


class VitalsPanel(QWidget):
    """Hardware vitals with progress bars — matches SVG render.

    Each row: label | dark track bar with colored fill | percentage | detail.
    Custom paintEvent for pixel-perfect rendering.
    """

    def __init__(self, panels_by_cls, parent=None):
        super().__init__(parent)
        self._panels = panels_by_cls
        self._vitals = []  # [(label, pct, bar_color, detail), ...]
        self.setMinimumHeight(80)

    def refresh(self):
        p = self._panels
        vitals = []

        cpu = p.get("CpuPanel")
        if cpu and cpu.values:
            avg = cpu.average
            vitals.append(("CPU", avg, self._heat(avg),
                           f"{len(cpu.values)}C"))

        gpu = p.get("GpuPanel")
        if gpu and gpu.gpus:
            g = gpu.gpus[0]
            util = g.get("util", 0)
            vt = g.get("vram_total", 1) or 1
            vp = g.get("vram_used", 0) / vt * 100
            vitals.append(("GPU", util, self._heat(util),
                           f"VRAM {vp:.0f}%"))

        ram = p.get("RamPanel")
        if ram:
            pct = ram.value
            used = ram.used / (1024**3) if hasattr(ram, "used") else 0
            total = ram.total / (1024**3) if hasattr(ram, "total") else 0
            vitals.append(("RAM", pct, self._heat(pct),
                           f"{used:.1f}/{total:.1f}G"))

        temps = p.get("TempsPanel")
        if temps and temps.readings:
            for label, current, _high in temps.readings[:2]:
                pct_t = min(100.0, current)
                vitals.append((label[:4].upper(), pct_t,
                               self._theat(current), f"{current:.0f}°C"))

        net = p.get("NetworkPanel")
        if net:
            rx_pct = min(100.0, net.rx_rate / 100_000)  # ~100KB/s = full
            vitals.append(("NET", rx_pct, QColor(14, 165, 233, 190),
                           f"{self._frate(net.rx_rate)}/{self._frate(net.tx_rate)}"))

        self._vitals = vitals
        self.update()

    @staticmethod
    def _heat(pct):
        if pct >= 90: return _BAR_RED
        if pct >= 70: return _BAR_AMBER
        return _BAR_GREEN

    @staticmethod
    def _theat(t):
        if t >= 85: return _BAR_RED
        if t >= 70: return _BAR_AMBER
        return _BAR_GREEN

    @staticmethod
    def _frate(bps):
        if bps >= 1_000_000: return f"{bps/1_000_000:.1f}MB/s"
        if bps >= 1_000: return f"{bps/1_000:.0f}KB/s"
        return f"{bps:.0f}B/s"

    def paintEvent(self, event):
        if not self._vitals:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        row_h = 18
        lbl_w = 34
        bar_x = lbl_w + 4
        bar_w = max(20, w - bar_x - 72)
        bar_h = 9
        br = 2.0  # border-radius
        y = 2

        for label, pct, color, detail in self._vitals:
            # Label
            painter.setPen(QPen(_VIT_LABEL))
            painter.setFont(_mono(8))
            painter.drawText(4, int(y + row_h - 5), label)

            # Bar track
            bar_y = y + (row_h - bar_h) // 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(_BAR_TRACK))
            painter.drawRoundedRect(int(bar_x), int(bar_y),
                                    int(bar_w), bar_h, br, br)

            # Bar fill
            fill_w = max(0, int(bar_w * min(100.0, pct) / 100.0))
            if fill_w > 0:
                painter.setBrush(QBrush(color))
                painter.drawRoundedRect(int(bar_x), int(bar_y),
                                        fill_w, bar_h, br, br)

            # Percentage value
            painter.setPen(QPen(_VIT_VALUE))
            painter.setFont(_mono(8))
            pct_txt = f"{pct:.0f}%"
            pct_x = bar_x + bar_w + 4
            painter.drawText(int(pct_x), int(y + row_h - 5), pct_txt)

            # Detail (right side, dim)
            painter.setPen(QPen(_VIT_DETAIL))
            painter.setFont(_mono(6))
            painter.drawText(int(pct_x + 34), int(y + row_h - 5), detail)

            y += row_h

        painter.end()


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

# Lookup table for sin — used by QPainter fallback path.
_SIN_LUT_N = 256
_SIN_LUT = [math.sin(2 * math.pi * i / _SIN_LUT_N) for i in range(_SIN_LUT_N)]
_COS_LUT = [math.cos(2 * math.pi * i / _SIN_LUT_N) for i in range(_SIN_LUT_N)]

def _fast_sin(x):
    return _SIN_LUT[int(x * _SIN_LUT_N / (2 * math.pi)) % _SIN_LUT_N]

def _fast_cos(x):
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


_WinstonCoreBase = QOpenGLWidget if _HAS_OPENGL else QWidget

# ── GLSL shaders for GPU-accelerated WinstonCore ──

_CORE_VERT_SRC = """
#version 330 core
layout(location = 0) in vec2 aPos;
out vec2 vUV;
void main() {
    vUV = aPos * 0.5 + 0.5;
    gl_Position = vec4(aPos, 0.0, 1.0);
}
"""

_CORE_FRAG_SRC = """
#version 330 core
in vec2 vUV;
out vec4 fragColor;

// ── uniforms set per frame from Python ──
uniform vec2  uResolution;
uniform float uTime;
uniform float uArcSpeed;
uniform float uTickEnergy;
uniform float uTickExtend;
uniform float uPulseRate;
uniform float uColorMode;      // 0=green, 1=red
uniform float uAmplitude;
uniform float uTickAngle;       // gravity-bounce offset (degrees)
uniform int   uOrbMode;        // 1 = floating orb (dark disc bg), 0 = panel

// ── 8 arc definitions baked as constants (matches _ARC_DEFS) ──
// (radius_frac, start_deg, sweep_deg, osc_amp, osc_freq, direction)
const int   N_ARCS = 8;
const float ARC_RF[8]    = float[](0.60, 0.60, 0.60, 0.72, 0.72, 0.72, 0.84, 0.84);
const float ARC_START[8] = float[](0.0, 120.0, 250.0, 30.0, 160.0, 270.0, 10.0, 200.0);
const float ARC_SWEEP[8] = float[](55.0, 70.0, 45.0, 80.0, 60.0, 50.0, 65.0, 55.0);
const float ARC_OA[8]    = float[](0.15, 0.20, 0.12, 0.18, 0.22, 0.14, 0.20, 0.16);
const float ARC_OF[8]    = float[](0.7, 0.9, 1.1, 0.5, 0.8, 1.0, 0.6, 1.2);
const float ARC_DIR[8]   = float[](1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0);

// ── 18 particles baked (matches Python random.Random(7) sequence) ──
const int   N_PARTICLES = 18;
// Pre-computed from Random(7): angle, r_frac, ang_speed, phase
const float P_ANG[18] = float[](
    4.254, 0.789, 3.411, 5.921, 1.145, 0.530, 2.783, 4.812,
    0.195, 3.720, 5.509, 1.884, 6.050, 2.401, 4.673, 0.887,
    3.156, 5.263);
const float P_RF[18] = float[](
    0.656, 0.893, 0.413, 0.765, 0.520, 0.380, 0.841, 0.607,
    0.932, 0.478, 0.712, 0.559, 0.870, 0.445, 0.801, 0.636,
    0.388, 0.745);
const float P_SPD[18] = float[](
    0.282, 0.443, 0.167, 0.398, 0.212, 0.489, 0.134, 0.356,
    0.267, 0.411, 0.188, 0.461, 0.145, 0.323, 0.478, 0.201,
    0.389, 0.256);
const float P_PH[18] = float[](
    2.143, 4.891, 0.672, 3.567, 5.234, 1.445, 4.012, 2.789,
    0.334, 5.678, 3.123, 1.890, 4.567, 0.112, 2.678, 5.901,
    3.445, 1.234);

const float PI  = 3.14159265;
const float TAU = 6.28318530;
const float DEG2RAD = PI / 180.0;

// ── tick definitions: 24 ticks at 15 deg spacing ──
const int   N_TICKS = 24;
const float TICK_RADIUS_FRAC = 0.90;

vec3 mainColor(float cm) {
    vec3 green = vec3(0.275, 0.824, 0.588);  // #46D296
    vec3 red   = vec3(0.937, 0.267, 0.267);  // #EF4444
    return mix(green, red, cm);
}

float sdArc(vec2 p, float r, float startRad, float sweepRad) {
    // Signed distance to an arc segment on a ring of radius r
    float angle = atan(p.y, p.x);           // -PI..PI
    // Normalize angle relative to arc center
    float center = startRad + sweepRad * 0.5;
    float da = mod(angle - center + PI, TAU) - PI;  // -PI..PI
    float halfSweep = sweepRad * 0.5;
    float angDist = abs(da) - halfSweep;
    float radDist = abs(length(p) - r);
    if (angDist <= 0.0) {
        return radDist;
    }
    // Distance to arc endpoint
    float endAng = center + sign(da) * halfSweep;
    vec2 ep = r * vec2(cos(endAng), sin(endAng));
    return length(p - ep);
}

void main() {
    vec2 px = gl_FragCoord.xy;
    vec2 res = uResolution;
    float aspect = res.x / res.y;
    float minDim = min(res.x, res.y);

    // Normalized coordinates centered at widget center
    vec2 center = res * 0.5;
    vec2 p = px - center;           // pixel coords from center
    float dist = length(p);
    float baseR = minDim * 0.42;    // matches QPainter base_r

    // Normalized distance from center (0..1 = within base radius)
    float nd = dist / baseR;
    float angle = atan(p.y, p.x);

    vec3 col = mainColor(uColorMode);
    float t = uTime;
    float amp = uAmplitude;
    float ampBoost = 1.0 + amp * 5.0;
    float breath = 0.5 + 0.5 * sin(t * uPulseRate);

    float totalAlpha = 0.0;
    vec3 totalColor = vec3(0.0);

    // ── 0. Dark disc background (orb mode) ──
    if (uOrbMode == 1) {
        float discR = minDim * 0.46;
        float dd = dist / discR;
        float discAlpha = 0.0;
        if (dd < 0.75)       discAlpha = 0.90;
        else if (dd < 0.92)  discAlpha = mix(0.86, 0.63, (dd - 0.75) / 0.17);
        else if (dd < 1.0)   discAlpha = mix(0.63, 0.0, (dd - 0.92) / 0.08);
        if (discAlpha > 0.0) {
            totalColor = vec3(0.02, 0.03, 0.02);
            totalAlpha = discAlpha;
        }
    }

    // ── 1. Outer halo ──
    float haloR = baseR * 1.3 * (1.0 + amp * 0.4);
    float haloD = dist / haloR;
    if (haloD < 1.0) {
        float haloA_base = (25.0 + 40.0 * breath + amp * 100.0) / 255.0;
        float haloFade;
        if (haloD < 0.4) haloFade = mix(1.0, 0.4, haloD / 0.4);
        else             haloFade = mix(0.4, 0.0, (haloD - 0.4) / 0.6);
        float ha = haloA_base * haloFade;
        totalColor = mix(totalColor, col, ha);
        totalAlpha = max(totalAlpha, totalAlpha + ha * (1.0 - totalAlpha));
    }

    // ── 2. Core glow layers ──
    float coreBreath = 0.85 + 0.15 * breath + amp * 1.5;
    float coreR = baseR * 0.32 * coreBreath;

    // Outer haze
    float hazeR = coreR * 2.8;
    float hazeD = dist / hazeR;
    if (hazeD < 1.0) {
        float hazeA;
        if (hazeD < 0.5) hazeA = mix(55.0, 20.0, hazeD / 0.5);
        else             hazeA = mix(20.0, 0.0, (hazeD - 0.5) / 0.5);
        hazeA = hazeA * coreBreath / 255.0;
        totalColor = mix(totalColor, col, hazeA);
        totalAlpha = max(totalAlpha, totalAlpha + hazeA * (1.0 - totalAlpha));
    }

    // Inner glow
    float igR = coreR * 1.5;
    float igD = dist / igR;
    if (igD < 1.0) {
        float igA;
        if (igD < 0.6) igA = mix(120.0, 45.0, igD / 0.6);
        else           igA = mix(45.0, 0.0, (igD - 0.6) / 0.4);
        igA = igA * coreBreath / 255.0;
        totalColor = mix(totalColor, col, igA);
        totalAlpha = max(totalAlpha, totalAlpha + igA * (1.0 - totalAlpha));
    }

    // Bright center (white-green blend)
    float brightR = coreR * 0.55;
    float bD = dist / brightR;
    if (bD < 1.0) {
        float centerA = min(1.0, (160.0 + amp * 300.0) * coreBreath / 255.0);
        vec3 warmWhite = vec3(0.706, 1.0, 0.824);
        float bFade = 1.0 - bD;
        vec3 bCol = mix(col, warmWhite, bFade * 0.7);
        float bA = centerA * bFade;
        totalColor = mix(totalColor, bCol, bA);
        totalAlpha = max(totalAlpha, totalAlpha + bA * (1.0 - totalAlpha));
    }

    // Voice pip (white glow when speaking)
    if (amp > 0.02) {
        float pipR = brightR * 0.4 * min(1.0, amp * 5.0);
        float pipD = dist / max(pipR, 0.5);
        if (pipD < 1.0) {
            float pipA = min(1.0, (100.0 + amp * 400.0) / 255.0) * (1.0 - pipD);
            vec3 pipCol = vec3(0.863, 1.0, 0.922);
            totalColor = mix(totalColor, pipCol, pipA);
            totalAlpha = max(totalAlpha, totalAlpha + pipA * (1.0 - totalAlpha));
        }
    }

    // ── 3. Guide rings (subtle circles) ──
    float guideRings[4] = float[](0.60, 0.72, 0.84, TICK_RADIUS_FRAC);
    for (int i = 0; i < 4; i++) {
        float gr = baseR * guideRings[i];
        float gd = abs(dist - gr);
        if (gd < 1.2) {
            float ga = 25.0 / 255.0 * (1.0 - gd / 1.2);
            totalColor = mix(totalColor, col, ga);
            totalAlpha = max(totalAlpha, totalAlpha + ga * (1.0 - totalAlpha));
        }
    }

    // ── 4. Arc segments ──
    for (int i = 0; i < N_ARCS; i++) {
        float ar = baseR * ARC_RF[i];
        float arcDist = abs(dist - ar);
        if (arcDist > 3.5) continue;  // early skip — too far from this ring

        float osc = ARC_OA[i] * sin(t * ARC_OF[i] * uArcSpeed * 6.0);
        float startDeg = ARC_START[i] + osc * 60.0
                       + t * uArcSpeed * 30.0 * ARC_DIR[i];
        float sweepDeg = ARC_SWEEP[i];

        float startRad = startDeg * DEG2RAD;
        float sweepRad = sweepDeg * DEG2RAD;

        // Angle check — is this pixel's angle within the arc sweep?
        float centerAng = startRad + sweepRad * 0.5;
        float da = mod(angle - centerAng + PI, TAU) - PI;
        float halfSweep = sweepRad * 0.5;

        if (abs(da) < halfSweep + 0.05) {
            // Fade at arc endpoints
            float edgeFade = 1.0 - smoothstep(halfSweep - 0.05, halfSweep, abs(da));
            // Fade across arc width
            float widthFade = 1.0 - arcDist / 3.5;
            float tierAlpha = min(1.0, (120.0 + 80.0 * ARC_RF[i]) * ampBoost / 255.0);
            float aa = tierAlpha * edgeFade * widthFade;
            if (aa > 0.005) {
                totalColor = mix(totalColor, col, aa);
                totalAlpha = max(totalAlpha, totalAlpha + aa * (1.0 - totalAlpha));
            }
        }
    }

    // ── 5. Tick marks (24 ticks, gravity-bounce) ──
    float tickR = baseR * TICK_RADIUS_FRAC;
    float tickBaseLen = baseR * 0.10;
    float tickExtLen  = baseR * 0.16 * uTickExtend;
    float tickAmpExt  = baseR * 0.10 * amp;
    float tickOffset  = uTickAngle * DEG2RAD;

    for (int i = 0; i < N_TICKS; i++) {
        float baseAng = float(i) * 15.0 * DEG2RAD + tickOffset;
        bool isMinor = (i % 2 == 1);

        float pulse = sin(t * uPulseRate * 2.0 + float(i) * 0.5);

        float tickLen;
        if (isMinor) {
            tickLen = tickBaseLen * 0.5
                    + tickExtLen * 0.3 * (0.5 + 0.5 * pulse)
                    + tickAmpExt * 0.4;
        } else {
            tickLen = tickBaseLen
                    + tickExtLen * (0.5 + 0.5 * pulse)
                    + tickAmpExt;
        }

        // Tick line: from (tickR - 0.3*len) to (tickR + 0.7*len)
        float innerR = tickR - tickLen * 0.3;
        float outerR = tickR + tickLen * 0.7;

        // Check if pixel is near this tick's radial line
        vec2 tickDir = vec2(cos(baseAng), sin(baseAng));
        // Project p onto tick direction
        float proj = dot(p, tickDir);
        if (proj < innerR - 1.0 || proj > outerR + 1.0) continue;

        // Perpendicular distance to the tick line
        float perp = abs(p.x * tickDir.y - p.y * tickDir.x);
        float halfWidth = isMinor ? 0.8 : 1.3;
        if (perp > halfWidth + 0.5) continue;

        float lineFade = 1.0 - smoothstep(halfWidth - 0.3, halfWidth + 0.5, perp);
        float endFade  = smoothstep(innerR - 1.0, innerR, proj)
                       * (1.0 - smoothstep(outerR, outerR + 1.0, proj));

        float ta;
        if (isMinor)
            ta = min(1.0, (50.0 + 60.0 * (0.5 + 0.5 * pulse)) * ampBoost / 255.0);
        else
            ta = min(1.0, (100.0 + 100.0 * (0.5 + 0.5 * pulse)) * ampBoost / 255.0);

        float tickA = ta * lineFade * endFade;
        if (tickA > 0.005) {
            totalColor = mix(totalColor, col, tickA);
            totalAlpha = max(totalAlpha, totalAlpha + tickA * (1.0 - totalAlpha));
        }
    }

    // ── 6. Orbital particles ──
    for (int i = 0; i < N_PARTICLES; i++) {
        // Particle position evolves over time
        float pAng = P_ANG[i] + P_SPD[i] * t * (0.5 + uArcSpeed) * (1.0 + amp * 3.0);
        float pPhase = P_PH[i] + t * 1.5;
        float pr = baseR * (P_RF[i] + 0.04 * sin(pPhase));
        vec2 pp = vec2(pr * cos(pAng), pr * sin(pAng));
        float pd = length(p - pp);
        if (pd < 5.0) {
            float pa = min(1.0, (60.0 + 70.0 * (0.5 + 0.5 * sin(pPhase * 1.3)))
                           * ampBoost / 255.0);
            float pFade = 1.0 - smoothstep(0.0, 4.0, pd);
            float partA = pa * pFade;
            if (partA > 0.005) {
                totalColor = mix(totalColor, col, partA);
                totalAlpha = max(totalAlpha, totalAlpha + partA * (1.0 - totalAlpha));
            }
        }
    }

    fragColor = vec4(totalColor, totalAlpha);
}
"""


def _compile_shader(src, shader_type):
    """Compile a GLSL shader, raise on error."""
    shader = glCreateShader(shader_type)
    glShaderSource(shader, src)
    glCompileShader(shader)
    if glGetShaderiv(shader, GL_COMPILE_STATUS) != GL_TRUE:
        log = glGetShaderInfoLog(shader).decode()
        raise RuntimeError(f"Shader compile error:\n{log}")
    return shader


def _link_program(vert, frag):
    """Link vertex + fragment shaders into a program."""
    prog = glCreateProgram()
    glAttachShader(prog, vert)
    glAttachShader(prog, frag)
    glLinkProgram(prog)
    if glGetProgramiv(prog, GL_LINK_STATUS) != GL_TRUE:
        log = glGetProgramInfoLog(prog).decode()
        raise RuntimeError(f"Program link error:\n{log}")
    glDeleteShader(vert)
    glDeleteShader(frag)
    return prog


class WinstonCore(_WinstonCoreBase):
    """GPU-accelerated sci-fi HUD circle — QOpenGLWidget with GLSL shaders.

    Falls back to QPainter if PyOpenGL is not installed.

    All visual math runs in the fragment shader:
      - Breathing core glow with layered radial gradients
      - 8 oscillating arc segments on 3 concentric rings
      - 24 tick marks with gravity-bounce rotation
      - 18 orbital particles
      - 4 guide rings
      - Outer halo

    Python per-frame cost: ~10 uniform uploads. GPU does the rest.
    """

    def __init__(self, winston_state=None, show_label=True, parent=None):
        super().__init__(parent)

        # Set up OpenGL surface format for this widget
        if _HAS_OPENGL:
            fmt = QSurfaceFormat()
            fmt.setVersion(3, 3)
            fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
            fmt.setAlphaBufferSize(8)
            fmt.setSamples(4)
            self.setFormat(fmt)
        self.setMinimumSize(60, 60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

        # For transparency in orb mode
        if _HAS_OPENGL and not show_label:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._t = 0.0
        self._state = "IDLE"
        self._state_params = _STATES["IDLE"]
        self._winston_state = winston_state
        self._gl_ready = False

        # Smooth state transitions — lerp current params toward target
        self._cur_arc_speed = 0.12
        self._cur_tick_energy = 0.15
        self._cur_tick_extend = 0.0
        self._cur_pulse_rate = 0.6
        self._cur_color_mode = 0.0  # 0=green, 1=red (lerped)

        # ── Gravity-bounce tick physics ──
        self._tick_angle = 0.0
        self._tick_vel = 0.0
        self._tick_phase = 0.0
        self._tick_bounce_amp = 25.0

        # Smoothed amplitude (EMA)
        self._displayed_amp = 0.0

        # QPainter fallback palettes (only used if no OpenGL)
        if not _HAS_OPENGL:
            self._pal_green = [QColor(70, 210, 150, a) for a in range(256)]
            self._pal_red = [QColor(239, 68, 68, a) for a in range(256)]
            # Particles for QPainter fallback
            rng = random.Random(7)
            self._particles = []
            for _ in range(18):
                self._particles.append([
                    rng.uniform(0, 2 * math.pi),
                    rng.uniform(0.35, 0.95),
                    rng.uniform(0.1, 0.5),
                    rng.uniform(0, 2 * math.pi),
                ])

        # Optional state label at bottom
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

        # Subscribe to unified state
        if winston_state is not None:
            winston_state.state_changed.connect(self.set_state)
            self.set_state(winston_state.state)

        # Self-driving animation timer
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

    # ── OpenGL setup ──

    def initializeGL(self):
        """Compile shaders, set up fullscreen quad VAO/VBO."""
        if not _HAS_OPENGL:
            return
        try:
            vert = _compile_shader(_CORE_VERT_SRC, GL_VERTEX_SHADER)
            frag = _compile_shader(_CORE_FRAG_SRC, GL_FRAGMENT_SHADER)
            self._program = _link_program(vert, frag)

            # Cache uniform locations
            self._u_resolution = glGetUniformLocation(self._program, "uResolution")
            self._u_time       = glGetUniformLocation(self._program, "uTime")
            self._u_arc_speed  = glGetUniformLocation(self._program, "uArcSpeed")
            self._u_tick_energy = glGetUniformLocation(self._program, "uTickEnergy")
            self._u_tick_extend = glGetUniformLocation(self._program, "uTickExtend")
            self._u_pulse_rate = glGetUniformLocation(self._program, "uPulseRate")
            self._u_color_mode = glGetUniformLocation(self._program, "uColorMode")
            self._u_amplitude  = glGetUniformLocation(self._program, "uAmplitude")
            self._u_tick_angle = glGetUniformLocation(self._program, "uTickAngle")
            self._u_orb_mode   = glGetUniformLocation(self._program, "uOrbMode")

            # Fullscreen quad: two triangles as a triangle strip
            quad = np.array([
                -1.0, -1.0,
                 1.0, -1.0,
                -1.0,  1.0,
                 1.0,  1.0,
            ], dtype=np.float32)

            self._vao = glGenVertexArrays(1)
            glBindVertexArray(self._vao)
            vbo = glGenBuffers(1)
            glBindBuffer(GL_ARRAY_BUFFER, vbo)
            glBufferData(GL_ARRAY_BUFFER, quad.nbytes, quad, GL_STATIC_DRAW)
            glEnableVertexAttribArray(0)
            glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, None)
            glBindVertexArray(0)

            self._gl_ready = True
        except Exception as e:
            print(f"[WinstonCore] OpenGL init failed, falling back to QPainter: {e}")
            self._gl_ready = False
            # Initialize fallback palettes
            self._pal_green = [QColor(70, 210, 150, a) for a in range(256)]
            self._pal_red = [QColor(239, 68, 68, a) for a in range(256)]
            rng = random.Random(7)
            self._particles = []
            for _ in range(18):
                self._particles.append([
                    rng.uniform(0, 2 * math.pi),
                    rng.uniform(0.35, 0.95),
                    rng.uniform(0.1, 0.5),
                    rng.uniform(0, 2 * math.pi),
                ])

    def paintGL(self):
        """Set uniforms and draw fullscreen quad — GPU does all rendering."""
        if not self._gl_ready:
            # Fall back to QPainter if GL init failed
            self._paint_fallback()
            return

        w = int(self.width() * self.devicePixelRatioF())
        h = int(self.height() * self.devicePixelRatioF())
        if w < 10 or h < 10:
            return

        glViewport(0, 0, w, h)
        glClearColor(0.0, 0.0, 0.0, 0.0)
        glClear(GL_COLOR_BUFFER_BIT)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        glUseProgram(self._program)
        glUniform2f(self._u_resolution, float(w), float(h))
        glUniform1f(self._u_time, self._t)
        glUniform1f(self._u_arc_speed, self._cur_arc_speed)
        glUniform1f(self._u_tick_energy, self._cur_tick_energy)
        glUniform1f(self._u_tick_extend, self._cur_tick_extend)
        glUniform1f(self._u_pulse_rate, self._cur_pulse_rate)
        glUniform1f(self._u_color_mode, self._cur_color_mode)
        glUniform1f(self._u_amplitude, self._displayed_amp)
        glUniform1f(self._u_tick_angle, self._tick_angle)
        glUniform1i(self._u_orb_mode, 0 if self._show_label else 1)

        glBindVertexArray(self._vao)
        glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        glBindVertexArray(0)

        glUseProgram(0)
        glDisable(GL_BLEND)

    # ── State management (unchanged public API) ──

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
        """Called by the internal QTimer — delegates to tick()."""
        if not self.isVisible():
            return
        self.tick(self._anim_dt)

    def tick(self, dt=0.017):
        self._t += dt
        sp = self._state_params

        # Lerp current params toward target (~200ms transition)
        lerp = min(1.0, dt * 5.0)
        self._cur_arc_speed += (sp[0] - self._cur_arc_speed) * lerp
        self._cur_tick_energy += (sp[1] - self._cur_tick_energy) * lerp
        self._cur_tick_extend += (sp[2] - self._cur_tick_extend) * lerp
        self._cur_pulse_rate += (sp[3] - self._cur_pulse_rate) * lerp
        self._cur_color_mode += (sp[4] - self._cur_color_mode) * lerp

        # Amplitude (voice reactivity)
        target_amp = 0.0
        if self._winston_state is not None:
            target_amp = self._winston_state.amplitude
        if target_amp > self._displayed_amp:
            self._displayed_amp = 0.6 * self._displayed_amp + 0.4 * target_amp
        else:
            self._displayed_amp = 0.92 * self._displayed_amp + 0.08 * target_amp

        # Gravity-bounce tick physics
        energy = self._cur_tick_energy
        self._tick_phase += dt * (0.8 + energy * 2.5)
        phase = self._tick_phase
        raw_sin = _fast_sin(phase)
        if raw_sin >= 0:
            swing = raw_sin * raw_sin
        else:
            swing = -(raw_sin * raw_sin)
        swing_range = self._tick_bounce_amp * (0.3 + energy * 1.5)
        self._tick_angle = swing * swing_range
        if energy > 0.7:
            jitter = _fast_sin(self._t * 18.0) * 3.0 * energy
            self._tick_angle += jitter

        # Update particles for QPainter fallback path
        if not _HAS_OPENGL or not self._gl_ready:
            amp_boost = 1.0 + self._displayed_amp * 3.0
            if hasattr(self, '_particles'):
                for p_item in self._particles:
                    p_item[0] += p_item[2] * dt * (0.5 + self._cur_arc_speed) * amp_boost
                    p_item[3] += dt * 1.5

        self.update()

    # ── QPainter fallback (used when OpenGL is unavailable) ──

    def _paint_fallback(self):
        """QPainter rendering — identical to old WinstonCore.paintEvent."""
        w, h = self.width(), self.height()
        if w < 10 or h < 10:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        _C_CLEAR = QColor(0, 0, 0, 0)
        _PEN_NONE = QPen(Qt.PenStyle.NoPen)
        _BRUSH_NONE = QBrush(Qt.BrushStyle.NoBrush)

        if not self._show_label:
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(0, 0, w, h, Qt.GlobalColor.transparent)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver)

        if not self._show_label:
            disc_cx, disc_cy = w * 0.5, h * 0.5
            disc_r = min(w, h) * 0.46
            disc_grad = QRadialGradient(QPointF(disc_cx, disc_cy), disc_r)
            disc_grad.setColorAt(0.0, QColor(5, 8, 5, 230))
            disc_grad.setColorAt(0.75, QColor(5, 8, 5, 220))
            disc_grad.setColorAt(0.92, QColor(5, 8, 5, 160))
            disc_grad.setColorAt(1.0, QColor(5, 8, 5, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(disc_grad))
            painter.drawEllipse(QPointF(disc_cx, disc_cy), disc_r, disc_r)

        cx, cy = w * 0.5, h * 0.5
        base_r = min(w, h) * 0.42
        t = self._t
        cm = self._cur_color_mode

        is_red = cm >= 0.5
        _pal = self._pal_red if is_red else self._pal_green

        def _clr(alpha):
            a = max(1, min(255, int(alpha)))
            return _pal[a]

        amp = self._displayed_amp
        amp_boost = 1.0 + amp * 5.0

        # Outer halo
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

        # Core glow
        core_breath = 0.85 + 0.15 * breath + amp * 1.5
        core_r = base_r * 0.32 * core_breath

        core_haze_r = core_r * 2.8
        core_haze = QRadialGradient(QPointF(cx, cy), core_haze_r)
        core_haze.setColorAt(0.0, _clr(int(min(255, 55 * core_breath))))
        core_haze.setColorAt(0.5, _clr(int(min(255, 20 * core_breath))))
        core_haze.setColorAt(1.0, _C_CLEAR)
        painter.setBrush(QBrush(core_haze))
        painter.drawEllipse(QPointF(cx, cy), core_haze_r, core_haze_r)

        core_glow = QRadialGradient(QPointF(cx, cy), core_r * 1.5)
        core_glow.setColorAt(0.0, _clr(int(min(255, 120 * core_breath))))
        core_glow.setColorAt(0.6, _clr(int(min(255, 45 * core_breath))))
        core_glow.setColorAt(1.0, _C_CLEAR)
        painter.setBrush(QBrush(core_glow))
        painter.drawEllipse(QPointF(cx, cy), core_r * 1.5, core_r * 1.5)

        bright_r = core_r * 0.55
        core_solid = QRadialGradient(QPointF(cx, cy), bright_r)
        center_a = int(min(255, (160 + amp * 300) * core_breath))
        core_solid.setColorAt(0.0, QColor(180, 255, 210,
                                          min(255, center_a + 80)))
        core_solid.setColorAt(0.5, _clr(min(255, center_a + 30)))
        core_solid.setColorAt(1.0, _clr(int(center_a * 0.3)))
        painter.setBrush(QBrush(core_solid))
        painter.drawEllipse(QPointF(cx, cy), bright_r, bright_r)

        if amp > 0.02:
            pip_r = bright_r * 0.4 * min(1.0, amp * 5)
            painter.setBrush(QBrush(QColor(220, 255, 235,
                                           int(min(255, 100 + amp * 400)))))
            painter.drawEllipse(QPointF(cx, cy), pip_r, pip_r)

        # Arc segments
        for rf, start_deg, sweep_deg, osc_amp, osc_freq, direction in _ARC_DEFS:
            r = base_r * rf
            osc = osc_amp * _fast_sin(t * osc_freq * self._cur_arc_speed * 6.0)
            actual_start = start_deg + osc * 60.0
            actual_start += t * self._cur_arc_speed * 30.0 * direction
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

        # Tick marks
        tick_r = base_r * _TICK_RADIUS_FRAC
        tick_base_len = base_r * 0.10
        tick_extend_len = base_r * 0.16 * self._cur_tick_extend
        tick_amp_extend = base_r * 0.10 * amp
        offset = self._tick_angle
        _DEG2RAD = math.pi / 180.0
        for i, (base_angle, is_minor) in enumerate(_TICK_ANGLES):
            angle_rad = (base_angle + offset) * _DEG2RAD
            pulse = _fast_sin(t * self._cur_pulse_rate * 2.0 + i * 0.5)
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
            if is_minor:
                a = int(min(255, (50 + 60 * (0.5 + 0.5 * pulse)) * amp_boost))
                pen = QPen(_clr(a), 1.2)
            else:
                a = int(min(255, (100 + 100 * (0.5 + 0.5 * pulse)) * amp_boost))
                pen = QPen(_clr(a), 2.0)
            painter.setPen(pen)
            painter.drawLine(QPointF(inner_x, inner_y),
                             QPointF(outer_x, outer_y))

        # Orbital particles
        painter.setPen(_PEN_NONE)
        for angle, r_frac, _aspd, ph in self._particles:
            pr = base_r * (r_frac + 0.04 * _fast_sin(ph))
            px = cx + pr * _fast_cos(angle)
            py = cy - pr * _fast_sin(angle)
            a = int(min(255, (60 + 70 * (0.5 + 0.5 * _fast_sin(ph * 1.3)))
                        * amp_boost))
            painter.setBrush(QBrush(_clr(a)))
            painter.drawEllipse(QPointF(px, py), 2.2, 2.2)

        # Guide rings
        ring_pen = QPen(_clr(25), 0.7)
        painter.setPen(ring_pen)
        painter.setBrush(_BRUSH_NONE)
        for rf in (0.60, 0.72, 0.84, _TICK_RADIUS_FRAC):
            r = base_r * rf
            painter.drawEllipse(QPointF(cx, cy), r, r)

        painter.end()

    def paintEvent(self, event):
        """QPainter fallback — only called when NOT using QOpenGLWidget."""
        if _HAS_OPENGL:
            # QOpenGLWidget uses paintGL(), not paintEvent()
            super().paintEvent(event)
            return
        self._paint_fallback()


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


# ──────────────── Dot-matrix globe with real geographic data ────────────────

# Simplified coastline polygons — real lat/lon coordinates for major
# landmasses.  Point-in-polygon tested once at init to build the
# dot-matrix sets.  ~24 polygons covering all continents + major islands.

_COASTLINE_POLYS = [
    # North America (mainland)
    [(49,-125),(55,-130),(58,-137),(60,-146),(64,-165),(71,-157),(72,-130),
     (71,-95),(68,-80),(60,-64),(52,-55),(47,-52),(44,-66),(41,-70),
     (35,-75),(30,-81),(25,-80),(26,-82),(30,-88),(29,-95),(26,-97),
     (20,-87),(16,-88),(16,-96),(20,-105),(23,-110),(32,-117),(34,-120),
     (40,-124),(48,-125),(49,-125)],
    # Central America
    [(20,-87),(15,-84),(10,-84),(8,-77),(10,-76),(14,-87),(17,-88),(20,-87)],
    # South America
    [(12,-72),(10,-75),(8,-77),(4,-77),(-2,-80),(-5,-81),(-5,-75),(0,-50),
     (-2,-42),(-8,-35),(-12,-38),(-23,-42),(-28,-49),(-34,-53),(-42,-65),
     (-46,-68),(-52,-70),(-55,-68),(-55,-64),(-50,-73),(-42,-73),(-38,-57),
     (-33,-52),(-18,-40),(-12,-37),(-5,-35),(2,-50),(7,-60),(10,-72),(12,-72)],
    # Europe
    [(36,-9),(37,-1),(43,3),(44,8),(48,5),(48,2),(50,-5),(52,-10),
     (54,-10),(58,-5),(61,5),(64,14),(68,16),(70,26),(70,30),
     (60,30),(57,24),(54,14),(52,10),(50,14),(48,17),(47,14),
     (44,12),(43,16),(42,17),(40,26),(38,26),(36,28),(35,25),
     (38,10),(36,-5),(36,-9)],
    # Africa
    [(35,-6),(37,10),(33,13),(31,32),(28,33),(22,36),(15,42),(12,44),
     (11,51),(2,42),(0,42),(-2,40),(-12,40),(-15,35),(-25,33),(-35,25),
     (-35,18),(-27,15),(-18,12),(-12,14),(-5,12),(0,10),(5,1),(5,-5),
     (5,-10),(7,-13),(15,-17),(20,-17),(25,-15),(30,-10),(35,-6)],
    # Asia (mainland)
    [(42,28),(42,40),(38,45),(40,50),(37,55),(25,58),(23,68),(20,73),
     (8,77),(1,104),(6,101),(8,98),(16,108),(22,106),(22,114),(30,122),
     (35,129),(40,130),(42,131),(46,140),(50,143),(55,137),(60,135),
     (63,143),(65,170),(68,180),(72,180),(72,120),(73,80),(73,60),
     (68,50),(55,40),(45,35),(42,28)],
    # India
    [(30,68),(28,72),(24,72),(22,68),(20,73),(16,74),(8,77),(10,80),
     (22,88),(27,88),(30,80),(30,68)],
    # SE Asia peninsula
    [(22,98),(20,93),(16,98),(10,99),(1,104),(6,101),(8,98),(16,108),
     (22,106),(22,98)],
    # Australia
    [(-12,130),(-12,137),(-17,146),(-22,150),(-28,153),(-35,151),
     (-38,145),(-37,140),(-35,136),(-32,133),(-23,114),(-15,129),
     (-12,130)],
    # Greenland
    [(60,-45),(65,-55),(70,-55),(76,-60),(80,-65),(83,-30),(81,-17),
     (77,-18),(72,-22),(65,-40),(60,-45)],
    # British Isles
    [(50,-6),(51,-3),(54,-3),(57,-6),(58,-5),(58,-3),(54,0),(51,1),(50,-6)],
    # Japan
    [(31,131),(33,130),(35,133),(36,137),(39,140),(42,141),(45,142),
     (44,145),(40,140),(36,140),(34,135),(31,131)],
    # Indonesia (Sumatra+Java)
    [(-6,95),(-6,106),(-8,110),(-8,115),(-7,112),(-6,106),(-2,100),
     (5,97),(5,95),(-2,99),(-6,95)],
    # Borneo
    [(7,117),(4,108),(1,109),(-3,110),(-4,116),(1,118),(4,118),(7,117)],
    # New Zealand
    [(-35,172),(-37,175),(-42,174),(-47,167),(-46,166),(-43,170),
     (-38,176),(-35,174),(-35,172)],
    # Madagascar
    [(-12,49),(-16,44),(-19,44),(-24,44),(-26,47),(-22,48),(-16,50),
     (-12,49)],
    # Antarctica
    [(-65,-60),(-68,-70),(-70,-100),(-72,-130),(-75,-170),(-78,180),
     (-77,150),(-72,130),(-70,100),(-68,70),(-65,30),(-65,-10),
     (-65,-60)],
    # Iceland
    [(64,-24),(64,-14),(66,-14),(66,-22),(64,-24)],
    # Philippines (Luzon)
    [(14,120),(18,121),(19,122),(16,122),(14,120)],
    # Papua New Guinea
    [(-2,141),(-6,141),(-8,147),(-6,155),(-5,152),(-3,145),(-2,141)],
    # Cuba
    [(20,-85),(22,-84),(23,-80),(21,-77),(20,-82),(20,-85)],
    # Scandinavia (supplement)
    [(60,5),(63,5),(66,14),(70,20),(70,30),(68,16),(64,14),(61,5),(60,5)],
]

# City markers — (lat, lon, 3-letter code)
_CITY_MARKERS = [
    (40.7, -74.0, "NYC"), (34.1, -118.2, "LAX"), (51.5, -0.1, "LON"),
    (48.9, 2.3, "PAR"), (35.7, 139.7, "TKY"), (31.2, 121.5, "SHA"),
    (19.1, 72.9, "MUM"), (-33.9, 151.2, "SYD"), (55.8, 37.6, "MOW"),
    (-23.5, -46.6, "SAO"), (30.0, 31.2, "CAI"), (-1.3, 36.8, "NBO"),
    (1.3, 103.8, "SIN"), (37.6, 127.0, "SEL"), (52.5, 13.4, "BER"),
    (25.3, 55.3, "DXB"), (39.9, 116.4, "BEI"), (41.0, 29.0, "IST"),
]


def _pip(lat, lon, poly):
    """Ray-casting point-in-polygon test."""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-10) + xi):
            inside = not inside
        j = i
    return inside


def _is_land(lat, lon):
    """Test if a lat/lon coordinate is on land."""
    for poly in _COASTLINE_POLYS:
        if _pip(lat, lon, poly):
            return True
    return False


def _globe_project(lat_deg, lon_deg, rotation, cx, cy, r):
    """Orthographic projection: lat/lon → screen (x, y, z).
    z > 0 means the point is on the visible hemisphere."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg) - rotation
    x = r * math.cos(lat) * math.sin(lon)
    y = -r * math.sin(lat)
    z = math.cos(lat) * math.cos(lon)
    return (cx + x, cy + y, z)


_GLOBE_FRAG_SRC = """
#version 330 core
in vec2 vUV;
out vec4 fragColor;

uniform vec2  uResolution;
uniform float uRotation;
uniform float uScanAngle;

// Land mask: 90x180 texture — each texel = 1.0 if land, 0.0 if ocean
uniform sampler2D uLandMask;

// City markers — up to 18 cities: (lat_rad, lon_rad) packed into uniforms
uniform vec2  uCities[18];
uniform int   uNumCities;

const float PI  = 3.14159265;
const float TAU = 6.28318530;
const float DEG2RAD = PI / 180.0;

const vec3 GREEN = vec3(0.133, 0.773, 0.369);  // #22C55E

// Orthographic back-projection: screen → (lat, lon, depth)
// Returns vec3(lat_rad, lon_rad, z). z>0 = front hemisphere.
vec3 backProject(vec2 p, float r, float rot) {
    float nx = p.x / r;
    float ny = p.y / r;
    float r2 = nx * nx + ny * ny;
    if (r2 > 1.0) return vec3(0.0, 0.0, -1.0);
    float nz = sqrt(1.0 - r2);
    float lat = asin(ny);
    float lon = atan(nx, nz) + rot;
    return vec3(lat, lon, nz);
}

// Sample land mask texture (lat in [-PI/2, PI/2], lon in [-PI, PI])
float isLand(float lat, float lon) {
    // Normalize lon to [0, 1]
    float u = mod(lon / TAU + 0.5, 1.0);
    // Normalize lat to [0, 1] (south=0, north=1)
    float v = lat / PI + 0.5;
    return texture(uLandMask, vec2(u, v)).r;
}

void main() {
    vec2 px = gl_FragCoord.xy;
    vec2 res = uResolution;
    vec2 center = res * 0.5;
    vec2 p = px - center;
    float dist = length(p);
    float minDim = min(res.x, res.y);
    float r = minDim * 0.42;

    float nd = dist / r;
    float totalAlpha = 0.0;
    vec3 totalColor = vec3(0.0);

    // ── Atmosphere glow ──
    float atmoR = r * 1.25;
    float atmoD = dist / atmoR;
    if (atmoD > 0.75 && atmoD < 1.0) {
        float glow = smoothstep(1.0, 0.88, atmoD) * smoothstep(0.75, 0.88, atmoD);
        float ga = glow * 15.0 / 255.0;
        totalColor = GREEN;
        totalAlpha = ga;
    }

    // ── Rim circle ──
    float rimDist = abs(dist - r);
    if (rimDist < 1.0) {
        float ra = 70.0 / 255.0 * (1.0 - rimDist);
        totalColor = mix(totalColor, GREEN, ra);
        totalAlpha = max(totalAlpha, totalAlpha + ra * (1.0 - totalAlpha));
    }

    // ── Globe content (inside sphere) ──
    if (nd < 1.0) {
        vec3 proj = backProject(p, r, uRotation);
        float lat = proj.x;
        float lon = proj.y;
        float z = proj.z;

        if (z > 0.02) {
            // Quantize to grid positions (every 4 degrees)
            float gridStep = 4.0 * DEG2RAD;
            float qLat = round(lat / gridStep) * gridStep;
            float qLon = round(lon / gridStep) * gridStep;

            // Distance from this pixel to nearest grid dot (in lat/lon space)
            float dLat = lat - qLat;
            float dLon = lon - qLon;
            // Scale by cos(lat) for longitude compression at poles
            float cosLat = cos(lat);
            float dotDist = sqrt(dLat * dLat + (dLon * cosLat) * (dLon * cosLat));
            float dotDistPx = dotDist * r;  // approx screen-space distance

            float land = isLand(qLat, qLon);

            // Dot rendering
            if (land > 0.5) {
                // Land dot: bright, larger
                float dotR = (1.2 + 0.6 * z) / r * 1.2;
                if (dotDist < dotR) {
                    float da = (100.0 + 120.0 * z) / 255.0;
                    float fade = 1.0 - dotDist / dotR;
                    float la = da * fade;
                    totalColor = mix(totalColor, GREEN, la);
                    totalAlpha = max(totalAlpha, totalAlpha + la * (1.0 - totalAlpha));
                }
            } else {
                // Ocean dot: dim, sparser (every other grid point)
                float sparseCheck = round(qLat / (gridStep * 2.0)) * 2.0 * gridStep
                                  + round(qLon / (gridStep * 2.0)) * 2.0 * gridStep;
                float modCheck = mod(abs(round(qLat / gridStep)) + abs(round(qLon / gridStep)), 2.0);
                if (modCheck < 0.5) {
                    float dotR = 0.8 / r * 1.2;
                    if (dotDist < dotR) {
                        float da = 18.0 * z / 255.0;
                        float fade = 1.0 - dotDist / dotR;
                        float oa = da * fade;
                        totalColor = mix(totalColor, GREEN, oa);
                        totalAlpha = max(totalAlpha, totalAlpha + oa * (1.0 - totalAlpha));
                    }
                }
            }

            // ── Scan line ──
            float scanLon = mod(uScanAngle, TAU) - PI;
            float dScan = mod(lon - scanLon + PI, TAU) - PI;
            if (abs(dScan) < 3.0 * DEG2RAD) {
                float fade = max(0.0, 1.0 - abs(dScan) / (4.0 * DEG2RAD));
                float sa = 35.0 * z * fade / 255.0;
                totalColor = mix(totalColor, GREEN, sa);
                totalAlpha = max(totalAlpha, totalAlpha + sa * (1.0 - totalAlpha));
            }

            // ── City markers ──
            for (int i = 0; i < uNumCities; i++) {
                float cLat = uCities[i].x;
                float cLon = uCities[i].y;
                // Forward-project city to screen space
                float cx2 = r * cos(cLat) * sin(cLon - uRotation);
                float cy2 = -r * sin(cLat);
                float cz = cos(cLat) * cos(cLon - uRotation);
                if (cz <= 0.15) continue;

                vec2 cityScreen = center + vec2(cx2, cy2);
                float cd = length(px - cityScreen);

                // Glow halo
                if (cd < 6.0) {
                    float ha = 40.0 * cz / 255.0 * (1.0 - cd / 6.0);
                    totalColor = mix(totalColor, GREEN, ha);
                    totalAlpha = max(totalAlpha, totalAlpha + ha * (1.0 - totalAlpha));
                }
                // Bright dot
                if (cd < 3.0) {
                    float ga = 180.0 * cz / 255.0 * (1.0 - cd / 3.0);
                    totalColor = mix(totalColor, GREEN, ga);
                    totalAlpha = max(totalAlpha, totalAlpha + ga * (1.0 - totalAlpha));
                }
            }
        }
    }

    fragColor = vec4(totalColor, totalAlpha);
}
"""


class GlobeWidget(_WinstonCoreBase):
    """GPU-accelerated dot-matrix globe with real coastline data.

    Uses a fragment shader with a 2D land-mask texture for
    continent/ocean classification. Falls back to QPainter if
    PyOpenGL is unavailable.

    GPU path: shader does orthographic back-projection per pixel,
    snaps to grid, samples land mask → draws dots. ~10 uniforms/frame.
    """

    SPIN_SPEED = 0.12  # rad/s — slow tactical spin
    GRID_STEP = 4      # degrees between grid points

    def __init__(self, parent=None):
        if _HAS_OPENGL:
            fmt = QSurfaceFormat()
            fmt.setVersion(3, 3)
            fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
            fmt.setAlphaBufferSize(8)
            fmt.setSamples(4)

        super().__init__(parent)

        if _HAS_OPENGL:
            self.setFormat(fmt)

        self.setMinimumSize(120, 100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._rotation = 0.0
        self._scan_angle = 0.0
        self._gl_ready = False

        # Pre-compute land and ocean dot sets (for fallback + land mask)
        step = self.GRID_STEP
        self._land_pts = []
        self._ocean_pts = []
        for lat in range(-85, 86, step):
            for lon in range(-180, 180, step):
                if _is_land(lat, lon):
                    self._land_pts.append((lat, lon))
                else:
                    if (lat + lon) % (step * 2) == 0:
                        self._ocean_pts.append((lat, lon))

        # Build land mask: 360x180 image (1 degree per pixel)
        self._land_mask_w = 360
        self._land_mask_h = 180
        self._land_mask_data = None
        if _HAS_OPENGL:
            mask = np.zeros((self._land_mask_h, self._land_mask_w),
                            dtype=np.float32)
            for lat in range(-89, 90):
                for lon in range(-180, 180):
                    if _is_land(lat, lon):
                        # v = lat mapping: -90 → row 0, +89 → row 179
                        row = lat + 90
                        col = (lon + 180) % 360
                        if 0 <= row < 180 and 0 <= col < 360:
                            mask[row, col] = 1.0
            self._land_mask_data = mask

        # Pre-allocated colors (for fallback)
        self._rim_color = QColor(34, 197, 94, 70)
        self._rim_glow = QColor(34, 197, 94, 15)

        # Animation timer ~30fps
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    def _tick(self):
        if not self.isVisible():
            return
        self._rotation += self.SPIN_SPEED * 0.033
        self._scan_angle += 0.6 * 0.033
        self.update()

    # ── OpenGL setup ──

    def initializeGL(self):
        if not _HAS_OPENGL:
            return
        try:
            from OpenGL.GL import (
                glGenTextures, glBindTexture, glTexImage2D, glTexParameteri,
                GL_TEXTURE_2D, GL_RED, GL_R32F, GL_TEXTURE_MIN_FILTER,
                GL_TEXTURE_MAG_FILTER, GL_LINEAR, GL_TEXTURE_WRAP_S,
                GL_TEXTURE_WRAP_T, GL_REPEAT,
            )

            vert = _compile_shader(_CORE_VERT_SRC, GL_VERTEX_SHADER)
            frag = _compile_shader(_GLOBE_FRAG_SRC, GL_FRAGMENT_SHADER)
            self._program = _link_program(vert, frag)

            # Uniform locations
            self._u_resolution = glGetUniformLocation(self._program, "uResolution")
            self._u_rotation   = glGetUniformLocation(self._program, "uRotation")
            self._u_scan_angle = glGetUniformLocation(self._program, "uScanAngle")
            self._u_num_cities = glGetUniformLocation(self._program, "uNumCities")

            # City uniform locations
            self._u_cities = []
            for i in range(18):
                loc = glGetUniformLocation(self._program, f"uCities[{i}]")
                self._u_cities.append(loc)

            # Fullscreen quad VAO
            quad = np.array([
                -1.0, -1.0,  1.0, -1.0,  -1.0, 1.0,  1.0, 1.0,
            ], dtype=np.float32)
            self._vao = glGenVertexArrays(1)
            glBindVertexArray(self._vao)
            vbo = glGenBuffers(1)
            glBindBuffer(GL_ARRAY_BUFFER, vbo)
            glBufferData(GL_ARRAY_BUFFER, quad.nbytes, quad, GL_STATIC_DRAW)
            glEnableVertexAttribArray(0)
            glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, None)
            glBindVertexArray(0)

            # Land mask texture
            self._land_tex = glGenTextures(1)
            glBindTexture(GL_TEXTURE_2D, self._land_tex)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT)
            glTexImage2D(GL_TEXTURE_2D, 0, GL_R32F,
                         self._land_mask_w, self._land_mask_h, 0,
                         GL_RED, GL_FLOAT, self._land_mask_data)

            # Store GL constants for use in paintGL
            self._GL_TEXTURE_2D = GL_TEXTURE_2D

            self._gl_ready = True
        except Exception as e:
            print(f"[GlobeWidget] OpenGL init failed, using QPainter: {e}")
            self._gl_ready = False

    def paintGL(self):
        if not self._gl_ready:
            self._paint_fallback()
            return

        from OpenGL.GL import (
            glActiveTexture, glBindTexture, GL_TEXTURE0, GL_TEXTURE_2D,
        )

        w = int(self.width() * self.devicePixelRatioF())
        h = int(self.height() * self.devicePixelRatioF())
        if w < 20 or h < 20:
            return

        glViewport(0, 0, w, h)
        glClearColor(0.0, 0.0, 0.0, 0.0)
        glClear(GL_COLOR_BUFFER_BIT)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        glUseProgram(self._program)
        glUniform2f(self._u_resolution, float(w), float(h))
        glUniform1f(self._u_rotation, self._rotation)
        glUniform1f(self._u_scan_angle, self._scan_angle)

        # Upload city positions as radians
        num_cities = min(len(_CITY_MARKERS), 18)
        glUniform1i(self._u_num_cities, num_cities)
        for i in range(num_cities):
            clat, clon, _ = _CITY_MARKERS[i]
            glUniform2f(self._u_cities[i],
                        math.radians(clat), math.radians(clon))

        # Bind land mask texture
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, self._land_tex)

        glBindVertexArray(self._vao)
        glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        glBindVertexArray(0)

        glUseProgram(0)
        glDisable(GL_BLEND)

    # ── QPainter fallback ──

    def _paint_fallback(self):
        w, h = self.width(), self.height()
        if w < 20 or h < 20:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx, cy = w * 0.5, h * 0.5
        r = min(w, h) * 0.42
        rot = self._rotation

        # Atmosphere glow
        atmo = QRadialGradient(QPointF(cx, cy), r * 1.25)
        atmo.setColorAt(0.0, QColor(34, 197, 94, 0))
        atmo.setColorAt(0.75, QColor(34, 197, 94, 0))
        atmo.setColorAt(0.88, self._rim_glow)
        atmo.setColorAt(1.0, QColor(34, 197, 94, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(atmo))
        painter.drawEllipse(QPointF(cx, cy), r * 1.25, r * 1.25)

        # Rim circle
        painter.setPen(QPen(self._rim_color, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), r, r)

        # Ocean dots
        painter.setPen(Qt.PenStyle.NoPen)
        for lat, lon in self._ocean_pts:
            sx, sy, z = _globe_project(lat, lon, rot, cx, cy, r)
            if z <= 0.02:
                continue
            a = int(18 * z)
            painter.setBrush(QBrush(QColor(34, 197, 94, max(5, a))))
            painter.drawEllipse(QPointF(sx, sy), 0.8, 0.8)

        # Land dots
        for lat, lon in self._land_pts:
            sx, sy, z = _globe_project(lat, lon, rot, cx, cy, r)
            if z <= 0.02:
                continue
            a = int(100 + 120 * z)
            dot_r = 1.2 + 0.6 * z
            painter.setBrush(QBrush(QColor(34, 197, 94, min(255, a))))
            painter.drawEllipse(QPointF(sx, sy), dot_r, dot_r)

        # City markers
        font_city = _mono(5)
        painter.setFont(font_city)
        for clat, clon, code in _CITY_MARKERS:
            sx, sy, z = _globe_project(clat, clon, rot, cx, cy, r)
            if z <= 0.15:
                continue
            ga = int(180 * z)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(34, 197, 94, min(255, ga))))
            painter.drawEllipse(QPointF(sx, sy), 2.5, 2.5)
            painter.setBrush(QBrush(QColor(34, 197, 94, int(40 * z))))
            painter.drawEllipse(QPointF(sx, sy), 5.0, 5.0)
            if z > 0.5:
                painter.setPen(QPen(QColor(34, 197, 94, int(140 * z))))
                painter.drawText(int(sx + 4), int(sy - 3), code)

        # Scan line
        scan_lon = math.degrees(self._scan_angle) % 360 - 180
        painter.setPen(Qt.PenStyle.NoPen)
        for lat in range(-80, 81, 3):
            for d_lon in range(-3, 4, 2):
                slon = scan_lon + d_lon
                sx, sy, z = _globe_project(lat, slon, rot, cx, cy, r)
                if z <= 0.05:
                    continue
                fade = max(0.0, 1.0 - abs(d_lon) / 4.0)
                a = int(35 * z * fade)
                painter.setBrush(QBrush(QColor(34, 197, 94, max(3, a))))
                painter.drawEllipse(QPointF(sx, sy), 1.0, 1.0)

        painter.end()

    def paintEvent(self, event):
        if _HAS_OPENGL:
            super().paintEvent(event)
            return
        self._paint_fallback()


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
        self._map = GlobeWidget()
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
