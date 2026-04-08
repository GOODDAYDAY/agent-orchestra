# REQ-018 Orchestrator-Centric Refinement

> Status: Completed
> Created: 2026-04-08
> Updated: 2026-04-08

## 1. Background

Three concrete issues reported after REQ-017 went live:

### 1.1 `clear_all` was never parallelised

REQ-016 F-03 parallelised `Supervisor.start_group`, `stop_group`, and
`resume_group` via `asyncio.gather(return_exceptions=True)`. It silently
missed `clear_all`, which still iterates groups and sessions with nested
sequential `await`s:

```python
async def clear_all(self) -> None:
    await self._cancel_dispatch_loop()
    for group in await self._repo.get_groups():
        sessions = await self._repo.get_sessions_for_group(group.id)
        for session in sessions:
            try:
                await self._sm.stop_agent_session(session)
            ...
```

With multiple groups and 6 sessions per group, `clear_all` can take 30+
seconds in the worst case (every session individually running its
`C-c` grace period plus `kill-pane`). This is visible during UI
shutdown and during the `c` key's Clear All action.

### 1.2 The orchestrator's roster carries no capability information

`workflows.render_roster(roster)` produces:

```
  - product_manager: sprint - Product Manager (pane %1)
  - tech_director:   sprint - Tech Director   (pane %2)
  - developer:       sprint - Developer       (pane %3)
  - tester:          sprint - Tester          (pane %4)
  - user:            sprint - User            (pane %5)
```

The orchestrator sees role name + actor name + pane id. It does not see
**what each subordinate is good at**. When deciding "who should do this
next", the orchestrator has to infer capabilities from the role name
alone — not great for a language model that responds to explicit
guidance.

A proper orchestrator-centric design gives the parent agent rich
information about its subordinates' capabilities, so routing decisions
are based on "who can do this" rather than just "who is named X".

### 1.3 Missing Chinese README

The project ships a comprehensive English README (`README.md`) but the
primary user is a Chinese developer. A Chinese-language README reduces
friction for the main audience and serves as a reference translation
for the English version.

### 1.4 Why bundle

All three are small, narrowly scoped, and touch a small blast radius.
Bundling them keeps the commit history coherent — they share the theme
"tighten the orchestrator-as-parent mental model and fix the parallel
gap we missed last time".

## 2. Target Users & Scenarios

- **S-01 Fast shutdown**: operator hits Clear All after running several
  test groups; all agents stop in parallel instead of serially. Total
  wall time stays under a few seconds regardless of group count.
