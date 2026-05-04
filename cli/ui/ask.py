"""Modal popup for asking Winston a question.

Replaces the always-visible inline Input widget. Pressing `/` in the
dashboard pushes this screen on top — Textual halts dashboard rendering
while a modal screen is active, so the input has the event loop to
itself. No more contention with panel re-renders, no more dropped
characters.

Flow:
  /          → push AskScreen
  type ...   → only the input is rendering
  Enter      → emit AskSubmitted message, dismiss screen
  Esc        → dismiss without submitting

The parent App listens for AskSubmitted and routes the question to the
CommentaryPanel — same as the old on_input_submitted handler used to do.

Why a Message rather than a direct method call? Keeps the screen
decoupled from CommentaryPanel — the screen doesn't need to know what
happens to the question, just that it was asked. Easier to reuse later
(e.g. /remember could push the same screen with a different label).
"""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Static


class AskScreen(ModalScreen):
    """Popup that prompts for a question. Self-contained.

    Construction:
      placeholder:  hint text shown in the empty input
      label:        small static label above the input
                    ("ask Winston", "remember", etc.)

    Reusable for other prompt-style flows later (/remember, /prefer).
    """

    # We override the default 'escape' binding behavior to make it
    # explicit and consistent with our submit path. The Binding API
    # makes the keybinding visible in any future help screen.
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    AskScreen {
        align: center middle;
        background: black 50%;
    }
    AskScreen > Vertical {
        width: 70;
        height: auto;
        max-width: 90%;
        padding: 1 2;
        background: black;
        border: round cyan;
    }
    AskScreen .ask-label {
        color: ansi_bright_cyan;
        text-style: bold;
        margin-bottom: 1;
    }
    AskScreen Input {
        border: round green;
        background: black;
        color: ansi_bright_cyan;
    }
    AskScreen Input:focus {
        border: round ansi_bright_cyan;
    }
    AskScreen .hint {
        color: grey50;
        margin-top: 1;
        text-align: center;
    }
    """

    class Submitted(Message):
        """Emitted when the user presses Enter with non-empty text.

        The parent App listens for this and routes the value to whoever
        should handle it (CommentaryPanel, a /remember handler, etc.).
        """
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def __init__(self, placeholder: str = "ask Winston something…",
                 label: str = "ASK WINSTON"):
        super().__init__()
        self._placeholder = placeholder
        self._label = label

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._label, classes="ask-label")
            yield Input(placeholder=self._placeholder, id="ask_input")
            yield Static("Enter to send · Esc to cancel", classes="hint")

    def on_mount(self) -> None:
        # Focus the input the moment the screen appears so the user can
        # just start typing — no extra click needed.
        self.query_one("#ask_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter pressed in the input. Emit our own Submitted message
        and dismiss the modal.
        """
        if event.input.id != "ask_input":
            return
        value = event.value.strip()
        if not value:
            # Empty submit → just dismiss, like a cancel.
            self.dismiss()
            return
        # Post the message *before* dismissing so the parent App is
        # guaranteed to receive it (Textual delivers messages in order).
        self.post_message(self.Submitted(value))
        self.dismiss()

    def action_cancel(self) -> None:
        """Esc handler — close without submitting anything."""
        self.dismiss()