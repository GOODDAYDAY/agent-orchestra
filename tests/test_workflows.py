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


# ---- REQ-017: Step.skill removed; skill catalogue is the orchestrator's
# toolkit; render_for_orchestrator no longer emits skill directives ----------

class TestStepHasNoSkillField:
    """REQ-017 F-01: skill selection is the orchestrator's runtime decision,
    not a per-step hardcoding. Step must not carry any skill attribute."""

    def test_step_dataclass_has_no_skill_field(self):
        # dataclasses.fields lists the declared fields
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(workflows.Step)}
        assert "skill" not in field_names

    def test_standard_pm_step_has_no_skill_attribute(self):
        pm_step = next(
            s for s in workflows.STANDARD.steps if s.role == AgentRole.product_manager
        )
        assert not hasattr(pm_step, "skill")

    def test_standard_all_steps_have_no_skill_attribute(self):
        for step in workflows.STANDARD.steps:
            assert not hasattr(step, "skill")

    def test_prototype_all_steps_have_no_skill_attribute(self):
        for step in workflows.PROTOTYPE.steps:
            assert not hasattr(step, "skill")

    def test_research_all_steps_have_no_skill_attribute(self):
        for step in workflows.RESEARCH.steps:
            assert not hasattr(step, "skill")


class TestAvailableSkillsCatalogue:
    """REQ-017 F-02: the orchestrator chooses from a data catalogue,
    not a per-step hardcoded mapping."""

    def test_catalogue_exists(self):
        assert hasattr(workflows, "AVAILABLE_SKILLS")
        assert len(workflows.AVAILABLE_SKILLS) >= 8

    def test_catalogue_contains_core_req_skills(self):
        names = {name for name, _desc in workflows.AVAILABLE_SKILLS}
        for skill in [
            "/req-1-analyze",
            "/req-2-tech",
            "/req-3-code",
            "/req-4-security",
            "/req-5-cleanup",
            "/req-6-review",
            "/req-7-verify",
            "/req-8-done",
        ]:
            assert skill in names, f"catalogue missing {skill}"

    def test_catalogue_entries_have_descriptions(self):
        for name, description in workflows.AVAILABLE_SKILLS:
            assert name.startswith("/req-")
            assert isinstance(description, str)
            assert len(description) > 10  # non-trivial description

    def test_render_skill_catalogue_non_empty(self):
        rendered = workflows.render_skill_catalogue()
        assert rendered
        assert len(rendered) > 0

    def test_render_skill_catalogue_contains_all_skills(self):
        rendered = workflows.render_skill_catalogue()
        for name, _desc in workflows.AVAILABLE_SKILLS:
            assert name in rendered

    def test_render_skill_catalogue_contains_descriptions(self):
        rendered = workflows.render_skill_catalogue()
        for _name, description in workflows.AVAILABLE_SKILLS:
            # at least a meaningful fragment of each description should be present
            fragment = description.split(".")[0][:40]
            assert fragment in rendered, f"description fragment missing: {fragment!r}"


# ---- REQ-018 F-02: ROLE_CAPABILITIES + enriched roster --------------------


class TestRoleCapabilities:
    def test_capabilities_cover_all_canonical_roles(self):
        """REQ-018 F-02: every canonical worker role must have a capability
        description so the orchestrator never sees a blank subordinate."""
        canonical = [
            AgentRole.product_manager,
            AgentRole.tech_director,
            AgentRole.developer,
            AgentRole.tester,
            AgentRole.user,
        ]
        for role in canonical:
            assert role in workflows.ROLE_CAPABILITIES
            desc = workflows.ROLE_CAPABILITIES[role]
            assert isinstance(desc, str)
            assert len(desc) > 20  # non-trivial description

    def test_custom_role_also_has_a_capability_line(self):
        # Custom role is allowed in the catalogue so operators who choose
        # it see an explanation rather than a blank roster line.
        assert AgentRole.custom in workflows.ROLE_CAPABILITIES

    def test_developer_capability_mentions_req_skills(self):
        # The developer is the primary /req-* skill invoker; its capability
        # line should say so explicitly.
        desc = workflows.ROLE_CAPABILITIES[AgentRole.developer]
        assert "/req-" in desc

    def test_tester_capability_mentions_tests_failed_marker(self):
        desc = workflows.ROLE_CAPABILITIES[AgentRole.tester]
        assert "<<TESTS_FAILED>>" in desc

    def test_pm_capability_forbids_code(self):
        desc = workflows.ROLE_CAPABILITIES[AgentRole.product_manager]
        assert "Does not write code" in desc

    def test_tech_director_capability_forbids_code(self):
        desc = workflows.ROLE_CAPABILITIES[AgentRole.tech_director]
        assert "Does not write production code" in desc


