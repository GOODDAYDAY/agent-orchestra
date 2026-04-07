# REQ-012 v2 Technical Design — LLM Orchestrator Replaces MCP Event Bus

> Status: Technical Finalized
> Requirement: requirement.md (v2)
> Created: 2026-04-07
> Updated: 2026-04-08

## 1. Technology Stack

| Module | Technology | Rationale |
|:---|:---|:---|
| Orchestrator agent | Vanilla Claude CLI in a 6th tmux pane | Same substrate as worker agents — no new runtime, no SDK lock-in, fully observable via attach |
| Dispatch transport | `tmux send-keys` + `tmux capture-pane` | Single channel replaces v1's dual MCP+sentinel path; the same transport that already drives worker startup and the F-03 attach feature |
| Dispatch parsing | Pure-function regex parser in `backend/orchestrator.py` | Stateless, unit-testable without subprocess, deterministic for replay-debugging |
| Workflow definitions | In-code immutable dataclasses in `backend/workflows.py` | Three built-ins only in v2 — no DSL, no DB seeding, zero parser surface area |
| Completion detection | Three-layer state machine in `backend/orchestrator.py` (marker / silence / stall) | Layered fallback so the workflow advances even when the LLM mis-formats output or hangs |
| Persistence | SQLite via existing `Repository` (aiosqlite) | Schema is reduced — drops 2 tables and 2 columns; adds 1 column and 1 enum value |
| Schema migration | Destructive reset modal | Local single-user tool; full migration script is overkill and slows iteration |
| TUI | Existing Textual app | New: workflow dropdown in group create form, repurposed AgentPane badge, stall toast with action buttons |
| Logging | stdlib `logging` (existing) | Per-dispatch INFO line, completion-layer annotation, step transitions |

## 2. Design Principles

- **Single transport** — every cross-pane interaction in v2 goes through `tmux send-keys` / `capture-pane`. There is no second channel that can desync.
- **No LLM self-policing** — workflow ordering and dispatch routing live in Python. The LLM's only obligation is to (a) emit `<<DISPATCH ...>>` blocks in the orchestrator's case and (b) emit `<<TASK_DONE>>` as the final line in the workers' case. Both rules are enforced by deterministic parsers, with silence and stall fallbacks for when the rules are not honoured.
- **Pure functions at the boundary** — `orchestrator.py` exposes pure functions taking captured pane text and returning structured results. All time, IO, and side-effects live in the supervisor. This makes the parser/detector unit-testable without tmux, fakes, or live LLMs.
- **Reuse v1 carry-over verbatim** — `tmux_attach.py` (F-03), the `_wait_for_pane_ready` poll in `session_manager.py` (F-04), and the F-06 toast strings in `app.py` are unchanged. v2 deletions touch only the v1 code that depended on the event bus.
- **Destructive over migrational** — schema changes are breaking; the application detects mismatch via `meta.schema_version` and forces a wipe. Saves engineering effort that local-tool users do not value.
- **Sequential dispatch only in v2** — orchestrator can only run one worker at a time. Parallelism is explicitly Out of Scope; this keeps the dispatch-loop state machine trivial.

## 3. Architecture Overview

See `tech-architecture.puml`.

```
┌─────────────────────────── tmux session: agent-mgmt-{group_id8} ───────────────────────────┐
│                                                                                            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐       │
│  │   PM pane   │  │  TD  pane   │  │  Dev pane   │  │ Tester pane │  │  User pane  │       │
│  │ (Claude CLI)│  │ (Claude CLI)│  │ (Claude CLI)│  │ (Claude CLI)│  │ (Claude CLI)│       │
│  └──────▲──────┘  └──────▲──────┘  └──────▲──────┘  └──────▲──────┘  └──────▲──────┘       │
│         │                │                │                │                │              │
│         │   send-keys    │   send-keys    │   send-keys    │   send-keys    │   send-keys │
│         │   capture-pane │   capture-pane │   capture-pane │   capture-pane │   capture-  │
│         │                │                │                │                │     pane    │
│         └────────────────┴───────┬────────┴────────────────┴────────────────┘              │
│                                  │                                                          │
│                          ┌───────▼────────┐                                                 │
│                          │ Orchestrator   │  ◄── 6th pane, runs vanilla Claude CLI         │
│                          │     pane       │      with workflow in its system prompt        │
│                          └───────▲────────┘                                                 │
└──────────────────────────────────┼──────────────────────────────────────────────────────────┘
                                   │ send-keys ([WORKER_RESULT...]) / capture-pane
                                   │
                          ┌────────┴────────┐
                          │   Supervisor    │ ◄── Textual Worker (asyncio task)
                          │  dispatch loop  │     reads orchestrator pane, parses
                          │                 │     <<DISPATCH ...>> blocks, drives
                          │                 │     workers, injects [WORKER_RESULT]
                          └────────┬────────┘
                                   │
       ┌───────────────────────────┼────────────────────────────┐
       │                           │                            │
┌──────▼──────┐            ┌───────▼──────┐            ┌────────▼────────┐
│ orchestrator│            │  workflows   │            │   Repository    │
│    .py      │            │     .py      │            │  (SQLite)       │
│ parsers +   │            │ standard /   │            │  groups,        │
│ detectors   │            │ prototype /  │            │  agents (incl.  │
│ (pure fns)  │            │ research     │            │  orchestrator), │
└─────────────┘            └──────────────┘            │  sessions, meta │
                                                        └─────────────────┘
```

Modules touched, by surface area:

