"""Modal dialog screens.

REQ-012 v2 changes:
    - AgentDialog drops Topic Subscriptions and Auto-respond fields
      (no event bus). Role selection still loads the role template's
      system_prompt automatically.
    - NewGroupDialog adds a Workflow dropdown (standard / prototype / research).
    - RoleTemplatesDialog drops the default_topics field.
    - SchemaResetDialog: destructive-reset confirmation modal shown on
      schema-version mismatch at startup.
    - StallActionDialog: Force Advance / Abort Workflow action sheet for
      F-09 tertiary stall handling.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, Input, Label, Select, TextArea

from agent_management.backend.models import Agent, AgentRole, RoleTemplate
from agent_management.backend import workflows as workflows_mod


class PathSuggester(Suggester):
    """Inline filesystem path completion for Input widgets."""

    def __init__(self, *, dirs_only: bool = False) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self._dirs_only = dirs_only

    async def get_suggestion(self, value: str) -> str | None:
        if not value:
            return None
        try:
            p = Path(value)
            if value.endswith(("/", os.sep)):
                parent, prefix = p, ""
            else:
                parent, prefix = p.parent, p.name

            if not parent.is_dir():
                return None

            matches = sorted(
                child
                for child in parent.iterdir()
                if child.name.startswith(prefix)
                and (not self._dirs_only or child.is_dir())
            )
            if not matches:
                return None

            first = matches[0]
            return str(first) + ("/" if first.is_dir() else "")
        except (OSError, PermissionError):
            return None


# ------------------------------------------------------------------
# New / Edit Agent dialog
# ------------------------------------------------------------------

class AgentDialog(ModalScreen[Optional[Agent]]):
    """Create or edit an agent. Dismisses with Agent | None."""

    CSS = """
    AgentDialog {
        align: center middle;
    }
    AgentDialog > Vertical {
        width: 70;
        height: auto;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    AgentDialog Label.field-label {
        margin-top: 1;
    }
    AgentDialog Input {
        margin-bottom: 0;
    }
    AgentDialog TextArea {
        height: 6;
    }
    AgentDialog .buttons {
        margin-top: 1;
        align: right middle;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        agent: Optional[Agent] = None,
        role_templates: Optional[dict[str, RoleTemplate]] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._agent = agent
        self._role_templates: dict[str, RoleTemplate] = role_templates or {}

    def compose(self) -> ComposeResult:
        ag = self._agent
        with Vertical():
            yield Label("New Agent" if not ag else f"Edit Agent: {ag.name}", id="dialog-title")

            yield Label("Name *", classes="field-label")
            yield Input(value=ag.name if ag else "", id="inp-name", placeholder="e.g. PM-Agent")

            yield Label("Role *", classes="field-label")
            role_opts = [(r.value.replace("_", " ").title(), r.value) for r in AgentRole]
            default_role = ag.role.value if ag else AgentRole.product_manager.value
            yield Select(role_opts, value=default_role, id="sel-role")

            yield Label("Working Directory *", classes="field-label")
            yield Input(
                value=ag.working_dir if ag else str(Path.home()),
                id="inp-dir",
                placeholder="/path/to/project",
                suggester=PathSuggester(dirs_only=True),
            )

            yield Label("System Prompt", classes="field-label")
            yield TextArea(
                text=ag.system_prompt if ag else "",
                id="ta-prompt",
            )

            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="btn-save")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-save":
            self._save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_select_changed(self, event: Select.Changed) -> None:
        """When role changes, overwrite system prompt from the role template."""
        if event.select.id != "sel-role":
            return
        role_val = str(event.value)
        tpl = self._role_templates.get(role_val)
        if not tpl:
            return
        ta = self.query_one("#ta-prompt", TextArea)
        ta.load_text(tpl.system_prompt)

    def _save(self) -> None:
        name = self.query_one("#inp-name", Input).value.strip()
        working_dir = self.query_one("#inp-dir", Input).value.strip()
        role_val = self.query_one("#sel-role", Select).value
        prompt = self.query_one("#ta-prompt", TextArea).text

        errors = []
        if not name:
            errors.append("Name is required.")
        if not working_dir:
            errors.append("Working directory is required.")
        elif not Path(working_dir).exists():
            errors.append(f"Directory does not exist: {working_dir}")
        if errors:
            self.notify("\n".join(errors), severity="error", timeout=5)
            return

        if self._agent:
            agent = self._agent
            agent.name = name
            agent.role = AgentRole(role_val)
            agent.working_dir = working_dir
            agent.system_prompt = prompt
        else:
            agent = Agent(
                name=name,
                role=AgentRole(role_val),
                working_dir=working_dir,
                system_prompt=prompt,
            )

        self.dismiss(agent)


# ------------------------------------------------------------------
# New Group dialog
# ------------------------------------------------------------------

class NewGroupDialog(ModalScreen[Optional[tuple[str, str, str]]]):
    """Create a new group. Dismisses with (name, working_dir, workflow_id) | None.

    REQ-012 v2 F-08/F-11: auto-creates 6 agents — Orchestrator + PM + Tech
    Director + Developer + Tester + User. The Workflow dropdown determines
    how the orchestrator drives them.
    """

    CSS = """
    NewGroupDialog {
        align: center middle;
    }
    NewGroupDialog > Vertical {
        width: 80;
        height: auto;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    NewGroupDialog Label.field-label {
        margin-top: 1;
    }
    NewGroupDialog .hint {
        color: $text-muted;
        margin-top: 1;
    }
    NewGroupDialog .buttons {
        margin-top: 1;
        align: right middle;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("New Group", id="dialog-title")
            yield Label("Group Name *", classes="field-label")
            yield Input(id="inp-group-name", placeholder="e.g. sprint-01")

            yield Label("Working Directory *", classes="field-label")
            yield Input(
                value=str(Path.home()),
                id="inp-group-dir",
                placeholder="/path/to/project",
                suggester=PathSuggester(dirs_only=True),
            )

            yield Label("Workflow *", classes="field-label")
            wf_opts = [
                (wf.display_name, wf.id)
                for wf in workflows_mod.BUILT_IN_WORKFLOWS.values()
            ]
            yield Select(
                wf_opts,
                value=workflows_mod.DEFAULT_WORKFLOW_ID,
                id="sel-workflow",
                allow_blank=False,
            )

            yield Label(
                "Auto-creates 6 agents: Orchestrator · Product Manager · Tech Director "
                "· Developer · Tester · User",
                classes="hint",
            )

            with Horizontal(classes="buttons"):
                yield Button("Create", variant="primary", id="btn-create")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-create":
            self._create()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _create(self) -> None:
        name = self.query_one("#inp-group-name", Input).value.strip()
        working_dir = self.query_one("#inp-group-dir", Input).value.strip()
        workflow_id = str(self.query_one("#sel-workflow", Select).value)

        errors = []
        if not name:
            errors.append("Group name is required.")
        if not working_dir:
            errors.append("Working directory is required.")
        elif not Path(working_dir).exists():
            errors.append(f"Directory does not exist: {working_dir}")
        if workflow_id not in workflows_mod.BUILT_IN_WORKFLOWS:
            errors.append(f"Unknown workflow: {workflow_id}")
        if errors:
            self.notify("\n".join(errors), severity="error", timeout=5)
            return

        self.dismiss((name, working_dir, workflow_id))


# ------------------------------------------------------------------
# Role Templates dialog
# ------------------------------------------------------------------

class RoleTemplatesDialog(ModalScreen[Optional[list[RoleTemplate]]]):
    """View and edit system prompt templates for each role.
    Dismisses with updated list[RoleTemplate] | None.
    """

    CSS = """
    RoleTemplatesDialog {
        align: center middle;
    }
    RoleTemplatesDialog > Vertical {
        width: 80;
        height: 90vh;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    RoleTemplatesDialog .role-row {
        height: auto;
        margin-bottom: 1;
    }
    RoleTemplatesDialog .role-label {
        width: 20;
        padding: 0 1;
    }
    RoleTemplatesDialog TextArea {
        height: 8;
    }
    RoleTemplatesDialog .buttons {
        margin-top: 1;
        align: right middle;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, templates: list[RoleTemplate], **kwargs) -> None:
        super().__init__(**kwargs)
        self._templates = templates

    def compose(self) -> ComposeResult:
        from textual.containers import ScrollableContainer
        with Vertical():
            yield Label("Role Templates", id="rt-title")
            with ScrollableContainer():
                for tpl in self._templates:
                    with Vertical(classes="role-row"):
                        yield Label(
                            f"[bold]{tpl.display_name}[/bold]  "
                            f"[dim]({tpl.role.value})[/dim]",
                            classes="role-label",
                        )
                        yield Label("System Prompt:", classes="field-label")
                        yield TextArea(
                            text=tpl.system_prompt,
                            id=f"ta-{tpl.role.value}",
                        )
            with Horizontal(classes="buttons"):
                yield Button("Reset Defaults", variant="warning", id="btn-reset")
                yield Button("Save", variant="primary", id="btn-save")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-save":
            updated = []
            for tpl in self._templates:
                ta = self.query_one(f"#ta-{tpl.role.value}", TextArea)
                updated.append(RoleTemplate(
                    role=tpl.role,
                    display_name=tpl.display_name,
                    system_prompt=ta.text,
                ))
            self.dismiss(updated)
        elif event.button.id == "btn-reset":
            self.dismiss([])

    def action_cancel(self) -> None:
        self.dismiss(None)


# ------------------------------------------------------------------
# Confirm Delete dialog
# ------------------------------------------------------------------

class ConfirmDeleteDialog(ModalScreen[bool]):
    """Generic yes/no confirmation dialog."""

    CSS = """
    ConfirmDeleteDialog {
        align: center middle;
    }
    ConfirmDeleteDialog > Vertical {
        width: 50;
        height: auto;
        background: $surface;
        border: thick $error;
        padding: 1 2;
    }
    ConfirmDeleteDialog .buttons {
        margin-top: 1;
        align: right middle;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._message, id="confirm-msg")
            with Horizontal(classes="buttons"):
                yield Button("Delete", variant="error", id="btn-confirm")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ------------------------------------------------------------------
# REQ-012 v2 — Schema Reset dialog (destructive)
# ------------------------------------------------------------------

class SchemaResetDialog(ModalScreen[bool]):
    """Shown at startup when the SQLite schema version does not match.

    Confirmation wipes the database and temp directory. Cancellation quits the app.
    """

    CSS = """
    SchemaResetDialog {
        align: center middle;
    }
    SchemaResetDialog > Vertical {
        width: 70;
        height: auto;
        background: $surface;
        border: thick $error;
        padding: 1 2;
    }
    SchemaResetDialog Label.title {
        text-style: bold;
        color: $error;
    }
    SchemaResetDialog .body {
        margin-top: 1;
    }
    SchemaResetDialog .warning {
        margin-top: 1;
        color: $warning;
    }
    SchemaResetDialog .buttons {
        margin-top: 1;
        align: right middle;
    }
    """

    BINDINGS = [("escape", "cancel", "Quit")]

    def __init__(self, actual: int, expected: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self._actual = actual
        self._expected = expected

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Schema Version Mismatch", classes="title")
            yield Label(
                f"On-disk schema version: {self._actual}\n"
                f"Expected schema version: {self._expected}\n\n"
                "Your existing data is incompatible with this version of the platform.",
                classes="body",
            )
            yield Label(
                "Clicking Reset will WIPE .agent_management/ "
                "(groups, agents, sessions, customised role templates).",
                classes="warning",
            )
            with Horizontal(classes="buttons"):
                yield Button("Reset & Continue", variant="error", id="btn-reset")
                yield Button("Quit", variant="default", id="btn-quit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-reset":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ------------------------------------------------------------------
# REQ-012 v2 — Stall action dialog (Force Advance / Abort)
# ------------------------------------------------------------------

class StallActionDialog(ModalScreen[str]):
    """Shown when the orchestrator's dispatch has stalled past ORCHESTRATOR_STALL_TIMEOUT.

    Returns one of: "advance" / "abort" / "wait".
    """

    CSS = """
    StallActionDialog {
        align: center middle;
    }
    StallActionDialog > Vertical {
        width: 70;
        height: auto;
        background: $surface;
        border: thick $warning;
        padding: 1 2;
    }
    StallActionDialog Label.title {
        text-style: bold;
        color: $warning;
    }
    StallActionDialog .body {
        margin-top: 1;
    }
    StallActionDialog .buttons {
        margin-top: 1;
        align: right middle;
    }
    """

    BINDINGS = [("escape", "wait", "Wait")]

    def __init__(self, role: str, elapsed: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self._role = role
        self._elapsed = elapsed

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Workflow Stalled", classes="title")
            yield Label(
                f"No completion signal from role={self._role} "
                f"after {int(self._elapsed)} seconds.\n\n"
                "Force Advance will capture whatever the worker has produced so far\n"
                "and feed it to the orchestrator as a 'silence' completion.\n\n"
                "Abort Workflow will stop the dispatch loop entirely.",
                classes="body",
            )
            with Horizontal(classes="buttons"):
                yield Button("Force Advance", variant="primary", id="btn-advance")
                yield Button("Abort Workflow", variant="error", id="btn-abort")
                yield Button("Wait", variant="default", id="btn-wait")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-advance":
            self.dismiss("advance")
        elif event.button.id == "btn-abort":
            self.dismiss("abort")
        else:
            self.dismiss("wait")

    def action_wait(self) -> None:
        self.dismiss("wait")
