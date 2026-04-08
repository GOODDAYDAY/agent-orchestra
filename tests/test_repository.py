"""REQ-012 v2 — Unit tests for the Repository class.

Uses an in-memory SQLite database via the tmp_path fixture. Verifies the v2
schema (no events / pending_events tables, no topic_subscriptions / auto_respond
columns, new groups.workflow_id column, AgentRole.orchestrator support, and
SchemaIncompatibleError detection).
"""
import pytest
import pytest_asyncio
from pathlib import Path

from agent_management.backend.models import (
    Agent,
    AgentRole,
    AgentStatus,
    Group,
    Session,
)
from agent_management.backend.repository import Repository, SchemaIncompatibleError
from agent_management.shared.config import SCHEMA_VERSION


@pytest_asyncio.fixture
async def repo(tmp_path: Path):
    r = Repository(db_path=tmp_path / "test.db")
    await r.init()
    yield r
    await r.close()


class TestSchemaVersion:
    async def test_fresh_db_writes_current_schema_version(self, tmp_path: Path):
        r = Repository(db_path=tmp_path / "fresh.db")
        await r.init()
        try:
            actual = await r._read_schema_version()
            assert actual == SCHEMA_VERSION
        finally:
            await r.close()

    async def test_mismatched_db_raises(self, tmp_path: Path):
        # Create a db, downgrade its schema_version, then re-open
        db_path = tmp_path / "stale.db"
        r1 = Repository(db_path=db_path)
        await r1.init()
        await r1._set_schema_version(SCHEMA_VERSION - 1)
        await r1._conn.commit()
        await r1.close()

        r2 = Repository(db_path=db_path)
        with pytest.raises(SchemaIncompatibleError) as exc:
            await r2.init()
        assert exc.value.expected == SCHEMA_VERSION
        assert exc.value.actual == SCHEMA_VERSION - 1


class TestAgentCRUD:
    async def test_save_and_get_agent(self, repo: Repository):
        agent = Agent(name="PM", role=AgentRole.product_manager, working_dir="/tmp")
        await repo.save_agent(agent)
        fetched = await repo.get_agent(agent.id)
        assert fetched is not None
        assert fetched.name == "PM"
        assert fetched.role == AgentRole.product_manager

    async def test_save_orchestrator_agent(self, repo: Repository):
        agent = Agent(name="Orch", role=AgentRole.orchestrator, working_dir="/tmp")
        await repo.save_agent(agent)
        fetched = await repo.get_agent(agent.id)
        assert fetched.role == AgentRole.orchestrator

    async def test_update_agent_status(self, repo: Repository):
        agent = Agent(name="Dev", role=AgentRole.developer, working_dir="/tmp")
        await repo.save_agent(agent)
        await repo.update_agent_status(agent.id, AgentStatus.active)
        fetched = await repo.get_agent(agent.id)
        assert fetched.status == AgentStatus.active

    async def test_delete_agent(self, repo: Repository):
        agent = Agent(name="X", role=AgentRole.developer, working_dir="/tmp")
        await repo.save_agent(agent)
        await repo.delete_agent(agent.id)
        assert await repo.get_agent(agent.id) is None


class TestGroupCRUD:
    async def test_save_group_with_workflow(self, repo: Repository):
        group = Group(name="g1", workflow_id="prototype")
        await repo.save_group(group)
        fetched = await repo.get_group(group.id)
        assert fetched is not None
        assert fetched.workflow_id == "prototype"

    async def test_default_workflow_is_standard(self, repo: Repository):
        group = Group(name="g2")
        await repo.save_group(group)
        fetched = await repo.get_group(group.id)
        assert fetched.workflow_id == "standard"

    async def test_set_workflow_id(self, repo: Repository):
        group = Group(name="g3")
        await repo.save_group(group)
        await repo.set_workflow_id(group.id, "research")
        fetched = await repo.get_group(group.id)
        assert fetched.workflow_id == "research"


