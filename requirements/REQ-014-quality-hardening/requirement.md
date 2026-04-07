# REQ-014 Quality Hardening for REQ-012 v2

> Status: Completed
> Created: 2026-04-08
> Updated: 2026-04-08

## 1. Background

REQ-012 v2 shipped the LLM-Orchestrator pivot (6th agent drives PM/TD/Dev/Tester/User via tmux dispatch). The implementation compiles, passes 90 unit tests, and runs under `--show-config`. Before the code is left unattended, the user requested an adversarial self-review and a significantly expanded test suite — specifically including **integration tests** exercising the supervisor dispatch loop end-to-end with fakes, not just per-function unit tests.

This REQ is a post-hoc quality hardening pass. It is not a new feature; it is a concentrated effort to catch bugs I missed the first time around and bolt on coverage so regressions become visible next time the code is touched.

## 2. Target Users & Scenarios

- **Future maintainer** (user / myself) needs confidence that the REQ-012 v2 dispatch loop behaves correctly before running it against live Claude CLI workers where bugs would be expensive to diagnose.
- **Regression detection**: when REQ-015+ modifies the dispatch path, the expanded test suite should fail loudly on breakage rather than silently permit subtle behavioural drift.

## 3. Functional Requirements

### F-01 Bug Fix: Scrollback Offset Wraparound

- Main flow: replace the byte-offset bookkeeping in `Supervisor.dispatch_loop` with content-signature deduplication. The loop remembers the raw text of the most recently processed dispatch and of the most recently seen workflow-end marker; if the same content reappears in a subsequent `capture_pane_full` result it is skipped as already-processed.
- Rationale: `tmux capture-pane -S -<N>` truncates old history when the pane scrollback exceeds the buffer. With the v1 byte-offset approach, that truncation made stored offsets point past the start of the captured text and all subsequent dispatches were silently dropped. Signature dedup is robust to any truncation.
- Error handling: if the orchestrator emits an identical dispatch block twice (which should not happen but could under LLM retry), the second occurrence is ignored.
- Edge cases: workflow-complete and workflow-abort markers are tracked via a one-shot boolean in the supervisor so they also survive truncation.

### F-02 Bug Fix: Orchestrator Prompt Tmp File Leak

- Main flow: `session_manager.start_agent_session` currently writes the rendered orchestrator prompt to `TEMP_DIR/orch_prompt_<id>.txt` but never deletes it. Add a delayed (30-second) `_cleanup_temp` call via `asyncio.get_running_loop().call_later` after Claude CLI has had time to read the file.
- Error handling: cleanup is best-effort (`Path.unlink(missing_ok=True)`).
- Edge cases: covered by the existing `_cleanup_temp` helper used for long-payload `send_keys` temp files.

### F-03 Bug Fix: Worker Pane Crash vs Silence Timeout

- Main flow: before calling `orchestrator.detect_completion` on the worker pane, verify the pane still exists via `SessionManager.pane_exists`. If the pane is gone, inject `[WORKER_ERROR role="X" reason="pane vanished"]` into the orchestrator pane and clear `in_flight` without waiting for the 60-second silence timeout.
- Error handling: pane existence check is idempotent and cheap.
- Edge cases: the pane may vanish between the existence check and the subsequent capture; that is fine — the next iteration will observe the disappearance.

### F-04 Medium Fix: Test Failure Loop Soft Cap

- Main flow: when the Tester's completion result contains `<<TESTS_FAILED>>`, the supervisor increments `_dev_tester_retries`. If the counter exceeds the workflow step's `max_retries` value, the supervisor logs a warning. Hard enforcement still lives in the orchestrator's prompt (the platform does not forcibly abort).
- Error handling: the workflow step's `max_retries` is looked up at loop start and stored; defaults to 3 if not present.
- Edge cases: workflows without a tester step never trigger this path.

### F-05 Dead Code Removal

- Main flow: delete the unused `PaneOutputRefresh` Textual message class from `supervisor.py` and the now-no-op `_check_mcp_alive` method (plus its callsite) from `app.py`. Also delete the stale `"F-01–F-07"` comment and clean up an unused `AgentRole` import in `app.py`.
- Rationale: REQ-012 v2 Stage 5 cleanup missed these. Keeping dead code confuses future readers.

### F-06 Unit Test Expansion

- Add the following test cases:
  - `tests/test_orchestrator.py`: trailing-backslash edge case in `_unescape_text`; multiple consecutive backslashes; empty dispatch text; dispatch with only whitespace inside text; unicode text; workflow complete intermixed with historical dispatches; abort with escaped quotes; completion marker at end of pane without trailing newline.
  - `tests/test_workflows.py`: per-step `max_retries` lookup helper; `required_roles` matches `Step.role` set; rendering stability (idempotent); failure-loop target index validity against step count.
  - `tests/test_repository.py`: agent pause toggle round-trip; session status transition `not_started → starting → active`; group member ordering; orchestrator template placeholder integrity (contains all three placeholders); delete agent cascades to sessions; `clear_all_runtime_state` resets statuses.
  - `tests/test_models.py`: Session claude_session_id auto-assignment; Group workflow_id explicit vs default.

### F-07 Integration Test — Supervisor Dispatch Loop with Fake SessionManager

