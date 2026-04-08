# REQ-017 Restore Orchestrator Autonomy

> Status: Completed
> Created: 2026-04-08
> Updated: 2026-04-08
> Corrects: REQ-016 F-05 (per-step skill hardcoding — architectural error)

## 1. Background

### 1.1 The mistake in REQ-016 F-05

REQ-012 v2 established the "LLM Orchestrator" architecture: a 6th Claude CLI agent whose job is to **autonomously decide** which worker to dispatch, with what task, at what time. The whole reason for having an orchestrator at all is that orchestration is a dynamic, context-dependent decision made by an LLM at runtime — not a hardcoded sequence in Python.

REQ-016 F-05 violated this principle on three levels:

1. **`Step.skill` field added to the workflow dataclass.** Each workflow step declared "PM → req-1-analyze", "Tech Director → req-2-tech", "Developer → req-3-code", "Tester → req-7-verify". This turned the workflow from a high-level playbook into a rigid mapping between roles and skills, made at *source code edit time* rather than at *orchestrator runtime*.

2. **`render_for_orchestrator` injected "⚡ must invoke /req-X" lines into the orchestrator's system prompt.** The orchestrator's LLM was reduced to a template substitution engine — "look at step N, grab step N's skill, paste it into the dispatch text, advance N". Zero actual orchestration happening.

3. **Worker templates hardcoded specific skill chains.** The Developer template said: *"You must run the full /req-3-code → /req-4-security → /req-5-cleanup → /req-6-review → /req-7-verify pipeline before declaring done."* This is not only wrong architecturally — it means "should the Developer chain all 5 skills in one go or split across 5 dispatches?" is decided by a template edit, not by the orchestrator observing the current situation.

4. **Worker templates referenced the orchestrator abstraction.** Lines like "if Orchestrator's prompt mentions a /req-* skill, you must invoke it" leaked implementation detail into the worker. The worker doesn't need to know about the orchestrator. It doesn't need to know anything except: "I received a task, I do the task, I emit `<<TASK_DONE>>`".

The user flagged all of this bluntly: *"你都有编排器了对吧，那对这些skills的使用就不对啊，怎么能内部做编排呢？你把编排器置于何地？然后编排器，不要看现在的编排，要根据每次的情况，自主进行编排。"*

### 1.2 The correct design

**The orchestrator owns all scheduling decisions.** Workflows are high-level playbook templates — they describe a typical sequence of roles to consult, but the orchestrator is free to deviate (skip steps, repeat steps, reorder, split work across multiple dispatches, combine multiple skills into one). Skill selection ("should the Developer invoke /req-3-code or run the full /req-3..7 chain?") is a runtime decision by the orchestrator based on the actual conversation history.

**Workers are stateless task executors.** They have no awareness of the orchestrator, no knowledge of /req-* skill names in advance, no ability to chain tasks on their own. Their template says: "You will receive a task as terminal input. Work on it. Do exactly that task. Emit `<<TASK_DONE>>` on the last line. Do not proactively start follow-up work." That's it.

**The callback from worker to orchestrator is enforced at the Python code level, not at the LLM prompt level.** `Supervisor.dispatch_loop` detects the worker's `<<TASK_DONE>>` marker, extracts the artifact, and injects `[WORKER_RESULT role="X" via="marker"]\n{artifact}\n[/WORKER_RESULT]` into the orchestrator pane. This happens unconditionally — no matter what the worker did or didn't do, as long as completion fires (marker/silence/stall), the result goes back to the orchestrator. The worker has zero responsibility for "returning" the result; it just produces output and stops.

### 1.3 What REQ-017 does

1. Delete the `Step.skill` field from `workflows.Step`.
2. Delete the skill hints from the three built-in workflows (STANDARD / PROTOTYPE / RESEARCH).
3. Rewrite `render_for_orchestrator` so it no longer emits "⚡ must invoke" lines.
4. Introduce an `AVAILABLE_SKILLS` catalogue (a pure data constant in `backend/workflows.py`) listing all eight `/req-*` skills with short human descriptions. This catalogue is purely informational — the orchestrator picks what it needs when it needs it.
5. Substitute the catalogue into the orchestrator's system prompt via a new `{{SKILL_CATALOGUE}}` placeholder handled by `session_manager._render_orchestrator_prompt`.
6. Rewrite the orchestrator's system prompt to emphasise autonomous decision-making ("你是自主编排者，不是顺序执行器"). The workflow is described as a "typical playbook", not a mandate.
7. Rewrite all four non-User worker templates (PM, Tech Director, Developer, Tester) to:
   - Never mention the orchestrator
   - Never hardcode specific /req-* skill names
   - Say: "You receive a task. Do exactly that task. If the task mentions a /req-* skill, invoke it. Emit `<<TASK_DONE>>` when done. Do not chain follow-up work on your own."
