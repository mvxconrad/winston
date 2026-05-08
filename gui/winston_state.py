"""Unified Winston state — single source of truth.

One instance lives for the entire app lifetime (created in presence.py).
Every UI face (floating orb, hardware mini-core, command full-core)
subscribes to `state_changed` and renders accordingly.

States: IDLE, LISTENING, TRANSCRIBING, THINKING, SPEAKING, ALERT, ERROR

The state object also tracks which *face* is currently active so that
trigger events and visual emphasis route to the right place. Faces
register/unregister themselves; the state object doesn't know or care
what kind of widget they are.
"""

from PyQt6.QtCore import QObject, pyqtSignal


class WinstonState(QObject):
    """Central state hub for Winston's visual presence.

    Signals
    -------
    state_changed(str)
        Emitted whenever the state changes (e.g. "IDLE" → "LISTENING").
    trigger_fired(str, str)
        Emitted when a trigger fires: (trigger_name, severity).
        Active faces can flash / animate in response.
    """

    state_changed = pyqtSignal(str)
    trigger_fired = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "IDLE"
        self._amplitude = 0.0  # audio amplitude [0..~0.4]
        self._get_amplitude = None  # callback to poll amplitude

    @property
    def state(self) -> str:
        return self._state

    @property
    def amplitude(self) -> float:
        """Current audio amplitude — polled from voice engine."""
        if self._get_amplitude is not None:
            try:
                self._amplitude = max(0.0, min(0.5,
                    self._get_amplitude() or 0.0))
            except Exception:
                pass
        return self._amplitude

    def set_amplitude_source(self, callback):
        """Register a callable that returns current audio amplitude."""
        self._get_amplitude = callback

    def set_state(self, new_state: str):
        """Set Winston's state. Emits state_changed if it actually changed."""
        new_state = new_state.upper()
        if new_state != self._state:
            self._state = new_state
            self.state_changed.emit(new_state)

    def fire_trigger(self, name: str, severity: str = "notable"):
        """Notify all faces that a trigger fired."""
        self.trigger_fired.emit(name, severity)