- Main flow: create a new file `tests/test_dispatch_integration.py` with a fake `SessionManager` that records `send_keys` calls and replays scripted `capture_pane_full` responses. Instantiate a real `Supervisor` against a real in-memory `Repository` with scripted orchestrator + worker pane output, and drive a complete workflow run end to end.
- Tests to include:
  - `test_happy_path_standard_workflow` — orchestrator dispatches PM → worker responds with `<<TASK_DONE>>` → supervisor injects WORKER_RESULT → orchestrator dispatches TD → ... → WORKFLOW_COMPLETE.
  - `test_unknown_role_dispatch` — orchestrator dispatches `role="marketing"` → supervisor injects `[PLATFORM_ERROR: unknown role ...]` → orchestrator recovers.
  - `test_forbidden_marker_in_dispatch_text` — orchestrator dispatches text containing `<<TASK_DONE>>` → supervisor rejects.
  - `test_worker_silence_completion` — worker never emits marker; after silence timeout elapses (fake time), supervisor extracts artifact and advances.
  - `test_worker_pane_vanishes_mid_flight` — pane_exists returns False during poll → supervisor injects WORKER_ERROR and clears in_flight.
  - `test_stall_force_advance` — no completion within stall timeout → supervisor posts `WorkflowStalled` → test calls `supervisor.force_advance` → dispatch advances via synthetic silence completion.
  - `test_stall_abort` — `supervisor.abort_workflow` ends the loop with `WorkflowAborted`.
  - `test_workflow_complete_marker` — orchestrator emits `<<WORKFLOW_COMPLETE>>` → loop exits cleanly with `WorkflowCompleted` message posted.
  - `test_scrollback_truncation_resilience` — fake capture_pane_full returns decreasing-length strings (simulating truncation); supervisor still processes new dispatches correctly via signature dedup.
  - `test_duplicate_dispatch_deduped` — same dispatch raw content appears in two consecutive captures; supervisor sends to worker only once.

### F-08 Integration Test — Session Manager Orchestrator Prompt Rendering

- Main flow: new file `tests/test_session_manager.py` exercising the pure helpers and the `_render_orchestrator_prompt` path with a real in-memory repository.
- Tests to include:
  - `test_sanitize_payload_strips_control_chars` — NUL, ESC sequences removed.
  - `test_sanitize_payload_preserves_newlines_and_tabs`.
  - `test_sanitize_payload_caps_at_50kb`.
  - `test_render_orchestrator_prompt_substitutes_placeholders` — all three placeholders replaced.
  - `test_render_orchestrator_prompt_rejects_missing_role` — workflow requires a role the group doesn't have → `RuntimeError` with role name.
  - `test_render_orchestrator_prompt_unknown_workflow_id` — group workflow_id not in `BUILT_IN_WORKFLOWS` → `RuntimeError`.

## 4. Non-functional Requirements

- All 90 existing tests must continue to pass.
- New tests must run in the CI loop in under 2 seconds total.
- No new runtime dependencies.
- Integration tests must be hermetic — no subprocess, no tmux, no network.

## 5. Out of Scope

- Performance profiling of the dispatch loop
- Memory profiling under long-running workflows
- Multi-group concurrency (still single `_active_group_id`)
- Re-rendering the orchestrator prompt on workflow step completion (templates are static per-start)
- TUI visual testing (requires a terminal)

## 6. Acceptance Criteria

| ID | Feature | Condition | Expected Result |
|:---|:---|:---|:---|
| AC-01 | F-01 | Drive dispatch_loop with scripted capture_pane outputs where the orchestrator pane text length decreases between polls (simulating scrollback truncation) and a new dispatch appears in the truncated view | The new dispatch is parsed and sent to the worker pane |
| AC-02 | F-01 | Same dispatch raw content appears in two consecutive captures | send_keys to the worker pane is called exactly once |
| AC-03 | F-02 | Start an orchestrator agent; immediately stop the app | Orchestrator prompt tmp file is scheduled for deletion via call_later |
| AC-04 | F-03 | Fake pane_exists returns False during in-flight dispatch | WORKER_ERROR injected into orchestrator pane; in_flight cleared |
| AC-05 | F-04 | Tester emits `<<TESTS_FAILED>>` four times in a row | Fourth occurrence logs a warning; platform still forwards result |
| AC-06 | F-05 | Grep `src/` for `PaneOutputRefresh` and `_check_mcp_alive` | Zero matches |
| AC-07 | F-06 | Run pytest tests/test_orchestrator.py | Ran > 35 tests, all pass |
| AC-08 | F-07 | Run pytest tests/test_dispatch_integration.py | Ran 10 tests, all pass |
| AC-09 | F-08 | Run pytest tests/test_session_manager.py | Ran 6 tests, all pass |
| AC-10 | Overall | Run pytest | Ran > 130 tests, all pass |

## 7. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-08 | Initial version — post-hoc hardening of REQ-012 v2: critical scrollback offset bug, orchestrator tmp file leak, worker pane crash detection, retry soft cap, dead code removal, ~40 new unit tests, 10 new integration tests, 6 new session_manager tests | ALL | User request for adversarial self-review before the code is left unattended |
