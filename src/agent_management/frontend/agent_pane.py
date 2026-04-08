"""AgentPane widget — per-agent terminal output and controls.

REQ-015 layout (Native-First Interaction):

    ┌────────── Header: name | status | role-marker ──────────┐
    │                                                          │
    │  Read-only preview (RichLog with ANSI rendering and      │
    │  scroll lock). Focusable: pressing Enter while focused   │
    │  triggers Attach (full tmux switch).                     │
    │                                                          │
    │  [↓ jump to latest]   ← shown only when scrolled up      │
    │                                                          │
    │  Admin controls: Pause Resume Edit Restart Delete Enter  │
    │  Quick keyboard:  Continue  Y  N  Esc  ^C  ↑  ↓  ^D      │
    │  ⌨ Input box (focus catcher for pure key forwarding)     │
    └──────────────────────────────────────────────────────────┘

The OLD per-pane Input + Send affordance has been deleted. Real-time
typing is handled by the InputBox focus catcher; common one-shot keys
are handled by the quick-keyboard buttons; full interactive sessions
use Attach (Enter button or Enter key on focused preview).
"""
from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Button, Label, RichLog, Static

from agent_management.backend.models import Agent, AgentRole, AgentStatus
from agent_management.frontend.key_forwarding import textual_to_tmux, tmux_args_for_key
from agent_management.shared.config import OUTPUT_BUFFER_LINES


# ---- Status colour map ------------------------------------------------------

_STATUS_COLOUR: dict[AgentStatus, str] = {
    AgentStatus.not_started: "dim",
    AgentStatus.starting: "yellow",
    AgentStatus.active: "green",
    AgentStatus.paused: "blue",
    AgentStatus.stopping: "yellow",
    AgentStatus.stopped: "red",
    AgentStatus.degraded: "red bold",
}


# ---- REQ-015 F-03: Quick keyboard layout ------------------------------------

# (button label, internal key id used in the button widget id)
QUICK_KEYS: list[tuple[str, str]] = [
    ("Continue", "continue"),
    ("Y",        "y"),
    ("N",        "n"),
    ("Esc",      "escape"),
    ("^C",       "ctrl-c"),
    ("↑",        "up"),
    ("↓",        "down"),
    ("^D",       "ctrl-d"),
]

# Maps a quick-key id to the tmux send-keys argv list it produces.
QUICK_KEY_SPECS: dict[str, list[str]] = {
    "continue": ["continue", "Enter"],
    "y":        ["y", "Enter"],
    "n":        ["n", "Enter"],
    "escape":   ["Escape"],
    "ctrl-c":   ["C-c"],
    "up":       ["Up"],
    "down":     ["Down"],
    "ctrl-d":   ["C-d"],
}


# ---- REQ-015 F-04: dedicated input box (focus catcher) ----------------------

