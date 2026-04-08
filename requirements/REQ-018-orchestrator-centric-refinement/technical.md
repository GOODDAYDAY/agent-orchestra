# REQ-018 Technical Design — Orchestrator-Centric Refinement

> Status: Completed
> Requirement: requirement.md
> Created: 2026-04-08
> Updated: 2026-04-08

## 1. Technology Stack

| Area | Technology | Rationale |
|:---|:---|:---|
| `clear_all` parallelism | `asyncio.gather(return_exceptions=True)` | Consistent with REQ-016 F-03's pattern for start/stop/resume_group |
| ROLE_CAPABILITIES constant | Frozen dict in `backend/workflows.py` | Lives next to AVAILABLE_SKILLS so the two orchestrator-context data sources are co-located |
| Roster rendering | Extended signature on `workflows.render_roster` | Backward-compatible default; legacy format still reachable via the flag |
| Supervisor docstring | Pure prose edit | No code change beyond the module-level docstring |
| Chinese README | New file + link bar on both READMEs | No build-time generation; maintained by hand |

## 2. Design Principles

- **Minimal diff** — this REQ is three narrow fixes, not a rewrite. Every
  change is localised to a single function or a single constant.
- **Orchestrator-centric framing** — wherever a prose description
  mentions workers, it describes them as "subordinates of the
  orchestrator", never as "peer agents". This guards against the
  REQ-016 F-05 mistake class.
- **Backward compatibility for external callers** — the new
  `render_roster` flag defaults to `True`, but existing code that
  already passed exactly the positional argument keeps working. Any
  caller that wanted the old format can opt out explicitly.

## 3. Module Design

### 3.1 `backend/supervisor.py` — `clear_all` parallelisation

```python
async def clear_all(self) -> None:
    """Stop every session across every group, then wipe runtime state.

    REQ-018 F-01: runs stop_agent_session calls for all sessions in
    parallel via asyncio.gather(return_exceptions=True). Per-session
    exceptions are isolated and logged individually; no broken session
    can block the others. Consistent with the pattern used by
    start_group / stop_group / resume_group after REQ-016 F-03.
    """
    import shutil

    await self._cancel_dispatch_loop()

    # 1. Collect every session across every group into one flat list
    all_sessions: list[Session] = []
    for group in await self._repo.get_groups():
        all_sessions.extend(await self._repo.get_sessions_for_group(group.id))

    # 2. Stop them in parallel
    if all_sessions:
        results = await asyncio.gather(
            *[self._sm.stop_agent_session(s) for s in all_sessions],
            return_exceptions=True,
        )
        for session, result in zip(all_sessions, results):
            if isinstance(result, Exception):
                logger.exception(
                    "clear_all: error stopping session %s: %s",
                    session.id, result,
                )
            else:
                self._app.post_message(AgentStatusChanged(
                    agent_id=session.agent_id, status=AgentStatus.stopped
                ))

    self._active_group_id = None

    # 3. Wipe runtime state and temp dir (unchanged)
    await self._repo.clear_all_runtime_state()
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
        TEMP_DIR.mkdir(exist_ok=True)
```

**Critical difference from the old code**:

- Sessions are collected first, stops run in parallel afterwards
- `return_exceptions=True` prevents one failing session from cancelling
  the sibling stops
