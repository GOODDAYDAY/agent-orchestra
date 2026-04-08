"""REQ-012 v2 — Built-in workflow templates.

Defines the three workflows that an Orchestrator agent can drive. Workflows
are immutable in-code data structures (no DB seeding, no DSL parser). The
orchestrator does not consume these structures directly — they are *rendered*
into a human-readable numbered list and injected into the orchestrator's
system prompt via the `{{WORKFLOW_DEFINITION}}` placeholder.

The supervisor uses `failure_loop` semantics to drive the Dev↔Tester loop in
the standard workflow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from agent_management.backend.models import AgentRole


@dataclass(frozen=True)
class Step:
    """A single step in a workflow playbook.

    REQ-017: intentionally carries NO skill field. Skill selection is a
    runtime decision made by the orchestrator LLM based on the current
    context, using the AVAILABLE_SKILLS catalogue below. Hardcoding
    `pm → /req-1-analyze` in the workflow dataclass (as REQ-016 did) would
    turn the orchestrator into a template substitution engine and defeat
    the purpose of having an LLM orchestrator in the first place.
    """
    role: AgentRole
    description: str
    on_failure_marker: Optional[str] = None   # e.g. "<<TESTS_FAILED>>"
    failure_loop_to: Optional[int] = None     # zero-based index of step to loop to
    max_retries: int = 0                      # only meaningful when failure_loop_to is set


@dataclass(frozen=True)
class Workflow:
    id: str
    display_name: str
    description: str
    steps: tuple[Step, ...]


# ---- Built-in workflow definitions ------------------------------------------

STANDARD = Workflow(
    id="standard",
    display_name="Standard (PM → TD → Dev → Tester → User)",
    description=(
        "Full requirement-to-acceptance playbook. The orchestrator typically "
        "walks this sequence but may skip, repeat, or deviate based on the "
        "actual situation."
    ),
    steps=(
        Step(role=AgentRole.product_manager,
             description="Produce a complete requirement specification."),
        Step(role=AgentRole.tech_director,
             description="Review the spec and produce a technical design."),
        Step(role=AgentRole.developer,
             description="Implement the technical design."),
        Step(role=AgentRole.tester,
             description="Run the test suite and report results.",
             on_failure_marker="<<TESTS_FAILED>>",
             failure_loop_to=2,        # back to Developer (zero-based)
             max_retries=3),
        Step(role=AgentRole.user,
             description="Acceptance review by the human (or human stand-in) user."),
    ),
)

PROTOTYPE = Workflow(
    id="prototype",
    display_name="Prototype (Dev → User)",
    description="Two-step playbook for quick experiments — Developer implements, User reviews.",
    steps=(
        Step(role=AgentRole.developer,
             description="Implement the prototype."),
        Step(role=AgentRole.user,
             description="Acceptance review of the prototype."),
    ),
)

RESEARCH = Workflow(
    id="research",
    display_name="Research (PM → TD → User)",
    description="Design-only playbook with no coding phase. Useful for spike investigations.",
    steps=(
        Step(role=AgentRole.product_manager,
             description="Frame the research question and the desired outcomes."),
        Step(role=AgentRole.tech_director,
             description="Investigate and produce a technical findings document."),
        Step(role=AgentRole.user,
             description="Acceptance review of the findings."),
    ),
)


# ---- REQ-017: Skill catalogue the orchestrator can choose from --------------

# Pure data. The orchestrator reads this catalogue via its system prompt
# (substituted into the {{SKILL_CATALOGUE}} placeholder) and decides at
# runtime which skill (if any) to include in each dispatch text. No code
# outside workflows.py should depend on specific entries — add a new skill
# by appending a tuple here.
AVAILABLE_SKILLS: tuple[tuple[str, str], ...] = (
    (
        "/req-1-analyze",
        "Expand a brief description into a complete requirement document "
        "(requirement.md) with background, functional requirements, "
        "acceptance criteria, and change log.",
    ),
    (
        "/req-2-tech",
        "Produce a technical design (technical.md) based on a finalised "
        "requirement: tech stack, architecture, module design, data model, "
        "key flows, risks.",
    ),
    (
        "/req-3-code",
        "Implement code following the technical design: high-cohesion "
        "low-coupling modules, logging, comments, automation scripts.",
    ),
    (
        "/req-4-security",
        "Security review of the code: injection attacks, data leakage, "
        "authentication issues, configuration vulnerabilities.",
    ),
    (
        "/req-5-cleanup",
        "Structural cleanup: detect unused code, dead code, duplicated "
        "logic, optimise cohesion/coupling without changing business logic.",
    ),
    (
        "/req-6-review",
        "Compare the implementation against the requirement document item "
        "by item; flag undeclared changes.",
    ),
    (
        "/req-7-verify",
        "Verification: build check, runtime check, automated testing, "
        "generate verification scripts.",
    ),
    (
        "/req-8-done",
        "Final archive: consistency check, update index.md status to Completed.",
    ),
)


def render_skill_catalogue() -> str:
    """Render the AVAILABLE_SKILLS catalogue as a multi-line bullet list
    suitable for injection into the orchestrator's system prompt via the
    ``{{SKILL_CATALOGUE}}`` placeholder.
    """
    if not AVAILABLE_SKILLS:
        return "(no skills available)"
    lines: list[str] = []
    for skill, description in AVAILABLE_SKILLS:
        lines.append(f"  - {skill}")
        lines.append(f"    {description}")
    return "\n".join(lines)


BUILT_IN_WORKFLOWS: dict[str, Workflow] = {
    STANDARD.id: STANDARD,
    PROTOTYPE.id: PROTOTYPE,
    RESEARCH.id: RESEARCH,
}

DEFAULT_WORKFLOW_ID: str = STANDARD.id


# ---- Public API --------------------------------------------------------------

def get_workflow(workflow_id: str) -> Workflow:
    """Look up a built-in workflow by ID. Raises KeyError on unknown id."""
    return BUILT_IN_WORKFLOWS[workflow_id]


def required_roles(workflow: Workflow) -> set[AgentRole]:
    """Return the set of distinct roles a group must contain to run this workflow."""
    return {step.role for step in workflow.steps}


def render_for_orchestrator(
    workflow: Workflow,
    roster: list[tuple[AgentRole, str, str]],
) -> str:
    """Render the workflow as the human-readable string injected into the
    orchestrator's system prompt via `{{WORKFLOW_DEFINITION}}`.

    REQ-017: skill hints are NOT emitted here. Skill selection is a runtime
    decision by the orchestrator LLM, using the AVAILABLE_SKILLS catalogue
    (substituted separately via {{SKILL_CATALOGUE}}). The workflow rendering
    describes only the role, actor, free-form description, and
    failure-loop metadata.

    `roster` is a list of `(role, agent_name, pane_id)` tuples — used so the
    rendered text can name the actual agents the orchestrator will dispatch
    to.
    """
    lines: list[str] = [f"Workflow: {workflow.display_name}", ""]
    name_by_role: dict[AgentRole, str] = {role: name for role, name, _ in roster}
    for idx, step in enumerate(workflow.steps, start=1):
        actor = name_by_role.get(step.role, f"<missing {step.role.value}>")
        line = f"  {idx}. {step.role.value}  ({actor})  —  {step.description}"
        if step.on_failure_marker and step.failure_loop_to is not None:
            target_idx = step.failure_loop_to + 1  # 1-based for humans
            line += (
                f"\n     If output contains {step.on_failure_marker}, loop back "
                f"to step {target_idx} (max {step.max_retries} retries)."
            )
        lines.append(line)
    return "\n".join(lines)


def render_roster(roster: list[tuple[AgentRole, str, str]]) -> str:
    """Render the worker roster for `{{WORKER_ROSTER}}`."""
    return "\n".join(
        f"  - {role.value}: {name} (pane {pane_id})"
        for role, name, pane_id in roster
    )