```
backend/orchestrator.py          ✨ NEW   parser, completion detector, dispatch state
backend/workflows.py             ✨ NEW   three built-in workflow templates + render helpers
backend/supervisor.py            🔥 REWRITTEN   delete fan-out, add dispatch loop
backend/session_manager.py       ✏️  MODIFIED   delete F-01 identity injection, add orchestrator startup branch; F-04 poll retained verbatim
backend/repository.py            ✏️  MODIFIED   schema diff, role template rewrite, _TEMPLATE_VERSION 4→5, schema_version detection
backend/models.py                ✏️  MODIFIED   add AgentRole.orchestrator, drop Agent.topic_subscriptions/auto_respond/topic_list, drop Event/PendingEvent dataclasses
backend/mcp_server.py            ❌ DELETED   entire file
shared/config.py                 ✏️  MODIFIED   add WORKER_SILENCE_TIMEOUT/ORCHESTRATOR_STALL_TIMEOUT/DISPATCH_POLL_INTERVAL; drop MCP_HOST/MCP_PORT/PENDING_EVENT_CAPACITY/SUPERVISOR_POLL_INTERVAL/get_mcp_port
frontend/app.py                  ✏️  MODIFIED   workflow dropdown wiring, stall-toast handler, schema-mismatch modal, drop MCP server start; F-03/F-06 carry-over retained
frontend/tmux_attach.py          ✓  UNCHANGED   F-03 carry-over
frontend/group_panel.py (or wherever Group create form lives)  ✏️  MODIFIED   workflow dropdown
frontend/agent_pane.py           ✏️  MODIFIED   badge text repurposed: "idle" / "working: step N/M (role)"
pyproject.toml                   ✏️  MODIFIED   drop mcp / fastmcp / uvicorn (only used by mcp_server)
tests/test_orchestrator.py       ✨ NEW   parser + detector unit tests
tests/test_workflows.py          ✨ NEW   built-in template structural tests
tests/test_tmux_attach.py        ✓  UNCHANGED
```

## 4. Module Design

### 4.1 `backend/orchestrator.py` (new)

**Responsibility.** Pure functions and small dataclasses for parsing the orchestrator's output and detecting worker completion. Contains no IO, no asyncio, no time calls — caller injects `now`.

**Public interface.**

```python
@dataclass(frozen=True)
class Dispatch:
    role: str          # raw role string from the dispatch attribute, lowercased
    text: str          # the prompt body to send to the worker
    raw: str           # the original DISPATCH block (for logging / artifact extraction offset)
    end_offset: int    # byte offset in the source where the dispatch block ends

class CompletionLayer(str, Enum):
    pending = "pending"   # nothing yet
    marker  = "marker"    # <<TASK_DONE>> seen
    silence = "silence"   # 60s no change
    stall   = "stall"     # 10min no signal at all
    error   = "error"     # capture-pane failed / pane gone

@dataclass(frozen=True)
class CompletionResult:
    layer: CompletionLayer
    artifact: str        # extracted worker output (empty for pending/error/stall)
    detail: str          # human-readable annotation for logging

# Parsers
def parse_latest_dispatch(orchestrator_pane_text: str, after_offset: int) -> Optional[Dispatch]:
    """Find the most recent <<DISPATCH role="..." text="...">>...<</DISPATCH>> block
    after the given offset. Returns None if no new dispatch is present.
    Recognises orchestrator emissions for WORKFLOW_COMPLETE / WORKFLOW_ABORT
    via separate helpers (see below)."""

def is_workflow_complete(orchestrator_pane_text: str, after_offset: int) -> bool:
    """True if <<WORKFLOW_COMPLETE>> appears at the start of a line after offset."""

def is_workflow_abort(orchestrator_pane_text: str, after_offset: int) -> Optional[str]:
    """If <<WORKFLOW_ABORT reason="..."/>> appears, return the reason; else None."""

# Completion detection
def detect_completion(
    pane_text: str,
    dispatch_end_offset: int,
    last_change_at: float,
    dispatch_at: float,
    now: float,
    silence_timeout: float = 60.0,
    stall_timeout: float = 600.0,
) -> CompletionResult:
    """Inspect the pane text below the dispatch and decide which layer fires.
    Caller provides timestamps; this function is pure."""

# Validation
def validate_dispatch_text(text: str) -> Optional[str]:
    """Return an error string if the proposed dispatch text contains '<<TASK_DONE>>'
    or other forbidden control sequences; None if OK."""
```

**Internal structure.** A single regex `_DISPATCH_RE` matches `<<DISPATCH role="(?P<role>[a-z_]+)" text="(?P<text>(?:[^"\\]|\\.)*)">>(.*?)<</DISPATCH>>` with `re.DOTALL`, supporting backslash-escaped quotes inside `text`. The completion detector uses `_TASK_DONE_RE = re.compile(r"^<<TASK_DONE>>\s*$", re.MULTILINE)`. Marker matching is restricted to text after `dispatch_end_offset` to prevent the dispatch's own text leaking the marker into history.

**Reuse notes.** Used exclusively by `supervisor.Supervisor.dispatch_loop()`. Not exposed to other backend modules. Tests live in `tests/test_orchestrator.py` and exercise (a) marker at end, (b) marker mid-stream then more output (must truncate at marker), (c) silence after partial output, (d) stall with no output, (e) malformed dispatch (no closing tag), (f) escaped quotes in text, (g) `validate_dispatch_text` rejecting embedded `<<TASK_DONE>>`.

### 4.2 `backend/workflows.py` (new)

**Responsibility.** Define the three built-in workflow templates as immutable in-code data structures and provide the helper that renders a workflow into the human-readable list injected into the orchestrator's system prompt.

**Public interface.**