- `AgentStatusChanged` messages still fire for successful stops (the
  old code didn't emit them during clear_all — this is a bonus UX fix)

### 3.2 `backend/workflows.py` — `ROLE_CAPABILITIES` + `render_roster` extension

```python
# REQ-018 F-02: per-role capability descriptions surfaced to the
# orchestrator in its system prompt, so it makes informed routing
# decisions. Pure data — to add a new role, append a dict entry.
ROLE_CAPABILITIES: dict[AgentRole, str] = {
    AgentRole.product_manager:
        "Expand rough requirements into complete spec documents; "
        "clarify target users, scenarios, functional + non-functional "
        "requirements, acceptance criteria. Does not write code.",
    AgentRole.tech_director:
        "Produce technical designs and review architecture; pick the "
        "right abstractions, module boundaries, data models, and "
        "highlight risks. Does not write production code.",
    AgentRole.developer:
        "Implement code following a technical design, run commands, "
        "write and run tests, invoke any /req-* skill the orchestrator "
        "specifies. The primary execution hand.",
    AgentRole.tester:
        "Run test suites, design edge-case coverage, report failures. "
        "Emits <<TESTS_FAILED>> before <<TASK_DONE>> when any test "
        "fails so the orchestrator can loop back to developer.",
    AgentRole.user:
        "Human-facing acceptance review. When a real operator attaches "
        "to this pane, control transfers to the human. Otherwise, "
        "plays a user persona for verification.",
    AgentRole.custom:
        "Free-form role — capabilities defined by whatever system "
        "prompt the operator gave when creating this agent.",
}


def render_roster(
    roster: list[tuple[AgentRole, str, str]],
    include_capabilities: bool = True,
) -> str:
    """Render the worker roster for the `{{WORKER_ROSTER}}` placeholder
    in the orchestrator's system prompt.

    REQ-018 F-02: when `include_capabilities=True` (default), each
    roster line is followed by a `→ <capability>` line pulled from
    ROLE_CAPABILITIES. This gives the orchestrator explicit routing
    guidance per subordinate. Legacy format (name + pane only) is
    still available via `include_capabilities=False`.
    """
    lines: list[str] = []
    for role, name, pane_id in roster:
        lines.append(f"  - {role.value}: {name} (pane {pane_id})")
        if include_capabilities:
            capability = ROLE_CAPABILITIES.get(role)
            if capability:
                lines.append(f"    → {capability}")
    return "\n".join(lines)
```

**Note on degenerate cases:**

- If a role is missing from `ROLE_CAPABILITIES`, the line is omitted
  (no `→` line printed). This is graceful degradation for future
  roles that haven't been documented yet.
- If `include_capabilities=False`, the output matches the pre-REQ-018
  format exactly (so tests that assert the legacy format can opt out).

**Caller changes:**

- `session_manager._render_orchestrator_prompt` continues to call
  `render_roster(roster)` without arguments — picks up the default
  `True`, orchestrator prompts now carry capabilities.
- No other caller in the codebase.

### 3.3 `backend/supervisor.py` — module docstring

The top of `supervisor.py` gets a rewritten docstring:

```python
"""Supervisor — the executive assistant to the Orchestrator agent.

Responsibilities:
    * Group lifecycle: start / stop / resume / clear, all parallelised
      across agents via asyncio.gather(return_exceptions=True).
    * The orchestrator dispatch loop: polls the orchestrator pane,
      parses <<DISPATCH ...>> blocks, routes their text to the target
      worker pane, and — crucially — injects the worker's output back
      into the orchestrator pane as [WORKER_RESULT ...].
    * Operator interventions: force_advance, abort_workflow, pause_agent,
      resume_agent.

Mental model (REQ-018):
    The Supervisor is NOT a peer-agent message bus. Workers do not
    communicate with each other directly; the orchestrator is the
    *single* cross-agent transit point. Every cross-pane piece of
    information flows: worker → supervisor → orchestrator → supervisor
    → next worker. The orchestrator is the parent; workers are its
    subordinates; the supervisor is the executive assistant that
    enforces this topology at the code level.

Key invariants:
    1. Workers have no knowledge of the orchestrator. The callback
       contract (<<TASK_DONE>> → [WORKER_RESULT]) is enforced by the
       dispatch_loop, not by anything in the worker prompts.
    2. The orchestrator is the single decision-maker for "who does
       what next". Python code provides it with a workflow playbook
       (render_for_orchestrator), a worker roster with capability
       descriptions (render_roster + ROLE_CAPABILITIES), and a skill
       catalogue (render_skill_catalogue) — but Python code never
       makes the decision itself.
    3. Group lifecycle operations (start_group, stop_group,
       resume_group, clear_all) are supervisor-owned and parallelise
       across agents. One failing agent never blocks its siblings.
    4. The orchestrator's dispatch_loop uses content-signature dedup
       (REQ-014 F-01), self-closing dispatch parsing (REQ-016 F-04a),
       and three-layer completion detection (REQ-012 v2 F-09).
"""
```

This docstring is the canonical reference for future maintainers
reading supervisor.py for the first time. It codifies the
orchestrator-centric architecture verbally alongside the code.

### 3.4 `README.zh-CN.md` — Chinese translation

A new file at the repo root. Structure and section order mirror
`README.md` exactly. The top of both README files gets a language bar:

```markdown
[English](README.md) | [简体中文](README.zh-CN.md)

---
```

**Translation policy:**

- **Protocol markers kept in English**: `<<DISPATCH>>`, `<<TASK_DONE>>`,
  `[WORKER_RESULT]`, `<<WORKFLOW_COMPLETE>>`, `<<WORKFLOW_ABORT>>`,
  `<<TESTS_FAILED>>`, `[PLATFORM_ERROR]`, `[WORKER_ERROR]`,
  `[PLATFORM_STALL]`, `/req-*` skill names — all unchanged.
- **Command examples kept in English**: `uv sync`, `bash scripts/start.sh`,
  `uv run pytest -q`, etc.
- **ASCII and Mermaid diagrams** left in place verbatim (language-neutral).
- **Code snippets** untranslated (code is code).
- **Prose sections** fully translated to idiomatic Mandarin.
- **REQ history table** retains the REQ-001..018 IDs but summaries are
  translated.
- **Known limitations** translated with the same bluntness as the
  English original.

## 4. Data Model

No schema changes.

## 5. API Design

Internal API changes:

| API | Change |
|:---|:---|
| `Supervisor.clear_all` | Rewritten body — same signature and semantics, just parallelised |
| `workflows.ROLE_CAPABILITIES` | **New** frozen `dict[AgentRole, str]` |
| `workflows.render_roster(roster, include_capabilities=True)` | **New parameter**, default True → orchestrator prompts now include capability lines. Backward-compatible when called with the single positional arg |

## 6. Key Flows

### 6.1 Clear All (parallelised)

1. Operator clicks Clear All in the TUI
2. App pushes a `ConfirmDeleteDialog`; on confirmation, calls `supervisor.clear_all()`
3. `clear_all` cancels the dispatch loop task
4. Collects every session across every group into `all_sessions`
5. Runs `asyncio.gather(*[stop_agent_session(s) for s in all_sessions], return_exceptions=True)`
6. Each stop runs in parallel; tmux pane kills happen concurrently
7. Exceptions are logged per-session; successful stops emit AgentStatusChanged messages
8. After gather returns, `_active_group_id = None`, `clear_all_runtime_state`, `rmtree(TEMP_DIR)`

### 6.2 Orchestrator receives capability-rich roster

1. `start_group` completes worker startup, calls `start_agent_session(orchestrator)`
2. `_render_orchestrator_prompt` fetches orchestrator template, workflow, roster
3. Builds `roster = [(role, agent_name, pane_id), ...]` for every worker with a live session
4. Substitutes `{{WORKFLOW_DEFINITION}}` with `render_for_orchestrator(workflow, roster)`
5. **Substitutes `{{WORKER_ROSTER}}` with `render_roster(roster)` — now includes capability lines from ROLE_CAPABILITIES**
6. Substitutes `{{SKILL_CATALOGUE}}` with `render_skill_catalogue()`
7. Substitutes `{{COMPLETION_MARKER}}` with `<<TASK_DONE>>`
8. Writes rendered prompt to tmp file, launches Claude CLI with it

The orchestrator's LLM reads the enriched roster and sees explicit
guidance about each subordinate's capabilities when choosing dispatch
targets.

## 7. Shared Modules & Reuse Strategy

| Shared | Used by | Notes |
|:---|:---|:---|
| `asyncio.gather(return_exceptions=True)` | clear_all (new), start/stop/resume_group (existing) | Same pattern across all four lifecycle operations |
| `ROLE_CAPABILITIES` dict | `render_roster` (and future worker-aware UIs) | Co-located with AVAILABLE_SKILLS so all orchestrator-context data lives in one module |
| `workflows.render_roster` | `session_manager._render_orchestrator_prompt` | Only caller; backward-compatible signature change |
| Language bar markup | `README.md` top, `README.zh-CN.md` top | Manually maintained cross-links |

## 8. Risks & Notes

| Risk | Mitigation |
|:---|:---|
| `asyncio.gather` with very many sessions hits the tmux rate limit | `return_exceptions=True` plus logger.exception isolates failures; the test fixture stress-tests with 6 sessions which is realistic for a single group |
| Capability descriptions could become stale as roles evolve | ROLE_CAPABILITIES is plain data; updates are one-line diffs |
| Chinese README drifts from English README | Accepted cost; language bar makes the discrepancy visible to readers who notice |
| New roster rendering changes existing test expectations | The only existing session_manager test that inspected rendered roster just checked for presence of specific name/pane strings; that still passes. New tests cover the capability line explicitly. |
| `include_capabilities=True` adds ~100 characters per worker to the orchestrator prompt | Negligible relative to the full prompt (thousands of characters) |

## 9. Test Strategy

| Test file | New cases |
|:---|:---|
| `tests/test_supervisor_concurrency.py` | `TestClearAllConcurrent`: 3 cases covering parallelism timing, exception isolation, and zero-group no-op |
| `tests/test_workflows.py` | `TestRoleCapabilities`: 3 cases covering constant coverage, default render with capabilities, and opt-out render |
| `tests/test_session_manager.py` | 1 case asserting the final rendered orchestrator prompt contains at least one capability fragment |

All new tests run under `uv run pytest` with no new fixtures or plugins.

## 10. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-08 | Initial — (1) `Supervisor.clear_all` parallelised via asyncio.gather; (2) new `ROLE_CAPABILITIES` dict + `render_roster(include_capabilities=True)` flag, orchestrator prompt now surfaces per-subordinate capability descriptions; (3) supervisor module docstring rewritten to explicitly document the orchestrator-as-parent / no-peer-bus mental model; (4) `README.zh-CN.md` created as a full Chinese translation with a language bar linking both READMEs; (5) 7 new tests covering clear_all parallelism, ROLE_CAPABILITIES coverage, and orchestrator prompt regression guard. | ALL | REQ-016 missed clear_all in its parallelism pass; orchestrator lacked capability guidance in its rendered roster; user needed Chinese-language documentation |
