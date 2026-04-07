"""AgentPane widget — per-agent terminal output and controls."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Button, Input, Label, RichLog

from agent_management.backend.models import Agent, AgentRole, AgentStatus


# Status badge colour map
_STATUS_COLOUR: dict[AgentStatus, str] = {
    AgentStatus.not_started: "dim",
    AgentStatus.starting: "yellow",
    AgentStatus.active: "green",
    AgentStatus.paused: "blue",
    AgentStatus.stopping: "yellow",
    AgentStatus.stopped: "red",
    AgentStatus.degraded: "red bold",
}


class AgentPane(Vertical):
    """Displays one agent's live tmux output and controls."""

    DEFAULT_CSS = """
    AgentPane {
        border-right: solid $primary;
        background: $surface;
        height: 1fr;
        min-height: 14;
    }
    AgentPane .pane-header {
        background: $primary-darken-1;
        height: 1;
        padding: 0 1;
    }
    AgentPane .pane-header Label {
        width: 1fr;
    }
    AgentPane .status-badge {
        width: auto;
    }
    AgentPane .pending-badge {
        width: auto;
        color: yellow;
    }
    AgentPane RichLog {
        height: 1fr;
        scrollbar-size: 1 1;
        background: $surface-darken-2;
    }
    AgentPane .pane-controls {
        height: 1;
        padding: 0;
        background: $surface-darken-1;
    }
    AgentPane Button#enter-agent {
        display: none;
    }
    AgentPane.has-active-pane Button#enter-agent {
        display: block;
    }
    AgentPane .pane-input {
        height: 2;
        padding: 0;
        background: $surface-darken-1;
        display: none;
    }
    AgentPane.focused .pane-input {
        display: block;
    }
    AgentPane .pane-input Input {
        width: 1fr;
    }
    """

    # Messages

    class PauseRequested(Message):
        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    class ResumeRequested(Message):
        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    class EditRequested(Message):
        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    class RestartRequested(Message):
        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    class SendRequested(Message):
        def __init__(self, agent_id: str, text: str) -> None:
            super().__init__()
            self.agent_id = agent_id
            self.text = text

    class DeleteRequested(Message):
        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    class AttachRequested(Message):
        """Posted when the user clicks the Enter button on this pane."""

        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    # Reactives
    status: reactive[AgentStatus] = reactive(AgentStatus.not_started)
    pending_count: reactive[int] = reactive(0)

    def __init__(self, agent: Agent, **kwargs) -> None:
        super().__init__(id=f"pane-{agent.id}", **kwargs)
        self.agent = agent
        self._pane_id: str = ""

    def compose(self) -> ComposeResult:
        with Horizontal(classes="pane-header"):
            yield Label(f"[bold]{self.agent.name}[/bold]", id=f"name-{self.agent.id}")
            yield Label(
                f"[{_STATUS_COLOUR[self.status]}]{self.status.value}[/]",
                id=f"status-{self.agent.id}",
                classes="status-badge",
            )
            yield Label("", id=f"pending-{self.agent.id}", classes="pending-badge")
        yield RichLog(
            id=f"log-{self.agent.id}",
            auto_scroll=True,
            markup=False,
            highlight=False,
        )
        with Horizontal(classes="pane-controls"):
            yield Button("Pause", id=f"btn-pause-{self.agent.id}", variant="warning", compact=True)
            yield Button("Resume", id=f"btn-resume-{self.agent.id}", variant="success", compact=True)
            yield Button("Edit", id=f"btn-edit-{self.agent.id}", variant="default", compact=True)
            yield Button("Restart", id=f"btn-restart-{self.agent.id}", variant="error", compact=True)
            yield Button("Delete", id=f"btn-delete-{self.agent.id}", variant="error", compact=True)
            # Enter button: visible only when agent has a live pane (has-active-pane CSS class)
            yield Button("Enter", id="enter-agent", variant="primary", compact=True)
        with Horizontal(classes="pane-input"):
            yield Input(
                placeholder="Send message to agent…",
                id=f"inp-send-{self.agent.id}",
            )
            yield Button("Send", id=f"btn-send-{self.agent.id}", variant="primary", compact=True)

    # ------------------------------------------------------------------
    # Focus tracking — show/hide input row
    # ------------------------------------------------------------------

    def on_descendant_focus(self) -> None:
        self.add_class("focused")

    def on_descendant_blur(self) -> None:
        # Defer the check so the newly focused widget is already set on app
        self.call_after_refresh(self._maybe_remove_focus)

    def _maybe_remove_focus(self) -> None:
        """Remove 'focused' class if focus has left this pane entirely."""
        focused = self.app.focused
        node = focused
        while node is not None:
            if node is self:
                return  # focus still within this pane
            node = node.parent
        self.remove_class("focused")

    # ------------------------------------------------------------------
    # Update helpers called by the app
    # ------------------------------------------------------------------

    # Statuses that indicate the agent has (or may have) a live tmux pane
    _ACTIVE_PANE_STATUSES = {AgentStatus.active, AgentStatus.paused}

    def update_status(self, status: AgentStatus, pending: int = 0) -> None:
        """Update the status badge and Enter button visibility.

        REQ-012 v2: the secondary `pending_count` field is retained for backwards
        compatibility with the existing AgentStatusChanged message but is no
        longer rendered (no event bus, no pending events).
        """
        self.status = status
        self.pending_count = pending
        badge = self.query_one(f"#status-{self.agent.id}", Label)
        colour = _STATUS_COLOUR.get(status, "white")
        badge.update(f"[{colour}]{status.value}[/]")

        # REQ-012 v2: orchestrator panes get a distinct visual marker so the
        # operator can tell at a glance which pane is in charge.
        pending_label = self.query_one(f"#pending-{self.agent.id}", Label)
        if self.agent.role == AgentRole.orchestrator:
            pending_label.update("[bold magenta] ◆ orchestrator[/]")
        else:
            pending_label.update("")

        if status in self._ACTIVE_PANE_STATUSES:
            self.add_class("has-active-pane")
        else:
            self.remove_class("has-active-pane")

    def append_output(self, text: str) -> None:
        """Append text to the terminal output pane."""
        log = self.query_one(f"#log-{self.agent.id}", RichLog)
        log.write(text)

    def set_output(self, text: str) -> None:
        """Replace the entire pane output (used for tmux capture-pane refresh)."""
        log = self.query_one(f"#log-{self.agent.id}", RichLog)
        log.clear()
        if text:
            log.write(text)

    def set_pane_id(self, pane_id: str) -> None:
        self._pane_id = pane_id

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        aid = self.agent.id
        if bid == f"btn-pause-{aid}":
            self.post_message(self.PauseRequested(agent_id=aid))
        elif bid == f"btn-resume-{aid}":
            self.post_message(self.ResumeRequested(agent_id=aid))
        elif bid == f"btn-edit-{aid}":
            self.post_message(self.EditRequested(agent_id=aid))
        elif bid == f"btn-restart-{aid}":
            self.post_message(self.RestartRequested(agent_id=aid))
        elif bid == f"btn-delete-{aid}":
            self.post_message(self.DeleteRequested(agent_id=aid))
        elif bid == "enter-agent":
            self._handle_enter_pressed()
        elif bid == f"btn-send-{aid}":
            inp = self.query_one(f"#inp-send-{aid}", Input)
            text = inp.value.strip()
            if text:
                self.post_message(self.SendRequested(agent_id=aid, text=text))
                inp.value = ""

    def _handle_enter_pressed(self) -> None:
        """Disable Enter button, post AttachRequested, arm a 5 s force-reenable timer."""
        btn = self.query_one("#enter-agent", Button)
        btn.disabled = True
        self.post_message(self.AttachRequested(agent_id=self.agent.id))
        # Safety net: re-enable after 5 s in case the attach handler never calls back
        self.set_timer(5.0, self._reenable_enter_button)

    def _reenable_enter_button(self) -> None:
        """Re-enable the Enter button (called after attach completes or on timeout)."""
        try:
            btn = self.query_one("#enter-agent", Button)
            btn.disabled = False
        except Exception:
            pass  # Pane may have been removed; ignore

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == f"inp-send-{self.agent.id}":
            text = event.value.strip()
            if text:
                self.post_message(self.SendRequested(agent_id=self.agent.id, text=text))
                event.input.value = ""