```python
from dataclasses import dataclass
from agent_management.backend.models import AgentRole

@dataclass(frozen=True)
class Step:
    role: AgentRole               # which worker handles this step
    description: str              # what the role should do at this step
    on_failure_marker: Optional[str] = None   # e.g. "<<TESTS_FAILED>>" → loop back
    failure_loop_to: Optional[int] = None     # zero-based index of step to loop to
    max_retries: int = 0

@dataclass(frozen=True)
class Workflow:
    id: str                       # "standard" / "prototype" / "research"
    display_name: str
    description: str
    steps: tuple[Step, ...]

STANDARD: Workflow      # PM → TD → Dev → Tester (loop to Dev on TESTS_FAILED, max 3) → User
PROTOTYPE: Workflow     # Dev → User
RESEARCH: Workflow      # PM → TD → User

BUILT_IN_WORKFLOWS: dict[str, Workflow] = {
    "standard":  STANDARD,
    "prototype": PROTOTYPE,
    "research":  RESEARCH,
}

def get_workflow(workflow_id: str) -> Workflow:
    """Lookup; raises KeyError on unknown id."""

def render_for_orchestrator(workflow: Workflow, roster: list[tuple[AgentRole, str, str]]) -> str:
    """Produce the numbered-list string injected into the orchestrator system prompt
    via the {{WORKFLOW_DEFINITION}} placeholder. `roster` is [(role, agent_name, pane_id), ...]."""

def required_roles(workflow: Workflow) -> set[AgentRole]:
    """The set of distinct roles a group must contain to run this workflow."""
```

**Internal structure.** Definitions are constructed once at import time and frozen. `render_for_orchestrator` produces output of the form documented in requirement.md F-08.

**Reuse notes.** Used by `session_manager.start_agent_session()` (orchestrator branch) to render the workflow into the system prompt, by `app.py` to populate the workflow dropdown and to validate role coverage before starting a group, and by `repository.py` to validate `groups.workflow_id` on load. Tests in `tests/test_workflows.py` cover (a) all three built-ins parse-able and renderable, (b) `required_roles` correct, (c) `get_workflow("unknown")` raises.

### 4.3 `backend/supervisor.py` (rewritten)

**Responsibility.** Group lifecycle (start, stop, clear) and the orchestrator dispatch loop. v1's polling+fan-out machinery is deleted entirely.

**Public interface (preserved from v1).**

- `start_group(group_id)` — workers first, orchestrator last (see §4.4 for ordering rules)
- `resume_group(group_id)` — same ordering as start_group, but uses `claude_session_id` for `--resume`
- `stop_group(group_id)`
- `clear_all()`
- `pause_agent(agent_id)` / `resume_agent(agent_id)` — still useful for human intervention; pausing the orchestrator pauses the workflow loop
- Textual messages preserved: `EventPublished` → renamed `WorkflowStepAdvanced`, `AgentStatusChanged`, `PaneOutputRefresh`. Add new `WorkflowStalled(group_id, dispatch)` and `WorkflowCompleted(group_id)` and `WorkflowAborted(group_id, reason)` messages.

**New: `dispatch_loop()`.** Replaces v1's `_tick`/`_fan_out`. One asyncio loop per active group, started by `start_group` and cancelled by `stop_group`. Pseudocode:

```python
async def dispatch_loop(self, group_id: str) -> None:
    orch = await self._repo.get_orchestrator_for_group(group_id)
    orch_pane = (await self._repo.get_session(orch.id, group_id)).tmux_pane_id
    consumed_offset = 0           # how much of the orchestrator pane we've already processed
    in_flight: Optional[InFlightDispatch] = None

    while self._running and self._active_group_id == group_id:
        await asyncio.sleep(DISPATCH_POLL_INTERVAL)

        pane_text = await self._sm.capture_pane_output(orch_pane, lines=2000)

        if in_flight is None:
            # 1. Look for a new dispatch / completion / abort
            dispatch = parse_latest_dispatch(pane_text, after_offset=consumed_offset)
            if is_workflow_complete(pane_text, consumed_offset):
                self._app.post_message(WorkflowCompleted(group_id))
                return
            abort_reason = is_workflow_abort(pane_text, consumed_offset)
            if abort_reason:
                self._app.post_message(WorkflowAborted(group_id, abort_reason))
                return
            if dispatch is None:
                continue
            err = validate_dispatch_text(dispatch.text)
            if err:
                await self._sm.send_keys(orch_pane, f"[PLATFORM_ERROR: {err}]")
                consumed_offset = dispatch.end_offset
                continue
            worker = await self._resolve_role(group_id, dispatch.role)
            if worker is None:
                await self._sm.send_keys(orch_pane,
                    f"[PLATFORM_ERROR: unknown role '{dispatch.role}' — valid roles: {', '.join(self._valid_roles())}]")
                consumed_offset = dispatch.end_offset
                continue
            worker_session = await self._repo.get_session(worker.id, group_id)
            if not worker_session or worker_session.status != AgentStatus.active:
                await self._sm.send_keys(orch_pane,
                    f"[WORKER_ERROR role=\"{dispatch.role}\" reason=\"pane not active\"]")
                consumed_offset = dispatch.end_offset
                continue
            await self._sm.send_keys(worker_session.tmux_pane_id, dispatch.text)
            now = asyncio.get_event_loop().time()
            in_flight = InFlightDispatch(
                dispatch=dispatch,
                worker=worker,
                worker_pane=worker_session.tmux_pane_id,
                dispatch_at=now,
                last_change_at=now,
                last_pane_hash=hash(""),
            )
            consumed_offset = dispatch.end_offset
            logger.info("dispatch group=%s role=%s", group_id, dispatch.role)
        else:
            # 2. Poll the worker pane for completion
            worker_text = await self._sm.capture_pane_output(in_flight.worker_pane, lines=2000)
            current_hash = hash(worker_text)
            now = asyncio.get_event_loop().time()
            if current_hash != in_flight.last_pane_hash:
                in_flight.last_pane_hash = current_hash
                in_flight.last_change_at = now
            result = detect_completion(
                pane_text=worker_text,
                dispatch_end_offset=0,           # workers receive only the dispatch text
                last_change_at=in_flight.last_change_at,
                dispatch_at=in_flight.dispatch_at,
                now=now,
                silence_timeout=WORKER_SILENCE_TIMEOUT,
                stall_timeout=ORCHESTRATOR_STALL_TIMEOUT,
            )
            if result.layer == CompletionLayer.pending:
                continue
            if result.layer == CompletionLayer.stall:
                self._app.post_message(WorkflowStalled(group_id, in_flight.dispatch))
                # Stay in_flight; user must Force Advance or Abort to clear
                continue
            # marker / silence / error: deliver result and clear in-flight
            tag = result.layer.value
            await self._sm.send_keys(
                orch_pane,
                f"[WORKER_RESULT role=\"{in_flight.dispatch.role}\" via=\"{tag}\"]\n{result.artifact}\n[/WORKER_RESULT]",
            )
            self._app.post_message(WorkflowStepAdvanced(group_id, in_flight.dispatch.role, tag))
            logger.info(
                "completion group=%s role=%s layer=%s len=%d",
                group_id, in_flight.dispatch.role, tag, len(result.artifact),
            )
            in_flight = None
```

