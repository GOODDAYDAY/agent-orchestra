# REQ-017 Technical Design — Restore Orchestrator Autonomy

> Status: Completed
> Requirement: requirement.md
> Created: 2026-04-08
> Updated: 2026-04-08

## 1. Technology Stack

| Area | Technology | Rationale |
|:---|:---|:---|
| Workflow data | Remove `Step.skill` field; keep frozen dataclass | Data change only — no new dependencies |
| Skill catalogue | Frozen tuple constant + render helper in `backend/workflows.py` | Keeps all orchestration-adjacent data in one module |
| Prompt substitution | New `{{SKILL_CATALOGUE}}` placeholder in `session_manager._render_orchestrator_prompt` | Same pattern as existing three placeholders |
| Worker templates | Pure string rewrite in `repository._DEFAULT_TEMPLATES` | Covered by the existing `_TEMPLATE_VERSION` force-update mechanism |
| Test updates | pytest | Existing fixtures still apply |

## 2. Design Principles

- **Orchestration decisions live in the orchestrator's runtime output, not in static code or templates.** The workflow is a hint; the skill catalogue is a toolkit; the orchestrator's LLM is the decision-maker.
- **Workers are oblivious to the orchestrator.** Their prompts never mention the orchestrator by name or concept. The `<<TASK_DONE>>` marker is described as "end of your turn", nothing more.
- **The code-level callback contract is enforced by the supervisor's dispatch_loop, not by worker prompts.** Worker output → supervisor detection → unconditional injection into orchestrator pane. This is how REQ-012 v2 originally designed it; REQ-016 didn't break this, only leaked the abstraction into the prompts.
- **Minimal diff from REQ-016.** Most of REQ-017 is a targeted revert plus a cleaner reimplementation of F-05. The supervisor, dispatch loop, orchestrator parser, key forwarding — all unchanged.

## 3. Architecture Overview

### 3.1 The code-level callback contract

```
┌────────────────┐  dispatch text   ┌───────────────┐
│  Orchestrator  │ ────────────▶    │   Worker      │
│   (LLM)        │                  │   (LLM)       │
│                │                  │               │
│  does NOT      │                  │  has NO       │
│  directly      │                  │  knowledge    │
│  observe the   │                  │  of the       │
│  worker        │                  │  orchestrator │
└────────────────┘                  └───────────────┘
         ▲                                    │
         │                                    │ emits any text
         │                                    │ ending in <<TASK_DONE>>
         │                                    ▼
         │                           ┌────────────────────┐
         │                           │ Supervisor         │
         │                           │ dispatch_loop      │
         │  [WORKER_RESULT role="X"  │                    │
         │   via="marker"]           │ 1. capture_pane    │
         │      <artifact>           │ 2. detect_completion│
         │   [/WORKER_RESULT]        │ 3. extract artifact│
         └───────────────────────────│ 4. send_keys back  │
                                     │    to orchestrator │
                                     └────────────────────┘
```

