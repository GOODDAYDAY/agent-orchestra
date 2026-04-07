# REQ-012 Replace MCP Event Bus with LLM Orchestrator

> Status: Requirement Finalized
> Created: 2026-04-07
> Updated: 2026-04-08
> Directory name retained (`REQ-012-mcp-agent-communication-fix`) for history continuity; the scope of this REQ has pivoted — see Change Log v2.

## 1. Background

### 1.1 What v1 tried to do
v1 of this REQ was a three-bug patch on an event-bus architecture: inject `AGENT_ID` / `GROUP_ID` into system prompts so agents could call `publish_event`; unify delivery through a `pending_events` table plus a tmux wake-up sentinel; fix `grouped_attach()` pane routing; replace `sleep(1.0)` with a readiness poll; and bump role templates to explain the new pull-after-sentinel protocol.

### 1.2 Why v1 was abandoned
Deeper architectural analysis revealed the event-bus model is fundamentally unsuited to LLM agents running inside Claude CLI processes. The specific failure mode:

> **An LLM in a chat loop has no inbox.** It only acts when prompted. Any "deliver a message" step therefore *must* eventually inject text into the agent's input. v1 tried to preserve a pull-model (`get_pending_events` MCP tool) on top of that reality by injecting a "doorbell" sentinel — but this created two tightly-coupled channels (sentinel + MCP pull) where either one failing silently breaks delivery, and relied on the LLM reliably interpreting the sentinel and choosing to poll.

Concretely, v1 had seven hops per message (publish → SQLite events → supervisor poll → pending_events → send-keys sentinel → LLM recognises sentinel → MCP pull → LLM reads payload) across two transports, three SQLite operations, and a 250 ms polling loop — all to move one message between two local processes. It was also impossible to test end-to-end without a live Claude model that would honour the role-template instructions.

### 1.3 The pivot: LLM Orchestrator
v2 replaces the event-bus entirely with an **orchestrator agent** — a 6th Claude CLI process running in its own tmux pane, whose sole job is to drive a workflow by prompting the five worker agents (PM / Tech Director / Developer / Tester / User) one at a time. Inter-agent messages no longer exist as a concept: the orchestrator holds workflow state, reads each worker's output via `tmux capture-pane`, detects completion via a `<<TASK_DONE>>` marker, extracts the artifact, and hands it to the next worker as a fresh prompt.

This collapses the seven-hop chain into one transport (tmux send-keys / capture-pane), deletes the entire MCP server, and moves "who talks to whom" from LLM-self-policing into an explicit workflow template chosen at group creation.

### 1.4 What survives from v1
Three v1 sub-features remain valid and are retained verbatim in v2: the Enter-button pane routing fix (F-03), the readiness poll replacing `sleep(1.0)` (F-04, now load-bearing because the orchestrator cannot prompt a worker until it is truly ready), and the distinct Enter-button error toasts (F-06). The v1 identity-injection (F-01), unified `pending_events` delivery (F-02), and pull-based role templates (F-05) are deleted; F-05 is re-introduced in v2 with completely different content.

## 2. Target Users & Scenarios

- **Platform operator** — creates a group, picks a workflow template, clicks Start Group, and observes the orchestrator prompting each worker in turn. The operator can attach (Enter button) to any pane — worker or orchestrator — to inspect or intervene at any time.
- **Workflow observer** — watches the 6 tmux panes (5 workers + 1 orchestrator) and uses the orchestrator pane as a live project dashboard showing "current stage, current actor, last artifact summary".
- **Manual fallback user** — when the orchestrator stalls (e.g. worker never emits `<<TASK_DONE>>`), receives a TUI toast and can either attach to the stuck pane or click a "Force Advance" action to resume.

## 3. Functional Requirements

### F-03 Enter Button Pane Routing Fix (retained from v1)