**Deleted from v1.** `_fan_out`, `_wake_agent`, `_buffer_event`, `_drain_pending`, `_deliver_event`, `deliver_pending_event`, `_WAKE_SIGNAL`, the events-table polling loop, the `EventPublished` message class.

**Reuse notes.** Reuses `SessionManager.send_keys()` and `SessionManager.capture_pane_output()` exactly as v1 did. The workflow definition is fetched once via `workflows.get_workflow(group.workflow_id)` and the result is passed into `session_manager.start_agent_session()` for the orchestrator pane only.

### 4.4 `backend/session_manager.py` (modified)

**Changes.**

1. **Delete `_build_identity_block`** entirely. The orchestrator does not need an identity (it does not call MCP tools), workers do not need one either.
2. **Delete `write_mcp_config()`** (no MCP server). Drop the `--mcp-config` argument from the claude command.
3. **Modify `start_agent_session()`** to branch on `agent.role`:
   - If `agent.role != AgentRole.orchestrator`: build the system prompt directly from `agent.system_prompt` / `agent.system_prompt_file` (no identity block prepend). Everything else (working_dir validation, pane creation, send-keys, readiness poll) is unchanged.
   - If `agent.role == AgentRole.orchestrator`: load the orchestrator template from `repository.get_orchestrator_template()`, fetch the group's `workflow_id` and roster, render `{{WORKFLOW_DEFINITION}}` via `workflows.render_for_orchestrator()`, render `{{WORKER_ROSTER}}` from the live `sessions` table (so `pane_id` is current), substitute `{{COMPLETION_MARKER}}` with the literal string `<<TASK_DONE>>`, write the rendered prompt to a temp file (it can exceed argv length limits), and pass via `--system-prompt-file`. Set the temp file mode to 0o600 and clean up on next start.
4. **Keep `_wait_for_pane_ready()`** verbatim (F-04 carry-over). It is now load-bearing — orchestrator dispatch is gated on worker `active` status.
5. **`stop_agent_session()`** unchanged.
6. **`restart_agent_session()`** unchanged.

**Group startup ordering** is enforced in `Supervisor.start_group()` (not session_manager). The supervisor calls `start_agent_session()` for each non-orchestrator agent first, awaits each in turn, verifies all are `active`, then starts the orchestrator last. Mixed parallel/sequential startup is intentional: workers start sequentially because tmux `new-window` is fast and ordered output in the TUI is more readable than racing.

**Reuse notes.** All tmux helpers (`_tmux`, `_ensure_tmux_session`, `_new_pane`, `capture_pane_output`, `send_keys`, `pane_exists`) are preserved; v2 callers in `Supervisor.dispatch_loop()` use `capture_pane_output` and `send_keys` directly.

### 4.5 `backend/repository.py` (modified)

**Schema changes.**

- DROP TABLE `events`
- DROP TABLE `pending_events`
- ALTER TABLE `agents` DROP COLUMN `topic_subscriptions` (SQLite limitation: actually rebuild table without the column)
- ALTER TABLE `agents` DROP COLUMN `auto_respond` (same)
- ALTER TABLE `groups` ADD COLUMN `workflow_id TEXT NOT NULL DEFAULT 'standard'`
- INSERT into `meta`: `('schema_version', '5')`

Because SQLite cannot drop columns in older versions, the actual implementation is destructive: when `meta.schema_version` differs from the expected `5`, the application shows the reset modal and re-creates the database fresh. In v2 the `_create_schema()` body therefore writes the new schema directly — there is no per-column ALTER for the migration.

**`_TEMPLATE_VERSION` bump 4 → 5.** All five worker role templates are rewritten (see §4.10). A new `orchestrator` template is added to `_DEFAULT_TEMPLATES`.

**New `Repository` methods.**

```python
async def get_schema_version(self) -> Optional[int]   # reads meta.schema_version
async def set_schema_version(self, version: int) -> None
async def get_workflow_id(self, group_id: str) -> str
async def set_workflow_id(self, group_id: str, workflow_id: str) -> None
async def get_orchestrator_template(self) -> str      # role_templates[orchestrator].system_prompt
async def get_orchestrator_for_group(self, group_id: str) -> Optional[Agent]   # finds the AgentRole.orchestrator member
```