- **S-02 Smart routing**: the orchestrator reads its system prompt and
  sees a capability list for each subordinate (e.g. *"developer —
  implements code, runs tests, invokes /req-\* skills end-to-end"*),
  giving it explicit guidance about who to dispatch to.
- **S-03 Mandarin onboarding**: a Mandarin-speaking operator clones the
  repo and reads `README.zh-CN.md`. Every section from the English
  README is present and accurate.

## 3. Functional Requirements

### F-01 Parallelise `Supervisor.clear_all`

- Main flow: replace the nested sequential for-loops in
  `Supervisor.clear_all` with:
  1. Cancel the dispatch loop (unchanged, already done).
  2. Collect *all* sessions across *all* groups into one flat list.
  3. Call `asyncio.gather(*[self._sm.stop_agent_session(s) for s in
     sessions], return_exceptions=True)` — every stop runs in parallel,
     exceptions are isolated per-session.
  4. Log any exceptions (per-session) and emit an
     `AgentStatusChanged(status=stopped)` message for each successfully
     stopped session.
  5. `_active_group_id = None`, clear runtime state, wipe temp dir
     (unchanged).
- Error handling: `return_exceptions=True` guarantees one broken
  session does not block the others. Exceptions are logged with
  `logger.exception`.
- Edge cases:
  - Zero groups → the flat session list is empty; `gather` with no
    arguments is a no-op.
  - Sessions that belong to the currently-active group must also stop
    (the dispatch loop was already cancelled by step 1).
  - Temp dir cleanup still runs even if all stops raised.

### F-02 Add per-role capability descriptions

- Main flow: `backend/workflows.py` exports a new frozen constant
  `ROLE_CAPABILITIES: dict[AgentRole, str]` mapping each non-orchestrator
  role to a one-line description of what that role is good at.
  Example entries:

  | Role | Capability |
  |:---|:---|
  | `product_manager` | Expand rough requirements into complete spec documents |
  | `tech_director` | Produce technical designs and review architecture |
  | `developer` | Implement code, run tests, invoke /req-* skills |
  | `tester` | Run test suites, report failures via `<<TESTS_FAILED>>` |
  | `user` | Human-facing acceptance review |
  | `custom` | Free-form role — capabilities defined by the user |

- `render_roster` gains a new optional parameter `include_capabilities:
  bool = True`. When True, each rendered line includes the capability
  description after the pane id:

  ```
    - product_manager: sprint - Product Manager (pane %1)
      → Expand rough requirements into complete spec documents
    - tech_director:   sprint - Tech Director   (pane %2)
      → Produce technical designs and review architecture
    - developer:       sprint - Developer       (pane %3)
      → Implement code, run tests, invoke /req-* skills
    - tester:          sprint - Tester          (pane %4)
      → Run test suites, report failures via <<TESTS_FAILED>>
    - user:            sprint - User            (pane %5)
      → Human-facing acceptance review
  ```

- Default call sites use `include_capabilities=True` so the
  orchestrator's system prompt always carries the richer roster.
- Roles not in `ROLE_CAPABILITIES` (e.g. hypothetical future custom
  roles) render without the capability line — graceful degradation.
- Error handling: none; pure rendering.

### F-03 Supervisor module docstring rewrite for orchestrator-centric model

- Main flow: rewrite the module-level docstring in `backend/supervisor.py`
  to explicitly document the orchestrator-as-parent mental model:

  ```
  The Supervisor is the **executive assistant to the Orchestrator agent**.
  It manages group lifecycle (start/stop/resume/clear) and drives the
  orchestrator's dispatch loop. The Supervisor is NOT a peer-agent
  message bus — workers are the orchestrator's subordinates, and every
  piece of cross-agent communication flows through the orchestrator's
  pane by construction.

  Key invariants:
    - Workers have no knowledge of the orchestrator. The callback
      contract (<<TASK_DONE>> → [WORKER_RESULT]) is enforced at the
      code level by dispatch_loop, not by worker prompts.
    - The orchestrator is the single decision-maker for "who does what
      next". Python code provides it with a workflow playbook, a worker
      roster (with capability descriptions), and a skill catalogue —
      but never makes the decision itself.
    - Group lifecycle operations (start/stop/resume/clear) are
      supervisor-owned and parallelise across agents via asyncio.gather.
  ```

- This is pure documentation; no code change beyond the docstring.
- Purpose: the next maintainer who reads supervisor.py sees the mental
  model explicitly and doesn't accidentally reintroduce peer-agent
  thinking (as happened in REQ-016 F-05).

### F-04 Chinese README (`README.zh-CN.md`)

- Main flow: create `README.zh-CN.md` at the repo root containing a
  faithful Chinese translation of the current `README.md`. All sections
  preserved in the same order:
  - 项目概述 (What is Agent Orchestra)
  - 为什么存在 (Why does it exist)
  - 架构总览 (Architecture at a glance)
  - 依赖要求 (Requirements)
  - 安装 (Installation)
  - 快速开始 (Quickstart)
  - 交互模型 (Interaction model)
  - Orchestrator 协议 (The orchestrator protocol)
  - 内置工作流 (Built-in workflows)
  - 技能目录 (Skill catalogue)
  - 键盘快捷键 (Keyboard shortcuts)
  - 配置 (Configuration)
  - 数据目录结构 (Data directory layout)
  - 开发 (Development)
  - 项目历史 (Project history)
  - 已知限制 (Known limitations)
- Both READMEs gain a language bar at the top:
  - `README.md`: `[English](README.md) | [简体中文](README.zh-CN.md)`
  - `README.zh-CN.md`: `[English](README.md) | [简体中文](README.zh-CN.md)`
- Technical terms that belong to the protocol (`<<DISPATCH>>`,
  `<<TASK_DONE>>`, `[WORKER_RESULT]`, `/req-*` skill names) are kept in
  their original form — only the surrounding prose is translated.
- Edge cases:
  - ASCII diagrams are preserved verbatim (they are language-neutral).
  - Mermaid diagrams are preserved verbatim (same).
  - The REQ history table stays in English IDs but gets Chinese
    summaries for readability.

### F-05 Tests for the above

- `tests/test_supervisor_concurrency.py` gains a `TestClearAllConcurrent`
  class:
  - Create 2 groups × 3 sessions each (6 total sessions).
  - `SlowFakeSessionManager` with 100 ms per-call sleep.
  - Call `supervisor.clear_all()`.
  - Assert total wall time < 400 ms (sequential would be 600 ms).
  - Assert all 6 sessions called `stop_agent_session`.
  - Assert one failing session does not block the others.
- `tests/test_workflows.py` gains a `TestRoleCapabilities` class:
  - `ROLE_CAPABILITIES` contains every non-custom AgentRole.
  - `render_roster(roster)` (default) contains each capability
    description as a separate line prefixed with `→`.
  - `render_roster(roster, include_capabilities=False)` produces the
    old plain format.
- `tests/test_session_manager.py` gets one new assertion that the
  rendered orchestrator prompt contains at least one capability line
  (regression guard against accidentally turning the rendering off in
  `_render_orchestrator_prompt`).

## 4. Non-functional Requirements

- All 454 existing tests must continue to pass.
- New tests: roughly 6 covering the three fixes.
- No new runtime dependencies.
- No schema changes.
- No template version bump (the orchestrator template text itself does
  not change — only the substituted roster contents change, and those
  are rendered at session start from live data).

## 5. Out of Scope

- **Auto-restart of degraded workers** by the supervisor — a real
  orchestrator-centric enhancement (the assistant restarts crashed
  subordinates before the orchestrator needs them again), but complex
  to implement safely. Deferred to a future REQ.
- **Parallel dispatch** of multiple workers from the orchestrator —
  still sequential per REQ-012 v2 decision.
- **Multiple active orchestrators** — the supervisor still supports
  only one `_active_group_id` at a time. Multi-group concurrency is a
  structural change, not a refinement.
- **Dynamic worker role creation** — AgentRole enum changes still
  require a source-code edit + template version bump.
- **English → Chinese README auto-sync** — the two files are maintained
  in parallel by hand. Future drift is a known accepted cost.

## 6. Acceptance Criteria

| ID | Feature | Condition | Expected Result |
|:---|:---|:---|:---|
| AC-01 | F-01 | `clear_all` with 2 groups × 3 sessions, each with 100 ms simulated stop | Total elapsed < 400 ms (sequential would be 600 ms) |
| AC-02 | F-01 | One session raises during stop | Exception is logged, other 5 sessions complete, `clear_all` returns normally |
| AC-03 | F-01 | Zero groups | `clear_all` completes in <50 ms with no errors |
| AC-04 | F-02 | `workflows.ROLE_CAPABILITIES` | Contains entries for at least PM / TD / Dev / Tester / User |
| AC-05 | F-02 | `render_roster(roster)` (default) | Each line of the output contains a capability description after the role/name/pane part |
| AC-06 | F-02 | `render_roster(roster, include_capabilities=False)` | Produces the legacy format without `→` lines |
| AC-07 | F-02 | `session_manager._render_orchestrator_prompt` | Resulting text contains at least one capability description from ROLE_CAPABILITIES |
| AC-08 | F-03 | `backend/supervisor.py` module docstring | Contains the phrase "executive assistant" or equivalent orchestrator-centric framing and mentions the "no peer-agent bus" invariant |
| AC-09 | F-04 | `README.zh-CN.md` | Exists at the repo root |
| AC-10 | F-04 | `README.zh-CN.md` | Contains all top-level sections from `README.md` (section-by-section presence check) |
| AC-11 | F-04 | `README.md` and `README.zh-CN.md` | Both start with a language bar linking to each other |
| AC-12 | Regression | Run pytest | All previous tests + 6 new tests pass; total ≥ 460 |

## 7. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-08 | Initial — (F-01) parallelise `Supervisor.clear_all` via `asyncio.gather`; (F-02) new `ROLE_CAPABILITIES` constant and enhanced `render_roster` with per-subordinate capability descriptions surfaced in the orchestrator's system prompt; (F-03) rewrite supervisor module docstring to document the orchestrator-as-parent mental model explicitly; (F-04) full Chinese translation `README.zh-CN.md` with language bar linking; (F-05) 6 new tests covering clear_all parallelism, ROLE_CAPABILITIES coverage, and rendered prompt regression guard. | ALL | User flagged that parallel clear_all was missed in REQ-016, asked for Mandarin README, and requested an orchestrator-centric refinement pass on the overall design. |
