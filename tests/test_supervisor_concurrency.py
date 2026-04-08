"""REQ-016 F-03 — verify that start/stop/resume_group parallelise agent lifecycles.

Uses a FakeSessionManager with a configurable per-call sleep to prove parallelism:
if 5 workers each sleep 100 ms and start_group runs them concurrently, the total
elapsed should be < 300 ms (well under the 500 ms sequential lower bound).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Optional

import pytest
import pytest_asyncio

from agent_management.backend.models import (
    Agent,
    AgentRole,
    AgentStatus,
    Group,
    Session,
)
from agent_management.backend.repository import Repository
from agent_management.backend.supervisor import Supervisor


class SlowFakeSessionManager:
    """FakeSessionManager that sleeps a fixed duration per start/stop call.

    The sleep simulates tmux pane creation + Claude CLI readiness polling
    wall time. Used only to measure whether start/stop_group parallelise.
    """

    def __init__(self, repo: Repository, delay_seconds: float = 0.1) -> None:
        self._repo = repo
        self.delay = delay_seconds
        self.start_calls: list[str] = []  # agent.id in order of completion
        self.stop_calls: list[str] = []
        self.fail_ids: set[str] = set()

    async def start_agent_session(
        self, agent, group_id, resume_session_id: Optional[str] = None
    ):
        await asyncio.sleep(self.delay)
        if agent.id in self.fail_ids:
            raise RuntimeError(f"scripted failure for {agent.name}")
        session = Session(
            agent_id=agent.id,
            group_id=group_id,
            tmux_pane_id=f"%{len(self.start_calls)}",
            status=AgentStatus.active,
        )
        await self._repo.save_session(session)
        await self._repo.update_agent_status(agent.id, AgentStatus.active)
        self.start_calls.append(agent.id)
        return session

    async def stop_agent_session(self, session) -> None:
        await asyncio.sleep(self.delay)
        await self._repo.update_session_status(
            session.id, AgentStatus.stopped, stopped_at="2026-04-08T12:00:00Z"
        )
        await self._repo.update_agent_status(session.agent_id, AgentStatus.stopped)
        self.stop_calls.append(session.agent_id)

    # Methods dispatch_loop needs — unused by the concurrency tests but present
    # so the Supervisor doesn't crash if it tries to spawn the loop.
    async def capture_pane_full(self, pane_id: str, history_lines: int = 2000, ansi: bool = False) -> str:
        return ""

    async def capture_pane_output(self, pane_id: str, lines: int = 50) -> str:
        return ""

    async def send_keys(self, pane_id: str, text: str) -> None:
        pass

    async def send_raw_keys(self, pane_id: str, *key_args: str):
        return 0, "", ""

    async def pane_exists(self, pane_id: str) -> bool:
        return True


class FakeApp:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def post_message(self, msg: Any) -> None:
        self.messages.append(msg)


@pytest_asyncio.fixture
async def concurrency_scenario(tmp_path: Path):
    repo = Repository(db_path=tmp_path / "conc.db")
    await repo.init()

    group = Group(name="conc", workflow_id="standard")
    await repo.save_group(group)

    agents: dict[AgentRole, Agent] = {}
    for role in [
        AgentRole.orchestrator,
        AgentRole.product_manager,
        AgentRole.tech_director,
        AgentRole.developer,
        AgentRole.tester,
        AgentRole.user,
    ]:
        a = Agent(name=f"conc - {role.value}", role=role, working_dir="/tmp")
        await repo.save_agent(a)
        await repo.add_group_member(group.id, a.id)
        agents[role] = a

    fake_sm = SlowFakeSessionManager(repo, delay_seconds=0.1)
    fake_app = FakeApp()
    sup = Supervisor(repo, fake_sm, fake_app)  # type: ignore[arg-type]

    yield {"sup": sup, "sm": fake_sm, "app": fake_app, "group": group, "agents": agents, "repo": repo}
    await repo.close()


# ---- start_group parallelism ------------------------------------------------


class TestStartGroupConcurrent:
    async def test_workers_run_in_parallel(self, concurrency_scenario):
        sup = concurrency_scenario["sup"]
        sm = concurrency_scenario["sm"]
        group = concurrency_scenario["group"]

        # 6 agents total (5 workers + orchestrator). Each sleeps 0.1 s.
        # Sequential worst case = 0.6 s.
        # Concurrent case = 0.1 s workers in parallel + 0.1 s orchestrator = ~0.2 s.
        # Allow generous headroom; anything under 0.4 s means gather() is working.
        t0 = time.monotonic()
        await sup.start_group(group.id)
        elapsed = time.monotonic() - t0

        assert len(sm.start_calls) == 6  # 5 workers + orchestrator
        assert elapsed < 0.4, f"Expected concurrent start < 0.4s, got {elapsed:.2f}s"

    async def test_one_worker_failure_does_not_block_others(self, concurrency_scenario):
        sup = concurrency_scenario["sup"]
        sm = concurrency_scenario["sm"]
        agents = concurrency_scenario["agents"]
        group = concurrency_scenario["group"]

        # Make the Developer worker fail; other workers should still start.
        sm.fail_ids.add(agents[AgentRole.developer].id)

        await sup.start_group(group.id)

        # 4 other workers should have completed successfully
        assert agents[AgentRole.product_manager].id in sm.start_calls
        assert agents[AgentRole.tech_director].id in sm.start_calls
        assert agents[AgentRole.tester].id in sm.start_calls
        assert agents[AgentRole.user].id in sm.start_calls
        # Developer is NOT in start_calls (it raised)
        assert agents[AgentRole.developer].id not in sm.start_calls

        # The failing worker should be marked degraded
        dev_after = await concurrency_scenario["repo"].get_agent(
            agents[AgentRole.developer].id
        )
        assert dev_after.status == AgentStatus.degraded


# ---- stop_group parallelism -------------------------------------------------


class TestStopGroupConcurrent:
    async def test_stop_workers_in_parallel(self, concurrency_scenario):
        sup = concurrency_scenario["sup"]
        sm = concurrency_scenario["sm"]
        group = concurrency_scenario["group"]

        await sup.start_group(group.id)
        # Cancel the dispatch loop that start_group spawned so stop_group
        # doesn't trip on it.
        await sup._cancel_dispatch_loop()

        sm.stop_calls.clear()
        t0 = time.monotonic()
        await sup.stop_group(group.id)
        elapsed = time.monotonic() - t0

        assert len(sm.stop_calls) == 6  # all sessions stopped
        # Sequential = 0.6 s; concurrent should be ~0.1 s. Allow 0.4 s margin.
        assert elapsed < 0.4, f"Expected concurrent stop < 0.4s, got {elapsed:.2f}s"


# ---- resume_group parallelism -----------------------------------------------


class TestResumeGroupConcurrent:
    async def test_resume_workers_in_parallel(self, concurrency_scenario):
        sup = concurrency_scenario["sup"]
        sm = concurrency_scenario["sm"]
        group = concurrency_scenario["group"]

        # Start then stop to populate session state
        await sup.start_group(group.id)
        await sup._cancel_dispatch_loop()
        await sup.stop_group(group.id)
        await sup._cancel_dispatch_loop()

        sm.start_calls.clear()
        t0 = time.monotonic()
        await sup.resume_group(group.id)
        elapsed = time.monotonic() - t0

        assert len(sm.start_calls) == 6
        assert elapsed < 0.4, f"Expected concurrent resume < 0.4s, got {elapsed:.2f}s"

        # Clean up dispatch task
        await sup._cancel_dispatch_loop()