- Orchestrator writes a dispatch block to its own pane → `dispatch_loop.parse_latest_dispatch` picks it up → `send_keys(worker_pane, dispatch.text)` routes it to the worker.
- Worker produces output ending with `<<TASK_DONE>>` → `detect_completion` returns a `Marker` result → supervisor calls `send_keys(orch_pane, "[WORKER_RESULT ...]")`.
- **The worker cannot "bypass" this.** The worker doesn't have access to the orchestrator's pane; its output only reaches the orchestrator because the supervisor copies it. If a worker tried to send something directly to another worker, it would fail (there's no such transport).

This contract was already in place since REQ-012 v2. REQ-017 simply makes the documentation and the prompt wording match it.

### 3.2 Skill catalogue data flow

```
workflows.AVAILABLE_SKILLS (tuple constant)
        │
        └──▶ workflows.render_skill_catalogue() — string formatter
                     │
                     └──▶ session_manager._render_orchestrator_prompt()
                                   │
                                   │  substitutes {{SKILL_CATALOGUE}}
                                   ▼
                          orchestrator's system prompt
                                   │
                                   │  Claude CLI --system-prompt-file
                                   ▼
                          orchestrator LLM chooses skills
                          based on current context
```

The catalogue is pure data. The orchestrator reads it in its system prompt, thinks about which skill applies to the current dispatch, and writes the skill name into its dispatch text. The Python code never "picks" a skill for any step.

## 4. Module Design

### 4.1 `backend/workflows.py` — Step.skill removal + catalogue

**Step dataclass — drop the `skill` field:**

```python
@dataclass(frozen=True)
class Step:
    role: AgentRole
    description: str
    on_failure_marker: Optional[str] = None
    failure_loop_to: Optional[int] = None
    max_retries: int = 0
```

**Built-in workflow updates — descriptions become role-neutral:**

```python
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
             failure_loop_to=2,
             max_retries=3),
        Step(role=AgentRole.user,
             description="Acceptance review by the human (or human stand-in) user."),
    ),
)
```

Same treatment for `PROTOTYPE` and `RESEARCH`: descriptions describe *what* the step represents, not *which skill* to invoke.

**New: `AVAILABLE_SKILLS` catalogue:**

```python
AVAILABLE_SKILLS: tuple[tuple[str, str], ...] = (
    ("/req-1-analyze",
     "Expand a brief description into a complete requirement document "
     "(requirement.md) with background, functional requirements, "
     "acceptance criteria, and change log."),
    ("/req-2-tech",
     "Produce a technical design (technical.md) based on a finalised "
     "requirement: tech stack, architecture, module design, data model, "
     "key flows, risks."),
    ("/req-3-code",
     "Implement code following the technical design: high-cohesion "
     "low-coupling modules, logging, comments, automation scripts."),
    ("/req-4-security",
     "Security review of the code: injection attacks, data leakage, "
     "authentication issues, configuration vulnerabilities."),
    ("/req-5-cleanup",
     "Structural cleanup: detect unused code, dead code, duplicated logic, "
     "optimise cohesion/coupling without changing business logic."),
    ("/req-6-review",
     "Compare the implementation against the requirement document item by "
     "item; flag undeclared changes."),
    ("/req-7-verify",
     "Verification: build check, runtime check, automated testing, "
     "generate verification scripts."),
    ("/req-8-done",
     "Final archive: consistency check, update index.md status to Completed."),
)


def render_skill_catalogue() -> str:
    """Render the catalogue as a multi-line bullet list for injection into
    the orchestrator's system prompt via the {{SKILL_CATALOGUE}} placeholder.
    """
    if not AVAILABLE_SKILLS:
        return "(no skills available)"
    lines = []
    for skill, description in AVAILABLE_SKILLS:
        lines.append(f"  - {skill}")
        lines.append(f"    {description}")
    return "\n".join(lines)
```

**`render_for_orchestrator` cleanup:**

```python
def render_for_orchestrator(
    workflow: Workflow,
    roster: list[tuple[AgentRole, str, str]],
) -> str:
    lines: list[str] = [f"Workflow: {workflow.display_name}", ""]
    name_by_role: dict[AgentRole, str] = {role: name for role, name, _ in roster}
    for idx, step in enumerate(workflow.steps, start=1):
        actor = name_by_role.get(step.role, f"<missing {step.role.value}>")
        line = f"  {idx}. {step.role.value}  ({actor})  —  {step.description}"
        if step.on_failure_marker and step.failure_loop_to is not None:
            target_idx = step.failure_loop_to + 1
            line += (
                f"\n     If output contains {step.on_failure_marker}, loop back "
                f"to step {target_idx} (max {step.max_retries} retries)."
            )
        lines.append(line)
    return "\n".join(lines)
```

Note the **removal** of the "⚡ must invoke /req-X" line. No per-step skill information is surfaced to the orchestrator by the workflow renderer.

### 4.2 `backend/repository.py` — template rewrite and version bump

**`_TEMPLATE_VERSION = 7`**.

**Orchestrator template** — see F-04 in requirement.md for the full text. The key structural changes from REQ-016's version:

- New opening sentence: *"你是自主编排者，不是顺序执行器。"*
- Workflow reference labeled as *"工作流参考模板"* and explicitly marked as non-mandatory
- New `## 可用技能目录` section with the `{{SKILL_CATALOGUE}}` placeholder
- Dispatch protocol example shows a skill reference but explicitly notes it is optional
- Hard rule added: *"不要捏造不在这个目录里的技能名。"*
- The REQ-016 instruction "must invoke skill from the workflow step's skill field" is gone

**Worker templates** (PM, Tech Director, Developer, Tester) — all four replaced with a uniform structure:

```
你是 <角色>。

## 你的职责
<两到三句，描述这个角色的工作内容 — 不要提 Orchestrator，不要写死具体技能名>

## 协议（必须遵守）
1. 你会通过终端输入收到一条任务描述。专心做那一件事，不要主动去做别的。
2. 如果任务描述里提到了 /req-X-Y 这样的技能，你必须在自己的终端里调用这个技能。
   不要跳过，不要假装执行过。
3. 完成任务之后，在最后一行（且仅最后一行）输出：

   <<TASK_DONE>>

4. 不要把 <<TASK_DONE>> 写在中间任何位置 —— 它只能作为整段输出的结束标记。
5. 输出 <<TASK_DONE>> 后停下，不要继续做后续任务。等待下一次任务。

## 输出格式
   <你的工作产出正文，可以多段、可以任意长度>
   <<TASK_DONE>>
```

**Tester template** additionally has:

```
## 失败上报格式
如果测试有失败，在 <<TASK_DONE>> 之前的一行单独输出 <<TESTS_FAILED>>：

   <测试报告 + 失败的复现步骤 + 期望与实际差异>
   <<TESTS_FAILED>>
   <<TASK_DONE>>
```

**Notably absent from all worker templates:**

- No "Orchestrator" mentioned anywhere
- No specific /req-* skill name hardcoded
- No "run /req-3-code → /req-4-security → ... chain" instruction for the Developer
- No "[WORKER_RESULT]" or "dispatch" terminology
- No per-role topic subscriptions (that was v1 territory, already dropped in v2)

### 4.3 `backend/session_manager.py` — new placeholder substitution

`_render_orchestrator_prompt` gains one line:

```python
rendered = template
rendered = rendered.replace(
    "{{WORKFLOW_DEFINITION}}", workflows.render_for_orchestrator(workflow, roster)
)
rendered = rendered.replace(
    "{{WORKER_ROSTER}}", workflows.render_roster(roster)
)
rendered = rendered.replace(
    "{{SKILL_CATALOGUE}}", workflows.render_skill_catalogue()  # REQ-017 F-07
)
rendered = rendered.replace("{{COMPLETION_MARKER}}", COMPLETION_MARKER)
return rendered
```

That's the only change to `session_manager.py`. The orchestrator startup flow, readiness poll, tmp file cleanup, and F-02 cleanup all stay the same.

### 4.4 Tests that must be updated / removed

**Removed or substantially rewritten:**

- `test_workflows.py::TestStepSkillField::test_standard_pm_has_req1_analyze` — Step.skill no longer exists. Remove.
- `test_workflows.py::TestStepSkillField::*` — entire class removed.
- `test_workflows.py::TestRenderSkillAnnotation` — entire class removed (render no longer emits skill lines).
- `test_repository.py::TestReq016TemplateVersion::test_developer_template_lists_full_req_pipeline` — Developer template no longer contains the /req-3..7 chain. Remove.
- `test_repository.py::TestReq016TemplateVersion::test_pm_template_mentions_req1_analyze` — PM template no longer mentions /req-1-analyze specifically. Remove.
- `test_repository.py::TestReq016TemplateVersion::test_tech_director_template_mentions_skill_rule` — same. Remove.
- `test_repository.py::TestReq016TemplateVersion::test_tester_template_mentions_req7_verify` — same. Remove.
- `test_repository.py::TestReq016TemplateVersion::test_template_version_is_6` — version is now 7. Update.
- `test_session_manager.py::TestRenderOrchestratorPrompt::test_substitutes_all_placeholders` — add `{{SKILL_CATALOGUE}}` to the list of substituted placeholders.

**Added:**

- `test_workflows.py::TestAvailableSkills::test_catalogue_contains_all_req_skills`
- `test_workflows.py::TestAvailableSkills::test_render_skill_catalogue_non_empty`
- `test_workflows.py::TestAvailableSkills::test_render_skill_catalogue_contains_descriptions`
- `test_workflows.py::TestRenderForOrchestrator::test_no_skill_annotations_in_output`
- `test_repository.py::TestReq017TemplateVersion::test_template_version_is_7`
- `test_repository.py::TestReq017WorkerTemplates::test_no_worker_mentions_orchestrator`
- `test_repository.py::TestReq017WorkerTemplates::test_no_worker_hardcodes_specific_req_skill`
- `test_repository.py::TestReq017WorkerTemplates::test_developer_template_does_not_contain_chain`
- `test_repository.py::TestReq017OrchestratorTemplate::test_orchestrator_template_has_skill_catalogue_placeholder`
- `test_repository.py::TestReq017OrchestratorTemplate::test_orchestrator_template_autonomous_language`
- `test_repository.py::TestReq017OrchestratorTemplate::test_orchestrator_template_no_forced_skill_rendering`
- `test_session_manager.py::TestRenderOrchestratorPrompt::test_skill_catalogue_substituted`
- `test_session_manager.py::TestRenderOrchestratorPrompt::test_rendered_prompt_contains_req1_analyze_description`

## 5. Data Model

No schema changes. `Step.skill` was an in-memory dataclass field only; removing it has no persistence impact.

## 6. API Design

Internal API changes:

| API | Change |
|:---|:---|
| `workflows.Step.skill` | **Removed** |
| `workflows.AVAILABLE_SKILLS` | **Added** — tuple of (skill, description) |
| `workflows.render_skill_catalogue()` | **Added** — returns multi-line string |
| `workflows.render_for_orchestrator()` | **Signature unchanged**; output no longer contains "⚡ must invoke" lines |
| `SessionManager._render_orchestrator_prompt` | One new `replace()` call for `{{SKILL_CATALOGUE}}` |
| `Repository._TEMPLATE_VERSION` | 6 → 7 |

## 7. Key Flows

### 7.1 Orchestrator startup with catalogue

1. `session_manager.start_agent_session(orch_agent, group_id, None)` runs
2. `_render_orchestrator_prompt` fetches the orchestrator template from the repo
3. Builds the roster from live session records
4. Substitutes `{{WORKFLOW_DEFINITION}}` with `render_for_orchestrator(workflow, roster)` (no more "⚡ must invoke" lines)
5. Substitutes `{{WORKER_ROSTER}}` with `render_roster(roster)`
6. **Substitutes `{{SKILL_CATALOGUE}}` with `render_skill_catalogue()` — REQ-017 new step**
7. Substitutes `{{COMPLETION_MARKER}}` with `<<TASK_DONE>>`
8. Writes the rendered prompt to `TEMP_DIR/orch_prompt_<id>.txt`
9. Launches Claude CLI with `--system-prompt-file <tmp>`
10. Schedules 30 s tmp file cleanup (REQ-014 F-02 unchanged)

### 7.2 Dispatch cycle — worker sees no orchestrator concept

1. Orchestrator LLM decides dispatch content, writes `<<DISPATCH role="developer" text="Please invoke /req-3-code with goal: implement feature X. After that, emit <<TASK_DONE>>.">>`
2. `dispatch_loop.parse_latest_dispatch` picks it up
3. `SessionManager.send_keys(worker_pane, dispatch.text)` routes the text to the Developer pane
4. The Developer's Claude CLI receives a user message: *"Please invoke /req-3-code with goal: ..."*. The Developer's system prompt has NO mention of the orchestrator. The Developer just sees "here's a task, do it"
5. Developer invokes `/req-3-code`, produces output, ends with `<<TASK_DONE>>`
6. `detect_completion` fires marker layer → extracts artifact
7. `send_keys(orch_pane, "[WORKER_RESULT role=\"developer\" via=\"marker\"]\n<artifact>\n[/WORKER_RESULT]")` — the callback is enforced here, at code level
8. Orchestrator LLM sees the WORKER_RESULT, decides the next step

Note that step 4 is the crucial point: **the Developer's system prompt has zero knowledge of the orchestrator**. It just executes the task it was given. The "return to orchestrator" happens in steps 6-7 entirely on the Python side.

## 8. Shared Modules & Reuse Strategy

| Shared | Used by | Notes |
|:---|:---|:---|
| `workflows.AVAILABLE_SKILLS` | `workflows.render_skill_catalogue` only | Pure data |
| `workflows.render_skill_catalogue` | `session_manager._render_orchestrator_prompt` | One call site |
| `{{SKILL_CATALOGUE}}` placeholder | Orchestrator template → session_manager substitution | Parallel to existing placeholders |
| Existing dispatch_loop → WORKER_RESULT injection | Unchanged | Already the code-level callback contract from REQ-012 v2 |

## 9. Risks & Notes

| Risk | Mitigation |
|:---|:---|
| Orchestrator LLM may not actually USE the catalogue and dispatch without skill references | Acceptable — the orchestrator is free to dispatch without a skill if the situation doesn't warrant one. This is the whole point of giving it autonomy. |
| Orchestrator LLM may reference a skill not in the catalogue | The orchestrator template explicitly says "不要捏造不在这个目录里的技能名". LLM should comply; if it does hallucinate, the worker will still try to run `/whatever-skill` and Claude CLI will report "unknown skill", and the orchestrator sees that in the WORKER_RESULT. |
| Existing tests fail because they assert REQ-016 hardcoding | They will fail — that's the point. REQ-017 updates them. |
| Users with customised role templates lose their edits on the template_version bump | Same known limitation as every prior bump. Documented in Out of Scope. |
| Simpler worker templates may underperform if the dispatch text is ambiguous | The orchestrator's autonomy is the compensating mechanism — it should write clearer dispatch text. Worker templates are deliberately minimal; complexity belongs in the orchestrator. |

## 10. Test Strategy

See §4.4 for the full list. Summary:

- **Remove** 6 tests that assert REQ-016 hardcoding (Step.skill, ⚡ rendering, developer /req chain).
- **Update** 2 tests (template version, placeholder list).
- **Add** 10+ new tests covering the catalogue, the new placeholder substitution, the cleaned-up worker templates (regression guards against re-introducing the hardcoding), and the autonomous-language markers in the orchestrator template.

All tests continue to run under the same `uv run pytest` invocation with no new fixtures or plugins.

## 11. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-08 | Initial — revert REQ-016 F-05 architectural error. Drop `Step.skill`. Introduce `AVAILABLE_SKILLS` catalogue + `render_skill_catalogue()`. Add `{{SKILL_CATALOGUE}}` placeholder substitution in `session_manager`. Rewrite orchestrator template for autonomous decision-making. Rewrite worker templates to hide the orchestrator abstraction and drop hardcoded /req-* chains. Bump `_TEMPLATE_VERSION` 6 → 7. Document the code-level callback contract. Update tests. | ALL | Skill selection and multi-step chaining must be orchestrator runtime decisions, not source-code-level hardcoding. REQ-016 got this wrong. |
