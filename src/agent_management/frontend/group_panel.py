"""GroupPanel widget — group selector and session controls."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Button, Label, Select
from textual.widget import Widget

from agent_management.backend.models import Group


class GroupPanel(Horizontal):
    """Header bar: group selector + Start / Stop All / Resume buttons."""

    DEFAULT_CSS = """
    GroupPanel {
        height: 1;
        background: $primary-darken-2;
        align: left middle;
        padding: 0 1;
    }
    GroupPanel Label {
        width: auto;
        margin-right: 1;
    }
    GroupPanel Select {
        width: 1fr;
        height: 1;
        margin-right: 1;
    }
    GroupPanel Button {
        height: 1;
        margin-right: 1;
    }
    """

    # Messages
    class GroupSelected(Message):
        def __init__(self, group_id: str) -> None:
            super().__init__()
            self.group_id = group_id

    class StartGroup(Message):
        def __init__(self, group_id: str) -> None:
            super().__init__()
            self.group_id = group_id

    class StopGroup(Message):
        def __init__(self, group_id: str) -> None:
            super().__init__()
            self.group_id = group_id

    class ResumeGroup(Message):
        def __init__(self, group_id: str) -> None:
            super().__init__()
            self.group_id = group_id

    class NewGroupRequested(Message):
        pass

    class NewAgentRequested(Message):
        pass

    class DeleteGroupRequested(Message):
        def __init__(self, group_id: str) -> None:
            super().__init__()
            self.group_id = group_id

    def __init__(self, groups: list[Group], **kwargs) -> None:
        super().__init__(**kwargs)
        self._groups = groups
        self._selected_group_id: str = groups[0].id if groups else ""

    def compose(self) -> ComposeResult:
        yield Label("Group:")
        opts = [(g.name, g.id) for g in self._groups] if self._groups else [("(none)", "")]
        yield Select(opts, value=self._selected_group_id, id="sel-group")
        yield Button("▶ Start", id="btn-start", variant="success", compact=True)
        yield Button("■ Stop All", id="btn-stop", variant="error", compact=True)
        yield Button("↺ Resume", id="btn-resume", variant="primary", compact=True)
        yield Button("+ Group", id="btn-new-group", variant="default", compact=True)
        yield Button("+ Agent", id="btn-new-agent", variant="default", compact=True)
        yield Button("× Del Group", id="btn-del-group", variant="error", compact=True)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sel-group" and event.value:
            self._selected_group_id = str(event.value)
            self.post_message(self.GroupSelected(group_id=self._selected_group_id))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        gid = self._selected_group_id
        if bid == "btn-start":
            self.post_message(self.StartGroup(group_id=gid))
        elif bid == "btn-stop":
            self.post_message(self.StopGroup(group_id=gid))
        elif bid == "btn-resume":
            self.post_message(self.ResumeGroup(group_id=gid))
        elif bid == "btn-new-group":
            self.post_message(self.NewGroupRequested())
        elif bid == "btn-new-agent":
            self.post_message(self.NewAgentRequested())
        elif bid == "btn-del-group":
            if gid:
                self.post_message(self.DeleteGroupRequested(group_id=gid))

    def refresh_groups(self, groups: list[Group]) -> None:
        """Update the group selector with a new list of groups."""
        self._groups = groups
        sel = self.query_one("#sel-group", Select)
        opts = [(g.name, g.id) for g in groups] if groups else [("(none)", "")]
        sel.set_options(opts)
        if groups:
            sel.value = groups[0].id
            self._selected_group_id = groups[0].id
