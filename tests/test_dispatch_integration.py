"""REQ-014 F-07 — Integration tests for Supervisor.dispatch_loop.

These tests drive the REAL Supervisor class against a REAL in-memory
Repository with a FAKE SessionManager that records send_keys calls and
replays scripted capture_pane responses. No tmux, no subprocess, no LLM.

Strategy:
  - Monkeypatch DISPATCH_POLL_INTERVAL and the silence/stall timeouts to
    fractions of a second so tests run fast.
  - Spawn the dispatch_loop as an asyncio task, poll fake_sm.send_keys_calls
    and fake_app.messages to verify behaviour, cancel the task on teardown.
  - Use scripted pane content deques keyed by pane_id; each capture call
    pops the next scripted state, falling back to the last known state.
"""
from __future__ import annotations

import asyncio
import collections
from pathlib import Path
from typing import Any, Optional

import pytest
import pytest_asyncio

from agent_management.backend import supervisor as supervisor_mod
from agent_management.backend.models import Agent, AgentRole, AgentStatus, Group, Session
from agent_management.backend.repository import Repository
from agent_management.backend.supervisor import (
    Supervisor,
    WorkflowAborted,
    WorkflowCompleted,
    WorkflowStalled,
    WorkflowStepAdvanced,
)


# ---- Fakes -------------------------------------------------------------------

class FakeSessionManager:
    """In-memory stand-in for SessionManager used by the dispatch_loop.

    Only the methods actually called by `Supervisor.dispatch_loop` are
    implemented. Anything else raises AttributeError — so API drift in the
    real SessionManager will surface as a test failure here.
    """

    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self.send_keys_calls: list[tuple[str, str]] = []
        # Per-pane scripted content: deque of strings. popleft on capture;
        # if empty, return the last-emitted value (so static panes work).
        self._scripts: dict[str, collections.deque] = {}
        self._last_emit: dict[str, str] = {}
        self._pane_exists_map: dict[str, bool] = {}

    # --- scripting helpers used by tests ---
    def script_pane(self, pane_id: str, values: list[str]) -> None:
        self._scripts[pane_id] = collections.deque(values)

    def set_pane_state(self, pane_id: str, value: str) -> None:
        self._last_emit[pane_id] = value

    def kill_pane(self, pane_id: str) -> None:
        self._pane_exists_map[pane_id] = False

    # --- interface mirroring SessionManager ---
    async def send_keys(self, pane_id: str, text: str) -> None:
        self.send_keys_calls.append((pane_id, text))
        # If text was sent to the orchestrator pane, append it to the pane's
        # scripted content so the next capture sees it (simulates tmux
        # faithfully: send-keys actually appears in the pane output).
        current = self._last_emit.get(pane_id, "")
        self._last_emit[pane_id] = current + ("\n" if current else "") + text

    async def capture_pane_full(self, pane_id: str, history_lines: int = 2000) -> str:
        if pane_id in self._scripts and self._scripts[pane_id]:
            value = self._scripts[pane_id].popleft()
            self._last_emit[pane_id] = value
            return value
        return self._last_emit.get(pane_id, "")

    async def capture_pane_output(self, pane_id: str, lines: int = 50) -> str:
        return await self.capture_pane_full(pane_id, lines)

    async def pane_exists(self, pane_id: str) -> bool:
        return self._pane_exists_map.get(pane_id, True)

    async def start_agent_session(
        self, agent, group_id, resume_session_id: Optional[str] = None
    ):
        # Minimal fake session creation — the integration tests exercise the
        # dispatch loop AFTER sessions already exist, not the start flow.
        raise NotImplementedError("FakeSessionManager does not implement start_agent_session")

    async def stop_agent_session(self, session) -> None:
        pass