- Main flow: `grouped_attach()` in `tmux_attach.py` accepts `pane_id: str` as a parameter. After `switch-client` succeeds, run `tmux select-pane -t {pane_id}` to ensure the correct pane is focused inside the grouped session.
- Out-of-tmux path: `suspend_attach()` passes `{tmux_session_name}:{pane_id}` (or derives the window index from `pane_id`) as the target to `tmux attach-session -t`, replacing the bare session name.
- Callers: `app.py`'s `_do_grouped_attach()` and `_do_suspend_attach()` extract `session.tmux_pane_id` from the resolved session and pass it to the attach functions.
- Orchestrator pane: the orchestrator is a regular agent from `AgentRole.orchestrator` with its own `tmux_pane_id` — no special case in attach routing; operators Enter it the same way they Enter any other pane.
- Error handling: if `select-pane` fails (pane was renumbered), show a toast "Could not select specific pane — landed on active window" but do not abort the attach.
- Edge cases: `pane_id` format is `%N` (tmux global pane ID), stable within a session lifetime; no conversion needed.

### F-04 Readiness Poll Replacing `sleep(1.0)` (retained and promoted)

- Main flow: in `start_agent_session()`, after sending the Claude CLI command via `send-keys`, replace `await asyncio.sleep(1.0)` with a `capture-pane` poll loop. Poll `tmux capture-pane -p -t {pane_id}` every 200 ms for up to `SESSION_START_TIMEOUT` seconds. Mark the agent `active` only once the pane contains substantial output (any non-empty content after the startup banner).
- Why this is now load-bearing: the orchestrator uses `agent.status == active` as the precondition for sending a prompt. Prompting a worker whose Claude CLI is still warming up would silently drop the prompt into the tmux input buffer before the TUI is ready to accept it. Any flake in readiness detection now becomes a workflow stall.
- Orchestrator startup ordering: the orchestrator agent itself must complete its own readiness poll before it is allowed to start prompting workers. The group-start sequence is: (1) start all workers and wait for each to reach `active`, (2) start the orchestrator, (3) wait for orchestrator `active`, (4) orchestrator begins its workflow loop.
- Fallback: if `SESSION_START_TIMEOUT` is exceeded without detecting readiness, mark the agent `degraded` and show a warning toast. If any worker is `degraded`, the orchestrator refuses to start and the TUI reports which worker failed.
- Error handling: `capture-pane` failures (tmux not running) must not hang — wrap in try/except and fall back to a bounded `asyncio.sleep(SESSION_START_TIMEOUT)` then mark degraded.
- Edge cases: the readiness check is "any substantial output present" rather than a specific Claude CLI banner string — version-agnostic across Claude CLI releases.

### F-05 Worker Role Templates — Orchestrator-Aware Rewrite (replaces v1 F-05)

- Main flow: bump `_TEMPLATE_VERSION` by one (v1 bumped 3→4; v2 bumps again to 5 because the template bodies are completely different). Rewrite the five worker role templates (PM, Tech Director, Developer, Tester, User) so each template describes (a) the role's responsibility within a workflow turn, (b) the expected input shape (a free-text task prompt from the orchestrator, possibly with prior artifacts inlined), (c) the expected output shape (the role's artifact followed by a mandatory final line `<<TASK_DONE>>`), and (d) the rule: never emit `<<TASK_DONE>>` anywhere except as the last line.
- Removed from all templates: any mention of `get_pending_events`, `publish_event`, `source_agent_id`, `group_id`, `AGENT_ID`, `GROUP_ID`, MCP tools, pending events, wake-up sentinels, or event topics.
- User role template: the User role is the human stand-in used by workflows that end with "await user review". Its template instructs Claude to wait for human text input via the native pane (attach) and only emit `<<TASK_DONE>>` after the human has approved. In the absence of human input it must not fabricate an approval.
- Custom role: the `custom` role has an empty template — users fill it in themselves — but the orchestrator protocol (final-line `<<TASK_DONE>>`) applies to custom-role agents if they are included in a workflow. A tooltip in the role-template editor documents this constraint.
- Template overwrite semantics: `_seed_role_templates(force_update=True)` on version bump still overwrites customised prompts. Users who customise must re-apply after the bump. (Same known limitation as v1; reform of this flow is still Out of Scope.)

### F-06 Distinct Error Messages for Enter Button Failures (retained from v1)

