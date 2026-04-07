"""tmux + Claude CLI session manager.

Manages the lifecycle of Claude CLI processes running inside tmux panes.
All Claude invocations use --dangerously-skip-permissions.

REQ-012 v2:
    - identity injection (F-01) deleted; no MCP tool calls = no AGENT_ID needed
    - per-agent MCP config files removed (no MCP server)
    - orchestrator-startup branch added: renders {{WORKFLOW_DEFINITION}},
      {{WORKER_ROSTER}}, {{COMPLETION_MARKER}} placeholders before launching
    - F-04 readiness poll retained verbatim (now load-bearing — orchestrator
      cannot dispatch until workers are truly active)
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import uuid
from pathlib import Path
from typing import Optional

from agent_management.backend.models import Agent, AgentRole, AgentStatus, Session, _now
from agent_management.backend.repository import Repository
from agent_management.backend import workflows
from agent_management.shared.config import (
    CLAUDE_CMD,
    DIRECT_SEND_MAX_LEN,
    SESSION_START_TIMEOUT,
    SESSION_STOP_TIMEOUT,
    TEMP_DIR,
    TMUX_SESSION_PREFIX,
)

logger = logging.getLogger(__name__)


def _cleanup_temp(path: Path) -> None:
    """Delete a temp file if it still exists (best-effort cleanup)."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


COMPLETION_MARKER = "<<TASK_DONE>>"