class TestGroupMembership:
    async def test_get_orchestrator_for_group(self, repo: Repository):
        group = Group(name="g")
        await repo.save_group(group)
        pm = Agent(name="PM", role=AgentRole.product_manager, working_dir="/tmp")
        orch = Agent(name="Orch", role=AgentRole.orchestrator, working_dir="/tmp")
        await repo.save_agent(pm)
        await repo.save_agent(orch)
        await repo.add_group_member(group.id, pm.id)
        await repo.add_group_member(group.id, orch.id)

        found = await repo.get_orchestrator_for_group(group.id)
        assert found is not None
        assert found.id == orch.id

    async def test_get_workers_for_group_excludes_orchestrator(self, repo: Repository):
        group = Group(name="g")
        await repo.save_group(group)
        pm = Agent(name="PM", role=AgentRole.product_manager, working_dir="/tmp")
        orch = Agent(name="Orch", role=AgentRole.orchestrator, working_dir="/tmp")
        await repo.save_agent(pm)
        await repo.save_agent(orch)
        await repo.add_group_member(group.id, pm.id)
        await repo.add_group_member(group.id, orch.id)

        workers = await repo.get_workers_for_group(group.id)
        assert len(workers) == 1
        assert workers[0].id == pm.id


class TestSessionCRUD:
    async def test_save_and_get_session(self, repo: Repository):
        # Create the parent rows so the FK constraint is satisfied
        agent = Agent(name="A", role=AgentRole.developer, working_dir="/tmp")
        await repo.save_agent(agent)
        group = Group(name="G")
        await repo.save_group(group)

        sess = Session(agent_id=agent.id, group_id=group.id, tmux_pane_id="%42")
        await repo.save_session(sess)
        fetched = await repo.get_session(agent.id, group.id)
        assert fetched is not None
        assert fetched.tmux_pane_id == "%42"


class TestRoleTemplates:
    async def test_orchestrator_template_present(self, repo: Repository):
        prompt = await repo.get_orchestrator_template()
        assert "{{WORKFLOW_DEFINITION}}" in prompt
        assert "{{WORKER_ROSTER}}" in prompt
        assert "<<DISPATCH" in prompt
        # Completion marker is referenced via the {{COMPLETION_MARKER}} placeholder
        # which session_manager substitutes at render time.
        assert "{{COMPLETION_MARKER}}" in prompt

    async def test_all_roles_have_templates(self, repo: Repository):
        templates = await repo.get_role_templates()
        roles = {t.role for t in templates}
        # All canonical roles should have templates
        assert AgentRole.orchestrator in roles
        assert AgentRole.product_manager in roles
        assert AgentRole.developer in roles
        assert AgentRole.tester in roles
        assert AgentRole.user in roles

    async def test_worker_templates_mention_task_done(self, repo: Repository):
        templates = await repo.get_role_templates()
        for tpl in templates:
            # Custom has empty content by design; orchestrator uses the
            # {{COMPLETION_MARKER}} placeholder instead of the literal marker.
            if tpl.role in (AgentRole.custom, AgentRole.orchestrator):
                continue
            assert "<<TASK_DONE>>" in tpl.system_prompt, (
                f"{tpl.role.value} template missing <<TASK_DONE>> instruction"
            )

    async def test_worker_templates_no_mcp_references(self, repo: Repository):
        """REQ-012 v2 F-05: ensure no MCP / event-bus relics remain."""
        templates = await repo.get_role_templates()
        forbidden = ["publish_event", "get_pending_events", "PENDING EVENTS",
                     "MCP_SERVER_URL", "AGENT_ID", "GROUP_ID"]
        for tpl in templates:
            for bad in forbidden:
                assert bad not in tpl.system_prompt, (
                    f"{tpl.role.value} template still references '{bad}'"
                )


# ---- REQ-014 F-06: additional repository coverage ---------------------------