- Main flow: in `app.py`'s `_handle_attach()`, replace the single generic "Agent has no active pane" toast with distinct actionable messages for each failure branch:
  - `self._active_group_id is None` → "Select a group first"
  - Session record not found (agent never started) → "Agent session not started — use Start Group"
  - `validate_pane_exists()` fails (stale pane_id) → "Agent pane is gone — restart the agent"
- Error handling: no change to attach logic; only the `self.notify()` call strings.
- Edge cases: None — isolated UI message change.

### F-07 Orchestrator Agent

- Definition: a new `AgentRole.orchestrator` enum value. Orchestrator agents are created automatically alongside worker agents by REQ-009's group auto-create logic (see F-11) and run as ordinary Claude CLI processes in dedicated tmux panes. They are distinguished only by their role and their bundled system prompt template.
- Orchestrator system prompt: seeded by `_seed_role_templates` as a new built-in template, parameterised at start-up with three placeholders filled in by `session_manager`:
  - `{{WORKFLOW_DEFINITION}}` — the chosen workflow template rendered as an ordered list of steps (see F-08)
  - `{{WORKER_ROSTER}}` — the workers in this group, one per line: `<role>: <agent name> (pane_id <%N>)`
  - `{{COMPLETION_MARKER}}` — the literal string `<<TASK_DONE>>`
- Orchestrator behaviour (as described by its system prompt): run an internal loop where each iteration (a) picks the next step from the workflow, (b) composes a prompt for the responsible role including any prior artifact, (c) emits a directive of the form `<<DISPATCH role="developer" text="...">>...<</DISPATCH>>` in its own output, (d) waits for the platform to return the worker's completed artifact back as the orchestrator's next user message, (e) records the artifact in its own working memory, (f) advances to the next step or branches based on workflow rules. The final step emits `<<WORKFLOW_COMPLETE>>` and the orchestrator stops.
- Dispatch protocol: when the platform detects a `<<DISPATCH role="..." text="...">>...<</DISPATCH>>` block in orchestrator output via `capture-pane`, it (a) resolves `role` to the target worker in the group roster, (b) sends the `text` content as a fresh prompt to that worker's pane via `send-keys`, (c) begins polling the worker pane for `<<TASK_DONE>>` (see F-09), (d) upon completion extracts the worker's output between the dispatch and the `<<TASK_DONE>>` line, (e) injects a follow-up message into the orchestrator pane of the form `[WORKER_RESULT role="developer"]\n<artifact>\n[/WORKER_RESULT]`.
- Error handling — worker unreachable: if the dispatched worker pane is missing or `degraded`, the platform injects `[WORKER_ERROR role="developer" reason="pane gone"]` into the orchestrator pane. The orchestrator's system prompt instructs it on graceful handling (retry once, else emit `<<WORKFLOW_ABORT reason="...">>`).
- Error handling — malformed dispatch: if the orchestrator emits a dispatch block with an unknown role or invalid XML-like syntax, the platform injects `[PLATFORM_ERROR: unknown role 'foo' — valid roles: pm, tech_director, developer, tester, user]` and waits for the orchestrator to retry.
- Edge cases: the orchestrator must never dispatch to itself; the platform rejects self-dispatch with `[PLATFORM_ERROR: orchestrator cannot dispatch to itself]`. Multiple workers cannot run in parallel in v2 — the orchestrator must wait for each worker to finish before dispatching the next. Parallel dispatch is explicitly Out of Scope.

### F-08 Workflow Templates

- Three built-in workflow templates must be seeded at start-up and selectable when creating a group:
  - **standard** — PM → Tech Director → Developer → Tester → User Review → Done. If Tester output contains the marker `<<TESTS_FAILED>>` (emitted by the tester role template when tests fail), the orchestrator loops back to Developer with the failure artifact. Maximum of 3 iterations of the Dev↔Tester loop before the workflow aborts with `<<WORKFLOW_ABORT reason="test loop exceeded">>`.
  - **prototype** — Developer → User Review → Done. Two-step workflow for quick experiments.
  - **research** — PM → Tech Director → User Review → Done. For design-only work with no coding phase.
