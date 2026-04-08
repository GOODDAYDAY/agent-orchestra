"""REQ-014 F-08 — session manager unit tests.

These tests exercise the parts of SessionManager that do not require a live
tmux or subprocess:
  - _sanitize_payload (static, pure)
  - _render_orchestrator_prompt (async, uses the Repository but no tmux)

Tests that require actual tmux / Claude CLI are deliberately NOT here —
they belong in a manual smoke test.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from agent_management.backend.models import Agent, AgentRole, Group, Session, AgentStatus
from agent_management.backend.repository import Repository
from agent_management.backend.session_manager import SessionManager, COMPLETION_MARKER


# ---- _sanitize_payload ------------------------------------------------------

class TestSanitizePayload:
    def test_passthrough_plain_text(self):
        out = SessionManager._sanitize_payload("hello world")
        assert out == "hello world"

    def test_preserves_newlines_and_tabs(self):
        out = SessionManager._sanitize_payload("a\nb\tc\n")
        assert out == "a\nb\tc\n"

    def test_strips_nul_byte(self):
        out = SessionManager._sanitize_payload("before\x00after")
        assert "\x00" not in out
        assert "before" in out and "after" in out

    def test_strips_bell(self):
        out = SessionManager._sanitize_payload("warn\x07ing")
        assert "\x07" not in out

    def test_strips_escape_sequences(self):
        out = SessionManager._sanitize_payload("\x1b[31mred\x1b[0m")
        assert "\x1b" not in out

    def test_strips_delete(self):
        out = SessionManager._sanitize_payload("abc\x7fdef")
        assert "\x7f" not in out

    def test_caps_at_50kb(self):
        big = "a" * 60_000
        out = SessionManager._sanitize_payload(big)
        assert len(out) < 60_000
        assert "[...truncated]" in out

    def test_empty_string(self):
        assert SessionManager._sanitize_payload("") == ""

    def test_unicode_preserved(self):
        out = SessionManager._sanitize_payload("你好 world")
        assert "你好 world" in out


# ---- _render_orchestrator_prompt --------------------------------------------

@pytest_asyncio.fixture
async def repo(tmp_path: Path):
    r = Repository(db_path=tmp_path / "test.db")
    await r.init()
    yield r
    await r.close()


@pytest_asyncio.fixture
async def group_with_workers(repo: Repository):
    """A group with the full 5-worker roster plus an orchestrator, each with a
    live session record so the roster renders with real pane IDs."""
    group = Group(name="sprint", workflow_id="standard")
    await repo.save_group(group)

    agents = {}
    roles = [
        AgentRole.orchestrator,
        AgentRole.product_manager,
        AgentRole.tech_director,
        AgentRole.developer,
        AgentRole.tester,
        AgentRole.user,
    ]
    for idx, role in enumerate(roles):
        agent = Agent(name=f"sprint - {role.value}", role=role, working_dir="/tmp")
        await repo.save_agent(agent)
        await repo.add_group_member(group.id, agent.id)
        # Create a fake session so _render_orchestrator_prompt can look up pane IDs
        sess = Session(
            agent_id=agent.id,
            group_id=group.id,
            tmux_pane_id=f"%{idx}",
            status=AgentStatus.active,
        )
        await repo.save_session(sess)
        agents[role] = agent
    return group, agents


class TestRenderOrchestratorPrompt:
    async def test_substitutes_all_placeholders(self, repo: Repository, group_with_workers):
        group, agents = group_with_workers
        sm = SessionManager(repo)
        rendered = await sm._render_orchestrator_prompt(
            agents[AgentRole.orchestrator], group.id
        )
        assert "{{WORKFLOW_DEFINITION}}" not in rendered
        assert "{{WORKER_ROSTER}}" not in rendered
        assert "{{COMPLETION_MARKER}}" not in rendered

    async def test_completion_marker_replaced_with_literal(
        self, repo: Repository, group_with_workers,
    ):
        group, agents = group_with_workers
        sm = SessionManager(repo)
        rendered = await sm._render_orchestrator_prompt(
            agents[AgentRole.orchestrator], group.id
        )
        assert COMPLETION_MARKER in rendered  # literal <<TASK_DONE>>

    async def test_roster_includes_all_worker_names(
        self, repo: Repository, group_with_workers,
    ):
        group, agents = group_with_workers
        sm = SessionManager(repo)
        rendered = await sm._render_orchestrator_prompt(
            agents[AgentRole.orchestrator], group.id
        )
        assert "sprint - product_manager" in rendered
        assert "sprint - tech_director" in rendered
        assert "sprint - developer" in rendered
        assert "sprint - tester" in rendered
        assert "sprint - user" in rendered
        # Orchestrator should NOT appear in the roster (it's rendering its own prompt)
        assert "sprint - orchestrator" not in rendered

    async def test_roster_includes_pane_ids(self, repo: Repository, group_with_workers):
        group, agents = group_with_workers
        sm = SessionManager(repo)
        rendered = await sm._render_orchestrator_prompt(
            agents[AgentRole.orchestrator], group.id
        )
        # Workers were given pane IDs %1..%5 (orchestrator got %0)
        for pane_id in ("%1", "%2", "%3", "%4", "%5"):
            assert pane_id in rendered

    async def test_workflow_definition_has_ordered_steps(
        self, repo: Repository, group_with_workers,
    ):
        group, agents = group_with_workers
        sm = SessionManager(repo)
        rendered = await sm._render_orchestrator_prompt(
            agents[AgentRole.orchestrator], group.id
        )
        # Standard workflow step order: PM → TD → Dev → Tester → User
        pm_pos = rendered.find("1. product_manager")
        td_pos = rendered.find("2. tech_director")
        dev_pos = rendered.find("3. developer")
        tester_pos = rendered.find("4. tester")
        user_pos = rendered.find("5. user")
        assert pm_pos < td_pos < dev_pos < tester_pos < user_pos

    async def test_rejects_unknown_workflow_id(self, repo: Repository):
        group = Group(name="g", workflow_id="nonexistent")
        await repo.save_group(group)
        orch = Agent(
            name="o", role=AgentRole.orchestrator, working_dir="/tmp"
        )
        await repo.save_agent(orch)
        await repo.add_group_member(group.id, orch.id)
        sm = SessionManager(repo)
        with pytest.raises(RuntimeError, match="unknown workflow"):
            await sm._render_orchestrator_prompt(orch, group.id)

    async def test_rejects_missing_required_role(self, repo: Repository):
        # Group that requires full standard roster but is missing the Tester
        group = Group(name="g", workflow_id="standard")
        await repo.save_group(group)
        for role in (
            AgentRole.orchestrator,
            AgentRole.product_manager,
            AgentRole.tech_director,
            AgentRole.developer,
            AgentRole.user,
        ):
            a = Agent(name=f"g-{role.value}", role=role, working_dir="/tmp")
            await repo.save_agent(a)
            await repo.add_group_member(group.id, a.id)
        orch = await repo.get_orchestrator_for_group(group.id)
        sm = SessionManager(repo)
        with pytest.raises(RuntimeError, match="requires roles"):
            await sm._render_orchestrator_prompt(orch, group.id)

    async def test_rejects_nonexistent_group(self, repo: Repository):
        orch = Agent(name="o", role=AgentRole.orchestrator, working_dir="/tmp")
        await repo.save_agent(orch)
        sm = SessionManager(repo)
        with pytest.raises(RuntimeError, match="not found"):
            await sm._render_orchestrator_prompt(orch, "nonexistent-group-id")

    async def test_prototype_workflow_rejects_standard_roster_mismatch(
        self, repo: Repository,
    ):
        # Prototype workflow requires developer + user; create a group that
        # only has orchestrator + pm — should fail at render time.
        group = Group(name="g", workflow_id="prototype")
        await repo.save_group(group)
        for role in (AgentRole.orchestrator, AgentRole.product_manager):
            a = Agent(name=f"g-{role.value}", role=role, working_dir="/tmp")
            await repo.save_agent(a)
            await repo.add_group_member(group.id, a.id)
        orch = await repo.get_orchestrator_for_group(group.id)
        sm = SessionManager(repo)
        with pytest.raises(RuntimeError, match="requires roles"):
            await sm._render_orchestrator_prompt(orch, group.id)

    async def test_send_raw_keys_invokes_tmux_correctly(
        self, repo: Repository, monkeypatch,
    ):
        """REQ-015 F-07: send_raw_keys must call tmux send-keys with the
        provided argv tokens, no trailing Enter, no payload sanitisation."""
        sm = SessionManager(repo)
        recorded_args: list[tuple] = []

        async def fake_tmux(*args):
            recorded_args.append(args)
            return 0, "", ""

        monkeypatch.setattr(sm, "_tmux", fake_tmux)

        rc, _, _ = await sm.send_raw_keys("%42", "C-c")
        assert rc == 0
        assert recorded_args == [("send-keys", "-t", "%42", "C-c")]

    async def test_send_raw_keys_with_multiple_args(
        self, repo: Repository, monkeypatch,
    ):
        sm = SessionManager(repo)
        recorded_args: list[tuple] = []

        async def fake_tmux(*args):
            recorded_args.append(args)
            return 0, "", ""

        monkeypatch.setattr(sm, "_tmux", fake_tmux)
        await sm.send_raw_keys("%1", "y", "Enter")
        assert recorded_args == [("send-keys", "-t", "%1", "y", "Enter")]

    async def test_send_raw_keys_empty_is_noop(
        self, repo: Repository, monkeypatch,
    ):
        sm = SessionManager(repo)
        called = []

        async def fake_tmux(*args):
            called.append(args)
            return 0, "", ""

        monkeypatch.setattr(sm, "_tmux", fake_tmux)
        rc, _, _ = await sm.send_raw_keys("%1")
        assert rc == 0
        assert called == []

    async def test_send_raw_keys_propagates_failure(
        self, repo: Repository, monkeypatch,
    ):
        sm = SessionManager(repo)

        async def fake_tmux(*args):
            return 1, "", "no such pane"

        monkeypatch.setattr(sm, "_tmux", fake_tmux)
        rc, _, err = await sm.send_raw_keys("%99", "Up")
        assert rc == 1
        assert "no such pane" in err

    async def test_capture_pane_full_ansi_flag_added(
        self, repo: Repository, monkeypatch,
    ):
        """REQ-015 F-08: ansi=True must add the -e flag to the tmux call."""
        sm = SessionManager(repo)
        recorded_args: list[tuple] = []

        async def fake_tmux(*args):
            recorded_args.append(args)
            return 0, "captured output", ""

        monkeypatch.setattr(sm, "_tmux", fake_tmux)
        await sm.capture_pane_full("%1", ansi=True)
        # The args should contain "-e" before "-S"
        args = recorded_args[0]
        assert "capture-pane" in args
        assert "-e" in args
        assert "-S" in args

    async def test_capture_pane_full_default_no_ansi_flag(
        self, repo: Repository, monkeypatch,
    ):
        sm = SessionManager(repo)
        recorded_args: list[tuple] = []

        async def fake_tmux(*args):
            recorded_args.append(args)
            return 0, "", ""

        monkeypatch.setattr(sm, "_tmux", fake_tmux)
        await sm.capture_pane_full("%1")
        args = recorded_args[0]
        assert "-e" not in args

    async def test_capture_pane_full_history_lines_param(
        self, repo: Repository, monkeypatch,
    ):
        sm = SessionManager(repo)
        recorded_args: list[tuple] = []

        async def fake_tmux(*args):
            recorded_args.append(args)
            return 0, "", ""

        monkeypatch.setattr(sm, "_tmux", fake_tmux)
        await sm.capture_pane_full("%1", history_lines=5000)
        args = recorded_args[0]
        assert "-5000" in args

    async def test_prototype_workflow_renders_with_minimal_roster(
        self, repo: Repository,
    ):
        # Prototype workflow requires developer + user — minimal roster should
        # render fine.
        group = Group(name="g", workflow_id="prototype")
        await repo.save_group(group)
        for idx, role in enumerate((
            AgentRole.orchestrator,
            AgentRole.developer,
            AgentRole.user,
        )):
            a = Agent(name=f"g-{role.value}", role=role, working_dir="/tmp")
            await repo.save_agent(a)
            await repo.add_group_member(group.id, a.id)
            sess = Session(
                agent_id=a.id, group_id=group.id,
                tmux_pane_id=f"%{idx}", status=AgentStatus.active,
            )
            await repo.save_session(sess)
        orch = await repo.get_orchestrator_for_group(group.id)
        sm = SessionManager(repo)
        rendered = await sm._render_orchestrator_prompt(orch, group.id)
        assert "1. developer" in rendered
        assert "2. user" in rendered