class InputBox(Static):
    """Focus catcher for pure key forwarding to a tmux pane.

    When focused, every Textual Key event is mapped via
    `key_forwarding.textual_to_tmux` and forwarded to the agent's pane via
    SessionManager.send_raw_keys (the message handler in app.py does the
    actual call). Unmapped keys are dropped silently.

    Leaving focus: double-press Esc, or click outside the widget. A single
    Esc is forwarded to the agent (Claude CLI uses Escape to dismiss menus).
    """

    DEFAULT_CSS = """
    InputBox {
        height: 1;
        background: $surface-darken-1;
        color: $text-muted;
        padding: 0 1;
    }
    InputBox:focus {
        background: $primary;
        color: $text;
        text-style: bold;
    }
    """

    can_focus = True

    PLACEHOLDER = "⌨ click here to type to agent (double-Esc to leave)"

    class KeyForwarded(Message):
        """Posted when a key is forwarded to the agent."""

        def __init__(self, agent_id: str, spec: list[str]) -> None:
            super().__init__()
            self.agent_id = agent_id
            self.spec = spec

    def __init__(self, agent_id: str, **kwargs) -> None:
        super().__init__(self.PLACEHOLDER, **kwargs)
        self.agent_id = agent_id
        self._last_key_was_escape = False
        self._echo: str = ""

    async def on_key(self, event: events.Key) -> None:
        # REQ-016 F-02: use the event.character-aware helper so shift-modified
        # punctuation (!, @, #, ...) forwards correctly.
        spec = tmux_args_for_key(event)
        if spec is None:
            return  # let it bubble (we don't recognise it)

        # Detect double-Esc to leave the input box. We forward the FIRST Esc
        # too — Claude CLI uses it to cancel menus, so it's a real keystroke
        # we want to send. The second Esc within the same focus session
        # exits without forwarding the second one.
        if event.key == "escape":
            if self._last_key_was_escape:
                event.stop()
                event.prevent_default()
                self._last_key_was_escape = False
                self._reset_echo()
                # Move focus to the parent AgentPane container if possible.
                self.app.set_focus(None)
                return
            self._last_key_was_escape = True
        else:
            self._last_key_was_escape = False

        event.stop()
        event.prevent_default()
        self.post_message(self.KeyForwarded(agent_id=self.agent_id, spec=spec))

        # Local echo for visual feedback (independent of capture-pane refresh).
        # REQ-016 F-02: prefer event.character for the displayed text so
        # punctuation via Shift+<key> shows correctly.
        display_char = getattr(event, "character", None) or event.key
        if event.key == "enter":
            self._reset_echo()
        elif event.key == "backspace":
            if self._echo:
                self._echo = self._echo[:-1]
                self._render_echo()
        elif display_char and len(display_char) == 1 and display_char.isprintable():
            self._echo += display_char
            self._render_echo()

    def on_blur(self) -> None:
        self._last_key_was_escape = False
        self._reset_echo()

    def _render_echo(self) -> None:
        if self._echo:
            self.update(f"⌨ {self._echo}")
        else:
            self.update(self.PLACEHOLDER)

    def _reset_echo(self) -> None:
        self._echo = ""
        self.update(self.PLACEHOLDER)


# ---- AgentPane --------------------------------------------------------------

