# REQ-014 Technical Design — Quality Hardening

> Status: Completed
> Requirement: requirement.md
> Created: 2026-04-08
> Updated: 2026-04-08

## 1. Technology Stack

| Module | Technology | Rationale |
|:---|:---|:---|
| Bug fixes | In-place edits to existing REQ-012 v2 modules | Minimal surface area change |
| Integration test scaffolding | `FakeSessionManager` class co-located with integration test file | Hermetic; no subprocess, no tmux |
| Time control | `asyncio.get_event_loop().time()` reads intercepted via a monkeypatched clock holder on the supervisor instance | Deterministic silence/stall timing without `asyncio.sleep` |

## 2. Design Principles

- **Fix root causes, not symptoms** — the scrollback bug is fixed by removing the byte-offset mechanism entirely, not by bumping the buffer size
- **Tests exercise real code** — integration tests instantiate the real `Supervisor` class with a real `Repository`; only the `SessionManager` tmux interface is faked
- **Fake SessionManager mirrors real API** — same method signatures so the integration tests will surface any future API drift as a type/attribute error

## 3. Architecture Overview

```
tests/test_dispatch_integration.py
   │
   ├── FakeSessionManager
   │     - records send_keys calls into a list
   │     - replays scripted capture_pane_full responses from a per-pane deque
   │     - pane_exists returns True by default, overridable
   │
   ├── in-memory Repository (tmp_path SQLite)
   │     - real schema, real templates, real workflow linkage
   │
   ├── Supervisor (real)
   │     - dispatch_loop driven manually one iteration at a time
   │     - timing control via monkeypatching asyncio.get_event_loop().time()
   │
   └── pytest_asyncio fixtures
         - scripted_orch_pane: deque of orchestrator pane states
         - scripted_worker_pane: deque of worker pane states
```

The key trick for integration: instead of spawning `dispatch_loop` as a real asyncio task, the test iterates manually by extracting the loop body into a private helper (`_run_one_dispatch_iteration`) that one test call can invoke and inspect. This avoids real-time sleeps and keeps tests under 1 ms each.

Actually — re-thinking this: extracting the loop body into a helper changes the production code to accommodate tests. Instead, the fake SessionManager controls `capture_pane_full` and the supervisor drives naturally; we use a very short `DISPATCH_POLL_INTERVAL` (0.001) and a bounded iteration count. Cleaner.

## 4. Module Design

### 4.1 `backend/supervisor.py` — Scrollback Offset Fix

Replace the `_consumed_offset` mechanism with:
- `_last_dispatch_raw: Optional[str]` on the Supervisor instance
- `_workflow_ended: bool` flag

In `_dispatch_loop`:
```python
if in_flight is None:
    pane_text = await self._sm.capture_pane_full(orch_pane)
    if not pane_text:
        continue
    if self._workflow_ended:
        return
    if orch_mod.is_workflow_complete(pane_text):
        self._workflow_ended = True
        self._app.post_message(WorkflowCompleted(group_id=group_id))
        return
    abort_reason = orch_mod.is_workflow_abort(pane_text)
    if abort_reason:
        self._workflow_ended = True
        self._app.post_message(WorkflowAborted(group_id=group_id, reason=abort_reason))
        return
    dispatch = orch_mod.parse_latest_dispatch(pane_text)  # no offset parameter
    if dispatch is None or dispatch.raw == self._last_dispatch_raw:
        continue
    # ... validate + send
    self._last_dispatch_raw = dispatch.raw
```

Robustness: signature dedup handles scrollback truncation because `parse_latest_dispatch` always searches the full captured text. The orchestrator's own output is unique per turn (LLM output is non-deterministic), so `_last_dispatch_raw` is a reliable key.

### 4.2 `backend/supervisor.py` — Worker Pane Crash Detection

In the `in_flight` branch, before calling `detect_completion`:
```python
if not await self._sm.pane_exists(in_flight.worker_pane):
    await self._sm.send_keys(
        orch_pane,
        f'[WORKER_ERROR role="{in_flight.dispatch.role}" reason="pane vanished"]',
    )
    in_flight = None
    self._stall_notified = False
    continue
```