class TestAgentPauseToggle:
    async def test_set_agent_paused_roundtrip(self, repo: Repository):
        agent = Agent(name="A", role=AgentRole.developer, working_dir="/tmp")
        await repo.save_agent(agent)
        await repo.set_agent_paused(agent.id, True)
        fetched = await repo.get_agent(agent.id)
        assert fetched is not None
        assert fetched.paused is True
        await repo.set_agent_paused(agent.id, False)
        fetched = await repo.get_agent(agent.id)
        assert fetched.paused is False

    async def test_status_transitions_roundtrip(self, repo: Repository):
        agent = Agent(name="A", role=AgentRole.developer, working_dir="/tmp")
        await repo.save_agent(agent)
        sequence = [
            AgentStatus.starting,
            AgentStatus.active,
            AgentStatus.paused,
            AgentStatus.active,
            AgentStatus.stopping,
            AgentStatus.stopped,
        ]
        for st in sequence:
            await repo.update_agent_status(agent.id, st)
            fetched = await repo.get_agent(agent.id)
            assert fetched.status == st


class TestDeleteCascade:
    async def test_delete_agent_cascades_to_sessions(self, repo: Repository):
        agent = Agent(name="A", role=AgentRole.developer, working_dir="/tmp")
        await repo.save_agent(agent)
        group = Group(name="G")
        await repo.save_group(group)
        sess = Session(agent_id=agent.id, group_id=group.id, tmux_pane_id="%1")
        await repo.save_session(sess)
        assert await repo.get_session(agent.id, group.id) is not None
        await repo.delete_agent(agent.id)
        assert await repo.get_agent(agent.id) is None
        assert await repo.get_session(agent.id, group.id) is None

    async def test_delete_agent_removes_group_membership(self, repo: Repository):
        agent = Agent(name="A", role=AgentRole.developer, working_dir="/tmp")
        await repo.save_agent(agent)
        group = Group(name="G")
        await repo.save_group(group)
        await repo.add_group_member(group.id, agent.id)
        assert agent.id in await repo.get_group_member_ids(group.id)
        await repo.delete_agent(agent.id)
        assert agent.id not in await repo.get_group_member_ids(group.id)


class TestClearAllRuntimeState:
    async def test_clear_resets_agent_statuses(self, repo: Repository):
        agent = Agent(name="A", role=AgentRole.developer, working_dir="/tmp")
        await repo.save_agent(agent)
        await repo.update_agent_status(agent.id, AgentStatus.active)
        await repo.clear_all_runtime_state()
        fetched = await repo.get_agent(agent.id)
        assert fetched.status == AgentStatus.not_started

    async def test_clear_deletes_sessions(self, repo: Repository):
        agent = Agent(name="A", role=AgentRole.developer, working_dir="/tmp")
        await repo.save_agent(agent)
        group = Group(name="G")
        await repo.save_group(group)
        sess = Session(agent_id=agent.id, group_id=group.id, tmux_pane_id="%1")
        await repo.save_session(sess)
        await repo.clear_all_runtime_state()
        assert await repo.get_session(agent.id, group.id) is None


class TestOrchestratorTemplateIntegrity:
    async def test_orchestrator_template_has_all_placeholders(self, repo: Repository):
        prompt = await repo.get_orchestrator_template()
        assert "{{WORKFLOW_DEFINITION}}" in prompt
        assert "{{WORKER_ROSTER}}" in prompt
        assert "{{COMPLETION_MARKER}}" in prompt

    async def test_orchestrator_template_documents_control_markers(self, repo: Repository):
        prompt = await repo.get_orchestrator_template()
        assert "<<DISPATCH" in prompt
        assert "<<WORKFLOW_COMPLETE>>" in prompt
        assert "<<WORKFLOW_ABORT" in prompt

    async def test_orchestrator_template_mentions_worker_result_format(self, repo: Repository):
        prompt = await repo.get_orchestrator_template()
        assert "[WORKER_RESULT" in prompt


class TestWorkflowIdPersistence:
    async def test_save_then_roundtrip_workflow_id(self, repo: Repository):
        g = Group(name="g", workflow_id="research")
        await repo.save_group(g)
        got = await repo.get_group(g.id)
        assert got.workflow_id == "research"

    async def test_upsert_group_updates_workflow_id(self, repo: Repository):
        g = Group(name="g", workflow_id="standard")
        await repo.save_group(g)
        g.workflow_id = "prototype"
        await repo.save_group(g)
        got = await repo.get_group(g.id)
        assert got.workflow_id == "prototype"

    async def test_get_groups_returns_workflow_id(self, repo: Repository):
        await repo.save_group(Group(name="g1", workflow_id="standard"))
        await repo.save_group(Group(name="g2", workflow_id="prototype"))
        groups = await repo.get_groups()
        ids = {g.name: g.workflow_id for g in groups}
        assert ids == {"g1": "standard", "g2": "prototype"}