8. Bump `_TEMPLATE_VERSION` from 6 to 7 so the updated templates propagate automatically.
9. Update tests to reflect the new architecture: no more `Step.skill` asserts; catalogue presence asserts instead.
10. Document the code-level callback contract explicitly in `technical.md`.

## 2. Target Users & Scenarios

- **S-01 Deviation from playbook**: User creates a group with the `standard` workflow but the actual task is "add a single CSS tweak". The orchestrator should skip PM / Tech Director and dispatch straight to Developer. Today's REQ-016 hardcoded pipeline would force it to run the full 5-step chain.
- **S-02 Skill combination**: The orchestrator dispatches a developer to "implement the caching layer and run all verification". The orchestrator decides to bundle `/req-3-code` + `/req-4-security` + `/req-7-verify` into one dispatch prompt (or split, or whatever the orchestrator's LLM judges best).
- **S-03 Repetition**: After a failed Tester run, the orchestrator dispatches back to Developer with a focused "fix these specific test failures" prompt. The orchestrator decides whether the Developer should re-run the whole /req-3..7 chain or only /req-3-code + /req-7-verify.
- **S-04 Worker abstraction**: The Developer agent's system prompt must not mention "orchestrator" anywhere. The developer should work identically whether it's being driven by an orchestrator, a human attaching directly via Enter, or some future alternative controller.
- **S-05 Skill introspection**: The orchestrator's system prompt lists all available /req-* skills with brief descriptions so the orchestrator's LLM can pick intelligently. Adding a new /req-* skill in the future is a one-line edit to the catalogue constant.

## 3. Functional Requirements

### F-01 Remove `Step.skill` field

- Main flow: `workflows.Step` dataclass drops the `skill: Optional[str]` field entirely.
- `STANDARD`, `PROTOTYPE`, `RESEARCH` step literals no longer pass `skill=...`.
- The descriptions stay — they describe *what* work the step represents (e.g. "Review the spec and produce a technical design") — but without hardcoding a specific /req-* skill name.
- Error handling: none (pure data change).
- Edge cases: any external code or test that read `step.skill` must be updated; grep confirms there are no other readers.

### F-02 Introduce the `AVAILABLE_SKILLS` catalogue

- Main flow: `backend/workflows.py` exports a new frozen constant `AVAILABLE_SKILLS: tuple[tuple[str, str], ...]` — each tuple is `(skill_name, one_line_description)`. Contents:

  | skill | description |
  |:---|:---|
  | `/req-1-analyze` | Expand a brief description into a complete requirement document (requirement.md) with background, functional requirements, acceptance criteria, and change log |
  | `/req-2-tech` | Produce a technical design (technical.md) based on a finalised requirement: tech stack, architecture, module design, data model, key flows, risks |
  | `/req-3-code` | Implement code following the technical design: high-cohesion low-coupling modules, logging, comments, automation scripts |
  | `/req-4-security` | Security review of the code: injection attacks, data leakage, authentication issues, configuration vulnerabilities |
  | `/req-5-cleanup` | Structural cleanup: detect unused code, dead code, duplicated logic, optimise cohesion/coupling without changing business logic |
  | `/req-6-review` | Compare implementation against the requirement document item by item |
  | `/req-7-verify` | Verification: build check, runtime check, automated testing, generate verification scripts |
  | `/req-8-done` | Final archive: consistency check, update index.md status to Completed |

- Also exported: a helper `render_skill_catalogue() -> str` that returns a human-readable multi-line rendering of the catalogue suitable for injection into a system prompt.
- Error handling: none.
- Edge cases: future expansion of the catalogue is a one-line append; no code outside `workflows.py` should depend on specific entries.

### F-03 `render_for_orchestrator` no longer emits skill directives

- Main flow: the function still renders each step's role, actor, and free-form description. The "⚡ must invoke /req-X" line is removed. The "(no skill — human review step)" line is also removed. The output becomes:

  ```
  Workflow: Standard (PM → TD → Dev → Tester → User)

    1. product_manager (sprint - Product Manager) — Produce a complete requirement specification.
    2. tech_director   (sprint - Tech Director)   — Review the spec and produce a technical design.
    3. developer       (sprint - Developer)       — Implement the technical design.
    4. tester          (sprint - Tester)          — Run the test suite and report results.
                                                     If output contains <<TESTS_FAILED>>, loop back to step 3 (max 3 retries).
    5. user            (sprint - User)            — Acceptance review by the human user.
  ```

- Error handling: none (presentation-only change).
- Edge cases: tests that previously asserted `/req-` substrings in the rendered text must be updated.

### F-04 Orchestrator system prompt rewrite

- Main flow: `_DEFAULT_TEMPLATES["orchestrator"]` is rewritten to present the workflow as a **typical playbook, not a strict state machine**, and to explicitly instruct autonomous decision-making. Structure:

  ```
  你是 Orchestrator —— 这个 group 的项目调度者。

  ## 你的职责
  你是自主编排者，不是顺序执行器。
  - 根据每次 [WORKER_RESULT] 的实际内容决定下一步做什么
  - 基于工作流模板（见下）选择合适的下属，但你有完全的权力跳过、重复、
    或者分叉步骤
  - 基于下面的技能目录选择合适的 /req-* 技能告诉下属去调用（如果适用）
  - 何时让一个下属一次调用多个技能，何时分多次 dispatch 过去，完全由你决定

  ## 工作流参考模板
  以下是本 group 的默认工作流，作为参考模板使用。你不必严格按顺序执行；
  每一步都是"通常情况下应该做的事"，不是"必须做的事"。
  {{WORKFLOW_DEFINITION}}

  ## 你的下属
  {{WORKER_ROSTER}}

  ## 可用技能目录
  以下 /req-* 技能由本项目的 .claude/skills/ 提供。你可以在 dispatch text
  里命令下属调用其中任何一个（或多个按顺序）。**不要捏造不在这个目录里的
  技能名。**

  {{SKILL_CATALOGUE}}

  ## 调度协议
  当你想让某个下属做事时，输出一行 dispatch（推荐自闭合形式）：

    <<DISPATCH role="developer" text="请调用 /req-3-code 技能，目标是：<具体描述>。完成后在最后一行输出 <<TASK_DONE>>。">>

  - role 必须是上面 "你的下属" 列出的角色名（小写）
  - text 是你要发给下属的完整 prompt
  - 如果适合该步骤，在 text 里明确命令下属调用某个技能；如果不适合（例如
    下属只需要做简单的一次性回答），就不提技能
  - text 字段不要包含换行符

  ## 接收结果
  平台会把下属的输出封装成 [WORKER_RESULT role="X" via="marker|silence|stall"]
  注入回你的对话。收到之后你根据内容决定：
  - 继续下一步 dispatch
  - 如果 Tester 报告 <<TESTS_FAILED>>，回到 Developer 重新 dispatch（可以
    只让它修复失败的测试，不必重跑全链路）
  - 如果全部完成，输出 <<WORKFLOW_COMPLETE>>
  - 如果遇到无法继续的情况，输出 <<WORKFLOW_ABORT reason="..."/>>

  ## 错误反馈
  [PLATFORM_ERROR: ...]    —— 你的 dispatch 写错了，改一下重发
  [WORKER_ERROR role="X" reason="..."] —— 那个下属不可用，考虑跳过或 abort
  [PLATFORM_STALL: ...]    —— 上个 dispatch 卡住了，等待操作员的处理结果

  ## 硬性规则
  - 一次只能 dispatch 一个角色，必须等到 [WORKER_RESULT] 才能 dispatch 下一个
  - text 字段不能包含 {{COMPLETION_MARKER}}、<<WORKFLOW_COMPLETE>>、
    <<WORKFLOW_ABORT —— 这些是平台控制标记
  - 工作流完成后输出 <<WORKFLOW_COMPLETE>>，不要再 dispatch
  - 不要伪造 [WORKER_RESULT]，只有平台能注入
  - 不要解释你的内部思考；直接产出 dispatch 或 workflow 控制标记
  ```

- A new placeholder `{{SKILL_CATALOGUE}}` is added. `session_manager._render_orchestrator_prompt` substitutes it with `workflows.render_skill_catalogue()` at session-start time.
- Error handling: if the catalogue is empty (future edge case), the substitution inserts "(no skills available)" and the orchestrator is told to dispatch without skill references.
- Edge cases: existing `{{WORKFLOW_DEFINITION}}`, `{{WORKER_ROSTER}}`, `{{COMPLETION_MARKER}}` placeholders continue to work unchanged.

### F-05 Worker template rewrite (hide the orchestrator abstraction)

- Main flow: the PM, Tech Director, Developer, and Tester templates are rewritten to:
  - Never mention "Orchestrator" or "dispatch" or "[WORKER_RESULT]"
  - Never name specific /req-* skills
  - Use a uniform structure: "You will receive a task via terminal input. Work on it. If the task mentions a /req-X-Y skill name, invoke it. Do exactly the one task you were given — do not chain follow-ups on your own. Emit `<<TASK_DONE>>` on the last line when finished."
  - Tester additionally documents the `<<TESTS_FAILED>>` secondary marker rule
- The User template is unchanged (human review step).
- The custom role template stays empty.
- Error handling: the templates tell the worker that if they encounter an unknown /req-* skill name, they should still try to invoke it (the orchestrator chose it deliberately).
- Edge cases: workers no longer need to know about `<<TASK_DONE>>` being "delivered to orchestrator" — they just treat it as "the end of my turn".

### F-06 Developer template — drop the hardcoded /req-3..7 chain

- Main flow: the Developer template no longer says "run /req-3-code → /req-4-security → /req-5-cleanup → /req-6-review → /req-7-verify in sequence". Instead it uses the generic rule from F-05: "do what the task prompt asks, don't chain follow-ups on your own".
- Rationale: whether the Developer runs one skill or five is a runtime decision by the orchestrator, communicated in the dispatch text.
- Error handling: none.
- Edge cases: orchestrator-side guidance in F-04's template now explicitly tells the orchestrator: "if you want the developer to run a full verification chain, include all five /req-* calls in one dispatch text, or split across multiple dispatches — your choice".

### F-07 `_render_orchestrator_prompt` substitutes `{{SKILL_CATALOGUE}}`

- Main flow: `SessionManager._render_orchestrator_prompt` is extended with a single new substitution step:

  ```python
  rendered = rendered.replace(
      "{{SKILL_CATALOGUE}}", workflows.render_skill_catalogue()
  )
  ```

  placed alongside the existing WORKFLOW_DEFINITION / WORKER_ROSTER / COMPLETION_MARKER substitutions.
- Error handling: if the template lacks the placeholder (e.g. a user has heavily customised it), the replace is a no-op.
- Edge cases: None.

### F-08 Code-level callback documentation

- Main flow: `technical.md` §3 gets a new subsection "The code-level callback contract" that explicitly documents:
  - Workers have no knowledge of the orchestrator
  - The supervisor's dispatch_loop is the only thing that knows about "who receives the worker's output"
  - The contract is: worker emits output ending with `<<TASK_DONE>>` (or goes silent, or stalls) → supervisor extracts artifact → supervisor unconditionally injects `[WORKER_RESULT role="X" via="..."]` into the orchestrator pane
  - This is enforced at code level, not at prompt level — the worker template mentions `<<TASK_DONE>>` only as "end-of-turn marker", never as "way to notify the orchestrator"
- No code change from this item; it's pure documentation to make the existing design explicit.
- Verification: the existing `test_dispatch_integration.py::TestHappyPath` test already proves the contract. A new test is added that confirms a worker pane receives exactly what the orchestrator dispatched (no "please notify the orchestrator" language is present in the worker prompt / dispatch).

### F-09 Template version bump

- Main flow: `Repository._TEMPLATE_VERSION` goes from 6 (REQ-016) to 7 (REQ-017). The force-update mechanism re-seeds all built-in templates on next app start.
- Error handling: same as every prior bump — user customisations to built-in templates are overwritten.
- Edge cases: documented in Out of Scope.

## 4. Non-functional Requirements

- All 441 existing tests must continue to pass where compatible. Tests that assert REQ-016's hardcoded skill mapping (Step.skill values, "⚡ must invoke" rendering, "/req-3-code → /req-7-verify" chain in Developer template) must be updated or removed.
- New tests: at least 10 covering the AVAILABLE_SKILLS catalogue, the SKILL_CATALOGUE placeholder substitution, the cleaned-up worker templates, and a regression guard that no worker template contains hardcoded /req-X skill names.
- No new runtime dependencies.
- No new modules (constant and helper live in existing `workflows.py`).
- Backwards-compatibility with orchestrator behaviour at the dispatch-loop level: the dispatch format (`<<DISPATCH role="..." text="...">>`), completion detection, force-advance, abort — all unchanged.

## 5. Out of Scope

- Auto-discovery of /req-* skills by scanning `.claude/skills/` — the catalogue is hardcoded; adding a new skill is a one-line edit to `AVAILABLE_SKILLS`.
- User-editable workflow definitions — still three built-ins.
- Orchestrator memory / context compression for long workflows.
- Template customisation preservation across version bumps.
- A "skill usage audit log" showing which skills the orchestrator actually chose — future telemetry REQ.

## 6. Acceptance Criteria

| ID | Feature | Condition | Expected Result |
|:---|:---|:---|:---|
| AC-01 | F-01 | Inspect `workflows.Step` dataclass | No `skill` field is present |
| AC-02 | F-01 | `workflows.STANDARD.steps[0]` | Is a Step instance WITHOUT a `skill` attribute; the description is the free-form role description |
| AC-03 | F-02 | `workflows.AVAILABLE_SKILLS` | Contains at least 8 entries covering `/req-1-analyze` through `/req-8-done` |
| AC-04 | F-02 | `workflows.render_skill_catalogue()` output | Is a non-empty string listing every catalogue entry with its description |
| AC-05 | F-03 | `render_for_orchestrator(STANDARD, roster)` output | Contains each role name and description; does NOT contain "must invoke" or "⚡" or "/req-" |
| AC-06 | F-04 | `repo.get_orchestrator_template()` raw (pre-substitution) | Contains `{{SKILL_CATALOGUE}}` placeholder; contains "自主编排者"; contains "典型模板"; contains `{{WORKFLOW_DEFINITION}}` and `{{WORKER_ROSTER}}` |
| AC-07 | F-04 | Orchestrator template | Does NOT contain the string "⚡ must invoke" (reverted) |
| AC-08 | F-05 | PM / Tech Director / Developer / Tester templates | Do NOT contain the word "Orchestrator"; do NOT contain any "/req-" substring; contain "<<TASK_DONE>>" as the end-of-turn marker |
| AC-09 | F-06 | Developer template | Does NOT contain the chain "req-3-code → req-4-security → req-5-cleanup → req-6-review → req-7-verify" anywhere |
| AC-10 | F-07 | `_render_orchestrator_prompt` output (full integration) | `{{SKILL_CATALOGUE}}` placeholder is replaced; the rendered text contains `/req-1-analyze` through `/req-8-done` from the catalogue |
| AC-11 | F-08 | Worker's prompt content after a dispatch cycle | Contains only the task text and `<<TASK_DONE>>` instruction; no reference to "orchestrator" or "WORKER_RESULT" |
| AC-12 | F-09 | `Repository._TEMPLATE_VERSION` | Equals 7 |
| AC-13 | Regression | Run pytest | All retained tests + new REQ-017 tests pass; total ≥ 440 |
| AC-14 | Regression | Existing dispatch_loop integration test (happy path) | Still passes — the code-level callback contract is unchanged |
| AC-15 | Regression | grep `src/agent_management/backend/repository.py` for `/req-` within worker template strings | Zero matches (all references live only in the orchestrator template's catalogue) |

## 7. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-08 | Initial — revert REQ-016 F-05 per-step skill hardcoding; remove `Step.skill` field; introduce `AVAILABLE_SKILLS` catalogue and `{{SKILL_CATALOGUE}}` placeholder; rewrite orchestrator prompt to emphasise autonomous decision-making; rewrite worker templates to hide the orchestrator abstraction and remove hardcoded /req-* skill chains; document the code-level callback contract; bump `_TEMPLATE_VERSION` to 7 | ALL | User identified that REQ-016 F-05 violated the LLM-orchestrator architecture: hardcoded skill selection, per-step mapping, and "dev must run /req-3..7 chain" were all Python-level decisions masquerading as orchestrator output |