class AgentPane(Vertical):
    """Displays one agent's live tmux output and controls."""

    DEFAULT_CSS = """
    AgentPane {
        border-right: solid $primary;
        background: $surface;
        height: 1fr;
        min-height: 18;
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
    AgentPane .role-marker {
        width: auto;
        color: yellow;
    }
    AgentPane RichLog {
        height: 1fr;
        scrollbar-size: 1 1;
        background: $surface-darken-2;
    }
    AgentPane RichLog:focus {
        border: round $primary;
    }
    AgentPane .pane-controls {
        height: 1;
        padding: 0;
        background: $surface-darken-1;
    }
    /* REQ-016 F-01: admin row is collapsible via a toggle button. */
    AgentPane .pane-controls.collapsed {
        display: none;
    }
    AgentPane .pane-quickkeys {
        height: 1;
        padding: 0;
        background: $surface-darken-1;
    }
    AgentPane Button.admin-toggle {
        width: auto;
    }
    AgentPane Button#enter-agent {
        display: none;
    }
    AgentPane.has-active-pane Button#enter-agent {
        display: block;
    }
    AgentPane Button.jump-button {
        height: 1;
        background: $warning;
        color: $text;
    }
    AgentPane Button.jump-button.hidden {
        display: none;
    }
    """

    # ---- Messages -----------------------------------------------------------

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

    class DeleteRequested(Message):
        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    class AttachRequested(Message):
        """Posted when the user clicks the Enter button OR presses Enter while
        the read-only preview has focus (REQ-015 F-06)."""

        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    class KeyForwarded(Message):
        """REQ-015 F-03: posted when a quick-keyboard button is clicked.

        Mirrors InputBox.KeyForwarded so app.py can handle both with one
        path. The spec is a list of tmux send-keys arguments.
        """

        def __init__(self, agent_id: str, spec: list[str]) -> None:
            super().__init__()
            self.agent_id = agent_id
            self.spec = spec

    # ---- Reactives ---------------------------------------------------------

    status: reactive[AgentStatus] = reactive(AgentStatus.not_started)

    def __init__(self, agent: Agent, **kwargs) -> None:
        super().__init__(id=f"pane-{agent.id}", **kwargs)
        self.agent = agent

    # ---- Layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(classes="pane-header"):
            yield Label(f"[bold]{self.agent.name}[/bold]", id=f"name-{self.agent.id}")
            yield Label(
                f"[{_STATUS_COLOUR[self.status]}]{self.status.value}[/]",
                id=f"status-{self.agent.id}",
                classes="status-badge",
            )
            yield Label("", id=f"role-marker-{self.agent.id}", classes="role-marker")
            # REQ-016 F-01: admin toggle button + Enter button promoted into
            # the header so Enter stays accessible when the admin row is
            # collapsed.
            yield Button(
                "⋯",
                id=f"btn-admin-toggle-{self.agent.id}",
                classes="admin-toggle",
                compact=True,
            )
            yield Button("Enter", id="enter-agent", variant="primary", compact=True)

        # REQ-015 F-02: read-only preview, focusable so Enter triggers Attach.
        yield RichLog(
            id=f"log-{self.agent.id}",
            auto_scroll=False,
            markup=False,
            highlight=False,
            max_lines=OUTPUT_BUFFER_LINES,
        )

        # Jump-to-latest indicator (hidden by default; revealed by set_output
        # when the user has scrolled up).
        yield Button(
            "↓ jump to latest",
            id=f"btn-jump-{self.agent.id}",
            classes="jump-button hidden",
            compact=True,
        )

        # REQ-016 F-01: admin controls row is now collapsible. Default state
        # is collapsed; user clicks the ⋯ toggle in the header to reveal.
        # Enter button moved to the header so it stays visible.
        with Horizontal(classes="pane-controls collapsed", id=f"controls-{self.agent.id}"):
            yield Button("Pause",   id=f"btn-pause-{self.agent.id}",   variant="warning", compact=True)
            yield Button("Resume",  id=f"btn-resume-{self.agent.id}",  variant="success", compact=True)
            yield Button("Edit",    id=f"btn-edit-{self.agent.id}",    variant="default", compact=True)
            yield Button("Restart", id=f"btn-restart-{self.agent.id}", variant="error",   compact=True)
            yield Button("Delete",  id=f"btn-delete-{self.agent.id}",  variant="error",   compact=True)

        # REQ-015 F-03: quick keyboard row.
        with Horizontal(classes="pane-quickkeys"):
            for label, key_id in QUICK_KEYS:
                yield Button(
                    label,
                    id=f"qk-{key_id}-{self.agent.id}",
                    variant="default",
                    compact=True,
                )

        # REQ-015 F-04: dedicated input box (focus catcher).
        yield InputBox(agent_id=self.agent.id, id=f"inp-fwd-{self.agent.id}")

    # ---- Mount: post initial focusability check ---------------------------

    def on_mount(self) -> None:
        # RichLog can_focus is set true after mount so the Enter-attach key
        # handler can fire when the preview is the focused widget.
        try:
            log = self.query_one(f"#log-{self.agent.id}", RichLog)
            log.can_focus = True
        except Exception:
            pass

    # ---- Update helpers called by the app ---------------------------------

    _ACTIVE_PANE_STATUSES = {AgentStatus.active, AgentStatus.paused}

    def update_status(self, status: AgentStatus, pending: int = 0) -> None:
        """Update the status badge and Enter button visibility.

        The `pending` parameter is retained for backwards compatibility with
        the AgentStatusChanged message but is ignored — REQ-012 v2 has no
        pending events and REQ-015 doesn't reintroduce them.
        """
        self.status = status
        badge = self.query_one(f"#status-{self.agent.id}", Label)
        colour = _STATUS_COLOUR.get(status, "white")
        badge.update(f"[{colour}]{status.value}[/]")

        # REQ-012 v2: orchestrator panes carry a magenta marker so the
        # operator can spot the dispatcher at a glance.
        marker = self.query_one(f"#role-marker-{self.agent.id}", Label)
        if self.agent.role == AgentRole.orchestrator:
            marker.update("[bold magenta] ◆ orchestrator[/]")
        else:
            marker.update("")

        if status in self._ACTIVE_PANE_STATUSES:
            self.add_class("has-active-pane")
        else:
            self.remove_class("has-active-pane")

    def append_output(self, text: str) -> None:
        """Append text to the terminal output pane."""
        log = self.query_one(f"#log-{self.agent.id}", RichLog)
        log.write(text)

    def set_output(self, text: str) -> None:
        """Replace the entire pane output (used for tmux capture-pane refresh).

        REQ-015 F-02:
            - Renders ANSI escape codes via Rich Text.from_ansi for colours
            - Implements scroll lock: only auto-scrolls if the user is
              currently at the bottom; otherwise leaves the viewport alone
              and reveals the "jump to latest" button.
        """
        log = self.query_one(f"#log-{self.agent.id}", RichLog)

        # Detect whether we should follow the bottom after writing.
        max_y = getattr(log, "max_scroll_y", None)
        if max_y is None:
            try:
                max_y = log.virtual_size.height - log.size.height
            except Exception:
                max_y = 0
        at_bottom = log.scroll_y >= max(max_y - 1, 0)

        log.clear()
        if text:
            try:
                from rich.text import Text
                log.write(Text.from_ansi(text))
            except Exception:
                # Fallback: write as plain text if Rich conversion fails.
                log.write(text)

        # Re-evaluate scroll bounds after write.
        try:
            jump = self.query_one(f"#btn-jump-{self.agent.id}", Button)
        except Exception:
            jump = None

        if at_bottom:
            log.scroll_end(animate=False)
            if jump is not None:
                jump.add_class("hidden")
        else:
            if jump is not None:
                jump.remove_class("hidden")

    # ---- Key handling: Enter on focused preview triggers Attach -----------

    async def on_key(self, event: events.Key) -> None:
        """REQ-015 F-06: when the read-only preview is focused and the user
        presses Enter, trigger the same Attach flow as the Enter button.

        The InputBox's own on_key calls event.stop() before the event reaches
        this handler, so this only fires when focus is on the RichLog (or
        any other non-input child).
        """
        if event.key != "enter":
            return
        focused = self.app.focused if self.app else None
        if focused is None:
            return
        if focused.id == f"log-{self.agent.id}":
            event.stop()
            self.post_message(self.AttachRequested(agent_id=self.agent.id))

    # ---- Button handlers --------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        aid = self.agent.id

        # REQ-016 F-01: admin toggle button — flip the collapsed class on
        # the controls row.
        if bid == f"btn-admin-toggle-{aid}":
            try:
                controls = self.query_one(f"#controls-{aid}", Horizontal)
            except Exception:
                return
            if "collapsed" in controls.classes:
                controls.remove_class("collapsed")
            else:
                controls.add_class("collapsed")
            return

        # Admin controls (existing wiring)
        if bid == f"btn-pause-{aid}":
            self.post_message(self.PauseRequested(agent_id=aid))
            return
        if bid == f"btn-resume-{aid}":
            self.post_message(self.ResumeRequested(agent_id=aid))
            return
        if bid == f"btn-edit-{aid}":
            self.post_message(self.EditRequested(agent_id=aid))
            return
        if bid == f"btn-restart-{aid}":
            self.post_message(self.RestartRequested(agent_id=aid))
            return
        if bid == f"btn-delete-{aid}":
            self.post_message(self.DeleteRequested(agent_id=aid))
            return
        if bid == "enter-agent":
            self._handle_enter_pressed()
            return

        # REQ-015 F-02: jump-to-latest button
        if bid == f"btn-jump-{aid}":
            log = self.query_one(f"#log-{aid}", RichLog)
            log.scroll_end(animate=False)
            event.button.add_class("hidden")
            return

        # REQ-015 F-03: quick keyboard buttons
        prefix = "qk-"
        suffix = f"-{aid}"
        if bid.startswith(prefix) and bid.endswith(suffix):
            key_id = bid[len(prefix):-len(suffix)]
            spec = QUICK_KEY_SPECS.get(key_id)
            if spec:
                self.post_message(self.KeyForwarded(agent_id=aid, spec=list(spec)))
            return

    def _handle_enter_pressed(self) -> None:
        """Disable Enter button, post AttachRequested, arm a 5 s force-reenable timer."""
        btn = self.query_one("#enter-agent", Button)
        btn.disabled = True
        self.post_message(self.AttachRequested(agent_id=self.agent.id))
        self.set_timer(5.0, self._reenable_enter_button)

    def _reenable_enter_button(self) -> None:
        try:
            btn = self.query_one("#enter-agent", Button)
            btn.disabled = False
        except Exception:
            pass
