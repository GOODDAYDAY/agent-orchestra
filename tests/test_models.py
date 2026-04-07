"""REQ-012 v2 — Unit tests for domain models."""
import uuid

from agent_management.backend.models import (
    Agent,
    AgentRole,
    AgentStatus,
    Group,
    Session,
)


class TestAgent:
    def test_default_id_is_uuid(self):
        agent = Agent(name="PM", role=AgentRole.product_manager, working_dir="/tmp")
        assert uuid.UUID(agent.id)

    def test_default_status(self):
        agent = Agent(name="PM", role=AgentRole.product_manager, working_dir="/tmp")
        assert agent.status == AgentStatus.not_started

    def test_orchestrator_role_exists(self):
        # REQ-012 v2 F-07
        agent = Agent(name="Orch", role=AgentRole.orchestrator, working_dir="/tmp")
        assert agent.role == AgentRole.orchestrator
        assert agent.role.value == "orchestrator"

    def test_no_topic_attributes(self):
        agent = Agent(name="PM", role=AgentRole.product_manager, working_dir="/tmp")
        assert not hasattr(agent, "topic_subscriptions")
        assert not hasattr(agent, "topic_list")
        assert not hasattr(agent, "auto_respond")


class TestGroup:
    def test_default_workflow_is_standard(self):
        # REQ-012 v2 F-08
        group = Group(name="sprint-1")
        assert group.workflow_id == "standard"

    def test_explicit_workflow(self):
        group = Group(name="r&d", workflow_id="research")
        assert group.workflow_id == "research"


class TestSession:
    def test_session_has_pane_id_field(self):
        sess = Session(agent_id="a", group_id="g", tmux_pane_id="%42")
        assert sess.tmux_pane_id == "%42"


class TestAgentRoleEnum:
    def test_all_expected_roles(self):
        expected = {
            "product_manager", "tech_director", "developer",
            "tester", "user", "orchestrator", "custom",
        }
        assert {r.value for r in AgentRole} == expected


# ---- REQ-014 F-06: additional model-level tests -----------------------------

class TestSessionDefaults:
    def test_session_claude_id_auto_uuid(self):
        from agent_management.backend.models import Session
        s = Session(agent_id="a", group_id="g")
        assert uuid.UUID(s.claude_session_id)

    def test_session_ids_are_unique(self):
        from agent_management.backend.models import Session
        s1 = Session(agent_id="a", group_id="g")
        s2 = Session(agent_id="a", group_id="g")
        assert s1.id != s2.id
        assert s1.claude_session_id != s2.claude_session_id

    def test_session_timestamps_empty_by_default(self):
        from agent_management.backend.models import Session
        s = Session(agent_id="a", group_id="g")
        assert s.started_at == ""
        assert s.stopped_at == ""


class TestGroupExplicit:
    def test_group_explicit_workflow(self):
        from agent_management.backend.models import Group
        g = Group(name="x", workflow_id="prototype")
        assert g.workflow_id == "prototype"

    def test_group_ids_unique(self):
        from agent_management.backend.models import Group
        g1 = Group(name="x")
        g2 = Group(name="x")
        assert g1.id != g2.id


class TestAgentEquality:
    def test_two_agents_same_name_have_distinct_ids(self):
        a1 = Agent(name="PM", role=AgentRole.product_manager, working_dir="/tmp")
        a2 = Agent(name="PM", role=AgentRole.product_manager, working_dir="/tmp")
        assert a1.id != a2.id
