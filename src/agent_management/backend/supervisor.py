"""Supervisor — orchestrator dispatch driver.

REQ-012 v2: replaces the v1 fan-out / pending_events / wake-up-sentinel
machinery with a single dispatch_loop coroutine per active group. The loop
reads the orchestrator's tmux pane via capture-pane, parses <<DISPATCH ...>>
blocks, sends the dispatch text to the target worker pane, polls the worker
pane for <<TASK_DONE>> (with silence/stall fallbacks), and injects the
[WORKER_RESULT ...] back into the orchestrator pane.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from textual.message import Message

from agent_management.backend import orchestrator as orch_mod
from agent_management.backend import workflows
from agent_management.backend.models import (
    Agent,
    AgentRole,
    AgentStatus,
)
from agent_management.backend.orchestrator import (
    CompletionLayer,
    CompletionResult,
    Dispatch,
)
from agent_management.backend.repository import Repository
from agent_management.backend.session_manager import SessionManager
from agent_management.shared.config import (
    DISPATCH_POLL_INTERVAL,
    ORCHESTRATOR_STALL_TIMEOUT,
    TEMP_DIR,
    WORKER_SILENCE_TIMEOUT,
)

if TYPE_CHECKING:
    from textual.app import App

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Textual messages posted back to the app
# ------------------------------------------------------------------

@dataclass
class AgentStatusChanged(Message):
    """Posted when an agent's status changes. `pending_count` is retained for
    backwards compatibility with the AgentPane widget but is now always 0
    (REQ-012 v2 has no pending events)."""
    agent_id: str
    status: AgentStatus
    pending_count: int = 0


@dataclass
class WorkflowStepAdvanced(Message):
    """Posted when a worker has completed a dispatch and the orchestrator's
    next dispatch can begin."""
    group_id: str
    role: str
    via: str             # 'marker' / 'silence' / 'stall'
    step_index: int      # zero-based
    step_total: int


@dataclass
class WorkflowStalled(Message):
    """Posted when a dispatch has been pending for ORCHESTRATOR_STALL_TIMEOUT."""
    group_id: str
    role: str
    elapsed: float


@dataclass
class WorkflowCompleted(Message):
    """Posted when the orchestrator emits <<WORKFLOW_COMPLETE>>."""
    group_id: str


@dataclass
class WorkflowAborted(Message):
    """Posted when the orchestrator emits <<WORKFLOW_ABORT reason="...">>."""
    group_id: str
    reason: str


@dataclass
class PaneOutputRefresh(Message):
    """Trigger a pane output refresh in the TUI."""
    agent_id: str
    pane_id: str


# ------------------------------------------------------------------
# In-flight dispatch tracking
# ------------------------------------------------------------------

@dataclass
class _InFlight:
    dispatch: Dispatch
    worker: Agent
    worker_pane: str
    dispatch_at: float
    last_change_at: float
    last_pane_text: str = ""
    step_index: int = 0
    retry_count: int = 0


class Supervisor:
    """Drives the per-group orchestrator dispatch loop."""

    def __init__(
        self,
        repo: Repository,
        session_manager: SessionManager,
        app: "App",
    ) -> None:
        self._repo = repo
        self._sm = session_manager
        self._app = app
        self._active_group_id: Optional[str] = None
        self._dispatch_task: Optional[asyncio.Task] = None
        self._stall_notified: bool = False
        self._force_advance_request: Optional[asyncio.Event] = None
        self._abort_request: Optional[asyncio.Event] = None
        self._consumed_offset: int = 0
        self._step_index: int = 0
        self._dev_tester_retries: int = 0  # for the standard workflow's failure loop

    # ------------------------------------------------------------------
    # Group lifecycle
    # ------------------------------------------------------------------

    async def start_group(self, group_id: str) -> None:
        """Start all workers, then the orchestrator, then begin the dispatch loop."""
        logger.info("Starting group %s", group_id)
        await self._cancel_dispatch_loop()
        self._active_group_id = group_id
        self._consumed_offset = 0
        self._step_index = 0
        self._dev_tester_retries = 0
        self._stall_notified = False

        members = await self._repo.get_group_members(group_id)
        workers = [a for a in members if a.role != AgentRole.orchestrator]
        orch_agent = next((a for a in members if a.role == AgentRole.orchestrator), None)

        # 1. Start workers first.
        for agent in workers:
            try:
                await self._sm.start_agent_session(agent, group_id, resume_session_id=None)
                self._app.post_message(AgentStatusChanged(
                    agent_id=agent.id, status=AgentStatus.active
                ))
            except Exception:
                logger.exception("Failed to start session for agent %s", agent.name)
                await self._repo.update_agent_status(agent.id, AgentStatus.degraded)
                self._app.post_message(AgentStatusChanged(
                    agent_id=agent.id, status=AgentStatus.degraded
                ))

        # 2. Verify all workers are active before starting the orchestrator.
        not_active = []
        for w in workers:
            sess = await self._repo.get_session(w.id, group_id)
            if not sess or sess.status != AgentStatus.active:
                not_active.append(w.name)
        if not_active:
            logger.error("Refusing to start orchestrator — workers not active: %s", not_active)
            return

        # 3. Start the orchestrator last.
        if orch_agent is None:
            logger.warning("Group %s has no orchestrator agent — dispatch loop will not run", group_id)
            return
        try:
            await self._sm.start_agent_session(orch_agent, group_id, resume_session_id=None)
            self._app.post_message(AgentStatusChanged(
                agent_id=orch_agent.id, status=AgentStatus.active
            ))
        except Exception:
            logger.exception("Failed to start orchestrator for group %s", group_id)
            return

        # 4. Spawn the dispatch loop as a background task.
        self._force_advance_request = asyncio.Event()
        self._abort_request = asyncio.Event()
        self._dispatch_task = asyncio.create_task(
            self._dispatch_loop(group_id, orch_agent),
            name=f"dispatch-{group_id[:8]}",
        )

    async def resume_group(self, group_id: str) -> None:
        """Resume all sessions in a group using stored claude_session_ids."""
        logger.info("Resuming group %s", group_id)
        await self._cancel_dispatch_loop()
        self._active_group_id = group_id
        self._consumed_offset = 0
        self._step_index = 0
        self._dev_tester_retries = 0

        members = await self._repo.get_group_members(group_id)
        workers = [a for a in members if a.role != AgentRole.orchestrator]
        orch_agent = next((a for a in members if a.role == AgentRole.orchestrator), None)

        for agent in workers + ([orch_agent] if orch_agent else []):
            try:
                existing = await self._repo.get_session(agent.id, group_id)
                resume_id = existing.claude_session_id if existing else None
                await self._sm.start_agent_session(agent, group_id, resume_session_id=resume_id)
                self._app.post_message(AgentStatusChanged(
                    agent_id=agent.id, status=AgentStatus.active
                ))
            except Exception:
                logger.exception("Failed to resume session for agent %s", agent.name)
                await self._repo.update_agent_status(agent.id, AgentStatus.degraded)

        if orch_agent:
            self._force_advance_request = asyncio.Event()
            self._abort_request = asyncio.Event()
            self._dispatch_task = asyncio.create_task(
                self._dispatch_loop(group_id, orch_agent),
                name=f"dispatch-{group_id[:8]}",
            )

    async def stop_group(self, group_id: str) -> None:
        logger.info("Stopping group %s", group_id)
        await self._cancel_dispatch_loop()
        sessions = await self._repo.get_sessions_for_group(group_id)
        for session in sessions:
            try:
                await self._sm.stop_agent_session(session)
                self._app.post_message(AgentStatusChanged(
                    agent_id=session.agent_id, status=AgentStatus.stopped
                ))
            except Exception:
                logger.exception("Error stopping session %s", session.id)
        if self._active_group_id == group_id:
            self._active_group_id = None

    async def clear_all(self) -> None:
        import shutil

        await self._cancel_dispatch_loop()
        for group in await self._repo.get_groups():
            sessions = await self._repo.get_sessions_for_group(group.id)
            for session in sessions:
                try:
                    await self._sm.stop_agent_session(session)
                except Exception:
                    logger.exception("Error stopping session %s", session.id)
        self._active_group_id = None

        await self._repo.clear_all_runtime_state()

        if TEMP_DIR.exists():
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
            TEMP_DIR.mkdir(exist_ok=True)

    async def _cancel_dispatch_loop(self) -> None:
        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except (asyncio.CancelledError, Exception):
                pass
        self._dispatch_task = None

    def stop(self) -> None:
        """Backwards-compat shim for old App.on_unmount that calls supervisor.stop()."""
        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    async def _dispatch_loop(self, group_id: str, orch_agent: Agent) -> None:
        """One coroutine per active group. Reads orchestrator output, parses
        DISPATCH blocks, drives workers, injects WORKER_RESULT back."""
        logger.info("dispatch_loop started for group=%s", group_id)
        orch_session = await self._repo.get_session(orch_agent.id, group_id)
        if not orch_session:
            logger.error("dispatch_loop: no session for orchestrator %s", orch_agent.id)
            return
        orch_pane = orch_session.tmux_pane_id

        group = await self._repo.get_group(group_id)
        try:
            workflow = workflows.get_workflow(group.workflow_id) if group else None
        except KeyError:
            workflow = None
        step_total = len(workflow.steps) if workflow else 0

        in_flight: Optional[_InFlight] = None

        try:
            while True:
                await asyncio.sleep(DISPATCH_POLL_INTERVAL)

                # Operator-initiated abort
                if self._abort_request and self._abort_request.is_set():
                    logger.info("dispatch_loop: abort requested for group=%s", group_id)
                    self._app.post_message(WorkflowAborted(group_id=group_id, reason="user aborted"))
                    return

                if in_flight is None:
                    # ---- Looking for a new dispatch / completion / abort ----
                    pane_text = await self._sm.capture_pane_full(orch_pane)
                    if not pane_text:
                        continue

                    if orch_mod.is_workflow_complete(pane_text, self._consumed_offset):
                        logger.info("dispatch_loop: <<WORKFLOW_COMPLETE>> seen for group=%s", group_id)
                        self._app.post_message(WorkflowCompleted(group_id=group_id))
                        return
                    abort_reason = orch_mod.is_workflow_abort(pane_text, self._consumed_offset)
                    if abort_reason:
                        logger.info("dispatch_loop: <<WORKFLOW_ABORT>> for group=%s reason=%s",
                                    group_id, abort_reason)
                        self._app.post_message(WorkflowAborted(group_id=group_id, reason=abort_reason))
                        return

                    dispatch = orch_mod.parse_latest_dispatch(pane_text, self._consumed_offset)
                    if dispatch is None:
                        continue

                    # Validate dispatch text doesn't contain forbidden markers
                    err = orch_mod.validate_dispatch_text(dispatch.text)
                    if err:
                        logger.warning("dispatch rejected: %s", err)
                        await self._sm.send_keys(orch_pane, f"[PLATFORM_ERROR: {err}]")
                        self._consumed_offset = dispatch.end_offset
                        continue

                    # Resolve target worker
                    worker = await self._resolve_worker(group_id, dispatch.role)
                    if worker is None:
                        valid = await self._valid_role_names(group_id)
                        msg = f"unknown role '{dispatch.role}' — valid roles: {', '.join(valid)}"
                        logger.warning("dispatch rejected: %s", msg)
                        await self._sm.send_keys(orch_pane, f"[PLATFORM_ERROR: {msg}]")
                        self._consumed_offset = dispatch.end_offset
                        continue

                    worker_session = await self._repo.get_session(worker.id, group_id)
                    if not worker_session or worker_session.status != AgentStatus.active:
                        msg = f'role="{dispatch.role}" reason="pane not active"'
                        logger.warning("dispatch rejected: worker %s not active", worker.name)
                        await self._sm.send_keys(orch_pane, f"[WORKER_ERROR {msg}]")
                        self._consumed_offset = dispatch.end_offset
                        continue

                    # Send the dispatch text to the worker
                    await self._sm.send_keys(worker_session.tmux_pane_id, dispatch.text)
                    now = asyncio.get_event_loop().time()
                    in_flight = _InFlight(
                        dispatch=dispatch,
                        worker=worker,
                        worker_pane=worker_session.tmux_pane_id,
                        dispatch_at=now,
                        last_change_at=now,
                        last_pane_text="",
                        step_index=self._step_index,
                    )
                    self._consumed_offset = dispatch.end_offset
                    self._stall_notified = False
                    if self._force_advance_request:
                        self._force_advance_request.clear()
                    logger.info(
                        "dispatch group=%s role=%s step=%d/%d",
                        group_id, dispatch.role, self._step_index + 1, step_total,
                    )
                    continue

                # ---- in_flight: poll the worker pane ----
                worker_text = await self._sm.capture_pane_full(in_flight.worker_pane)
                now = asyncio.get_event_loop().time()
                if worker_text != in_flight.last_pane_text:
                    in_flight.last_pane_text = worker_text
                    in_flight.last_change_at = now

                # Operator force-advance: synthesise a silence-layer completion
                if self._force_advance_request and self._force_advance_request.is_set():
                    logger.info("dispatch_loop: force advance for group=%s", group_id)
                    result = CompletionResult(
                        layer=CompletionLayer.silence,
                        artifact=worker_text.rstrip(),
                        detail="force-advanced by operator",
                    )
                    self._force_advance_request.clear()
                else:
                    result = orch_mod.detect_completion(
                        pane_text=worker_text,
                        dispatch_end_offset=0,
                        last_change_at=in_flight.last_change_at,
                        dispatch_at=in_flight.dispatch_at,
                        now=now,
                        silence_timeout=WORKER_SILENCE_TIMEOUT,
                        stall_timeout=ORCHESTRATOR_STALL_TIMEOUT,
                    )

                if result.layer == CompletionLayer.pending:
                    continue

                if result.layer == CompletionLayer.stall:
                    if not self._stall_notified:
                        elapsed = now - in_flight.dispatch_at
                        logger.warning(
                            "dispatch_loop: STALL group=%s role=%s elapsed=%.0fs",
                            group_id, in_flight.dispatch.role, elapsed,
                        )
                        await self._sm.send_keys(
                            orch_pane,
                            f"[PLATFORM_STALL: no completion signal from "
                            f'role="{in_flight.dispatch.role}" after '
                            f"{int(ORCHESTRATOR_STALL_TIMEOUT)} seconds]",
                        )
                        self._app.post_message(WorkflowStalled(
                            group_id=group_id,
                            role=in_flight.dispatch.role,
                            elapsed=elapsed,
                        ))
                        self._stall_notified = True
                    continue

                # marker / silence / error: deliver result, clear in-flight
                via = result.layer.value
                worker_result_block = (
                    f'[WORKER_RESULT role="{in_flight.dispatch.role}" via="{via}"]\n'
                    f"{result.artifact}\n"
                    f"[/WORKER_RESULT]"
                )
                await self._sm.send_keys(orch_pane, worker_result_block)
                logger.info(
                    "completion group=%s role=%s via=%s artifact_len=%d",
                    group_id, in_flight.dispatch.role, via, len(result.artifact),
                )

                # Tester failure-loop bookkeeping (only meaningful for the standard workflow)
                if (
                    workflow
                    and result.tests_failed
                    and via == "marker"
                    and in_flight.dispatch.role == AgentRole.tester.value
                ):
                    self._dev_tester_retries += 1
                    logger.info(
                        "dispatch_loop: tester reported failures, retry #%d",
                        self._dev_tester_retries,
                    )

                self._app.post_message(WorkflowStepAdvanced(
                    group_id=group_id,
                    role=in_flight.dispatch.role,
                    via=via,
                    step_index=in_flight.step_index,
                    step_total=step_total,
                ))
                self._step_index += 1
                in_flight = None
        except asyncio.CancelledError:
            logger.info("dispatch_loop cancelled for group=%s", group_id)
            raise
        except Exception:
            logger.exception("dispatch_loop crashed for group=%s", group_id)

    async def _resolve_worker(self, group_id: str, role: str) -> Optional[Agent]:
        """Find the agent in `group_id` whose role matches `role` (case-insensitive)."""
        try:
            target_role = AgentRole(role)
        except ValueError:
            return None
        if target_role == AgentRole.orchestrator:
            return None  # forbid self-dispatch
        members = await self._repo.get_group_members(group_id)
        return next((a for a in members if a.role == target_role), None)

    async def _valid_role_names(self, group_id: str) -> list[str]:
        members = await self._repo.get_group_members(group_id)
        return sorted({a.role.value for a in members if a.role != AgentRole.orchestrator})

    # ------------------------------------------------------------------
    # Operator interventions (called from the TUI)
    # ------------------------------------------------------------------

    def force_advance(self, group_id: str) -> None:
        """Operator clicked Force Advance on a stalled dispatch toast."""
        if self._active_group_id == group_id and self._force_advance_request:
            self._force_advance_request.set()

    def abort_workflow(self, group_id: str) -> None:
        """Operator clicked Abort Workflow on a stalled dispatch toast."""
        if self._active_group_id == group_id and self._abort_request:
            self._abort_request.set()

    # ------------------------------------------------------------------
    # Individual agent controls
    # ------------------------------------------------------------------

    async def pause_agent(self, agent_id: str) -> None:
        await self._repo.set_agent_paused(agent_id, True)
        await self._repo.update_agent_status(agent_id, AgentStatus.paused)
        self._app.post_message(AgentStatusChanged(
            agent_id=agent_id, status=AgentStatus.paused
        ))
        logger.info("Agent %s paused", agent_id)

    async def resume_agent(self, agent_id: str) -> None:
        await self._repo.set_agent_paused(agent_id, False)
        await self._repo.update_agent_status(agent_id, AgentStatus.active)
        self._app.post_message(AgentStatusChanged(
            agent_id=agent_id, status=AgentStatus.active
        ))
        logger.info("Agent %s resumed", agent_id)
