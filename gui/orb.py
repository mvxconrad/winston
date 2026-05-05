"""Winston's orb — the visual heartbeat.

Self-contained QWidget that paints a single glowing circle whose color
is driven by state and whose size + brightness are driven by audio
amplitude. The word "WINSTON" sits inside the orb body — small,
spaced, hi-tech. No grid, no particles, no caption strip.

Decoupled from VoiceEngine via two callbacks:
    get_state     → str  ("IDLE" | "LISTENING" | "TRANSCRIBING" | …)
    get_amplitude → float in roughly [0, 0.4]

This decoupling lets the same widget render presence-mode audio AND a
synthetic preview in tests, AND (future) a small companion orb embedded
in the dashboard's corner.

Repaint cadence:
    A 30Hz timer drives _tick. If neither state nor amplitude has
    meaningfully changed, we skip the paintEvent — idle-state CPU
    usage stays near zero. When you're talking or Winston is talking,
    every frame counts and we paint.

Visual response curve:
    `amp * 0.8` on the body radius, `amp * 1.4` on the glow radius —
    these are tuned for "obvious feedback at typical speech RMS (~0.1)
    while still readable at peaks (~0.3)". Earlier iterations were too
    subtle and felt static; bumping the multipliers up made the orb
    feel like it was reacting to you.
"""
from __future__ import annotations

import time
from typing import Callable

from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPen, QRadialGradient,
)
from PyQt6.QtWidgets import QWidget


# ──────────────── Color palette per state ────────────────
# Three colors per state: orb_center, orb_edge, ring. Tuned to feel like
# variations on one green rather than disjoint state colors. THINKING +
# TRANSCRIBING share an amber; ERROR is the only red. SPEAKING is the
# brightest because it's the moment you're meant to attend to.
STATE_COLORS = {
    "IDLE":          ("#2a8a2a", "#0a3a0a", "#3aa83a"),
    "LISTENING":     ("#3acc3a", "#0a4a0a", "#7CFC00"),
    "TRANSCRIBING":  ("#c8a838", "#3a3010", "#e6c84a"),
    "THINKING":      ("#c8a838", "#3a3010", "#e6c84a"),
    "SPEAKING":      ("#5fdc5f", "#1a4a0a", "#aaff44"),
    "ERROR":         ("#a82020", "#3a0a0a", "#cc4444"),
    "DISABLED":      ("#404040", "#1a1a1a", "#606060"),
}