- Representation: each workflow is stored as a list of steps, where each step is `{role: str, on_failure: Optional[str], max_retries: int}`. Workflows live in code (a new module `backend/workflows.py`) rather than in the DB — they are built-in and not user-editable in v2.
- Rendering into orchestrator prompt: at orchestrator start-up, `session_manager` renders the chosen workflow into a human-readable numbered list and substitutes it into `{{WORKFLOW_DEFINITION}}`. Example rendering for `standard`:
  ```
  1. pm — produce the requirement spec
  2. tech_director — review and produce technical design
  3. developer — implement the design
  4. tester — run tests; on <<TESTS_FAILED>> go back to step 3 (max 3 retries)
  5. user — approve the completed work
  ```
- Group creation UI: the group create/edit form adds a "Workflow" dropdown listing the three built-ins. Default selection: `standard`. The selection is stored in `groups.workflow_id` (column name chosen for future extensibility, but only accepts one of the three built-in IDs in v2).
- Error handling: if a group's stored `workflow_id` is unknown (e.g. an old DB row), the orchestrator logs an error and refuses to start; the TUI shows "Unknown workflow — edit group to select a valid workflow".
- Edge cases: workflows that reference a role not present in the group roster (e.g. a `standard` workflow on a group that was manually edited to remove the Tester) must fail fast at orchestrator start-up with a clear error toast listing missing roles.

### F-09 Completion Detection — Three-Layer Fallback

- Primary signal: **`<<TASK_DONE>>` marker**. The platform monitors the dispatched worker's pane by polling `tmux capture-pane -p -t {pane_id}` every 500 ms. When the captured output contains a new-line starting with `<<TASK_DONE>>` after the dispatch marker, the worker is considered done and its artifact (everything between dispatch and the marker line) is extracted.
- Secondary signal: **60-second silence detection**. If `capture-pane` output has not changed for 60 consecutive seconds (configurable via `WORKER_SILENCE_TIMEOUT` in `shared/config.py`), the platform treats the worker as done by silence. The extracted artifact is whatever is currently in the pane below the dispatch marker. A warning is logged ("worker X completed by silence, not by marker") and a diagnostic TUI toast is shown so the operator knows the role template was not fully honoured.
- Tertiary signal: **orchestrator stall timeout**. If no completion signal fires within `ORCHESTRATOR_STALL_TIMEOUT` (default 10 minutes, configurable) after a dispatch, the platform injects `[PLATFORM_STALL: no completion signal from role=developer after 10 minutes]` into the orchestrator pane AND shows a TUI toast with a "Force Advance" and "Abort Workflow" button. Force Advance captures whatever is currently in the worker pane as the artifact and proceeds. Abort Workflow terminates the orchestrator's current run.
- Detection precision: to avoid false positives, the `<<TASK_DONE>>` detector requires (a) the marker is at the start of a line, (b) the marker appears after the dispatched prompt's last line in the capture-pane output. False-positive mitigation: the dispatched prompt must NOT contain the literal string `<<TASK_DONE>>`; if it would, the platform refuses the dispatch with a platform error.
- Error handling: if `capture-pane` fails mid-poll (pane vanished), treat as worker error and report `[WORKER_ERROR role=... reason="pane gone"]` to the orchestrator.
- Edge cases: the marker must match exactly `<<TASK_DONE>>` — variations (`<<task_done>>`, `<< TASK_DONE >>`) are NOT accepted. The role template's instruction is explicit and case-sensitive. If a worker emits `<<TASK_DONE>>` but then continues producing text (ignoring the rule), the platform truncates the artifact at the marker and logs a warning.

### F-10 Deletion of Event Bus Infrastructure

The following code and data must be removed as part of this REQ. The list is authoritative: leaving any item behind is a regression.

- **Files deleted**:
  - `src/agent_management/backend/mcp_server.py` (entire file)
- **Code deleted from `backend/supervisor.py`**:
  - `_fan_out()`, `_wake_agent()`, `_buffer_event()`, `_deliver_event()`, `_drain_pending()`, `deliver_pending_event()`
  - The `_WAKE_SIGNAL` constant
  - The supervisor's polling loop for `get_undelivered_events()` (the orchestrator replaces the supervisor's fan-out role)