class TestSessionRoundtrip:
    async def test_session_fields_preserved(self, repo: Repository):
        agent = Agent(name="A", role=AgentRole.developer, working_dir="/tmp")
        await repo.save_agent(agent)
        group = Group(name="G")
        await repo.save_group(group)
        sess = Session(
            agent_id=agent.id,
            group_id=group.id,
            tmux_session_name="agent-mgmt-abc",
            tmux_pane_id="%99",
            status=AgentStatus.active,
            started_at="2026-04-08T10:00:00Z",
        )
        await repo.save_session(sess)
        got = await repo.get_session(agent.id, group.id)
        assert got is not None
        assert got.tmux_session_name == "agent-mgmt-abc"
        assert got.tmux_pane_id == "%99"
        assert got.status == AgentStatus.active
        assert got.started_at == "2026-04-08T10:00:00Z"

    async def test_get_sessions_for_group_excludes_stopped(self, repo: Repository):
        agent = Agent(name="A", role=AgentRole.developer, working_dir="/tmp")
        await repo.save_agent(agent)
        group = Group(name="G")
        await repo.save_group(group)
        sess = Session(agent_id=agent.id, group_id=group.id,
                       tmux_pane_id="%1", status=AgentStatus.active)
        await repo.save_session(sess)
        assert len(await repo.get_sessions_for_group(group.id)) == 1
        await repo.update_session_status(sess.id, AgentStatus.stopped)
        assert len(await repo.get_sessions_for_group(group.id)) == 0


class TestRoleTemplateUserEdits:
    async def test_save_role_template_override(self, repo: Repository):
        from agent_management.backend.models import RoleTemplate
        templates_before = await repo.get_role_templates()
        dev = next(t for t in templates_before if t.role == AgentRole.developer)
        modified = RoleTemplate(
            role=AgentRole.developer,
            display_name=dev.display_name,
            system_prompt="custom prompt",
        )
        await repo.save_role_template(modified)
        templates_after = await repo.get_role_templates()
        dev_after = next(t for t in templates_after if t.role == AgentRole.developer)
        assert dev_after.system_prompt == "custom prompt"

    async def test_reset_role_templates_restores_defaults(self, repo: Repository):
        from agent_management.backend.models import RoleTemplate
        modified = RoleTemplate(
            role=AgentRole.developer,
            display_name="Developer",
            system_prompt="custom prompt",
        )
        await repo.save_role_template(modified)
        await repo.reset_role_templates()
        templates = await repo.get_role_templates()
        dev = next(t for t in templates if t.role == AgentRole.developer)
        assert dev.system_prompt != "custom prompt"
        assert "<<TASK_DONE>>" in dev.system_prompt


# ---- REQ-017: orchestrator autonomy restored; template version bumped ------


class TestReq017TemplateVersion:
    async def test_template_version_is_7(self, repo: Repository):
        assert Repository._TEMPLATE_VERSION == 7


class TestReq017OrchestratorTemplate:
    """REQ-017 F-04: orchestrator template emphasises autonomous
    decision-making and carries a SKILL_CATALOGUE placeholder instead of
    hardcoded per-step skill directives."""

    async def test_orchestrator_template_has_skill_catalogue_placeholder(
        self, repo: Repository,
    ):
        prompt = await repo.get_orchestrator_template()
        assert "{{SKILL_CATALOGUE}}" in prompt

    async def test_orchestrator_template_has_autonomous_language(
        self, repo: Repository,
    ):
        prompt = await repo.get_orchestrator_template()
        # Explicit autonomy language — this is the key architectural marker
        assert "自主编排者" in prompt

    async def test_orchestrator_template_describes_workflow_as_reference(
        self, repo: Repository,
    ):
        prompt = await repo.get_orchestrator_template()
        assert "参考模板" in prompt or "参考" in prompt

    async def test_orchestrator_template_still_has_existing_placeholders(
        self, repo: Repository,
    ):
        prompt = await repo.get_orchestrator_template()
        assert "{{WORKFLOW_DEFINITION}}" in prompt
        assert "{{WORKER_ROSTER}}" in prompt
        assert "{{COMPLETION_MARKER}}" in prompt

    async def test_orchestrator_template_does_not_hardcode_must_invoke_per_step(
        self, repo: Repository,
    ):
        # REQ-016 introduced "⚡ must invoke /req-X" wording tied to
        # per-step skill field; that wording is reverted in REQ-017.
        prompt = await repo.get_orchestrator_template()
        assert "⚡ must invoke" not in prompt


