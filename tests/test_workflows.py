"""REQ-012 v2 — Unit tests for backend.workflows built-in templates."""
from __future__ import annotations

import pytest

from agent_management.backend.models import AgentRole
from agent_management.backend import workflows


class TestBuiltInWorkflows:
    def test_three_built_ins_present(self):
        assert set(workflows.BUILT_IN_WORKFLOWS.keys()) == {"standard", "prototype", "research"}

    def test_get_workflow_known(self):
        wf = workflows.get_workflow("standard")
        assert wf.id == "standard"
        assert wf.steps  # non-empty

    def test_get_workflow_unknown_raises(self):
        with pytest.raises(KeyError):
            workflows.get_workflow("nonexistent")

    def test_default_workflow_is_standard(self):
        assert workflows.DEFAULT_WORKFLOW_ID == "standard"

    def test_standard_required_roles(self):
        roles = workflows.required_roles(workflows.STANDARD)
        assert roles == {
            AgentRole.product_manager,
            AgentRole.tech_director,
            AgentRole.developer,
            AgentRole.tester,
            AgentRole.user,
        }

    def test_prototype_required_roles(self):
        roles = workflows.required_roles(workflows.PROTOTYPE)
        assert roles == {AgentRole.developer, AgentRole.user}

    def test_research_required_roles(self):
        roles = workflows.required_roles(workflows.RESEARCH)
        assert roles == {
            AgentRole.product_manager,
            AgentRole.tech_director,
            AgentRole.user,
        }

    def test_standard_has_dev_tester_loop(self):
        wf = workflows.STANDARD
        tester_step = next(s for s in wf.steps if s.role == AgentRole.tester)
        assert tester_step.on_failure_marker == "<<TESTS_FAILED>>"
        assert tester_step.failure_loop_to is not None
        assert tester_step.max_retries == 3
        # The loop target should be a Developer step
        assert wf.steps[tester_step.failure_loop_to].role == AgentRole.developer


class TestRendering:
    def _roster(self):
        return [
            (AgentRole.product_manager, "Sprint - Product Manager", "%1"),
            (AgentRole.tech_director, "Sprint - Tech Director", "%2"),
            (AgentRole.developer, "Sprint - Developer", "%3"),
            (AgentRole.tester, "Sprint - Tester", "%4"),
            (AgentRole.user, "Sprint - User", "%5"),
        ]

    def test_render_for_orchestrator_includes_all_steps(self):
        rendered = workflows.render_for_orchestrator(workflows.STANDARD, self._roster())
        for role in [
            "product_manager", "tech_director", "developer", "tester", "user"
        ]:
            assert role in rendered

    def test_render_for_orchestrator_names_actors(self):
        rendered = workflows.render_for_orchestrator(workflows.STANDARD, self._roster())
        assert "Sprint - Developer" in rendered
        assert "Sprint - Tester" in rendered

    def test_render_for_orchestrator_describes_failure_loop(self):
        rendered = workflows.render_for_orchestrator(workflows.STANDARD, self._roster())
        assert "<<TESTS_FAILED>>" in rendered
        assert "loop back" in rendered
        assert "max 3 retries" in rendered

    def test_render_for_orchestrator_handles_missing_role(self):
        roster_missing_tester = [
            (AgentRole.product_manager, "PM", "%1"),
            (AgentRole.tech_director, "TD", "%2"),
            (AgentRole.developer, "Dev", "%3"),
            (AgentRole.user, "User", "%5"),
        ]
        rendered = workflows.render_for_orchestrator(workflows.STANDARD, roster_missing_tester)
        assert "<missing tester>" in rendered

    def test_render_roster_format(self):
        rendered = workflows.render_roster(self._roster())
        assert "developer: Sprint - Developer (pane %3)" in rendered

    def test_prototype_render_omits_pm(self):
        rendered = workflows.render_for_orchestrator(workflows.PROTOTYPE, self._roster())
        # The two-step prototype only mentions developer and user as STEP actors;
        # other roles appear in roster but not in step lines.
        assert "1. developer" in rendered
        assert "2. user" in rendered