- **Code deleted from `backend/repository.py`**:
  - `insert_pending_event()`, `count_pending_events()`, `get_pending_events_for_agent()`, `drop_oldest_pending_event()`, `mark_pending_event_delivered()`
  - `insert_event()`, `get_undelivered_events()`, `mark_event_supervisor_delivered()` — the `events` table is dropped entirely in v2
- **Database schema changes** (see also Non-functional §4):
  - DROP table `pending_events`
  - DROP table `events`
  - DROP column `agents.topic_list`
  - DROP column `agents.auto_respond`
- **Dependency cleanup**: remove `mcp` and `fastmcp` from `pyproject.toml` unless they are still required by another feature (they are not).
- **Configuration cleanup**: remove `MCP_HOST`, `MCP_PORT`, `PENDING_EVENT_CAPACITY`, `SUPERVISOR_POLL_INTERVAL` from `shared/config.py`. Add the new constants: `WORKER_SILENCE_TIMEOUT`, `ORCHESTRATOR_STALL_TIMEOUT`, `DISPATCH_POLL_INTERVAL`.
- **TUI cleanup**: any UI that rendered pending-event counts (badges, indicators) must be removed. The AgentPane badge is reused to show the worker's current workflow step ("idle" / "working: developer (step 3/5)") instead.

### F-11 REQ-009 Group Auto-Create Update

- REQ-009 currently creates five agents when a new group is created (PM / Tech Director / Developer / Tester / User). v2 changes this to create **six** agents — the original five plus a new `Orchestrator` agent — all with the same working directory.
- Naming: the orchestrator agent is named `"{group} - Orchestrator"`, following the existing `"{group} - {role}"` pattern.
- Ordering: UI listing order in the Group panel must put the Orchestrator first, then the five workers in the canonical order PM → Tech Director → Developer → Tester → User. (Makes the "who is in charge" visual obvious.)
- Start-up ordering: group start must start the five workers first, wait for all to reach `active`, then start the orchestrator last — otherwise the orchestrator might dispatch to a worker that has not yet finished its readiness poll.
- Delete semantics: REQ-010 cascade-delete on group deletion already removes all agents in the group — no change needed; the orchestrator is just one more row removed.
- Edge cases: groups created before v2 only have five workers. On first load after upgrade, the system detects such groups (see §4 DB migration policy) and either (a) auto-creates the missing orchestrator or (b) the whole DB is wiped per the migration policy. v2 adopts the latter: schema version bump forces a clean DB, so there are no legacy groups to repair.

## 4. Non-functional Requirements

