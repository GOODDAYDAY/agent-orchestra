"""EventLog widget — REQ-012 v2: workflow event log.

In v1 this displayed published events from the MCP event bus. In v2 the
event bus is gone — this widget now displays workflow lifecycle events
(WorkflowStepAdvanced, WorkflowCompleted, WorkflowAborted) emitted by the
supervisor's dispatch loop, plus any free-form text the app wants to log.
"""
from __future__ import annotations

from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, RichLog


class EventLog(Vertical):
    """Scrollable log of workflow lifecycle events."""

    DEFAULT_CSS = """
    EventLog {
        height: 5;
        border: solid $primary;
        background: $surface-darken-1;
    }
    EventLog Label#event-log-title {
        background: $primary;
        color: $text;
        text-align: center;
        width: 100%;
    }
    EventLog RichLog {
        height: 1fr;
        scrollbar-size: 1 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Workflow Log", id="event-log-title")
        yield RichLog(id="event-log-content", auto_scroll=True, markup=True)

    def append_text(self, text: str) -> None:
        """Append a free-form line, prefixed with the local time."""
        log = self.query_one("#event-log-content", RichLog)
        ts = datetime.now().strftime("%H:%M:%S")
        log.write(f"[dim]{ts}[/dim] {text}")

    def clear(self) -> None:
        self.query_one("#event-log-content", RichLog).clear()
