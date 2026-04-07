"""AgentManagementApp — root Textual application."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer, Vertical
from textual.widgets import Footer, Header

from agent_management.backend.models import Agent, AgentRole, AgentStatus, Group
from agent_management.backend.repository import Repository, SchemaIncompatibleError
from agent_management.backend.session_manager import SessionManager
from agent_management.backend.supervisor import (
    AgentStatusChanged,
    Supervisor,
    WorkflowAborted,
    WorkflowCompleted,
    WorkflowStalled,
    WorkflowStepAdvanced,
)
from agent_management.frontend.agent_pane import AgentPane
from agent_management.frontend.dialogs import (
    AgentDialog,
    ConfirmDeleteDialog,
    NewGroupDialog,
    RoleTemplatesDialog,
    SchemaResetDialog,
    StallActionDialog,
)
from agent_management.frontend.event_log import EventLog
from agent_management.frontend.group_panel import GroupPanel
from agent_management.frontend.shell_pane import ShellPane
from agent_management.frontend.tmux_attach import (
    AttachResult,
    NestedTmuxError,
    check_concurrent_access,
    cleanup_all_sessions,
    cleanup_stale_sessions,
    grouped_attach,
    suspend_attach,
    validate_pane_exists,
    detect_environment,
)
from agent_management.shared.config import PANE_REFRESH_INTERVAL

logger = logging.getLogger(__name__)

from agent_management.shared.config import BASE_DIR as _BASE_DIR
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    filename=str(_BASE_DIR / "platform.log"),
)


class AgentManagementApp(App):
    """Multi-agent Claude Code CLI orchestration TUI."""

    TITLE = "Agent Management Platform"
    CSS = """
    Screen {
        layout: vertical;
    }
    #pane-area {
        height: 1fr;
    }
    #pane-grid {
        layout: grid;
        grid-size: 2;
        grid-gutter: 0;
        padding: 0;
    }
    EventLog {
        dock: bottom;
    }
    """

    BINDINGS = [
        ("n", "new_agent", "New Agent"),
        ("g", "new_group", "New Group"),
        ("t", "manage_templates", "Role Templates"),
        ("z", "debug_shell", "Debug Shell"),
        ("c", "clear_all", "Clear"),
        ("q", "quit", "Quit"),
    ]

    _DEBUG_TMUX_SESSION = "agent-mgmt-debug"

    def __init__(self) -> None:
        super().__init__()
        self._repo = Repository()
        self._session_manager: Optional[SessionManager] = None
        self._supervisor: Optional[Supervisor] = None
        self._agents: list[Agent] = []
        self._groups: list[Group] = []
        self._active_group_id: Optional[str] = None
        self._pane_refresh_timer = None
        self._debug_tmux_pane_id: Optional[str] = None  # tmux pane for debug shell

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield GroupPanel(groups=[], id="group-panel")
        with ScrollableContainer(id="pane-area"):
            yield Vertical(id="pane-grid")
        yield EventLog(id="event-log")
        yield Footer()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_mount(self) -> None:
        # 1. Initialise database; on schema mismatch show destructive reset modal
        try:
            await self._repo.init()
        except SchemaIncompatibleError as exc:
            confirmed = await self._prompt_schema_reset(exc)
            if not confirmed:
                self.exit()
                return
            await self._destructive_reset()
            await self._repo.init()

        # 2. Create service objects (REQ-012 v2: no MCP server to start)
        self._session_manager = SessionManager(self._repo)
        self._supervisor = Supervisor(self._repo, self._session_manager, self)

        # 3. Load persisted data
        self._agents = await self._repo.get_agents()
        self._groups = await self._repo.get_groups()

        # 4. Render initial UI
        await self._rebuild_panes()
        self.query_one("#group-panel", GroupPanel).refresh_groups(self._groups)

        # 5. Pane refresh timer (4 Hz)
        self._pane_refresh_timer = self.set_interval(
            PANE_REFRESH_INTERVAL, self._refresh_pane_outputs
        )

        # 6. Clean up any agmgr-enter-* sessions left by a previous crash (F-06/AC-11)
        await cleanup_stale_sessions(os.getpid())

        logger.info("AgentManagementApp started")

    async def on_unmount(self) -> None:
        if self._supervisor:
            self._supervisor.stop()
        # Kill all agmgr-enter-* view sessions owned by this instance (F-06)
        await cleanup_all_sessions(os.getpid())
        await self._repo.close()

    async def _prompt_schema_reset(self, exc: SchemaIncompatibleError) -> bool:
        """Show the destructive-reset modal and return True if the user confirmed."""
        future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()

        def _done(ok: bool) -> None:
            if not future.done():
                future.set_result(ok)

        self.push_screen(SchemaResetDialog(exc.actual, exc.expected), _done)
        return await future

    async def _destructive_reset(self) -> None:
        """Wipe the SQLite DB and temp dir; called after the user confirms the reset modal."""
        from agent_management.shared.config import DB_PATH, TEMP_DIR
        import shutil
        try:
            DB_PATH.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to delete DB at %s", DB_PATH)
        if TEMP_DIR.exists():
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        logger.warning("Destructive schema reset complete")

    # ------------------------------------------------------------------
    # Pane management
    # ------------------------------------------------------------------

    async def _rebuild_panes(self) -> None:
        """Remove all agent panes and rebuild from current agents list."""
        grid = self.query_one("#pane-grid", Vertical)
        for pane in list(grid.query("AgentPane")):
            await pane.remove()
        if self._active_group_id:
            members = await self._repo.get_group_members(self._active_group_id)
        else:
            members = self._agents
        for agent in members:
            pane = AgentPane(agent)
            await grid.mount(pane)

    async def _refresh_pane_outputs(self) -> None:
        """Poll tmux capture-pane for all active agents and update panes."""
        if not self._active_group_id or not self._session_manager:
            return
        sessions = await self._repo.get_sessions_for_group(self._active_group_id)
        for session in sessions:
            if session.tmux_pane_id and session.status == AgentStatus.active:
                try:
                    output = await self._session_manager.capture_pane_output(
                        session.tmux_pane_id
                    )
                    pane_widget = self.query_one(f"#pane-{session.agent_id}", AgentPane)
                    pane_widget.set_output(output)
                except Exception:
                    pass  # Pane may not exist yet; ignore

        # Also refresh debug shell pane if open
        if self._debug_tmux_pane_id and self._session_manager:
            try:
                output = await self._session_manager.capture_pane_output(
                    self._debug_tmux_pane_id
                )
                shell_widget = self.query_one("#debug-shell-pane", ShellPane)
                shell_widget.set_output(output)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    def on_agent_status_changed(self, message: AgentStatusChanged) -> None:
        try:
            pane = self.query_one(f"#pane-{message.agent_id}", AgentPane)
            pane.update_status(message.status, message.pending_count)
        except Exception:
            pass  # Pane may not be mounted yet

    # ------------------------------------------------------------------
    # REQ-012 v2 — Workflow lifecycle messages from Supervisor
    # ------------------------------------------------------------------

    def on_workflow_step_advanced(self, message: WorkflowStepAdvanced) -> None:
        """Update AgentPane badges when a worker has completed its turn."""
        try:
            log = self.query_one("#event-log", EventLog)
            log.append_text(
                f"[step {message.step_index + 1}/{message.step_total}] "
                f"{message.role} completed via={message.via}"
            )
        except Exception:
            pass

    def on_workflow_completed(self, message: WorkflowCompleted) -> None:
        self.notify("Workflow completed.", severity="information", timeout=8)
        try:
            self.query_one("#event-log", EventLog).append_text("[workflow] COMPLETE")
        except Exception:
            pass

    def on_workflow_aborted(self, message: WorkflowAborted) -> None:
        self.notify(f"Workflow aborted: {message.reason}", severity="warning", timeout=10)
        try:
            self.query_one("#event-log", EventLog).append_text(
                f"[workflow] ABORT reason={message.reason}"
            )
        except Exception:
            pass

    def on_workflow_stalled(self, message: WorkflowStalled) -> None:
        """Show the Force Advance / Abort Workflow modal."""
        self.run_worker(
            self._handle_stall(message.group_id, message.role, message.elapsed),
            name=f"stall-{message.group_id[:8]}",
        )

    async def _handle_stall(self, group_id: str, role: str, elapsed: float) -> None:
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

        def _done(choice: str) -> None:
            if not future.done():
                future.set_result(choice)

        self.push_screen(StallActionDialog(role=role, elapsed=elapsed), _done)
        choice = await future
        if not self._supervisor:
            return
        if choice == "advance":
            self._supervisor.force_advance(group_id)
            self.notify("Force advance requested.")
        elif choice == "abort":
            self._supervisor.abort_workflow(group_id)
            self.notify("Workflow abort requested.", severity="warning")

    # ------------------------------------------------------------------
    # GroupPanel message handlers
    # ------------------------------------------------------------------

    async def on_group_panel_group_selected(self, message: GroupPanel.GroupSelected) -> None:
        self._active_group_id = message.group_id
        if self._supervisor:
            self._supervisor._active_group_id = message.group_id
        await self._rebuild_panes()

    async def on_group_panel_start_group(self, message: GroupPanel.StartGroup) -> None:
        if not message.group_id or not self._supervisor:
            return
        self._active_group_id = message.group_id
        self.notify(f"Starting group session…")
        self.run_worker(
            self._supervisor.start_group(message.group_id),
            name="start-group",
        )
        await self._rebuild_panes()

    async def on_group_panel_stop_group(self, message: GroupPanel.StopGroup) -> None:
        if not message.group_id or not self._supervisor:
            return
        self.notify("Stopping all agents…")
        self.run_worker(
            self._supervisor.stop_group(message.group_id),
            name="stop-group",
        )

    async def on_group_panel_resume_group(self, message: GroupPanel.ResumeGroup) -> None:
        if not message.group_id or not self._supervisor:
            return
        self._active_group_id = message.group_id
        self.notify("Resuming group session…")
        self.run_worker(
            self._supervisor.resume_group(message.group_id),
            name="resume-group",
        )
        await self._rebuild_panes()

    def on_group_panel_new_group_requested(
        self, _message: GroupPanel.NewGroupRequested
    ) -> None:
        self.action_new_group()

    def on_group_panel_new_agent_requested(
        self, _message: GroupPanel.NewAgentRequested
    ) -> None:
        self.run_worker(self._open_new_agent_dialog(), name="new-agent-dialog")

    # ------------------------------------------------------------------
    # AgentPane message handlers
    # ------------------------------------------------------------------

    async def on_agent_pane_pause_requested(self, message: AgentPane.PauseRequested) -> None:
        if self._supervisor:
            self.run_worker(
                self._supervisor.pause_agent(message.agent_id),
                name=f"pause-{message.agent_id}",
            )

    async def on_agent_pane_resume_requested(self, message: AgentPane.ResumeRequested) -> None:
        if self._supervisor:
            self.run_worker(
                self._supervisor.resume_agent(message.agent_id),
                name=f"resume-{message.agent_id}",
            )

    def on_agent_pane_edit_requested(self, message: AgentPane.EditRequested) -> None:
        agent = next((a for a in self._agents if a.id == message.agent_id), None)
        if agent:
            self.run_worker(self._open_edit_agent_dialog(agent), name=f"edit-agent-{agent.id}")

    def on_agent_pane_delete_requested(self, message: AgentPane.DeleteRequested) -> None:
        agent = next((a for a in self._agents if a.id == message.agent_id), None)
        if not agent:
            return
        label = f"Delete agent '{agent.name}'? This cannot be undone."
        self.push_screen(ConfirmDeleteDialog(label), lambda ok: (
            self.run_worker(self._delete_agent(agent.id), name="delete-agent") if ok else None
        ))

    async def _delete_agent(self, agent_id: str) -> None:
        await self._repo.delete_agent(agent_id)
        self._agents = await self._repo.get_agents()
        self.notify("Agent deleted.")
        await self._rebuild_panes()

    def on_group_panel_delete_group_requested(self, message: GroupPanel.DeleteGroupRequested) -> None:
        group = next((g for g in self._groups if g.id == message.group_id), None)
        if not group:
            return
        label = f"Delete group '{group.name}' and all its agents? This cannot be undone."
        self.push_screen(ConfirmDeleteDialog(label), lambda ok: (
            self.run_worker(self._delete_group(group.id), name="delete-group") if ok else None
        ))

    async def _delete_group(self, group_id: str) -> None:
        # Delete all agents that belong only to this group
        members = await self._repo.get_group_members(group_id)
        for agent in members:
            await self._repo.delete_agent(agent.id)
        await self._repo.delete_group(group_id)
        if self._active_group_id == group_id:
            self._active_group_id = None
            if self._supervisor:
                self._supervisor._active_group_id = None
        self._agents = await self._repo.get_agents()
        self._groups = await self._repo.get_groups()
        self.query_one("#group-panel", GroupPanel).refresh_groups(self._groups)
        self.notify("Group deleted.")
        await self._rebuild_panes()

    async def on_agent_pane_send_requested(self, message: AgentPane.SendRequested) -> None:
        if not self._session_manager or not self._active_group_id:
            self.notify("No active group session.", severity="warning")
            return
        sessions = await self._repo.get_sessions_for_group(self._active_group_id)
        session = next(
            (s for s in sessions
             if s.agent_id == message.agent_id and s.status == AgentStatus.active),
            None,
        )
        if not session or not session.tmux_pane_id:
            self.notify("Agent is not running.", severity="warning")
            return
        await self._session_manager.send_keys(session.tmux_pane_id, message.text)

    async def on_agent_pane_restart_requested(self, message: AgentPane.RestartRequested) -> None:
        if not self._supervisor or not self._active_group_id:
            return
        agent = next((a for a in self._agents if a.id == message.agent_id), None)
        if agent:
            self.run_worker(
                self._session_manager.restart_agent_session(agent, self._active_group_id),
                name=f"restart-{message.agent_id}",
            )

    async def on_agent_pane_attach_requested(
        self, message: AgentPane.AttachRequested
    ) -> None:
        """Entry point for the Enter button attach flow — validate group, resolve
        session, validate pane, detect tmux environment, dispatch to the correct
        attach path, show result toast, refresh pane."""
        self.run_worker(
            self._handle_attach(message.agent_id),
            name=f"attach-{message.agent_id}",
        )

    async def _handle_attach(self, agent_id: str) -> None:
        """Orchestrate the full attach flow for one agent pane."""
        pane = self._find_pane_widget(agent_id)

        # 1. Guard: active group must be selected (F-06)
        if not self._active_group_id:
            logger.debug("Attach aborted — no active group selected, agent_id=%s", agent_id)
            self.notify("Select a group first.", severity="warning")
            self._reenable_enter(pane)
            return

        # 2. Resolve the current session for this agent (F-06)
        session = await self._resolve_agent_session(agent_id)
        if session is None:
            logger.debug("Attach aborted — no active session, agent_id=%s", agent_id)
            self.notify("Agent session not started — use Start Group.", severity="warning")
            self._reenable_enter(pane)
            return

        # 3. Validate the tmux pane is still alive (F-06)
        if not await validate_pane_exists(session.tmux_pane_id):
            logger.warning(
                "Pane no longer exists, agent_id=%s, pane_id=%s",
                agent_id,
                session.tmux_pane_id,
            )
            self.notify("Agent pane is gone — restart the agent.", severity="warning")
            self._reenable_enter(pane)
            return

        # 4. Detect environment: in-tmux, out-of-tmux, or nested
        try:
            current_session = await detect_environment()
        except NestedTmuxError as exc:
            self.notify(str(exc), severity="error")
            self._reenable_enter(pane)
            return
        except FileNotFoundError:
            logger.error("tmux binary not found in PATH")
            self.notify("tmux not found in PATH.", severity="error")
            self._reenable_enter(pane)
            return

        # 5. Dispatch to the appropriate attach path (F-03: pass pane_id)
        pane_id = session.tmux_pane_id
        if current_session:
            result = await self._do_grouped_attach(agent_id, session.tmux_session_name, pane_id)
        else:
            result = await self._do_suspend_attach(agent_id, session.tmux_session_name, pane_id)

        # 5. Show result toast
        if result.message:
            severity = "information" if result.ok else "error"
            self.notify(result.message, severity=severity)

        # 6. Force a capture-pane poll to refresh AgentPane output (F-07/AC-12)
        await self._force_refresh_agent_pane(agent_id, session)

        # 7. Re-enable Enter button (the 5 s timer is a safety net; we call early)
        self._reenable_enter(pane)

    async def _do_grouped_attach(
        self,
        agent_id: str,
        tmux_session_name: str,
        pane_id: str = "",
    ) -> AttachResult:
        """Attach via grouped session (asyncio loop stays live).

        pane_id (F-03): passed through to grouped_attach so select-pane is called
        after switch-client, ensuring the user lands on the correct agent window.
        """
        # Check for concurrent access before switching
        has_concurrent = await check_concurrent_access(agent_id, os.getpid())
        if has_concurrent:
            confirmed = await self._confirm_concurrent_access()
            if not confirmed:
                return AttachResult(ok=False, message="")

        return await grouped_attach(agent_id, tmux_session_name, os.getpid(), pane_id)

    async def _do_suspend_attach(
        self,
        agent_id: str,
        tmux_session_name: str,
        pane_id: str = "",
    ) -> AttachResult:
        """Attach via TUI suspend (out-of-tmux path).

        pane_id (F-03): passed through to suspend_attach so the correct pane is
        selected after the user detaches from the blocking tmux session.
        """
        result = await suspend_attach(
            agent_id=agent_id,
            tmux_session_name=tmux_session_name,
            app_pid=os.getpid(),
            pane_id=pane_id,
            suspend_fn=self.suspend,
        )
        # Restore terminal rendering after resume (AC-09)
        self.refresh(layout=True)
        await self._reset_terminal()
        return result

    async def _confirm_concurrent_access(self) -> bool:
        """Show a confirmation dialog for F-05 concurrent access and return the user's choice."""
        result_holder: list[bool] = []

        async def _ask() -> None:
            event = asyncio.Event()

            def _callback(ok: bool) -> None:
                result_holder.append(ok)
                event.set()

            self.push_screen(
                ConfirmDeleteDialog(
                    "Agent pane already being accessed by another client. Enter anyway?"
                ),
                _callback,
            )
            await event.wait()

        await _ask()
        return bool(result_holder and result_holder[0])

    async def _resolve_agent_session(self, agent_id: str):
        """Return the active/paused session for agent_id, or None if unavailable."""
        if not self._active_group_id:
            return None
        sessions = await self._repo.get_sessions_for_group(self._active_group_id)
        return next(
            (
                s
                for s in sessions
                if s.agent_id == agent_id
                and s.status in (AgentStatus.active, AgentStatus.paused)
                and s.tmux_pane_id
            ),
            None,
        )

    async def _force_refresh_agent_pane(self, agent_id: str, session) -> None:
        """Trigger an immediate pane output + status refresh for the agent (F-07/AC-12)."""
        # Refresh pane output from tmux
        if self._session_manager and session.tmux_pane_id:
            try:
                output = await self._session_manager.capture_pane_output(session.tmux_pane_id)
                pane_widget = self.query_one(f"#pane-{agent_id}", AgentPane)
                pane_widget.set_output(output)
                logger.debug("Force-refreshed pane output, agent_id=%s", agent_id)
            except Exception:
                logger.debug(
                    "Force refresh failed for agent_id=%s (pane may be gone)", agent_id
                )

        # Refresh agent status from database (F-07: update status in TUI)
        try:
            updated_agent = await self._repo.get_agent(agent_id)
            if updated_agent:
                pane_widget = self.query_one(f"#pane-{agent_id}", AgentPane)
                pane_widget.update_status(updated_agent.status)
                logger.debug(
                    "Force-refreshed agent status, agent_id=%s, status=%s",
                    agent_id,
                    updated_agent.status,
                )
        except Exception:
            logger.debug("Agent status refresh failed for agent_id=%s", agent_id)

    async def _reset_terminal(self) -> None:
        """Run tput reset to clear any terminal rendering artifacts after suspend (AC-09)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "tput", "reset",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception:
            pass  # tput unavailable on this platform; ignore

    def _find_pane_widget(self, agent_id: str) -> AgentPane | None:
        """Return the AgentPane widget for agent_id, or None if not mounted."""
        try:
            return self.query_one(f"#pane-{agent_id}", AgentPane)
        except Exception:
            return None

    def _reenable_enter(self, pane: AgentPane | None) -> None:
        """Re-enable the Enter button on the given pane (idempotent, safe)."""
        if pane:
            pane._reenable_enter_button()

    # ------------------------------------------------------------------
    # Keybinding actions
    # ------------------------------------------------------------------

    def action_new_agent(self) -> None:
        self.run_worker(self._open_new_agent_dialog(), name="new-agent-dialog")

    def action_new_group(self) -> None:
        self.run_worker(self._open_new_group_dialog(), name="new-group-dialog")

    def action_manage_templates(self) -> None:
        self.run_worker(self._open_templates_dialog(), name="templates-dialog")

    def action_debug_shell(self) -> None:
        self.run_worker(self._toggle_debug_shell(), name="debug-shell-toggle")

    def action_clear_all(self) -> None:
        label = "Clear all sessions and event history?\nAgents and groups are kept; all sessions and events will be wiped."
        self.push_screen(
            ConfirmDeleteDialog(label),
            lambda ok: self.run_worker(self._clear_all(), name="clear-all") if ok else None,
        )

    async def _clear_all(self) -> None:
        if self._supervisor:
            await self._supervisor.clear_all()
        else:
            await self._repo.clear_all_runtime_state()
        self._agents = await self._repo.get_agents()
        self.query_one(EventLog).clear()
        await self._rebuild_panes()
        self.notify("Cleared. All agents reset to not_started.")

    # ------------------------------------------------------------------
    # Dialog callbacks
    # ------------------------------------------------------------------

    async def _open_new_agent_dialog(self) -> None:
        templates = await self._load_templates_dict()
        self.push_screen(AgentDialog(role_templates=templates), self._handle_new_agent)

    def _handle_new_agent(self, result: Agent | None) -> None:
        if result:
            self.run_worker(self._save_new_agent(result), name="save-new-agent")

    async def _save_new_agent(self, result: Agent) -> None:
        await self._repo.save_agent(result)
        self._agents = await self._repo.get_agents()
        self.notify(f"Agent '{result.name}' created.")
        await self._rebuild_panes()

    async def _open_edit_agent_dialog(self, agent: Agent) -> None:
        templates = await self._load_templates_dict()
        self.push_screen(AgentDialog(agent=agent, role_templates=templates), self._handle_edit_agent)

    def _handle_edit_agent(self, result: Agent | None) -> None:
        if result:
            self.run_worker(self._save_edited_agent(result), name="save-edited-agent")

    async def _save_edited_agent(self, result: Agent) -> None:
        await self._repo.save_agent(result)
        self._agents = await self._repo.get_agents()
        self.notify(f"Agent '{result.name}' updated. Restart session to apply prompt changes.")

    async def _open_new_group_dialog(self) -> None:
        self.push_screen(NewGroupDialog(), self._handle_new_group)

    def _handle_new_group(self, result: tuple | None) -> None:
        if result:
            name, working_dir, workflow_id = result
            self.run_worker(
                self._save_new_group(name, working_dir, workflow_id),
                name="save-new-group",
            )

    async def _open_templates_dialog(self) -> None:
        templates = await self._repo.get_role_templates()
        self.push_screen(RoleTemplatesDialog(templates=templates), self._handle_templates)

    def _handle_templates(self, result) -> None:
        if result is None:
            return  # cancelled
        if len(result) == 0:
            # Empty list = reset sentinel from "Reset Defaults" button
            self.run_worker(self._reset_templates(), name="reset-templates")
        else:
            self.run_worker(self._save_templates(result), name="save-templates")

    async def _save_templates(self, templates) -> None:
        for tpl in templates:
            await self._repo.save_role_template(tpl)
        self.notify("Role templates saved.")

    async def _reset_templates(self) -> None:
        await self._repo.reset_role_templates()
        self.notify("Role templates reset to defaults.")

    async def _toggle_debug_shell(self) -> None:
        """Open or close the debug zsh shell pane."""
        grid = self.query_one("#pane-grid", Vertical)

        if self._debug_tmux_pane_id:
            # ---- Close ----
            await self._kill_debug_shell()
            try:
                shell_widget = self.query_one("#debug-shell-pane", ShellPane)
                await shell_widget.remove()
            except Exception:
                pass
            self.notify("Debug shell closed.")
            return

        # ---- Open ----
        # Ensure tmux session exists
        rc, _, _ = await self._tmux("has-session", "-t", self._DEBUG_TMUX_SESSION)
        if rc != 0:
            await self._tmux("new-session", "-d", "-s", self._DEBUG_TMUX_SESSION, "-x", "220", "-y", "50")

        # Create a new window running zsh
        rc, pane_id, _ = await self._tmux(
            "new-window", "-t", self._DEBUG_TMUX_SESSION,
            "-P", "-F", "#{session_name}:#{window_index}.#{pane_index}",
            "zsh",
        )
        if rc != 0:
            self.notify("Failed to open debug shell.", severity="error")
            return

        self._debug_tmux_pane_id = pane_id.strip()
        shell = ShellPane(id="debug-shell-pane")
        await grid.mount(shell)
        self.notify(
            f"Debug shell opened — attach with: tmux attach -t {self._DEBUG_TMUX_SESSION}",
            timeout=6,
        )

    async def _kill_debug_shell(self) -> None:
        if self._debug_tmux_pane_id:
            await self._tmux("kill-pane", "-t", self._debug_tmux_pane_id)
            self._debug_tmux_pane_id = None

    async def _tmux(self, *args: str) -> tuple[int, str, str]:
        """Run a tmux subcommand."""
        import asyncio as _asyncio
        proc = await _asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

    def on_shell_pane_close_requested(self, _message: ShellPane.CloseRequested) -> None:
        self.run_worker(self._toggle_debug_shell(), name="debug-shell-toggle")

    async def _load_templates_dict(self) -> dict:
        """Return {role_value: RoleTemplate} for all templates."""
        tpls = await self._repo.get_role_templates()
        return {t.role.value: t for t in tpls}

    async def _save_new_group(self, name: str, working_dir: str, workflow_id: str) -> None:
        # REQ-012 v2 F-11: auto-create 6 agents (Orchestrator + 5 workers).
        # Orchestrator goes first in listing order so the operator can see who's
        # in charge. Workers follow in canonical order.
        templates = await self._load_templates_dict()

        roles_in_order = [
            AgentRole.orchestrator,
            AgentRole.product_manager,
            AgentRole.tech_director,
            AgentRole.developer,
            AgentRole.tester,
            AgentRole.user,
        ]

        # Create group first (with the chosen workflow)
        group = Group(name=name, workflow_id=workflow_id)
        await self._repo.save_group(group)

        created = 0
        for role in roles_in_order:
            tpl = templates.get(role.value)
            display = tpl.display_name if tpl else role.value.replace("_", " ").title()
            agent = Agent(
                name=f"{name} - {display}",
                role=role,
                working_dir=working_dir,
                system_prompt=tpl.system_prompt if tpl else "",
            )
            await self._repo.save_agent(agent)
            await self._repo.add_group_member(group.id, agent.id)
            created += 1

        self._agents = await self._repo.get_agents()
        self._groups = await self._repo.get_groups()
        self.query_one("#group-panel", GroupPanel).refresh_groups(self._groups)
        self.notify(f"Group '{name}' created with {created} agents (workflow={workflow_id}).")