class SessionManager:
    """Manages tmux session + Claude CLI lifecycle for all agents."""

    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    # ------------------------------------------------------------------
    # tmux helpers
    # ------------------------------------------------------------------

    async def _tmux(self, *args: str) -> tuple[int, str, str]:
        """Run a tmux command. Returns (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

    async def _ensure_tmux_session(self, session_name: str) -> None:
        rc, _, _ = await self._tmux("has-session", "-t", session_name)
        if rc != 0:
            rc2, _, err = await self._tmux(
                "new-session", "-d", "-s", session_name,
                "-x", "220", "-y", "50",
            )
            if rc2 != 0:
                raise RuntimeError(f"Failed to create tmux session {session_name!r}: {err}")
            logger.info("Created tmux session: %s", session_name)
        else:
            logger.debug("tmux session %s already exists", session_name)

    async def _new_pane(self, session_name: str) -> str:
        rc, stdout, err = await self._tmux(
            "new-window", "-P", "-F", "#{pane_id}", "-t", session_name,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to create tmux pane: {err}")
        pane_id = stdout.strip()
        logger.debug("Created tmux pane %s in session %s", pane_id, session_name)
        return pane_id

    async def capture_pane_output(self, pane_id: str, lines: int = 50) -> str:
        """Return the last N visible lines from a tmux pane."""
        rc, stdout, _ = await self._tmux("capture-pane", "-p", "-t", pane_id)
        if rc != 0:
            return ""
        all_lines = stdout.splitlines()
        return "\n".join(all_lines[-lines:]) if all_lines else ""

    async def capture_pane_full(self, pane_id: str, history_lines: int = 2000) -> str:
        """Capture pane content including scrollback up to `history_lines` lines.

        REQ-012 v2: used by Supervisor.dispatch_loop to track orchestrator/worker
        output across many turns without losing earlier dispatches.
        """
        rc, stdout, _ = await self._tmux(
            "capture-pane", "-p", "-S", f"-{history_lines}", "-t", pane_id,
        )
        return stdout if rc == 0 else ""

    @staticmethod
    def _sanitize_payload(text: str) -> str:
        """Strip tmux control sequences and cap payload size."""
        import re
        sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
        max_bytes = 50_000
        if len(sanitized) > max_bytes:
            sanitized = sanitized[:max_bytes] + "\n[...truncated]"
        return sanitized

    async def send_keys(self, pane_id: str, text: str) -> None:
        """Inject text into a tmux pane.

        Short payloads sent directly. Long payloads written to a temp file and
        injected via `cat`. All payloads are sanitized.
        """
        safe_text = self._sanitize_payload(text)
        if len(safe_text) <= DIRECT_SEND_MAX_LEN:
            rc, _, err = await self._tmux("send-keys", "-t", pane_id, safe_text, "Enter")
            if rc != 0:
                logger.warning("send-keys failed for pane %s: %s", pane_id, err)
        else:
            tmp_path = TEMP_DIR / f"agent_msg_{uuid.uuid4().hex}.txt"
            tmp_path.write_text(safe_text, encoding="utf-8")
            try:
                tmp_path.chmod(0o600)
            except OSError:
                pass  # Windows
            cmd = f"cat {shlex.quote(str(tmp_path))}"
            rc, _, err = await self._tmux("send-keys", "-t", pane_id, cmd, "Enter")
            if rc != 0:
                logger.warning("send-keys (cat) failed for pane %s: %s", pane_id, err)
            asyncio.get_running_loop().call_later(5.0, _cleanup_temp, tmp_path)

    async def pane_exists(self, pane_id: str) -> bool:
        rc, _, _ = await self._tmux("display-message", "-p", "-t", pane_id, "")
        return rc == 0

    # ------------------------------------------------------------------
    # Orchestrator system prompt rendering
    # ------------------------------------------------------------------

    async def _render_orchestrator_prompt(
        self, agent: Agent, group_id: str
    ) -> str:
        """Build the orchestrator's system prompt by substituting placeholders.

        Pulls the orchestrator template from the repository, looks up the group's
        workflow, and assembles the worker roster from live session state so the
        rendered text reflects the actual pane IDs the orchestrator can dispatch to.
        """
        template = await self._repo.get_orchestrator_template()
        if not template:
            template = agent.system_prompt or ""

        group = await self._repo.get_group(group_id)
        if not group:
            raise RuntimeError(f"Cannot render orchestrator prompt — group {group_id} not found")
        try:
            workflow = workflows.get_workflow(group.workflow_id)
        except KeyError:
            raise RuntimeError(
                f"Group {group_id} references unknown workflow '{group.workflow_id}'. "
                f"Edit the group to select a valid workflow."
            )

        # Build roster: (role, agent_name, pane_id) for each non-orchestrator member.
        workers = await self._repo.get_workers_for_group(group_id)
        roster: list[tuple[AgentRole, str, str]] = []
        for w in workers:
            sess = await self._repo.get_session(w.id, group_id)
            pane_id = sess.tmux_pane_id if sess else "?"
            roster.append((w.role, w.name, pane_id))

        # Validate role coverage
        required = workflows.required_roles(workflow)
        present = {r for r, _, _ in roster}
        missing = required - present
        if missing:
            raise RuntimeError(
                f"Workflow '{workflow.id}' requires roles {sorted(r.value for r in missing)} "
                f"but the group only contains {sorted(r.value for r in present)}. "
                f"Add the missing agents or pick a different workflow."
            )

        rendered = template
        rendered = rendered.replace(
            "{{WORKFLOW_DEFINITION}}", workflows.render_for_orchestrator(workflow, roster)
        )
        rendered = rendered.replace(
            "{{WORKER_ROSTER}}", workflows.render_roster(roster)
        )
        rendered = rendered.replace("{{COMPLETION_MARKER}}", COMPLETION_MARKER)
        return rendered

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def start_agent_session(
        self,
        agent: Agent,
        group_id: str,
        resume_session_id: Optional[str] = None,
    ) -> Session:
        """Start (or resume) a Claude CLI session for an agent inside tmux."""
        # 0. Validate working directory
        working_dir = Path(agent.working_dir).resolve()
        if not working_dir.is_dir():
            raise ValueError(
                f"Agent '{agent.name}' working directory does not exist: {agent.working_dir}"
            )
        if agent.system_prompt_file:
            prompt_file = Path(agent.system_prompt_file).resolve()
            if not prompt_file.is_file():
                raise ValueError(
                    f"Agent '{agent.name}' system_prompt_file does not exist: "
                    f"{agent.system_prompt_file}"
                )

        # 1. Ensure tmux session
        session_name = f"{TMUX_SESSION_PREFIX}-{group_id[:8]}"
        await self._ensure_tmux_session(session_name)

        # 2. Create pane
        pane_id = await self._new_pane(session_name)

        # 3. Build claude command
        new_session_uuid = str(uuid.uuid4())
        cmd_parts = list(CLAUDE_CMD)

        if resume_session_id:
            cmd_parts += ["--resume", resume_session_id]
            logger.info("Resuming agent %s with session %s", agent.name, resume_session_id)
        else:
            cmd_parts += ["--session-id", new_session_uuid]
            logger.info("Starting fresh session for agent %s", agent.name)

        # 4. Resolve effective system prompt.
        # REQ-012 v2: orchestrator agents get the rendered template; workers get
        # their stored prompt verbatim. No identity block is prepended to anyone.
        if agent.role == AgentRole.orchestrator:
            effective_prompt = await self._render_orchestrator_prompt(agent, group_id)
            tmp_prompt = TEMP_DIR / f"orch_prompt_{agent.id[:8]}.txt"
            tmp_prompt.write_text(effective_prompt, encoding="utf-8")
            try:
                tmp_prompt.chmod(0o600)
            except OSError:
                pass
            cmd_parts += ["--system-prompt-file", str(tmp_prompt)]
            logger.debug("Orchestrator prompt rendered, agent_id=%s, tmp=%s", agent.id, tmp_prompt)
        elif agent.system_prompt:
            cmd_parts += ["--system-prompt", agent.system_prompt]
        elif agent.system_prompt_file:
            cmd_parts += ["--system-prompt-file", agent.system_prompt_file]
        # else: custom role with no prompt — no --system-prompt arg

        cmd_parts += ["--name", agent.name]

        cd_cmd = f"cd {shlex.quote(str(working_dir))}"
        claude_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
        full_cmd = f"{cd_cmd} && {claude_cmd}"

        rc, _, err = await self._tmux("send-keys", "-t", pane_id, full_cmd, "Enter")
        if rc != 0:
            raise RuntimeError(f"Failed to start claude in pane {pane_id}: {err}")

        # 5. Persist session record
        session = Session(
            agent_id=agent.id,
            group_id=group_id,
            claude_session_id=new_session_uuid,
            previous_session_id=resume_session_id or "",
            tmux_session_name=session_name,
            tmux_pane_id=pane_id,
            status=AgentStatus.starting,
            started_at=_now(),
        )
        await self._repo.save_session(session)
        await self._repo.update_agent_status(agent.id, AgentStatus.starting)

        # 6. Poll for readiness (F-04 — load-bearing in v2)
        await self._wait_for_pane_ready(agent, pane_id, session)

        logger.info(
            "Agent %s session started: pane=%s session_id=%s status=%s",
            agent.name, pane_id, new_session_uuid, session.status.value,
        )
        return session

    async def _wait_for_pane_ready(
        self,
        agent: Agent,
        pane_id: str,
        session: Session,
    ) -> None:
        """F-04: Poll capture-pane until Claude CLI produces output or timeout."""
        logger.debug("Polling pane for readiness, agent=%s, pane=%s", agent.name, pane_id)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + SESSION_START_TIMEOUT
        ready = False

        try:
            while loop.time() < deadline:
                await asyncio.sleep(0.2)
                rc, output, _ = await self._tmux("capture-pane", "-p", "-t", pane_id)
                if rc == 0 and output.strip():
                    ready = True
                    logger.debug("Pane ready, agent=%s, pane=%s", agent.name, pane_id)
                    break
        except Exception:
            logger.exception("capture-pane poll failed for agent %s", agent.name)

        if ready:
            session.status = AgentStatus.active
            await self._repo.save_session(session)
            await self._repo.update_agent_status(agent.id, AgentStatus.active)
            logger.info("Agent %s is active, pane=%s", agent.name, pane_id)
        else:
            session.status = AgentStatus.degraded
            await self._repo.save_session(session)
            await self._repo.update_agent_status(agent.id, AgentStatus.degraded)
            logger.error(
                "Agent %s did not start within %ss — marked degraded, pane=%s",
                agent.name, SESSION_START_TIMEOUT, pane_id,
            )

    async def stop_agent_session(self, session: Session) -> None:
        pane_id = session.tmux_pane_id
        if not pane_id:
            logger.warning("No pane_id for session %s", session.id)
            return

        logger.info("Stopping session for pane %s", pane_id)
        await self._tmux("send-keys", "-t", pane_id, "C-c", "")
        await asyncio.sleep(SESSION_STOP_TIMEOUT)
        if await self.pane_exists(pane_id):
            await self._tmux("kill-pane", "-t", pane_id)

        stopped_at = _now()
        await self._repo.update_session_status(session.id, AgentStatus.stopped, stopped_at)
        await self._repo.update_agent_status(session.agent_id, AgentStatus.stopped)
        logger.info("Session %s stopped", session.id)

    async def restart_agent_session(
        self,
        agent: Agent,
        group_id: str,
    ) -> Session:
        existing = await self._repo.get_session(agent.id, group_id)
        if existing and existing.status not in (AgentStatus.stopped, AgentStatus.degraded):
            await self.stop_agent_session(existing)
        resume_id = existing.claude_session_id if existing else None
        return await self.start_agent_session(agent, group_id, resume_session_id=resume_id)
