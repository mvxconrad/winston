"""Winston's presence face — small orb + voice loop, full Winston brain.

This is the *default* way to run Winston. The dashboard (gui/main.py) is
the same brain with a numbers face on it; presence is the same brain
with a voice + tiny orb. Both share the orchestrator: panels tick,
triggers fire, memory learns, log writes.

Layout:
  - `gui/orb.py`            — the visual itself. Reusable.
  - `PresenceWindow`         — small window: orb + caption strip.
  - `PresenceFace`           — controller: owns CommentaryEngine, routes
                               its output to TTS instead of a typewriter
                               panel.
  - `run()`                  — winston.py entry.

Design rules:
- Same llm_config + memory bootstrap as gui.main.run / cli.display.run —
  Winston is identical regardless of face.
- Triggers fire whether or not anyone's looking. Heartbeat speaks aloud.
  ARK busy speaks aloud. Memory updates from spoken replies just like
  text replies.
- One QApplication. F11 toggles fullscreen. Ctrl+Q quits from anywhere.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Optional

import psutil

from PyQt6.QtCore import (
    Qt, QTimer, QObject, QPoint, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QIcon, QKeyEvent, QMouseEvent, QPainter, QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QMenu, QPushButton, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)

from .orb import Orb


# ──────────────── Voice ↔ UI bridge (thread marshaling) ────────────────
class _PresenceBridge(QObject):
    """Voice + LLM callbacks run on worker threads. Qt signals queue
    them onto the UI thread for safe widget updates. Same role as the
    LlmBridge / VoiceBridge in gui/main.py.

    Why every callback into Qt-touching code goes through a signal:
    QTimer / QObject mutation MUST happen on the thread the object was
    created on. Calling QTimer.start from the speaker's drain callback
    (a fresh worker thread) raises 'Timers cannot be started from
    another thread' and leaves the event loop in a bad state — that's
    what made the second voice response feel laggy. Routing through
    pyqtSignal forces the slot to run on the UI thread.
    """
    state_changed = pyqtSignal(str)
    user_text     = pyqtSignal(str)
    winston_text  = pyqtSignal(str)
    error         = pyqtSignal(str)
    chunk         = pyqtSignal(str)
    done          = pyqtSignal(str)
    llm_error     = pyqtSignal()
    # Fired when speech finishes (TTS playback drained). The speaker
    # callback runs on PortAudio's thread; we use this signal to hop
    # back to the UI thread before mutating engine state or restarting
    # the trigger QTimer.
    speech_done   = pyqtSignal()


# ──────────────── The window ────────────────
class PresenceWindow(QMainWindow):
    """Floating Winston orb. Frameless, transparent, always-on-top.

    Just the circle — no caption, no chrome by default. The orb shows
    its own state via color + amplitude pulse, and the WINSTON name is
    rendered inside the orb body (see gui/orb.py). All other UI is
    hover-only:

      Hover      → close (×) + minimize (−) buttons fade in top-right
      Drag       → click + move anywhere; the whole window follows
      Double-click → opens the dashboard (gui/main.py) as a separate
                     process so the orb keeps running alongside it
      Hold space → push-to-talk (same as before)
      J          → opens the dashboard (alternative to double-click)
      Ctrl+Q     → quit

    Why frameless + always-on-top:
      The orb is meant to be a *presence* on your screen, not a panel
      to be looked at. You glance over, see the state color, talk to
      Winston, glance back at your work. A title bar + taskbar row
      breaks that feel.
    """

    KEY_TALK = Qt.Key.Key_Space
    KEY_DASHBOARD = Qt.Key.Key_J
    HOVER_FADE_MS = 180   # how long enter/leave hover-control fade lasts

    def __init__(self, voice_engine, face, parent=None):
        super().__init__(parent)
        self.engine = voice_engine
        self.face = face

        # Frameless + always-on-top + translucent so only the orb is
        # visible. WA_TranslucentBackground lets the rounded orb edges
        # blend with whatever's behind the window.
        #
        # NOTE: deliberately NO Qt.Tool flag. On Windows, Tool windows
        # auto-hide when focus shifts elsewhere; combined with Qt's
        # default quitOnLastWindowClosed=True, the entire app would
        # exit the moment Windows decided to hide the orb. (We also
        # set quitOnLastWindowClosed=False in run() as belt-and-
        # suspenders against this same class of bug.) Tool was
        # originally added to hide from the Wayland taskbar — that
        # benefit isn't worth the Windows fragility.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Winston")
        self.resize(280, 280)
        # Track mouse so enterEvent / leaveEvent fire even when the
        # button isn't pressed. Without this, hover state is unreliable
        # on some Wayland compositors.
        self.setMouseTracking(True)

        # ── Central widget = the orb ──
        # The orb fills the window. We make the orb mouse-transparent so
        # all mouse events (drag, double-click) reach PresenceWindow's
        # handlers — otherwise the orb's QWidget would swallow them.
        central = QWidget()
        central.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        central.setMouseTracking(True)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._orb = Orb(
            get_state=lambda: self.engine.state,
            get_amplitude=lambda: self.engine.amplitude(),
        )
        # Make the orb invisible to the mouse so clicks fall through
        # to the window. We still SEE it; the hit-testing layer is
        # transparent.
        self._orb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._orb)

        # ── Hover controls (close + minimize) ──
        # Two tiny round buttons in the top-right corner. Hidden by
        # default; fade in on enter, out on leave.
        btn_style = """
            QPushButton {
                color: rgba(220, 220, 220, 230);
                background: rgba(20, 20, 20, 200);
                border: 1px solid rgba(120, 120, 120, 120);
                border-radius: 11px;
                font-family: monospace;
                font-size: 14px;
                font-weight: bold;
                padding: 0;
            }
            QPushButton:hover {
                color: white;
                background: rgba(70, 70, 70, 230);
                border-color: rgba(180, 180, 180, 200);
            }
        """
        self._close_btn = QPushButton("×", self)
        self._close_btn.setFixedSize(22, 22)
        self._close_btn.setStyleSheet(btn_style)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # Explicit QApplication.quit() — we set quitOnLastWindowClosed
        # to False in run() so window-close alone wouldn't actually
        # exit the process. The user pressing × means they want OUT.
        self._close_btn.clicked.connect(QApplication.quit)
        self._close_btn.hide()

        self._min_btn = QPushButton("−", self)
        self._min_btn.setFixedSize(22, 22)
        self._min_btn.setStyleSheet(btn_style)
        self._min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._min_btn.clicked.connect(self.showMinimized)
        self._min_btn.hide()

        # Drag state — populated on press, consumed on move/release.
        self._drag_origin: Optional[QPoint] = None

        # ── Bridge wiring (unchanged from the captioned version) ──
        self._bridge = _PresenceBridge()
        self._bridge.state_changed.connect(self._on_state)
        self._bridge.error.connect(self._on_error)
        # LLM stream signals — face routes brain.client callbacks here.
        self._bridge.chunk.connect(self.face.on_llm_chunk)
        self._bridge.done.connect(self.face.on_llm_done)
        self._bridge.llm_error.connect(self.face.on_llm_error)

        # Hook voice engine callbacks → bridge signals.
        self.engine.on_state_change = self._bridge.state_changed.emit
        # No caption to update — voice engine.on_user_text / on_winston_text
        # are intentionally unwired in this UI. (Brain still records
        # them via memory.json + history; we just don't show them.)
        self.engine.on_user_text = lambda _t: None
        self.engine.on_winston_text = lambda _t: None
        self.engine.on_error = self._bridge.error.emit

        # Tell the face about the bridge so it can emit chunk/done from
        # brain.client's worker threads safely.
        self.face.set_bridge(self._bridge)

        # ── System tray icon (watchdog mode) ──
        # Always created so Winston has a tray presence. In watchdog mode
        # the window starts hidden and the tray icon is the only visible
        # sign Winston is running. Double-click the tray icon to toggle
        # the orb. Right-click for a context menu.
        self._tray = None
        self._tray_menu = None   # prevent GC — Qt doesn't own the menu
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(self._make_tray_icon(), self)
            self._tray.setToolTip("Winston")
            self._tray_menu = QMenu()
            self._tray_menu.addAction("Show Orb", self._tray_show_orb)
            self._tray_menu.addAction("Open Dashboard", self._open_dashboard)
            self._tray_menu.addSeparator()
            self._tray_menu.addAction("Quit", QApplication.quit)
            self._tray.setContextMenu(self._tray_menu)
            self._tray.activated.connect(self._on_tray_activated)

        # ── Linger timer (watchdog auto-hide) ──
        # After Winston finishes speaking in watchdog mode, the orb stays
        # visible for WATCHDOG_LINGER_SEC then hides back to tray.
        self._linger_timer = QTimer(self)
        self._linger_timer.setSingleShot(True)
        self._linger_timer.timeout.connect(self._linger_expired)

        # True when the user manually opened the orb from the tray icon.
        # Prevents watchdog auto-hide so the orb stays up until the user
        # explicitly closes it.
        self._user_summoned = False

        self._talk_held = False
        self._reposition_controls()

    # ──────────────── Hover control layout ────────────────
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_controls()

    def _reposition_controls(self):
        # Top-right corner, with a small inset margin.
        inset = 8
        spacing = 6
        x = self.width() - self._close_btn.width() - inset
        y = inset
        self._close_btn.move(x, y)
        self._min_btn.move(x - self._min_btn.width() - spacing, y)

    def enterEvent(self, event):
        self._close_btn.show()
        self._min_btn.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._close_btn.hide()
        self._min_btn.hide()
        super().leaveEvent(event)

    # ──────────────── Drag (frameless window has no title bar) ────────
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Wayland compositors REFUSE manual window.move() — security
            # rule, clients can't position themselves arbitrarily. The
            # right API is QWindow.startSystemMove(), which delegates the
            # drag to the compositor. Works on Wayland, Windows, and X11
            # (X11 uses _NET_WM_MOVERESIZE under the hood). If for some
            # reason it returns False we fall back to manual move so
            # we still drag on platforms that don't expose the API.
            wh = self.windowHandle()
            if wh is not None and wh.startSystemMove():
                event.accept()
                return
            # Fallback: capture origin for manual move in mouseMoveEvent.
            self._drag_origin = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # Manual-move fallback path. Most of the time startSystemMove
        # took over and we never get here.
        if (self._drag_origin is not None
                and (event.buttons() & Qt.MouseButton.LeftButton)):
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        # Double-click anywhere on the orb / window opens the dashboard.
        if event.button() == Qt.MouseButton.LeftButton:
            self._open_dashboard()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # ──────────────── Bridge slots ────────────────
    def _on_state(self, _state: str):
        # No caption to update; the orb already shifts color.
        # Kept as a hook in case we add subtle window-level effects
        # later (e.g. a colored border tint at peak amplitude).
        pass

    def _on_error(self, msg: str):
        # No caption strip in this UI. Errors land in stdout via
        # voice_engine's [voice] ERROR print so dev still sees them.
        # Could surface as a brief red tint on the orb — left for later.
        print(f"[presence] error: {msg}", flush=True)

    # ──────────────── Tray icon helpers ────────────────
    @staticmethod
    def _make_tray_icon() -> QIcon:
        """Generate a simple green circle icon for the system tray.
        No external asset needed — painted at runtime."""
        size = 64
        pm = QPixmap(size, size)
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(QColor(100, 220, 120)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(4, 4, size - 8, size - 8)
        p.end()
        return QIcon(pm)

    def _on_tray_activated(self, reason):
        """Click or double-click the tray icon to show the orb.

        On Windows, single left-click fires Trigger (the most common
        interaction); double-click fires DoubleClick. We handle both
        so the user doesn't have to guess.
        """
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._tray_show_orb()

    def _tray_show_orb(self):
        """Bring the orb window back from the tray.

        Cancels any pending watchdog auto-hide and marks the orb as
        user-summoned so it stays visible until the user explicitly
        closes it. Also fires a time-aware greeting ("Good afternoon,
        Max") so Winston acknowledges being summoned.
        """
        self._linger_timer.stop()
        self._user_summoned = True
        self.show()
        self.raise_()
        self.activateWindow()
        # Ensure keyboard focus so push-to-talk (space) works immediately.
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        # Fire a greeting — Winston should say hello when summoned.
        # Deferred by 200ms so the window has fully painted and the Qt
        # event loop can settle after the tray activation signal.
        QTimer.singleShot(200, self.face.fire_tray_greeting)

    def show_for_watchdog(self):
        """Watchdog trigger fired — show the orb. Cancel any pending
        linger-hide so the window stays up for the full speech.

        If the orb is already visible (user summoned it from tray),
        leave _user_summoned True so the orb won't auto-hide after
        the triggered speech finishes.
        """
        if not self.isVisible():
            self._user_summoned = False
        self._linger_timer.stop()
        self.show()
        self.raise_()

    def hide_after_linger(self, linger_sec: float):
        """Start the linger countdown. After linger_sec the orb hides
        back to the system tray."""
        ms = int(linger_sec * 1000)
        self._linger_timer.start(max(ms, 500))

    def _linger_expired(self):
        """Linger time up — hide back to tray.

        If the user manually opened the orb from the tray icon, we
        respect that and leave it visible. Auto-hide only applies to
        watchdog-triggered appearances.
        """
        if self._user_summoned:
            return
        self.hide()

    # ──────────────── Dashboard launch ────────────────
    def _open_dashboard(self):
        """Open the dashboard in-process. Same panels, same hub, no
        duplicate polling. The dashboard is panels-only (satellite mode)
        — the orb keeps running the brain and voice.

        Closing the dashboard window just hides it and tells the hub
        to deactivate the dashboard-only panels (CpuGraphPanel, DiskPanel).

        Always defers to the UI thread via QTimer.singleShot(0) because
        this can be called from a voice-command worker thread — creating
        QWidgets off the main thread crashes Qt with "Cannot create
        children for a parent that is in a different thread".
        """
        QTimer.singleShot(0, self._open_dashboard_ui)

    def _open_dashboard_ui(self):
        """Actual dashboard creation — guaranteed to run on the UI thread."""
        # Already open — just raise it.
        if (hasattr(self, '_dashboard') and self._dashboard is not None
                and self._dashboard.isVisible()):
            self._dashboard.raise_()
            self._dashboard.activateWindow()
            return

        try:
            from gui.main import WinstonGui
            hub = getattr(self, '_hub', None)
            # Build the dashboard using the same sections the hub polls.
            sections = hub.sections if hub else []
            dash = WinstonGui(sections, logger=None, satellite=True, hub=hub)
            dash.show()
            self._dashboard = dash

            # Tell the hub to start polling dashboard-only panels
            # (CpuGraphPanel, DiskPanel) now that someone's looking.
            if hub is not None:
                hub.activate_all()
        except Exception as e:
            print(f"[presence] failed to open dashboard: {e!r}", flush=True)

    # ──────────────── Keys ────────────────
    def keyPressEvent(self, event: QKeyEvent):
        if event.isAutoRepeat():
            return
        key = event.key()
        mods = event.modifiers()
        # Ctrl+Q from anywhere — quit. Matches the dashboard hotkey.
        # QApplication.quit() not self.close() — we disabled
        # quitOnLastWindowClosed in run().
        if key == Qt.Key.Key_Q and (mods & Qt.KeyboardModifier.ControlModifier):
            QApplication.quit()
            return
        if key == self.KEY_TALK:
            if not self._talk_held:
                self._talk_held = True
                self.engine.start_listening()
            return
        if key == self.KEY_DASHBOARD:
            self._open_dashboard()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent):
        if event.isAutoRepeat():
            return
        if event.key() == self.KEY_TALK and self._talk_held:
            self._talk_held = False
            self.engine.stop_listening()
            return
        super().keyReleaseEvent(event)


# ──────────────── PresenceFace — the controller ────────────────
class PresenceFace:
    """The brain wiring for presence mode.

    Owns:
      - CommentaryEngine (the same brain GUI/CLI use)
      - 1Hz trigger tick
      - LLM stream wiring → engine.on_chunk/on_done/on_error
      - Bridge-aware speak path: when a stream finishes, finalize the
        engine (marker parsing, memory writes, history) and hand the
        final text to VoiceEngine.speak_text. When playback drains,
        release the engine cooldown so the next event can fire.

    Doesn't own any widgets. The window holds the bridge; the face just
    needs a reference to it so it can route brain.client's worker-thread
    callbacks safely onto the UI thread.
    """

    def __init__(self, voice_engine, llm_config, memory, sections,
                 watchdog: bool = False):
        self.voice = voice_engine
        from brain.commentary_engine import CommentaryEngine
        self.engine = CommentaryEngine(sections, llm_config or {}, memory)
        self.sections = sections

        # Watchdog mode: dormant by default, wake on trigger, sleep after
        # speech. When False, behaves like the old always-visible presence.
        self.watchdog = watchdog

        self._bridge: Optional[_PresenceBridge] = None
        # Back-reference to the window — set by run() after construction.
        # Needed so the face can call window.show_for_watchdog() when a
        # trigger fires and window.hide_after_linger() when speech ends.
        self._window: Optional["PresenceWindow"] = None

        # 1Hz tick. The Qt timer is created on bridge attach so the face
        # is constructible without a running QApplication (test mode).
        self._trigger_timer: Optional[QTimer] = None

        # Active SentenceStreamSpeaker while an LLM stream + TTS pipeline
        # is in flight. None when idle.
        self._sentence_streamer = None

        # Wire VoiceEngine's push-to-talk to route through OUR brain. The
        # engine sets state THINKING after STT and waits; we handle the
        # rest (LLM via CommentaryEngine, then TTS via voice.speak_text).
        self.voice.on_user_text_complete = self.handle_user_speech

    # ──────────────── Setup ────────────────
    def set_bridge(self, bridge: _PresenceBridge):
        """Called by PresenceWindow at construction. Lets brain.client's
        worker threads emit chunk/done/error onto the UI thread."""
        self._bridge = bridge
        # Now we have a Qt context — set up the trigger ticker.
        self._trigger_timer = QTimer()
        self._trigger_timer.timeout.connect(self._trigger_tick)
        # Marshal speech-finished onto the UI thread. The speaker's
        # drain callback runs on PortAudio's thread; mutating engine
        # state or restarting QTimers from there crashes Qt's timer
        # subsystem ("Timers cannot be started from another thread").
        bridge.speech_done.connect(self._on_speech_done_ui)

    def start(self):
        """Begin Winston's regular loop.

        Three paths:
          - Watchdog mode → skip greeting entirely, go straight to
            trigger loop. Winston is dormant — no reason to announce.
          - Greeting on   → fire the greeting prompt immediately ("Good
            afternoon, max" or similar). When that finishes speaking,
            the post-speech callback drops us into the regular trigger
            loop. Skips the GUI's longer "retrospective" step — voice
            mode wants short.
          - Greeting off  → straight into trigger loop, no opening line.

        The trigger timer only starts AFTER the greeting plays so we
        don't fire a heartbeat observation on top of "Good afternoon".
        """
        if self.engine.state == "DISABLED":
            return
        # Watchdog mode: no greeting, straight to dormant monitoring.
        if self.watchdog:
            self._begin_regular_loop()
            return
        if self.engine.config.get("startup_greeting", True):
            self.engine.startup_step = "greeting"
            system, prompt, tier = self.engine.build_greeting()
            if system is None:
                self._begin_regular_loop()
                return
            self._fire_stream(system, prompt, tier)
        else:
            self._begin_regular_loop()

    def _begin_regular_loop(self):
        """Init triggers and start the 1Hz tick. Idempotent — safe to
        call from either the post-greeting hook or directly from
        start()."""
        self.engine.startup_step = None
        self.engine.init_triggers()
        if self._trigger_timer is not None and not self._trigger_timer.isActive():
            self._trigger_timer.start(1000)

    # ──────────────── Tray-summoned greeting ────────────────
    def fire_tray_greeting(self):
        """User clicked the tray icon — greet with a time-appropriate
        "Good morning/afternoon/evening, Max".

        Guards:
          - Engine disabled → skip.
          - Engine already busy (streaming, cooldown) → skip silently.
            The user hears the in-flight speech instead.
          - build_greeting returns None → skip (shouldn't happen).
        """
        if self.engine.state == "DISABLED":
            return
        if self.engine.is_busy:
            return
        self.engine.startup_step = "greeting"
        system, prompt, tier = self.engine.build_greeting()
        if system is None:
            return
        self._fire_stream(system, prompt, tier)

    # ──────────────── Voice commands ────────────────
    # Regex patterns for voice commands that bypass the LLM entirely.
    # Each entry is (compiled_regex, handler_method_name).
    _VOICE_COMMANDS = [
        (re.compile(
            r"\b(show|open|pull up|bring up|launch|display)"
            r"\b.{0,15}\b(dashboard|the dashboard|my dashboard)\b",
            re.IGNORECASE,
        ), "_cmd_open_dashboard"),
    ]

    def _check_voice_commands(self, text: str) -> bool:
        """Check if `text` matches a voice command. If so, execute it
        and speak a short confirmation. Returns True if handled."""
        for pattern, method_name in self._VOICE_COMMANDS:
            if pattern.search(text):
                handler = getattr(self, method_name, None)
                if handler:
                    handler()
                    return True
        return False

    def _cmd_open_dashboard(self):
        """Voice command: open the dashboard and confirm verbally."""
        if self._window is not None:
            self._window._open_dashboard()
        # Speak a short confirmation without hitting the LLM.
        self.voice.speak_text(
            "Opening the dashboard.",
            on_done=lambda: None,
        )

    # ──────────────── User-driven path ────────────────
    def handle_user_speech(self, text: str):
        """Voice engine just transcribed `text`. Route through the
        commentary engine's conversational path so memory + tiered
        models + history all apply, exactly like the dashboard's ASK
        input would.

        Called from VoiceEngine's worker thread — we marshal to the UI
        thread via the bridge so commentary engine state isn't touched
        cross-thread. Concretely: we ask brain.client to async-stream;
        its callbacks (also worker-thread) hop through bridge signals
        to the engine on the UI thread.
        """
        # Check for voice commands first (dashboard, etc.)
        if self._check_voice_commands(text):
            return
        recorded = self.engine.handle_user_question(text)
        if recorded is None:
            return
        system, prompt, tier = self.engine.build_conversational(recorded)
        if system is None:
            return
        self._fire_stream(system, prompt, tier)

    # ──────────────── Trigger-driven path ────────────────
    def _trigger_tick(self):
        """1Hz: ask the engine if there's anything worth saying. If there
        is, fire a stream. Engine handles cooldown, busy-state, severity
        preemption — we just relay.

        In watchdog mode: heartbeats and stale-quiet are suppressed — only
        real trigger events wake Winston. When a trigger does fire, the
        orb window is un-hidden before the LLM stream starts so the user
        sees the orb appear as Winston starts thinking.
        """
        result = self.engine.evaluate_triggers()
        if result is None:
            return
        kind, payload = result
        # Watchdog mode: suppress heartbeat and stale-quiet when
        # WATCHDOG_SUPPRESS_HEARTBEAT is True (default). Only actual
        # trigger events ("event") get through.
        if self.watchdog and kind != "event":
            import config as _cfg
            if getattr(_cfg, "WATCHDOG_SUPPRESS_HEARTBEAT", True):
                return
        if kind == "event":
            system, prompt, tier = self.engine.build_triggered(payload)
        else:
            system, prompt, tier = self.engine.build_observation()
        if system is None:
            return
        # Watchdog: show the orb before the LLM starts streaming so the
        # user sees Winston wake up as he begins thinking.
        if self.watchdog and self._window is not None:
            self._window.show_for_watchdog()
        self._fire_stream(system, prompt, tier)

    # ──────────────── Stream wiring ────────────────
    def _fire_stream(self, system: str, prompt: str, tier: str):
        from brain.client import generate_stream_async
        self.engine.begin_streaming()
        model, keep_alive = self.engine.pick_model(tier)
        # Latency telemetry — strip these prints once you trust the
        # numbers. Each line answers a specific question:
        #   [t] LLM fired       → did the request actually go out fast?
        #   [t] LLM first chunk → cold-load tax visible here? (3-10s = bad)
        #   [t] LLM done        → total LLM time
        #   [t] TTS first audio → time from "speak" to ear
        # Compare to STT timing logged in voice_engine._run_pipeline.
        self._t_fire = time.monotonic()
        self._t_first_chunk: Optional[float] = None
        self._t_llm_done: Optional[float] = None
        # Sentence-streaming TTS: start a SentenceStreamSpeaker that
        # will receive LLM chunks in on_llm_chunk, fire TTS per
        # sentence, and play audio through the speaker as sentences
        # complete — all while the LLM is still generating.
        self._sentence_streamer = self.voice.speak_streamed(
            on_done=self._bridge.speech_done.emit,
        )
        # Bridge signals are queued; safe to emit from brain.client's
        # worker thread.
        b = self._bridge
        generate_stream_async(
            prompt, system=system, model=model, keep_alive=keep_alive,
            on_chunk=b.chunk.emit if b else (lambda c: None),
            on_done=b.done.emit if b else (lambda t: None),
            on_error=b.llm_error.emit if b else (lambda: None),
        )

    # Bridge slots — these run on the UI thread.
    def on_llm_chunk(self, chunk: str):
        if self._t_first_chunk is None:
            self._t_first_chunk = time.monotonic()
        self.engine.on_chunk(chunk)
        # Feed chunk to sentence streamer — it will fire TTS as soon
        # as a complete sentence accumulates.
        if self._sentence_streamer is not None:
            self._sentence_streamer.add_chunk(chunk)

    def on_llm_done(self, full_text: str):
        """Stream finished. Finalize the commentary engine (marker
        parsing, memory writes, history) and flush the sentence streamer
        so any remaining text gets TTS'd.

        Key difference from the old non-streaming path: we do NOT call
        voice.speak_text here. The sentence streamer has already been
        firing TTS per-sentence as the LLM streamed. We just flush
        whatever partial sentence remained and let the speaker drain
        naturally. The on_finished callback (wired in _fire_stream)
        will emit speech_done when all audio has played.
        """
        self._t_llm_done = time.monotonic()
        self.engine.on_done(full_text)
        # Snap the typewriter to the end so the next tick finalizes.
        self.engine.typed_chars = len(self.engine.streaming_buffer)
        # Drain any pending markers + push the message into history.
        self.engine.typewriter_advance()

        # Flush the sentence streamer — sends any remaining buffered
        # text to TTS and signals the worker to mark_complete the
        # speaker when done. If no text was generated (empty reply),
        # the flush sends nothing and mark_complete fires immediately,
        # which triggers the on_finished → speech_done path.
        if self._sentence_streamer is not None:
            self._sentence_streamer.flush()
            self._sentence_streamer = None
        else:
            # Fallback: no streamer (shouldn't happen). Emit speech_done
            # directly so cooldown still releases.
            self._bridge.speech_done.emit()

    def _on_speech_done_ui(self):
        """Speech finished. Runs on the UI thread (via bridge signal).
        Release the engine cooldown so the next event can fire. If we
        just finished the startup greeting, drop into the regular
        trigger loop — start the QTimer here, where it's legal.

        In watchdog mode: start the linger countdown so the orb auto-
        hides back to the system tray after WATCHDOG_LINGER_SEC.
        """
        was_greeting = self.engine.startup_step == "greeting"
        self.engine.end_cooldown()
        if was_greeting:
            self._begin_regular_loop()
        # Watchdog: start the linger-then-hide countdown.
        if self.watchdog and self._window is not None:
            import config as _cfg
            linger = getattr(_cfg, "WATCHDOG_LINGER_SEC", 8)
            self._window.hide_after_linger(linger)

    def on_llm_error(self):
        """LLM stream errored. Engine wants to know; voice goes back to
        IDLE; cooldown released so a follow-up can be tried. If the
        failed call was the startup greeting, advance past it instead
        of getting stuck.

        Already on UI thread (bridge.llm_error → here), but we still
        emit speech_done rather than calling _on_speech_done_ui
        directly so there's exactly one path that releases cooldown
        and restarts the timer.
        """
        # Cancel any in-flight sentence streamer so it doesn't try to
        # TTS partial text or leave the speaker in a weird state.
        if self._sentence_streamer is not None:
            self._sentence_streamer.cancel()
            self._sentence_streamer = None
        self.engine.on_error()
        self._bridge.speech_done.emit()


# ──────────────── Entry point — winston.py calls this ────────────────
def run(sections, logger, config=None, hub=None, watchdog=None):
    """Same signature as gui.main.run / cli.display.run, so winston.py
    can dispatch by flag without further plumbing.

    `hub` — SensorHub instance that owns all panel polling. If provided,
    the orb doesn't start its own panel loop — it reads from the hub's
    shared panel objects.

    `watchdog` — if True, Winston starts hidden in the system tray and
    only shows the orb when a trigger fires. After speaking, the orb
    lingers for WATCHDOG_LINGER_SEC then hides again. No greeting, no
    heartbeats, no periodic chatter — spikes only. None means "read
    config.WATCHDOG_MODE"; True/False are authoritative overrides.

    Boot sequence:
      1. Prime panels (one update so first frame has real data).
      2. Build llm_config + memory (mirror of gui.main.run).
      3. Build VoiceEngine; warm STT + TTS in background.
      4. Build PresenceFace + PresenceWindow; wire bridge.
      5. Start the panel ticker (1Hz across all panels — same data the
         dashboard reads). Triggers see fresh data, log writes too.
      6. Start the face's regular loop. Show window. Run Qt.

    The logger ticks via a separate timer at LOGGER_HZ — same as the
    dashboards. Voice mode is full Winston, not a stripped-down voice
    toy.
    """
    if config is None:
        import config as default_config
        config = default_config

    # Resolve watchdog flag. None = "nobody told me" → read config.
    # True/False from the caller (--presence vs default) are authoritative.
    if watchdog is None:
        watchdog = getattr(config, "WATCHDOG_MODE", False)

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

    # llm_config — same shape as gui.main.run() but with voice-mode-only
    # overrides for keep_alive.
    #
    # Why voice mode bumps keep_alive: config.LLM_*_KEEP_ALIVE_SEC = 0
    # is the right default for the dashboard (Winston speaks every few
    # minutes — fine to free VRAM between heartbeats). For voice it's
    # devastating: every push-to-talk pays a 3-10s cold-load tax for
    # the 7b. After 10 minutes idle the model still unloads, so we
    # don't permanently squat on VRAM during a game session.
    voice_keep_alive_sec = 600
    # Voice mode picks the FAST model for both tiers. The 7b is great
    # for the dashboard's longer typed answers but costs ~300-500ms more
    # per reply in voice mode — and that latency is what you feel as
    # "laggy". 3b's reply quality on short conversational turns is
    # nearly identical and the latency win is dramatic.
    voice_model = getattr(config, "LLM_MODEL_FAST", config.LLM_MODEL)
    llm_config = {
        "enabled":                 config.LLM_ENABLED,
        "model":                   voice_model,
        "use_tiered":              getattr(config, "LLM_USE_TIERED", False),
        "model_fast":              voice_model,
        "model_quality":           voice_model,  # collapse tiers in voice
        "fast_keep_alive_sec":     voice_keep_alive_sec,
        "quality_keep_alive_sec":  voice_keep_alive_sec,
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

    # Memory bootstrap — exact mirror of gui.main.run.
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

    # Qt app + voice engine.
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    from brain.voice.voice_engine import VoiceEngine
    voice = VoiceEngine(
        sections=[p for p, _ in sections],
        llm_config=llm_config,
        memory=memory,
    )
    voice.warm_up()

    face = PresenceFace(
        voice_engine=voice,
        llm_config=llm_config,
        memory=memory,
        sections=sections,
        watchdog=watchdog,
    )

    window = PresenceWindow(voice_engine=voice, face=face)
    face._window = window   # back-reference for show/hide in watchdog

    if watchdog:
        # Watchdog: start hidden, tray icon is the only visible presence.
        # The orb will appear when a trigger fires.
        if window._tray is not None:
            window._tray.show()
    else:
        window.show()
        # Show tray icon even in normal presence mode — gives a way to
        # restore the orb if it gets minimized or lost.
        if window._tray is not None:
            window._tray.show()

    # SensorHub — single source of truth for all hardware data. The hub
    # owns the polling thread and the logger. Both the orb and (later)
    # the in-process dashboard read from the same panel objects.
    if hub is not None:
        hub.start()
    else:
        # Fallback for direct calls without a hub (e.g. tests).
        from sensor_hub import SensorHub
        hub = SensorHub(sections, logger=logger, config=config)
        hub.start()

    # Store hub reference on the window so _open_dashboard can pass it
    # to the in-process dashboard.
    window._hub = hub

    # Stop the hub BEFORE Qt tears down its event dispatcher. Without
    # this, the hub's daemon thread keeps calling panel.update() while
    # Qt is destroying widgets, producing floods of "QBasicTimer::start:
    # current thread's event dispatcher has already been destroyed".
    def _cleanup():
        hub.stop()
        if logger is not None:
            try:
                logger.close()
            except Exception:
                pass
    app.aboutToQuit.connect(_cleanup)

    # Kick off the brain after a short delay so panels have ticked at
    # least once and the first observation has real data to talk about.
    QTimer.singleShot(800, face.start)

    app.exec()