class Orb(QWidget):
    """Compact glowing circle. Decoupled from voice engine via callbacks."""

    # Layout fractions (all relative to min(width, height))
    DIAMETER_FRAC = 0.50   # base orb diameter at amp=0
    GLOW_FRAC     = 1.55   # glow outer radius as multiple of orb radius

    # Amplitude-response multipliers — tuned for obvious feedback at
    # typical speech RMS (~0.1) while still readable at peaks (~0.3).
    # Earlier iterations had `amp * 0.35` on the body which was too
    # subtle; the orb felt static even when shouting at the mic.
    BODY_AMP_GAIN  = 0.80   # max body radius bonus = +80% at amp=1.0
    GLOW_AMP_GAIN  = 1.40   # max glow radius bonus = +140%
    ALPHA_AMP_GAIN = 0.50   # max glow alpha bonus = +0.50

    def __init__(
        self,
        get_state: Callable[[], str],
        get_amplitude: Callable[[], float],
        parent=None,
    ):
        super().__init__(parent)
        self._get_state = get_state
        self._get_amplitude = get_amplitude

        self.setMinimumSize(160, 160)
        self.setStyleSheet("background: transparent;")

        # Smoothed amplitude (EMA) so the orb doesn't twitch on noisy RMS.
        self._displayed_amp = 0.0
        self._last_state = ""
        self._last_state_change = time.monotonic()

        # 30Hz repaint timer with dirty-skip — see file docstring.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

        self._last_paint_amp = -1.0

    # ──────────────── Frame logic ────────────────
    def _tick(self):
        target = max(0.0, min(0.4, self._get_amplitude() or 0.0))
        # Faster attack than decay — when speech starts, the orb wakes
        # up quickly; when it stops, it fades back gracefully instead of
        # snapping to zero. (0.35 attack, 0.15 decay → 65/85 EMA splits.)
        if target > self._displayed_amp:
            self._displayed_amp = 0.65 * self._displayed_amp + 0.35 * target
        else:
            self._displayed_amp = 0.85 * self._displayed_amp + 0.15 * target

        state = self._get_state() or "IDLE"
        if state != self._last_state:
            self._last_state = state
            self._last_state_change = time.monotonic()
            self.update()
            return

        # Skip the redraw if nothing visible has meaningfully changed.
        if abs(self._displayed_amp - self._last_paint_amp) > 0.005:
            self.update()

    # ──────────────── Paint ────────────────
    def paintEvent(self, _event):
        amp = self._displayed_amp
        state = self._last_state or "IDLE"
        self._last_paint_amp = amp

        cx = self.width() / 2
        cy = self.height() / 2
        size = min(self.width(), self.height())
        base_r = size * self.DIAMETER_FRAC * 0.5
        orb_r = base_r * (1.0 + amp * self.BODY_AMP_GAIN)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        center, edge, ring = STATE_COLORS.get(state, STATE_COLORS["IDLE"])

        # 1) Outer halo — biggest, faintest layer. Expands aggressively
        # with amplitude so even at peak there's still an "edge" beyond
        # the bright glow. Visible mostly when SPEAKING/LISTENING.
        halo_r = orb_r * self.GLOW_FRAC * (1.0 + amp * self.GLOW_AMP_GAIN)
        halo = QRadialGradient(QPointF(cx, cy), halo_r)
        h_color = QColor(center)
        h_color.setAlphaF(0.18 + amp * 0.20)
        halo.setColorAt(0.0, h_color)
        halo.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setBrush(QBrush(halo))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(cx, cy), halo_r, halo_r)

        # 2) Inner glow — denser ring of light hugging the orb. This is
        # what gives it the "lit up" feel at speech peaks.
        glow_r = orb_r * (1.0 + amp * 0.9) * 1.45
        glow = QRadialGradient(QPointF(cx, cy), glow_r)
        g_color = QColor(center)
        g_color.setAlphaF(min(1.0, 0.45 + amp * self.ALPHA_AMP_GAIN))
        glow.setColorAt(0.0, g_color)
        mid = QColor(center)
        mid.setAlphaF(0.18 + amp * 0.30)
        glow.setColorAt(0.55, mid)
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setBrush(QBrush(glow))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(cx, cy), glow_r, glow_r)

        # 3) Thin sharp ring just outside the orb body. Holds the eye
        # to the orb's actual edge through the glow blur.
        ring_pen = QPen(QColor(ring))
        ring_pen.setWidthF(1.5)
        painter.setPen(ring_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), orb_r + 4, orb_r + 4)

        # 4) Orb body — radial gradient lit from upper-left for a soft
        # 3D feel without leaning into skeuomorphism.
        body = QRadialGradient(
            QPointF(cx - orb_r * 0.25, cy - orb_r * 0.3),
            orb_r * 1.3,
        )
        body.setColorAt(0.0, QColor(center))
        body.setColorAt(0.7, QColor(edge))
        body.setColorAt(1.0, QColor("#000000"))
        painter.setBrush(QBrush(body))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(cx, cy), orb_r, orb_r)

        # 5) WINSTON name centered inside the orb. Letter-spaced for a
        # hi-tech feel; size scales with orb radius so it doesn't look
        # off if the window is resized. Drawn near-black for contrast
        # against the bright orb body — this is the same trick the
        # original orb used and looked sharp.
        font = QFont()
        font.setFamilies(["JetBrains Mono", "Consolas", "monospace"])
        # Size scales with orb so the name is legible at any window size
        # without overflowing on small windows.
        font.setPointSizeF(max(8, orb_r * 0.20))
        font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 145)
        painter.setFont(font)
        # Slightly translucent black so the gradient reads through —
        # makes the text feel embedded in the orb's surface.
        text_color = QColor("#000000")
        text_color.setAlphaF(0.78)
        painter.setPen(text_color)
        rect = QRectF(cx - orb_r, cy - orb_r, orb_r * 2, orb_r * 2)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "WINSTON")

        painter.end()