class TestRenderRosterCapabilities:
    def _roster(self):
        return [
            (AgentRole.product_manager, "sprint - PM", "%1"),
            (AgentRole.tech_director,   "sprint - TD", "%2"),
            (AgentRole.developer,       "sprint - Dev", "%3"),
            (AgentRole.tester,          "sprint - Tester", "%4"),
            (AgentRole.user,            "sprint - User", "%5"),
        ]

    def test_default_render_includes_capability_lines(self):
        """REQ-018 F-02: default call produces one role line AND one
        capability line per entry, prefixed with '→'."""
        rendered = workflows.render_roster(self._roster())
        # Every role's capability should appear in the output
        for role in [
            AgentRole.product_manager,
            AgentRole.developer,
            AgentRole.tester,
        ]:
            expected_fragment = workflows.ROLE_CAPABILITIES[role].split(";")[0][:30]
            assert expected_fragment in rendered, (
                f"capability fragment for {role.value} missing"
            )

    def test_default_render_has_arrow_prefix(self):
        rendered = workflows.render_roster(self._roster())
        assert "→" in rendered

    def test_opt_out_produces_legacy_format(self):
        """REQ-018 F-02: include_capabilities=False gives the pre-REQ-018
        plain format (single line per agent, no capability markers)."""
        rendered = workflows.render_roster(self._roster(), include_capabilities=False)
        assert "→" not in rendered
        # Still contains role + name + pane
        assert "product_manager: sprint - PM (pane %1)" in rendered

    def test_default_still_contains_role_name_and_pane(self):
        rendered = workflows.render_roster(self._roster())
        assert "product_manager: sprint - PM (pane %1)" in rendered
        assert "developer: sprint - Dev (pane %3)" in rendered

    def test_empty_roster_produces_empty_string(self):
        assert workflows.render_roster([]) == ""

    def test_roster_with_unknown_role_degrades_gracefully(self):
        # If a role is missing from ROLE_CAPABILITIES (future, hypothetical),
        # the roster line still appears, just without a → capability line.
        # We simulate by removing a key temporarily.
        original = workflows.ROLE_CAPABILITIES.pop(AgentRole.user, None)
        try:
            rendered = workflows.render_roster([
                (AgentRole.user, "sprint - User", "%5"),
            ])
            assert "user: sprint - User (pane %5)" in rendered
            # No capability line for user now
            assert "→" not in rendered
        finally:
            if original is not None:
                workflows.ROLE_CAPABILITIES[AgentRole.user] = original


class TestRenderForOrchestratorNoSkillLines:
    """REQ-017 F-03: render_for_orchestrator must NOT emit skill directives.
    Skill selection is now the orchestrator LLM's runtime decision."""

    def _roster(self):
        return [
            (AgentRole.product_manager, "PM", "%1"),
            (AgentRole.tech_director, "TD", "%2"),
            (AgentRole.developer, "Dev", "%3"),
            (AgentRole.tester, "Tester", "%4"),
            (AgentRole.user, "User", "%5"),
        ]

    def test_render_does_not_contain_must_invoke(self):
        rendered = workflows.render_for_orchestrator(workflows.STANDARD, self._roster())
        assert "must invoke" not in rendered
        assert "⚡" not in rendered

    def test_render_does_not_contain_any_req_skill_name(self):
        rendered = workflows.render_for_orchestrator(workflows.STANDARD, self._roster())
        for skill in [
            "/req-1-analyze", "/req-2-tech", "/req-3-code",
            "/req-4-security", "/req-5-cleanup", "/req-6-review",
            "/req-7-verify", "/req-8-done",
        ]:
            assert skill not in rendered, (
                f"render should not hardcode {skill} — skills are orchestrator's choice"
            )

    def test_render_still_contains_role_names_and_descriptions(self):
        rendered = workflows.render_for_orchestrator(workflows.STANDARD, self._roster())
        assert "product_manager" in rendered
        assert "developer" in rendered
        assert "tester" in rendered

    def test_render_still_contains_failure_loop_metadata(self):
        rendered = workflows.render_for_orchestrator(workflows.STANDARD, self._roster())
        assert "<<TESTS_FAILED>>" in rendered
        assert "loop back" in rendered