### 4.3 `backend/supervisor.py` — Test Failure Retry Soft Cap

Fetch the tester step's `max_retries` from the active workflow at loop start:
```python
tester_max_retries = 0
if workflow:
    for step in workflow.steps:
        if step.on_failure_marker == "<<TESTS_FAILED>>":
            tester_max_retries = step.max_retries
            break
```

Then in the test-failure branch:
```python
if result.tests_failed and in_flight.dispatch.role == AgentRole.tester.value:
    self._dev_tester_retries += 1
    if tester_max_retries and self._dev_tester_retries > tester_max_retries:
        logger.warning(
            "dispatch_loop: tester failure retry #%d exceeds soft cap %d",
            self._dev_tester_retries, tester_max_retries,
        )
```

### 4.4 `backend/session_manager.py` — Tmp File Cleanup

In the orchestrator branch of `start_agent_session`, after the Claude CLI command is sent:
```python
# Schedule cleanup of the rendered orchestrator prompt tmp file
asyncio.get_running_loop().call_later(30.0, _cleanup_temp, tmp_prompt)
```

The 30-second delay gives Claude CLI time to read the file during startup before we try to delete it.

### 4.5 Dead Code Removal

- `backend/supervisor.py`: delete `PaneOutputRefresh` Textual message class (never posted).
- `frontend/app.py`: delete `_check_mcp_alive` method and its single call in `_do_suspend_attach`.
- `frontend/app.py`: delete the stale `"Entry point for F-01–F-07"` docstring reference.
- `frontend/app.py`: `AgentRole` import was added during v2 but never referenced directly — verify and remove if unused.

### 4.6 `tests/test_dispatch_integration.py` — New File

**FakeSessionManager** — matches `SessionManager`'s public API exactly so the integration tests will break if the real API changes:

```python
class FakeSessionManager:
    def __init__(self):
        self.send_keys_calls: list[tuple[str, str]] = []  # (pane_id, text)
        self.pane_scripts: dict[str, collections.deque] = {}
        self.pane_exists_map: dict[str, bool] = {}
        self._default_pane_text = ""

    async def send_keys(self, pane_id: str, text: str) -> None:
        self.send_keys_calls.append((pane_id, text))
        # If the injected text is addressed to the orchestrator pane
        # (e.g. [WORKER_RESULT...]), append it to the orch pane script
        # so the next capture_pane_full sees it.

    async def capture_pane_full(self, pane_id: str, history_lines: int = 2000) -> str:
        if pane_id in self.pane_scripts and self.pane_scripts[pane_id]:
            return self.pane_scripts[pane_id].popleft()
        return self._pane_state.get(pane_id, "")

    async def capture_pane_output(self, pane_id: str, lines: int = 50) -> str:
        return await self.capture_pane_full(pane_id)

    async def pane_exists(self, pane_id: str) -> bool:
        return self.pane_exists_map.get(pane_id, True)

    async def start_agent_session(self, agent, group_id, resume_session_id=None):
        # create a fake Session, save to repo
        ...

    async def stop_agent_session(self, session) -> None:
        pass
```

**Test fixture**: `dispatch_scenario` builds a complete group (orchestrator + 5 workers) in the in-memory repo, creates fake sessions for each agent with distinct pane_ids, and returns a `(supervisor, fake_sm, repo, group_id, app)` tuple.

**Driving the dispatch loop**: each test appends scripted pane content to `fake_sm.pane_scripts[orch_pane_id]` / `pane_scripts[worker_pane_id]`, then:
```python
task = asyncio.create_task(supervisor._dispatch_loop(group_id, orch_agent))
await asyncio.sleep(0.05)  # allow a few iterations (DISPATCH_POLL_INTERVAL=0.001 in tests)
# inspect fake_sm.send_keys_calls
task.cancel()
```

To make tests fast and deterministic, we monkeypatch `DISPATCH_POLL_INTERVAL` to 0.001 via `shared.config`. Silence / stall timeouts are either bypassed (by always providing a completion signal in the script) or tested by monkeypatching `asyncio.get_event_loop().time()` to return fake advancing values.