- **Schema migration policy — destructive**. v2 introduces column drops, table drops, and new columns/tables. Because this is a local single-user tool with no production data, v2 does **not** write a migration script. On start-up, the application compares `meta.schema_version` against the expected value; if they differ, the application shows a modal: *"Schema version mismatch. Your existing data is incompatible with this version. Click Reset to wipe `.agent_management/` and continue, or quit and downgrade."* The Reset button deletes the SQLite file and the associated temp directory contents, then re-runs the seed flow. **Users must be warned in the CHANGELOG and on the first-run screen that upgrading to REQ-012 v2 wipes their prior groups, agents, and custom role templates.**
- **Testability**: the orchestrator dispatch detector and the `<<TASK_DONE>>` completion detector must be isolated functions in `backend/orchestrator.py` accepting a string (captured pane contents) and returning a structured result — testable without any tmux or subprocess. The v1 test file `tests/test_tmux_attach.py` continues to apply to F-03 changes.
- **Token budget**: adding a 6th Claude CLI process increases total token consumption per workflow run by roughly 30–50 % (the orchestrator's context grows linearly with workflow length). This is an acceptable trade-off explicitly approved by the user in the v2 planning discussion.
- **Constants**: `WORKER_SILENCE_TIMEOUT=60`, `ORCHESTRATOR_STALL_TIMEOUT=600`, `DISPATCH_POLL_INTERVAL=0.5`, `SESSION_START_TIMEOUT=30` — all defined in `shared/config.py`.
- **Logging**: every orchestrator dispatch, worker completion signal (with which layer triggered — marker / silence / stall), and workflow step transition must be logged at INFO level with the group ID and workflow step index for post-mortem debugging.
- **Template version bump**: `_TEMPLATE_VERSION` bumps to 5. The value is logged on start-up.
- **Backward compatibility with REQ-001/002/003/004/005/006/007/008/009/010/011**: all prior REQs remain in force except (a) REQ-009's agent count (5 → 6, handled by F-11), (b) any REQ that touched the `topic_list` / `auto_respond` fields (none did in observable behavior terms), and (c) the single `_active_group_id` limitation in supervisor (out of scope — still only one group running at a time). REQ-013 is orthogonal; its scroll-bar fix and attach interaction improvements apply unchanged to v2's 6-pane layout.

## 5. Out of Scope

- Parallel dispatch (orchestrator running two workers at once) — sequential only in v2
- User-editable workflow templates (DSL, YAML files, TUI editor) — only three built-ins in v2
- Persistence of workflow state across application restarts — on restart the orchestrator starts over from step 1 or the user can edit prompts manually via attach
- Orchestrator using Claude's Agent SDK or Task tool instead of the tmux send-keys protocol — orchestrator is a vanilla Claude CLI pane just like the workers
- MCP server for anything else (debugging, external integrations) — the entire `mcp_server.py` file is removed; future re-introduction of MCP is a separate REQ
- Acknowledgement mechanism, causal ordering, event replay — all concerns of the deleted event bus; orchestrator's state is its own chat history, no additional guarantees
- Multi-user concurrency — local tool, single user
- DB migration from v1 schemas to v2 — destructive reset only
- `restart_agent_session` stale session ID bug — still a separate bug fix
- Dead code cleanup of `AgentPane._pane_id`, tmux prefix constants — still a future cleanup REQ
- Template version bump prompt-preservation UX — still a future REQ
- REQ-013's terminal attach interaction and output scroll fix — orthogonal, handled in REQ-013 independently
- Orchestrator "smart retry" strategies beyond the workflow's declared `max_retries` — workflows are linear and simple in v2

## 6. Acceptance Criteria

| ID | Feature | Condition | Expected Result |
|:---|:---|:---|:---|
| AC-01 | F-03 | Open TUI in tmux; start group with 6 agents; click Enter on Developer | Terminal switches to Developer's pane, not the last-active window |
| AC-02 | F-03 | Open TUI outside tmux; click Enter on Orchestrator | `tmux attach-session` lands on Orchestrator's pane |
| AC-03 | F-04 | Start a group | Each agent (workers and orchestrator) status changes from `starting` to `active` only after Claude CLI produces output in the pane |
| AC-04 | F-04 | Claude CLI fails to start within 30 s | Agent status set to `degraded`; warning toast shown; orchestrator refuses to start |
| AC-05 | F-05 | After template version bump; open role template editor for Developer | Template shows orchestrator-aware instructions with `<<TASK_DONE>>` rule; no mention of MCP / pending events / topics |
| AC-06 | F-06 | Click Enter with no group selected | Toast reads "Select a group first" |
| AC-07 | F-06 | Click Enter on agent that was never started | Toast reads "Agent session not started — use Start Group" |
| AC-08 | F-07 | Start group with `standard` workflow; observe orchestrator pane | Orchestrator emits a dispatch block targeting PM as its first action |
| AC-09 | F-07 | Orchestrator dispatches to Developer; Developer emits `<<TASK_DONE>>` | Orchestrator pane receives a `[WORKER_RESULT role="developer"]` block and proceeds to dispatch to Tester |
| AC-10 | F-07 | Orchestrator emits a dispatch to an unknown role | Orchestrator pane receives `[PLATFORM_ERROR: unknown role ...]`; orchestrator remains alive |
| AC-11 | F-08 | Create a group; workflow dropdown visible with `standard`, `prototype`, `research` | All three options selectable; default is `standard` |
| AC-12 | F-08 | Create a group with `prototype` workflow; start group; observe orchestrator | Orchestrator's first dispatch targets Developer (not PM) |
| AC-13 | F-08 | Standard workflow runs; Tester emits `<<TESTS_FAILED>>` followed by `<<TASK_DONE>>` | Orchestrator re-dispatches to Developer; iteration counter increments |
| AC-14 | F-08 | Standard workflow's Dev↔Tester loop exceeds 3 iterations | Orchestrator emits `<<WORKFLOW_ABORT reason="test loop exceeded">>` and stops |
| AC-15 | F-09 | Worker produces output ending with `<<TASK_DONE>>` | Marker detected within 1 s of appearing; dispatch completes |
| AC-16 | F-09 | Worker produces output but never emits `<<TASK_DONE>>`; pane goes silent for 60 s | Silence timeout fires; artifact extracted from current pane content; warning toast shown |
| AC-17 | F-09 | Worker hangs; no output changes; 10 minutes elapse | Orchestrator pane receives `[PLATFORM_STALL: ...]`; TUI shows Force Advance / Abort Workflow toast buttons |
| AC-18 | F-09 | Dispatched prompt accidentally contains `<<TASK_DONE>>` in its text | Platform refuses dispatch with a platform error; orchestrator is asked to retry |
| AC-19 | F-10 | After upgrade; `src/agent_management/backend/mcp_server.py` | File does not exist |
| AC-20 | F-10 | After upgrade; run `grep -r "publish_event\|get_pending_events\|pending_events\|topic_list\|auto_respond\|_WAKE_SIGNAL" src/ tests/` | Zero matches (outside of historical documentation) |
| AC-21 | F-10 | After upgrade; inspect SQLite schema | Tables `pending_events` and `events` do not exist; columns `agents.topic_list` and `agents.auto_respond` do not exist |
| AC-22 | F-10 | After upgrade; `pyproject.toml` | `mcp` / `fastmcp` dependencies are removed |
| AC-23 | F-11 | Create a new group in the TUI | Six agents created (Orchestrator + PM + Tech Director + Developer + Tester + User), all with the same working directory |
| AC-24 | F-11 | Start a newly created group | Workers start first; orchestrator starts last; orchestrator only becomes `active` after all workers are `active` |
| AC-25 | Schema migration | Start application against a pre-v2 SQLite file | Modal appears warning of schema incompatibility; Reset button wipes DB; cancel button quits the app |
| AC-26 | Logging | Run a full `standard` workflow end to end | Logs contain one INFO entry per dispatch, one per completion (annotated with detection layer), and one per step transition |

## 7. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-07 | Initial version — three-bug patch on event-bus architecture (F-01 identity injection, F-02 unified `pending_events` + wake-up sentinel, F-03 Enter pane routing, F-04 readiness poll, F-05 role templates pull-after-sentinel, F-06 error toasts) | ALL | - |
| v2 | 2026-04-08 | Architectural pivot: event bus replaced with LLM Orchestrator model. Deleted F-01 (identity injection — no MCP to call), F-02 (event bus removed). Retained F-03 / F-04 / F-06 unchanged. Fully rewrote F-05 to describe orchestrator-aware worker templates with `<<TASK_DONE>>` marker. Added F-07 (Orchestrator Agent via new `AgentRole.orchestrator`), F-08 (three built-in workflow templates: standard / prototype / research), F-09 (three-layer completion detection: marker + 60 s silence + 10 min stall), F-10 (deletion manifest for MCP server, `pending_events`, `events`, `topic_list`, `auto_respond`), F-11 (REQ-009 auto-create from 5 → 6 agents including orchestrator). Rewrote Background, Target Users, Acceptance Criteria, Out of Scope. Added destructive schema migration policy. | ALL | Deep analysis of v1's event-bus design revealed fundamental structural problems: dual-channel delivery (sentinel + MCP pull), dependence on LLM self-policing of a pull loop, seven-hop message path for local IPC, and untestable end-to-end without live LLM cooperation. User approved architectural pivot to LLM Orchestrator (A2) with built-in workflow templates (B2) and layered completion detection (C combo). Chose to amend REQ-012 in place (D2) rather than open a new REQ, preserving the original REQ number and history. Status rolled back from `Security Reviewed` to `Requirement Finalized`; Stages 2–8 will be re-run against v2. |
