"""REQ-012 v2 — Unit tests for backend.workflows built-in templates."""
from __future__ import annotations

import pytest

from agent_management.backend.models import AgentRole
from agent_management.backend import workflows  # noqa: F401


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


# ---- REQ-014 F-06: expanded coverage ----------------------------------------

class TestWorkflowStability:
    def _roster(self):
        return [
            (AgentRole.product_manager, "PM", "%1"),
            (AgentRole.tech_director, "TD", "%2"),
            (AgentRole.developer, "Dev", "%3"),
            (AgentRole.tester, "Tester", "%4"),
            (AgentRole.user, "User", "%5"),
        ]

    def test_render_is_idempotent(self):
        r1 = workflows.render_for_orchestrator(workflows.STANDARD, self._roster())
        r2 = workflows.render_for_orchestrator(workflows.STANDARD, self._roster())
        assert r1 == r2

    def test_render_roster_is_idempotent(self):
        r1 = workflows.render_roster(self._roster())
        r2 = workflows.render_roster(self._roster())
        assert r1 == r2

    def test_step_is_frozen(self):
        step = workflows.STANDARD.steps[0]
        with pytest.raises((AttributeError, Exception)):
            step.role = AgentRole.developer  # type: ignore[misc]

    def test_workflow_is_frozen(self):
        with pytest.raises((AttributeError, Exception)):
            workflows.STANDARD.id = "tampered"  # type: ignore[misc]


class TestFailureLoopMetadata:
    def test_standard_failure_loop_target_is_valid_step_index(self):
        wf = workflows.STANDARD
        for idx, step in enumerate(wf.steps):
            if step.failure_loop_to is not None:
                assert 0 <= step.failure_loop_to < len(wf.steps)

    def test_standard_failure_loop_target_role_matches_marker_source(self):
        # The tester's failure loop should target a Developer step so that the
        # Dev↔Tester feedback loop makes sense.
        wf = workflows.STANDARD
        tester_step = next(s for s in wf.steps if s.role == AgentRole.tester)
        assert tester_step.failure_loop_to is not None
        target = wf.steps[tester_step.failure_loop_to]
        assert target.role == AgentRole.developer

    def test_prototype_has_no_failure_loops(self):
        for step in workflows.PROTOTYPE.steps:
            assert step.on_failure_marker is None
            assert step.failure_loop_to is None

    def test_research_has_no_failure_loops(self):
        for step in workflows.RESEARCH.steps:
            assert step.on_failure_marker is None
            assert step.failure_loop_to is None

    def test_max_retries_is_non_negative(self):
        for wf in workflows.BUILT_IN_WORKFLOWS.values():
            for step in wf.steps:
                assert step.max_retries >= 0


class TestRequiredRolesConsistency:
    def test_required_roles_equals_distinct_step_roles(self):
        # The set returned by required_roles must match the set of distinct
        # roles referenced across all steps.
        for wf in workflows.BUILT_IN_WORKFLOWS.values():
            expected = {step.role for step in wf.steps}
            assert workflows.required_roles(wf) == expected

    def test_all_built_ins_reachable_via_get_workflow(self):
        for wf_id in workflows.BUILT_IN_WORKFLOWS:
            assert workflows.get_workflow(wf_id) is workflows.BUILT_IN_WORKFLOWS[wf_id]


# ---- REQ-016 F-05: Step.skill field ----------------------------------------

class TestStepSkillField:
    def test_standard_pm_has_req1_analyze(self):
        pm_step = next(
            s for s in workflows.STANDARD.steps if s.role == AgentRole.product_manager
        )
        assert pm_step.skill == "req-1-analyze"

    def test_standard_tech_director_has_req2_tech(self):
        td_step = next(
            s for s in workflows.STANDARD.steps if s.role == AgentRole.tech_director
        )
        assert td_step.skill == "req-2-tech"

    def test_standard_developer_has_req3_code(self):
        dev_step = next(
            s for s in workflows.STANDARD.steps if s.role == AgentRole.developer
        )
        assert dev_step.skill == "req-3-code"

    def test_standard_tester_has_req7_verify(self):
        tester_step = next(
            s for s in workflows.STANDARD.steps if s.role == AgentRole.tester
        )
        assert tester_step.skill == "req-7-verify"

    def test_standard_user_has_no_skill(self):
        user_step = next(
            s for s in workflows.STANDARD.steps if s.role == AgentRole.user
        )
        assert user_step.skill is None

    def test_prototype_developer_has_req3_code(self):
        dev_step = next(
            s for s in workflows.PROTOTYPE.steps if s.role == AgentRole.developer
        )
        assert dev_step.skill == "req-3-code"

    def test_research_pm_and_tech_director_have_skills(self):
        for role, expected in [
            (AgentRole.product_manager, "req-1-analyze"),
            (AgentRole.tech_director, "req-2-tech"),
        ]:
            step = next(s for s in workflows.RESEARCH.steps if s.role == role)
            assert step.skill == expected


class TestRenderSkillAnnotation:
    def _roster(self):
        return [
            (AgentRole.product_manager, "PM", "%1"),
            (AgentRole.tech_director, "TD", "%2"),
            (AgentRole.developer, "Dev", "%3"),
            (AgentRole.tester, "Tester", "%4"),
            (AgentRole.user, "User", "%5"),
        ]

    def test_render_includes_req1_analyze_for_pm_step(self):
        rendered = workflows.render_for_orchestrator(workflows.STANDARD, self._roster())
        assert "/req-1-analyze" in rendered
        assert "must invoke" in rendered

    def test_render_includes_all_standard_skills(self):
        rendered = workflows.render_for_orchestrator(workflows.STANDARD, self._roster())
        for skill in ["req-1-analyze", "req-2-tech", "req-3-code", "req-7-verify"]:
            assert f"/{skill}" in rendered, f"{skill} missing from render"

    def test_render_user_step_marked_no_skill(self):
        rendered = workflows.render_for_orchestrator(workflows.STANDARD, self._roster())
        assert "no skill — human review" in rendered

    def test_prototype_render(self):
        rendered = workflows.render_for_orchestrator(workflows.PROTOTYPE, self._roster())
        assert "/req-3-code" in rendered
        # User step → no skill note
        assert "no skill — human review" in rendered