**Fake app** (for supervisor `_app` parameter): a minimal stub that captures posted Textual messages into a list.

```python
class FakeApp:
    def __init__(self):
        self.messages: list = []
    def post_message(self, msg) -> None:
        self.messages.append(msg)
```

**Time control**: For stall / silence tests, the integration test uses a `FakeClock` class with `.now` attribute and `.advance(seconds)` method, patched into `supervisor.asyncio.get_event_loop().time`. Simpler: just pass tiny timeouts (e.g. `WORKER_SILENCE_TIMEOUT=0.1`, `ORCHESTRATOR_STALL_TIMEOUT=0.2`) via monkeypatching the module-level constants used in the supervisor.

### 4.7 `tests/test_session_manager.py` — New File

Covers the parts of `SessionManager` that don't require tmux:
- `_sanitize_payload` (static method, pure)
- `_render_orchestrator_prompt` (async, requires repo fixture but no tmux)

No fake tmux needed — we never call the tmux helpers.

### 4.8 Expanded Test Files

`test_orchestrator.py`, `test_workflows.py`, `test_repository.py`, `test_models.py` each gain new test cases (§F-06 in requirement.md).

## 5. Data Model

No schema changes.

## 6. API Design

No external API changes. Internal API changes:
- `Supervisor` no longer has `_consumed_offset`; adds `_last_dispatch_raw` and `_workflow_ended`.
- `PaneOutputRefresh` message class removed (was never posted).

## 7. Key Flows

**Scrollback resilience**:
1. Orchestrator emits dispatch D1 at offset 1MB in the pane
2. Supervisor processes D1, stores `_last_dispatch_raw = D1.raw`
3. Pane scrollback truncates, dropping the oldest content
4. Next capture_pane_full returns text where D1 is now at offset 500KB
5. `parse_latest_dispatch` returns D1 (still the latest in the captured text)
6. `D1.raw == _last_dispatch_raw` → skip, no duplicate send
7. Orchestrator emits D2
8. Next capture_pane_full returns D1 and D2
9. `parse_latest_dispatch` returns D2 (the latest)
10. `D2.raw != _last_dispatch_raw` → process D2, update `_last_dispatch_raw = D2.raw`

**Worker pane crash**:
1. Dispatch sent to worker
2. Worker pane killed externally (e.g. by user)
3. Next iteration: `pane_exists(worker_pane) == False`
4. `[WORKER_ERROR role="X" reason="pane vanished"]` injected into orchestrator
5. `in_flight = None`; orchestrator reads the error on next turn and decides next action

## 8. Shared Modules & Reuse Strategy

| Shared | Used By | Notes |
|:---|:---|:---|
| `orch_mod.parse_latest_dispatch` / `is_workflow_complete` / `is_workflow_abort` | supervisor (unchanged call sites; offset parameter dropped) | defaults to `after_offset=0` already |
| `_cleanup_temp` helper in session_manager | orchestrator tmp file scheduling | reused verbatim |
| `pane_exists` on SessionManager | supervisor (new call) | existing method |

## 9. Risks & Notes

| Risk | Mitigation |
|:---|:---|
| Signature dedup fails if the LLM emits the same dispatch twice legitimately | Accepted — LLM output is non-deterministic; duplicates are extremely rare and would be harmless in practice |
| Monkeypatching `DISPATCH_POLL_INTERVAL` affects other tests in the same run | Use `monkeypatch.setattr` with pytest fixture auto-cleanup |
| FakeSessionManager diverges from real SessionManager API | Mitigated: both classes share the same method names; any future API drift will break the fake import/use |
| 30s delayed cleanup leaks if the app exits within 30s | Accepted — the tmp file is in `.agent_management/tmp/` and will be cleaned up on next `clear_all` or destructive reset |

## 10. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-08 | Initial version — bug fixes (scrollback offset, tmp file leak, pane crash detection, retry soft cap), dead code removal, and ~55 new tests including a full integration suite with FakeSessionManager | ALL | REQ-014 quality hardening pass |