class TestReq017WorkerTemplatesHideOrchestrator:
    """REQ-017 F-05: worker templates must hide the orchestrator abstraction
    entirely — no mention of 'Orchestrator', no hardcoded /req-* skill names,
    no 'run chain of req-3 → req-7' instructions."""

    _WORKER_ROLES = [
        AgentRole.product_manager,
        AgentRole.tech_director,
        AgentRole.developer,
        AgentRole.tester,
    ]

    async def _workers(self, repo: Repository):
        templates = await repo.get_role_templates()
        return [t for t in templates if t.role in self._WORKER_ROLES]

    async def test_no_worker_mentions_orchestrator(self, repo: Repository):
        for tpl in await self._workers(repo):
            assert "Orchestrator" not in tpl.system_prompt, (
                f"{tpl.role.value} template leaks the orchestrator abstraction"
            )

    async def test_no_worker_hardcodes_specific_req_skill(self, repo: Repository):
        # Individual /req-* skill names should not appear in worker templates;
        # the orchestrator decides skills at runtime.
        specific_skills = [
            "/req-1-analyze", "/req-2-tech", "/req-3-code",
            "/req-4-security", "/req-5-cleanup", "/req-6-review",
            "/req-7-verify", "/req-8-done",
        ]
        for tpl in await self._workers(repo):
            for skill in specific_skills:
                assert skill not in tpl.system_prompt, (
                    f"{tpl.role.value} template hardcodes {skill}; "
                    "skill selection is the orchestrator's runtime decision"
                )

    async def test_developer_template_does_not_contain_chain_instruction(
        self, repo: Repository,
    ):
        templates = await repo.get_role_templates()
        dev = next(t for t in templates if t.role == AgentRole.developer)
        # The REQ-016 chain language must be gone
        assert "req-3-code → req-4-security" not in dev.system_prompt
        assert "/req-4-security" not in dev.system_prompt
        assert "按顺序依次调用" not in dev.system_prompt

    async def test_workers_still_have_task_done_rule(self, repo: Repository):
        for tpl in await self._workers(repo):
            assert "<<TASK_DONE>>" in tpl.system_prompt

    async def test_workers_mention_generic_skill_invocation_rule(
        self, repo: Repository,
    ):
        # They should still say "if the task mentions a /req-X-Y skill,
        # invoke it" — but genericly, with no specific name.
        for tpl in await self._workers(repo):
            assert "/req-X-Y" in tpl.system_prompt, (
                f"{tpl.role.value} template missing generic skill invocation rule"
            )

    async def test_no_worker_still_mentions_worker_result(self, repo: Repository):
        for tpl in await self._workers(repo):
            assert "[WORKER_RESULT" not in tpl.system_prompt, (
                f"{tpl.role.value} template leaks internal messaging format"
            )


class TestReq017OrchestratorStillHasSkillReferences:
    """The catalogue is rendered into the orchestrator's prompt, so specific
    skill names DO appear there — just not in worker templates."""

    async def test_orchestrator_template_mentions_dispatch_format(
        self, repo: Repository,
    ):
        prompt = await repo.get_orchestrator_template()
        assert "DISPATCH" in prompt

    async def test_orchestrator_template_documents_stall_abort_handling(
        self, repo: Repository,
    ):
        prompt = await repo.get_orchestrator_template()
        assert "WORKFLOW_COMPLETE" in prompt
        assert "WORKFLOW_ABORT" in prompt
