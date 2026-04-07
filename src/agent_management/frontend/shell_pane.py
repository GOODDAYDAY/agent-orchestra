"""ShellPane — ephemeral debug zsh pane, shown in the agent grid."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Label, RichLog


class ShellPane(Vertical):
    """A temporary zsh debug pane displayed alongside agent panes.

    Not backed by any Agent or DB record.  The pane lives only as long
    as the user keeps it open (toggled via the 'z' keybinding).
    """

    DEFAULT_CSS = """
    ShellPane {
        border: solid $warning;
        background: $surface;
        height: 20;
    }
    ShellPane .pane-header {
        background: $warning-darken-2;
        height: 3;
        padding: 0 1;
    }
    ShellPane .pane-header Label {
        width: 1fr;
    }
    ShellPane RichLog {
        height: 1fr;
        scrollbar-size: 1 1;
        background: $surface-darken-2;
    }
    ShellPane .pane-controls {
        height: 3;
        padding: 0 1;
        background: $surface-darken-1;
    }
    """

    class CloseRequested(Message):
        """Posted when the user clicks the Close button."""

    def compose(self) -> ComposeResult:
        with Horizontal(classes="pane-header"):
            yield Label("[bold]Debug Shell[/bold] [dim](zsh)[/dim]")
            yield Label(
                "[yellow]tmux attach -t agent-mgmt-debug[/yellow]",
                id="shell-hint",
            )
        yield RichLog(
            id="shell-log",
            auto_scroll=True,
            markup=False,
            highlight=False,
        )
        with Horizontal(classes="pane-controls"):
            yield Button("Close", id="btn-close-shell", variant="warning", compact=True)

    def set_output(self, text: str) -> None:
        log = self.query_one("#shell-log", RichLog)
        log.clear()
        if text:
            log.write(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close-shell":
            self.post_message(self.CloseRequested())