**Deleted `Repository` methods.**

```
insert_event, get_undelivered_events, mark_event_supervisor_delivered,
insert_pending_event, count_pending_events, get_pending_events_for_agent,
drop_oldest_pending_event, mark_pending_event_delivered
```

**Schema-mismatch detection.** `Repository.init()` now reads `meta.schema_version` after opening the connection. If the row is missing or differs from `5`, it raises `SchemaIncompatibleError(actual, expected)` instead of silently running migrations. The frontend `app.py` catches this exception during startup and shows the destructive reset modal.

**Reuse notes.** All other Repository methods (`save_agent`, `get_groups`, `get_session`, `update_agent_status`, etc.) are unchanged and reused directly by the supervisor and frontend.

### 4.6 `backend/models.py` (modified)

**Changes.**

- Add `AgentRole.orchestrator = "orchestrator"`.
- Remove `Agent.topic_subscriptions`, `Agent.auto_respond`, `Agent.topic_list()`, `Agent.set_topics()`.
- Remove `Event` and `PendingEvent` dataclasses entirely.
- Keep `RoleTemplate.default_topics` field but mark deprecated (TODO comment); do not write to it. It can be removed in a future cleanup REQ once all code paths are confirmed dead.

**Reuse notes.** No new dataclasses introduced — `InFlightDispatch` and the orchestrator data live in `supervisor.py` as a private dataclass since they have no DB persistence.

### 4.7 `frontend/app.py` (modified)

**Carry-over preserved.** F-03 attach routing wiring (`_do_grouped_attach` / `_do_suspend_attach` accepting `pane_id`) and F-06 toast strings in `_handle_attach()` are unchanged.

**New: schema-mismatch modal.** In `on_mount()`, wrap `repo.init()` in try/except for `SchemaIncompatibleError`. On catch, push a `ConfirmModal("Schema version mismatch. Your existing data is incompatible with this version. Click Reset to wipe .agent_management/ and continue, or quit.")`. On Reset confirmation: close repo, delete `DB_PATH` and `TEMP_DIR/*`, re-init repo, continue mount. On cancel: `app.exit()`.

**New: workflow dropdown wiring.** When the user opens the Group create/edit form, populate a Select widget with the entries from `workflows.BUILT_IN_WORKFLOWS`. The selected `workflow_id` is stored on the `Group` row via `repo.set_workflow_id()`. Default selection: `"standard"`.

**New: stall toast handler.** Subscribe to `WorkflowStalled` messages from the supervisor. On receipt, show a non-dismissable toast with two buttons: **Force Advance** and **Abort Workflow**. Force Advance calls `supervisor.force_advance(group_id, dispatch)`, which captures whatever is currently in the worker pane and feeds it back as a `silence`-layer artifact to the dispatch loop. Abort calls `supervisor.abort_workflow(group_id)`, which terminates the dispatch loop and posts `WorkflowAborted`.

**Deleted from v1.** Any code that started the MCP server (`start_mcp_server`, port allocation, MCP-server lifetime tracking). The TUI no longer has an MCP "started on port" indicator. All "pending events" badge code in `agent_pane.py` is replaced with workflow-step text (see §4.9).

### 4.8 `frontend/group_panel.py` / Group create form

**Changes.** Add a `Select` widget labelled "Workflow" with the three built-ins. Position: directly below the "Group name" input. The selected value flows into the new `Group(name=..., workflow_id=...)` constructor.

When editing an existing group: the dropdown shows the current selection. Changing it before the next Start Group is allowed; changing it while a workflow is running is blocked with a toast.

### 4.9 `frontend/agent_pane.py` (modified)

**Badge re-purpose.** Previously the badge showed pending-event count. v2 shows:

- For **worker** agents: "idle" / "working" (based on whether the supervisor's `dispatch_loop` currently has them as `in_flight.worker`).
- For the **orchestrator** agent: "step N/M" (based on the workflow's step count and the index of the most-recently-advanced step).

The supervisor posts a `WorkflowStepAdvanced` Textual message after each completion; AgentPane subscribes to it.

**Status icons.** Add a small bordered indicator distinguishing the orchestrator pane visually so the operator can tell at a glance which pane is in charge.

### 4.10 Role Templates (rewritten in `repository.py`'s `_DEFAULT_TEMPLATES`)

All five worker templates and the new orchestrator template share a common shape:

```
你是 <角色>。

## 你的职责
<role-specific responsibility paragraph>

## 协议（必须遵守）
1. 当前对话中你只会收到一条来自 orchestrator 的任务 prompt — 它直接出现在你的输入中，没有任何 MCP 工具调用。
2. 完成任务后，必须在最后一行输出且仅输出：
   <<TASK_DONE>>
3. 不要在中间任何位置输出 <<TASK_DONE>> — 只能作为结束标记。
4. 不要等待"下一条消息"或调用任何工具去拉取队列 — orchestrator 会再次给你 prompt。
5. <role-specific failure marker rule, e.g. tester emits <<TESTS_FAILED>> on the line above <<TASK_DONE>> when tests fail>

## 格式
- 你的输出 = 任务结果（任意长度）+ 最后一行 <<TASK_DONE>>
- 不要包含元评论（"我现在开始..."、"我已完成..."），结果即可
```

The orchestrator template additionally describes the dispatch protocol:

```
你是 Orchestrator — 这个 group 的项目调度者。

## 你的工作流
{{WORKFLOW_DEFINITION}}

## 你的下属
{{WORKER_ROSTER}}

## 调度协议
- 你通过输出一段 dispatch block 来调用一个下属：
  <<DISPATCH role="developer" text="请实现 ...">>
  <</DISPATCH>>
- 平台会捕捉这个 block，把 text 内容作为 prompt 发送给 developer。
- 等待平台返回 [WORKER_RESULT role="..."] 块——这就是该下属的输出。
- 收到 [WORKER_RESULT] 后，决定下一步：再 dispatch 下一个角色，或者发出 <<WORKFLOW_COMPLETE>>。
- 如果遇到无法继续的错误，发出 <<WORKFLOW_ABORT reason="...">>。

## 规则
- 一次只能 dispatch 一个角色，必须等 [WORKER_RESULT] 才能 dispatch 下一个。
- text 字段不能包含字符串 {{COMPLETION_MARKER}}（会被识别为完成标记）。
- 工作流完成时只能输出 <<WORKFLOW_COMPLETE>>，不要再 dispatch。
```

Templates live in code (`_DEFAULT_TEMPLATES`) so they survive `_TEMPLATE_VERSION` bumps via `force_update=True`. Users who customise them must re-apply after upgrade — same known limitation as v1, still Out of Scope.

## 5. Data Model

```
┌─────────────────┐                  ┌─────────────────┐
│     groups      │                  │     agents      │
├─────────────────┤    1     0..*    ├─────────────────┤
│ id PK           │◄─────────────────│ id PK           │
│ name            │      via         │ name            │
│ workflow_id ★   │  group_members   │ role ★ (now     │
│ created_at      │                  │   includes      │
└─────────────────┘                  │   'orchestrator')│
                                     │ working_dir     │
                                     │ system_prompt   │
                                     │ system_prompt_file│
                                     │ paused          │
                                     │ status          │
                                     │ created_at      │
                                     │ updated_at      │
                                     │   ※ DROPPED:    │
                                     │   topic_subscriptions │
                                     │   auto_respond  │
                                     └─────────────────┘
                                              ▲
                                              │
┌─────────────────┐                  ┌────────┴────────┐
│     meta        │                  │   sessions      │
├─────────────────┤                  ├─────────────────┤
│ key PK          │                  │ id PK           │
│ value           │                  │ agent_id FK     │
│  ('schema_version','5') │           │ group_id FK     │
│  ('template_version','5')│          │ tmux_session_name│
└─────────────────┘                  │ tmux_pane_id    │
                                     │ status          │
                                     │ ...             │
                                     └─────────────────┘

┌─────────────────┐
│ role_templates  │   role PK includes 'orchestrator' in v2
├─────────────────┤
│ role            │
│ display_name    │
│ system_prompt   │
│ default_topics  │   (deprecated, always '[]' in v2)
└─────────────────┘

DROPPED in v2:
  events                — replaced by orchestrator pane chat history
  pending_events        — replaced by direct send-keys delivery
```

Schema additions are minimal and backward-incompatible by intent. The schema-mismatch detection mechanism is the only "migration" the v2 tool implements.

## 6. API Design

No external API. The orchestrator-platform contract uses in-pane control sequences only:

| Direction | Sequence | Emitter | Consumer | Purpose |
|:---|:---|:---|:---|:---|
| Orchestrator → Platform | `<<DISPATCH role="X" text="...">>...<</DISPATCH>>` | orchestrator LLM output | `orchestrator.parse_latest_dispatch` | Request platform to invoke a worker |
| Orchestrator → Platform | `<<WORKFLOW_COMPLETE>>` | orchestrator LLM output | `orchestrator.is_workflow_complete` | Workflow finished successfully |
| Orchestrator → Platform | `<<WORKFLOW_ABORT reason="...">>` | orchestrator LLM output | `orchestrator.is_workflow_abort` | Workflow finished with failure |
| Worker → Platform | `<<TASK_DONE>>` (last line) | worker LLM output | `orchestrator.detect_completion` (marker layer) | Worker finished its turn |
| Worker → Platform | `<<TESTS_FAILED>>` (line above `<<TASK_DONE>>`) | tester LLM output | supervisor's failure-loop logic via workflow `Step.on_failure_marker` | Tests failed; workflow should loop back |
| Platform → Orchestrator | `[WORKER_RESULT role="X" via="marker|silence|stall"]\n<artifact>\n[/WORKER_RESULT]` | supervisor.dispatch_loop | orchestrator LLM (next turn input) | Deliver worker output |
| Platform → Orchestrator | `[PLATFORM_ERROR: <reason>]` | supervisor.dispatch_loop | orchestrator LLM (next turn input) | Reject malformed dispatch |
| Platform → Orchestrator | `[WORKER_ERROR role="X" reason="..."]` | supervisor.dispatch_loop | orchestrator LLM (next turn input) | Worker pane unavailable |
| Platform → Orchestrator | `[PLATFORM_STALL: no completion signal from role=X after 10 minutes]` | supervisor (on stall) | orchestrator LLM (next turn input) | Inform orchestrator that the dispatch has stalled |

This is the entire wire protocol. There are no tool calls, no MCP, no HTTP, no SSE.

## 7. Key Flows

See `tech-sequence.puml` for the canonical dispatch cycle.

**Group startup (F-04, F-07, F-11):**
1. Operator clicks Start Group
2. Supervisor calls `session_manager.start_agent_session()` for each worker (5 of them) sequentially
3. Each call awaits `_wait_for_pane_ready` until the worker is `active` or `degraded`
4. If any worker is `degraded`, refuse to start orchestrator and report to TUI
5. Supervisor calls `start_agent_session()` for the orchestrator with the rendered system prompt
6. Orchestrator's `_wait_for_pane_ready` completes
7. Supervisor spawns `dispatch_loop(group_id)` as an asyncio task
8. The orchestrator's first prompt (its rendered system prompt) leads it to emit its first `<<DISPATCH>>` block within ~5 seconds

**Single dispatch cycle (F-07, F-09):**
1. Orchestrator emits a dispatch block in its pane
2. `dispatch_loop` polls the orchestrator pane every 500 ms via `capture_pane_output`
3. `parse_latest_dispatch` returns a `Dispatch` with the text and end offset
4. Supervisor validates role + worker availability
5. Supervisor calls `send_keys(worker_pane, dispatch.text)`
6. `dispatch_loop` enters its in-flight branch and polls the worker pane
7. Worker emits output, eventually ending in `<<TASK_DONE>>`
8. `detect_completion` returns `marker` layer with extracted artifact
9. Supervisor injects `[WORKER_RESULT ...]` into the orchestrator pane via `send_keys`
10. Supervisor logs INFO line and posts `WorkflowStepAdvanced` to TUI
11. Orchestrator's next turn reads the result and emits the next dispatch

**Stall recovery (F-09 tertiary):**
1. In-flight dispatch; no completion signal for 10 minutes
2. `detect_completion` returns `stall` layer
3. Supervisor posts `WorkflowStalled` to TUI
4. Frontend shows toast with Force Advance / Abort Workflow buttons
5. Operator clicks Force Advance → `supervisor.force_advance(group_id)` synthesises a `CompletionResult(layer=silence, artifact=current_pane_text)` and feeds it through the same delivery path
6. Or operator clicks Abort → `supervisor.abort_workflow(group_id)` cancels the loop task and posts `WorkflowAborted`

**Schema-mismatch boot:**
1. App starts; `repo.init()` raises `SchemaIncompatibleError(actual=4, expected=5)`
2. App catches and pushes the destructive reset modal
3. Operator clicks Reset
4. App closes connection, deletes `DB_PATH`, clears `TEMP_DIR`, re-runs `repo.init()` (which now seeds the v2 schema and templates)
5. App continues mount

## 8. Shared Modules & Reuse Strategy

| Shared Component | Used By | Notes |
|:---|:---|:---|
| `SessionManager.send_keys()` | supervisor.dispatch_loop (worker dispatch + orchestrator result injection), v1 carry-over for paused/resume | unchanged |
| `SessionManager.capture_pane_output()` | supervisor.dispatch_loop (orchestrator + worker polling), `_wait_for_pane_ready` (F-04) | unchanged |
| `_wait_for_pane_ready()` (F-04) | session_manager.start_agent_session for both workers and orchestrator | unchanged from v1 |
| `tmux_attach.grouped_attach` / `suspend_attach` (F-03) | app.py for Enter button on any pane (worker or orchestrator) | unchanged from v1 |
| `Repository.save_agent` / `get_session` / `update_agent_status` / etc. | supervisor, session_manager, app | unchanged |
| `workflows.get_workflow()` / `render_for_orchestrator()` / `required_roles()` | session_manager (orchestrator startup), app (dropdown + validation) | new |
| `orchestrator.parse_latest_dispatch()` / `detect_completion()` / `validate_dispatch_text()` | supervisor.dispatch_loop only | new, pure functions |
| `WORKER_SILENCE_TIMEOUT` / `ORCHESTRATOR_STALL_TIMEOUT` / `DISPATCH_POLL_INTERVAL` | supervisor.dispatch_loop | new constants in `shared/config.py` |
| `SESSION_START_TIMEOUT` | session_manager._wait_for_pane_ready (F-04) | unchanged |

## 9. Risks & Notes

| Risk | Likelihood | Mitigation |
|:---|:---|:---|
| Orchestrator LLM mis-formats dispatch block (missing closing tag, escaped quotes, wrong role name) | Medium | `parse_latest_dispatch` returns None on malformed input; supervisor injects `[PLATFORM_ERROR: ...]` as feedback to the orchestrator's next turn so it self-corrects. Tested via `tests/test_orchestrator.py` malformed-input cases. |
| Worker emits `<<TASK_DONE>>` mid-stream | Medium | Marker detector requires line-start match and truncates artifact at the marker; subsequent text is dropped with a warning log. |
| Worker emits `<<TASK_DONE>>` inside a code block (e.g. when documenting this very protocol) | Low | Documented in role template as forbidden; the `<<TASK_DONE>>` literal is uncommon enough in real artifacts that the false-positive rate is low. Out-of-scope for v2: a "bracketed by ``` " escape mechanism. |
| Orchestrator infinite-loops between Dev and Tester | Medium | Workflow `max_retries` enforced in `Step` definition; supervisor counts iterations and emits `<<WORKFLOW_ABORT reason="test loop exceeded">>` when exceeded. |
| 6-pane tmux layout overflows small terminals | Medium | Out of scope for v2 — operators are expected to use a wide terminal (≥160 cols). REQ-013 may improve layout adaptivity. |
| Token consumption ~30–50% higher than v1 | High | Accepted by user. Logged at start-up so the cost is visible. Future REQ may compress orchestrator context. |
| Destructive reset surprises users with custom role templates | High | The reset modal explicitly mentions "this will wipe your customised role templates"; CHANGELOG entry warns prominently. |
| `<<TASK_DONE>>` literal typed by a human in the User pane is interpreted as a worker completion | Medium | The User role template instructs the human to type `<<TASK_DONE>>` *intentionally* to advance — this is a feature, not a bug. Document in user-role template. |
| `dispatch_loop` busy-loop if `DISPATCH_POLL_INTERVAL` is set too low | Low | Default 500 ms; constant is documented. |
| `capture_pane_output` returns truncated history if pane scrolled past 2000 lines | Low | Use `tmux capture-pane -p -S -2000` (already supported by the tmux helper signature) and rely on `consumed_offset` tracking; long-running orchestrator sessions may need a higher buffer in a future REQ. |
| Force Advance feeds incomplete artifact to orchestrator | Medium | Operator's explicit choice; the `via="silence"` annotation in the `[WORKER_RESULT]` block tells the orchestrator the artifact may be incomplete so it can decide whether to retry or accept. |

## 10. Code Carry-over from v1

| Area | v1 status | v2 fate |
|:---|:---|:---|
| `tmux_attach.py` F-03 pane_id parameter + `select-pane` | Merged at Stage 3 (v1) | **Keep** |
| `session_manager.py` `_wait_for_pane_ready` F-04 readiness poll | Merged | **Keep**, becomes load-bearing |
| `app.py` `_handle_attach` F-06 toast strings | Merged | **Keep** |
| `app.py` F-03 wiring (pane_id passed to attach helpers) | Merged | **Keep** |
| `session_manager.py` `_build_identity_block` + identity prepend in `start_agent_session` (F-01) | Merged | **Revert** |
| `session_manager.py` `write_mcp_config` + `--mcp-config` flag | Merged | **Revert** |
| `supervisor.py` `_fan_out` / `_wake_agent` / `_buffer_event` / `_drain_pending` / `deliver_pending_event` (F-02) | Merged | **Revert / delete** |
| `mcp_server.py` `mark_pending_event_delivered` after `get_pending_events` (F-02) | Merged | **Delete entire file** |
| `repository.py` v1 role templates with `get_pending_events` Step 0 (F-05) | Merged | **Replace with v2 templates** |
| `repository.py` `_TEMPLATE_VERSION = 4` | Merged | Bump to 5 |

The v2 implementation in Stage 3 begins by reverting the v1 commits in the "Revert" rows above, then layering on the v2-only changes. Git history is preserved (no force-push).

## 11. Test Strategy

| Test file | Scope | Style |
|:---|:---|:---|
| `tests/test_orchestrator.py` (new) | All `parse_latest_dispatch` cases (well-formed, malformed, escaped quotes, no dispatch); `detect_completion` cases (marker, mid-stream marker truncation, silence, stall, pending, error); `validate_dispatch_text` rejection of embedded marker; `is_workflow_complete` and `is_workflow_abort` parsing | Pure unit tests, no fixtures, no mocks |
| `tests/test_workflows.py` (new) | All three built-ins parse via `get_workflow`; `required_roles` matches expected for each; `render_for_orchestrator` produces non-empty string with role names interpolated; unknown id raises `KeyError` | Pure unit tests |
| `tests/test_tmux_attach.py` (existing 25 tests) | F-03 carry-over | Unchanged — must continue to pass |
| `tests/test_supervisor.py` (new, optional) | `dispatch_loop` integration test using a fake `SessionManager` that records `send_keys` calls and replays scripted `capture_pane_output` results | One end-to-end happy path + one stall path |
| Manual smoke test | Run `python -m agent_management` against an empty `.agent_management/`, create a group with `standard` workflow, observe orchestrator dispatching, attach to each pane to verify content | Documented in `scripts/smoke-test-req-012.sh` (optional, generated in Stage 7) |

## 12. Configuration Constants

`shared/config.py` changes:

- **Add**: `WORKER_SILENCE_TIMEOUT = 60.0`, `ORCHESTRATOR_STALL_TIMEOUT = 600.0`, `DISPATCH_POLL_INTERVAL = 0.5`
- **Keep**: `SESSION_START_TIMEOUT`, `DB_PATH`, `TEMP_DIR`, `TMUX_SESSION_PREFIX`, `CLAUDE_CMD`, `DIRECT_SEND_MAX_LEN`, `SESSION_STOP_TIMEOUT`
- **Drop**: `MCP_HOST`, `MCP_PORT`, `MCP_CONFIG_DIR`, `PENDING_EVENT_CAPACITY`, `SUPERVISOR_POLL_INTERVAL`, `get_mcp_port()`, the runtime port allocation logic

`pyproject.toml` changes:

- **Drop**: `mcp`, `fastmcp`, `uvicorn` (only used by the deleted `mcp_server.py`)
- **Keep**: `aiosqlite`, `textual`, all other deps

## 13. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-07 | Initial six-file targeted-patch design for the v1 event-bus fix (identity injection, unified pending_events delivery, pane routing, readiness poll, role templates, error toasts) | ALL | Initial design tied to v1 requirement.md |
| v2 (stub) | 2026-04-08 (am) | Document temporarily reduced to a regeneration stub immediately after requirement.md was amended via `/req-amend`, deferring detailed design to this Stage 2 re-run. | ALL | requirement.md v2 (Affected Scope = ALL) invalidated v1 design instantly; honest pipeline behaviour required regeneration through Stage 2 with its own approval gate. |
| v2 | 2026-04-08 (pm) | Replaced stub with the full v2 technical design covering all 13 areas required by the stub: tech stack, design principles, architecture, ten module designs (`backend/orchestrator.py`, `backend/workflows.py`, `backend/supervisor.py` rewrite, `backend/session_manager.py` modifications, `backend/repository.py` schema diff + template rewrite, `backend/models.py` enum + dataclass changes, `frontend/app.py` schema modal + dropdown + stall toast, `frontend/group_panel.py` workflow dropdown, `frontend/agent_pane.py` badge re-purpose, role templates rewrite), data model with v2 schema diff and dropped tables/columns, in-pane wire protocol API, key flows, shared module reuse strategy, 11-row risk register, explicit code carry-over from v1, test strategy, configuration constants. | ALL | Stage 2 regeneration after the v1 → v2 architectural pivot. Source of truth: requirement.md v2 (F-03/F-04/F-05/F-06/F-07/F-08/F-09/F-10/F-11). |