class FakeApp:
    """Minimal stand-in for textual.App.post_message."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    def post_message(self, msg: Any) -> None:
        self.messages.append(msg)


# ---- Fixtures ----------------------------------------------------------------


@pytest_asyncio.fixture
async def repo(tmp_path: Path):
    r = Repository(db_path=tmp_path / "test.db")
    await r.init()
    yield r
    await r.close()


@pytest_asyncio.fixture
async def scenario(repo: Repository, monkeypatch):
    """Build a group with orchestrator + 5 workers, all with active sessions.

    Returns: (supervisor, fake_sm, fake_app, group, agents_by_role,
              orch_pane_id, worker_pane_ids)
    """
    # Fast timeouts so tests don't wait real seconds.
    monkeypatch.setattr(supervisor_mod, "DISPATCH_POLL_INTERVAL", 0.005)
    monkeypatch.setattr(supervisor_mod, "WORKER_SILENCE_TIMEOUT", 0.15)
    monkeypatch.setattr(supervisor_mod, "ORCHESTRATOR_STALL_TIMEOUT", 0.30)

    group = Group(name="test-group", workflow_id="standard")
    await repo.save_group(group)

    agents_by_role: dict[AgentRole, Agent] = {}
    pane_ids: dict[AgentRole, str] = {}
    roles = [
        AgentRole.orchestrator,
        AgentRole.product_manager,
        AgentRole.tech_director,
        AgentRole.developer,
        AgentRole.tester,
        AgentRole.user,
    ]
    for idx, role in enumerate(roles):
        agent = Agent(name=f"test - {role.value}", role=role, working_dir="/tmp")
        await repo.save_agent(agent)
        await repo.update_agent_status(agent.id, AgentStatus.active)
        await repo.add_group_member(group.id, agent.id)
        pane_id = f"%{idx}"
        sess = Session(
            agent_id=agent.id,
            group_id=group.id,
            tmux_pane_id=pane_id,
            status=AgentStatus.active,
        )
        await repo.save_session(sess)
        agents_by_role[role] = agent
        pane_ids[role] = pane_id

    fake_sm = FakeSessionManager(repo)
    fake_app = FakeApp()
    supervisor = Supervisor(repo, fake_sm, fake_app)  # type: ignore[arg-type]
    supervisor._active_group_id = group.id
    supervisor._force_advance_request = asyncio.Event()
    supervisor._abort_request = asyncio.Event()

    return {
        "supervisor": supervisor,
        "sm": fake_sm,
        "app": fake_app,
        "group": group,
        "agents": agents_by_role,
        "orch_pane": pane_ids[AgentRole.orchestrator],
        "pane_ids": pane_ids,
    }


async def _drive(supervisor: Supervisor, orch_agent: Agent, max_seconds: float = 0.8):
    """Run the dispatch loop for up to max_seconds, then cancel it."""
    group_id = supervisor._active_group_id
    task = asyncio.create_task(supervisor._dispatch_loop(group_id, orch_agent))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=max_seconds)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    return task


def _sends_to(sm: FakeSessionManager, pane_id: str) -> list[str]:
    return [text for (pid, text) in sm.send_keys_calls if pid == pane_id]


# ---- Tests -------------------------------------------------------------------


class TestHappyPath:
    async def test_single_dispatch_and_marker_completion(self, scenario):
        """Orchestrator dispatches PM; PM emits <<TASK_DONE>>; supervisor
        injects [WORKER_RESULT] back into the orchestrator pane; next turn
        the orchestrator emits <<WORKFLOW_COMPLETE>> and the loop exits."""
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        app = scenario["app"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        pm_pane = scenario["pane_ids"][AgentRole.product_manager]
        orch_pane = scenario["orch_pane"]

        sm.script_pane(orch_pane, [
            # Iteration 1: orchestrator has emitted a dispatch
            '<<DISPATCH role="product_manager" text="write a spec">><</DISPATCH>>',
        ])
        # After the dispatch is injected into the PM pane, it will emit TASK_DONE
        sm.script_pane(pm_pane, [
            "",                        # polling before worker responds
            "thinking...",
            "thinking...\nspec body here\n<<TASK_DONE>>",
        ])
        # Once the result is injected, orchestrator follows with WORKFLOW_COMPLETE
        # (FakeSessionManager's send_keys appends to the pane state, so the
        # injected [WORKER_RESULT] is already in orch_pane text; we append
        # the orchestrator's own WORKFLOW_COMPLETE via a separate scripted state.)
        # Arrange for a later orchestrator emission once the result is delivered.
        # Easier: just wait for the completion marker side-effect and then
        # manually advance the orchestrator pane.

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        # Let the loop discover the dispatch and deliver the result.
        await asyncio.sleep(0.10)

        # Confirm the PM pane received the dispatch text
        pm_sends = _sends_to(sm, pm_pane)
        assert any("write a spec" in s for s in pm_sends), (
            f"PM pane never received the dispatch text; got: {pm_sends}"
        )

        # Confirm the orchestrator pane received a WORKER_RESULT injection
        orch_sends = _sends_to(sm, orch_pane)
        assert any("[WORKER_RESULT" in s for s in orch_sends), (
            f"orch pane never received WORKER_RESULT; got: {orch_sends}"
        )
        assert any("spec body here" in s for s in orch_sends), (
            "WORKER_RESULT missing extracted artifact"
        )

        # Confirm WorkflowStepAdvanced was posted
        step_msgs = [m for m in app.messages if isinstance(m, WorkflowStepAdvanced)]
        assert len(step_msgs) >= 1
        assert step_msgs[0].role == "product_manager"
        assert step_msgs[0].via == "marker"

        # Now signal workflow completion by appending to the orch pane
        sm.set_pane_state(orch_pane, sm._last_emit[orch_pane] + "\n<<WORKFLOW_COMPLETE>>")
        # Let the loop observe it
        await asyncio.sleep(0.05)

        # Loop should exit and post WorkflowCompleted
        completed = [m for m in app.messages if isinstance(m, WorkflowCompleted)]
        assert len(completed) == 1

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def test_dispatch_dedup(self, scenario):
        """Same dispatch content appears in two consecutive captures — the
        worker pane should only receive it once (signature dedup)."""
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        pm_pane = scenario["pane_ids"][AgentRole.product_manager]
        orch_pane = scenario["orch_pane"]

        dispatch_block = (
            '<<DISPATCH role="product_manager" text="do it">><</DISPATCH>>'
        )
        sm.set_pane_state(orch_pane, dispatch_block)
        sm.set_pane_state(pm_pane, "working\n<<TASK_DONE>>")

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        await asyncio.sleep(0.15)  # multiple poll iterations
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        # The PM pane should have received "do it" exactly once.
        pm_sends = _sends_to(sm, pm_pane)
        do_it_count = sum(1 for s in pm_sends if "do it" in s)
        assert do_it_count == 1, (
            f"Expected exactly 1 dispatch delivery, got {do_it_count}: {pm_sends}"
        )


class TestDispatchValidation:
    async def test_unknown_role_triggers_platform_error(self, scenario):
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        orch_pane = scenario["orch_pane"]

        sm.set_pane_state(
            orch_pane,
            '<<DISPATCH role="marketing" text="do sth">><</DISPATCH>>',
        )

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        await asyncio.sleep(0.10)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        orch_sends = _sends_to(sm, orch_pane)
        assert any("[PLATFORM_ERROR" in s for s in orch_sends)
        assert any("unknown role 'marketing'" in s for s in orch_sends)

    async def test_forbidden_marker_in_dispatch_text_rejected(self, scenario):
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        orch_pane = scenario["orch_pane"]
        dev_pane = scenario["pane_ids"][AgentRole.developer]

        # Dispatch text contains <<TASK_DONE>> — validate_dispatch_text rejects.
        sm.set_pane_state(
            orch_pane,
            '<<DISPATCH role="developer" text="do <<TASK_DONE>>">><</DISPATCH>>',
        )

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        await asyncio.sleep(0.10)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        orch_sends = _sends_to(sm, orch_pane)
        assert any("[PLATFORM_ERROR" in s and "TASK_DONE" in s for s in orch_sends)
        # And the worker should NEVER have been sent anything
        assert _sends_to(sm, dev_pane) == []

    async def test_dispatch_to_self_rejected(self, scenario):
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        orch_pane = scenario["orch_pane"]

        sm.set_pane_state(
            orch_pane,
            '<<DISPATCH role="orchestrator" text="loop">><</DISPATCH>>',
        )

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        await asyncio.sleep(0.10)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        orch_sends = _sends_to(sm, orch_pane)
        assert any("[PLATFORM_ERROR" in s for s in orch_sends)


class TestCompletionLayers:
    async def test_silence_layer_fires_when_worker_never_emits_marker(self, scenario):
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        app = scenario["app"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        pm_pane = scenario["pane_ids"][AgentRole.product_manager]
        orch_pane = scenario["orch_pane"]

        sm.set_pane_state(
            orch_pane,
            '<<DISPATCH role="product_manager" text="silent one">><</DISPATCH>>',
        )
        # PM pane has some content but never emits <<TASK_DONE>>
        sm.set_pane_state(pm_pane, "some partial output")

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        # WORKER_SILENCE_TIMEOUT is 0.15s in tests; give 0.4s total.
        await asyncio.sleep(0.40)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        # The completion must have fired via 'silence', not 'marker'.
        step_msgs = [m for m in app.messages if isinstance(m, WorkflowStepAdvanced)]
        assert any(m.via == "silence" for m in step_msgs), (
            f"Expected silence layer; got msgs: {[(m.role, m.via) for m in step_msgs]}"
        )

    async def test_stall_layer_posts_workflow_stalled(self, scenario, monkeypatch):
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        app = scenario["app"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        pm_pane = scenario["pane_ids"][AgentRole.product_manager]
        orch_pane = scenario["orch_pane"]

        # To exercise the stall layer specifically we need silence to NOT fire
        # first. FakeSessionManager echoes the dispatch text into the worker
        # pane (matching real tmux), so the pane is never truly empty and the
        # silence layer would normally trip after 0.15s. Push silence timeout
        # well above the stall timeout so the test isolates stall behaviour.
        monkeypatch.setattr(supervisor_mod, "WORKER_SILENCE_TIMEOUT", 10.0)
        monkeypatch.setattr(supervisor_mod, "ORCHESTRATOR_STALL_TIMEOUT", 0.20)

        sm.set_pane_state(
            orch_pane,
            '<<DISPATCH role="product_manager" text="silent">><</DISPATCH>>',
        )
        sm.set_pane_state(pm_pane, "partial response never finishing")

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        # Let the stall timeout elapse.
        await asyncio.sleep(0.35)

        stalled = [m for m in app.messages if isinstance(m, WorkflowStalled)]
        assert len(stalled) >= 1
        assert stalled[0].role == "product_manager"

        # Now have the operator force-advance; the dispatch should complete.
        sup.force_advance(scenario["group"].id)
        await asyncio.sleep(0.10)

        step_msgs = [m for m in app.messages if isinstance(m, WorkflowStepAdvanced)]
        assert any(m.via == "silence" for m in step_msgs), (
            "Force advance should produce a silence-layer completion"
        )

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def test_abort_workflow(self, scenario):
        sup = scenario["supervisor"]
        app = scenario["app"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        await asyncio.sleep(0.05)
        sup.abort_workflow(scenario["group"].id)
        await asyncio.sleep(0.10)

        aborted = [m for m in app.messages if isinstance(m, WorkflowAborted)]
        assert len(aborted) == 1
        assert aborted[0].reason == "user aborted"

        # Task should have returned on its own after posting WorkflowAborted.
        assert task.done()

    async def test_workflow_complete_marker_exits_loop(self, scenario):
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        app = scenario["app"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        orch_pane = scenario["orch_pane"]

        sm.set_pane_state(orch_pane, "nothing to do\n<<WORKFLOW_COMPLETE>>")

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        await asyncio.sleep(0.05)

        completed = [m for m in app.messages if isinstance(m, WorkflowCompleted)]
        assert len(completed) == 1
        assert task.done()

    async def test_workflow_abort_marker_exits_loop(self, scenario):
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        app = scenario["app"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        orch_pane = scenario["orch_pane"]

        sm.set_pane_state(
            orch_pane,
            '<<WORKFLOW_ABORT reason="unrecoverable error"/>>',
        )

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        await asyncio.sleep(0.05)

        aborted = [m for m in app.messages if isinstance(m, WorkflowAborted)]
        assert len(aborted) == 1
        assert aborted[0].reason == "unrecoverable error"
        assert task.done()


class TestWorkerPaneCrash:
    async def test_worker_pane_vanished_reports_worker_error(self, scenario):
        """REQ-014 F-03: if pane_exists returns False while in-flight, the
        supervisor injects WORKER_ERROR instead of waiting for silence timeout."""
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        pm_pane = scenario["pane_ids"][AgentRole.product_manager]
        orch_pane = scenario["orch_pane"]

        sm.set_pane_state(
            orch_pane,
            '<<DISPATCH role="product_manager" text="will crash">><</DISPATCH>>',
        )
        sm.set_pane_state(pm_pane, "starting")

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        # Let dispatch reach the worker
        await asyncio.sleep(0.05)
        # Kill the pane mid-flight
        sm.kill_pane(pm_pane)
        await asyncio.sleep(0.10)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        orch_sends = _sends_to(sm, orch_pane)
        error_msgs = [s for s in orch_sends if "[WORKER_ERROR" in s]
        assert len(error_msgs) >= 1
        assert any("pane vanished" in s for s in error_msgs)


class TestScrollbackResilience:
    async def test_scrollback_truncation_does_not_skip_new_dispatch(self, scenario):
        """REQ-014 F-01 regression: when capture_pane_full returns a shorter
        string on the second poll (simulating scrollback truncation), the new
        dispatch must still be processed."""
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        pm_pane = scenario["pane_ids"][AgentRole.product_manager]
        orch_pane = scenario["orch_pane"]

        # First: a big historical pane with dispatch D1 at byte offset ~500
        history = "x" * 500
        d1 = '<<DISPATCH role="product_manager" text="first">><</DISPATCH>>'
        d2 = '<<DISPATCH role="product_manager" text="second">><</DISPATCH>>'

        sm.script_pane(orch_pane, [
            f"{history}\n{d1}",
            # Scrollback truncation — the historical prefix is gone; only d1
            # and d2 remain. With byte-offset bookkeeping (v1), the stored
            # offset would point past the end of this short string and d2
            # would never be parsed. Signature dedup handles it.
            f"{d1}\n{d2}",
        ])
        sm.set_pane_state(pm_pane, "done\n<<TASK_DONE>>")

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        await asyncio.sleep(0.25)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        pm_sends = _sends_to(sm, pm_pane)
        # The worker should have received BOTH distinct dispatches (first and second).
        assert any("first" in s for s in pm_sends), f"first dispatch dropped: {pm_sends}"
        assert any("second" in s for s in pm_sends), (
            f"second dispatch dropped by scrollback wraparound: {pm_sends}"
        )


# ---- REQ-016 F-04c/d: newline strip + diagnostic log ----------------------


class TestNewlineStripAndDiagnosticLog:
    async def test_multi_line_dispatch_text_is_flattened(self, scenario):
        """REQ-016 F-04c: the dispatch_loop must replace newlines in the
        dispatch text with spaces before calling send_keys, so tmux doesn't
        submit the first line and drop the rest."""
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        pm_pane = scenario["pane_ids"][AgentRole.product_manager]
        orch_pane = scenario["orch_pane"]

        # Orchestrator emits a dispatch with escaped quotes but a real newline
        # inside the text attribute (Claude sometimes produces this).
        sm.set_pane_state(
            orch_pane,
            (
                '<<DISPATCH role="product_manager" text="line one\n'
                'line two\n'
                'line three">>'
            ),
        )
        sm.set_pane_state(pm_pane, "done\n<<TASK_DONE>>")

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        pm_sends = _sends_to(sm, pm_pane)
        # All three lines must be present, concatenated with spaces.
        delivered = next((s for s in pm_sends if "line one" in s), None)
        assert delivered is not None, f"no dispatch reached PM: {pm_sends}"
        assert "line two" in delivered
        assert "line three" in delivered
        # No literal newline in the delivered text
        assert "\n" not in delivered

    async def test_parse_failure_with_dispatch_literal_logs_warning(self, scenario, caplog):
        """REQ-016 F-04d: when the orchestrator pane contains '<<DISPATCH'
        but the parser can't extract a valid dispatch, a warning line is
        emitted so the operator has a breadcrumb trail."""
        import logging
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        orch_pane = scenario["orch_pane"]

        # Malformed dispatch — missing closing quote
        sm.set_pane_state(
            orch_pane,
            '<<DISPATCH role="developer" text="unterminated',
        )

        with caplog.at_level(logging.WARNING, logger="agent_management.backend.supervisor"):
            task = asyncio.create_task(
                sup._dispatch_loop(scenario["group"].id, orch_agent)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        warning_lines = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING
            and "<<DISPATCH" in r.message
            and "parse failed" in r.message
        ]
        assert len(warning_lines) >= 1, (
            f"Expected a parse-failure warning; got: {[r.message for r in caplog.records]}"
        )

    async def test_parse_failure_warning_deduped(self, scenario, caplog):
        """REQ-016 F-04d: the same malformed pane content should log only
        once per unique tail, not once per poll."""
        import logging
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        orch_pane = scenario["orch_pane"]

        sm.set_pane_state(
            orch_pane,
            '<<DISPATCH role="developer" text="still-bad',
        )

        with caplog.at_level(logging.WARNING, logger="agent_management.backend.supervisor"):
            task = asyncio.create_task(
                sup._dispatch_loop(scenario["group"].id, orch_agent)
            )
            # Give the loop many iterations so it would log many times
            # without dedup.
            await asyncio.sleep(0.25)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        parse_warnings = [
            r for r in caplog.records
            if "parse failed" in r.message
        ]
        # Should log exactly once (dedup by tail), not once per poll tick.
        assert len(parse_warnings) == 1, (
            f"Expected 1 deduped warning, got {len(parse_warnings)}"
        )


class TestTesterFailureLoop:
    async def test_tests_failed_marker_recognised_and_counter_increments(self, scenario):
        """Tester emits <<TESTS_FAILED>> then <<TASK_DONE>>; the result is
        delivered with tests_failed=True metadata and the retry counter
        increments."""
        sup = scenario["supervisor"]
        sm = scenario["sm"]
        orch_agent = scenario["agents"][AgentRole.orchestrator]
        tester_pane = scenario["pane_ids"][AgentRole.tester]
        orch_pane = scenario["orch_pane"]

        sm.set_pane_state(
            orch_pane,
            '<<DISPATCH role="tester" text="run tests">><</DISPATCH>>',
        )
        sm.set_pane_state(
            tester_pane,
            "test 1 passed\ntest 2 FAILED\n<<TESTS_FAILED>>\n<<TASK_DONE>>",
        )

        task = asyncio.create_task(
            sup._dispatch_loop(scenario["group"].id, orch_agent)
        )
        await asyncio.sleep(0.10)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        orch_sends = _sends_to(sm, orch_pane)
        assert any(
            "[WORKER_RESULT" in s and "tester" in s for s in orch_sends
        )
        assert sup._dev_tester_retries == 1
