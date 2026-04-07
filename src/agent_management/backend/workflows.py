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
        "Full requirement-to-acceptance pipeline. PM produces a spec, Tech "
        "Director designs, Developer implements, Tester verifies (loops back "
        "to Developer up to 3 times if tests fail), and finally User reviews."
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
    description="Two-step workflow for quick experiments — Developer implements, User reviews.",
    steps=(
        Step(role=AgentRole.developer,
             description="Implement the prototype as described."),
        Step(role=AgentRole.user,
             description="Acceptance review of the prototype."),
    ),
)

RESEARCH = Workflow(
    id="research",
    display_name="Research (PM → TD → User)",
    description="Design-only workflow with no coding phase. Useful for spike investigations.",
    steps=(
        Step(role=AgentRole.product_manager,
             description="Frame the research question and the desired outcomes."),
        Step(role=AgentRole.tech_director,
             description="Investigate and produce a technical findings document."),
        Step(role=AgentRole.user,
             description="Acceptance review of the findings."),
    ),
)


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

    `roster` is a list of `(role, agent_name, pane_id)` tuples — used so the
    rendered text can name the actual agents the orchestrator will dispatch to.
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
