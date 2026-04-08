[English](README.md) | [简体中文](README.zh-CN.md)

---

# Agent Orchestra

> A local TUI that conducts a team of Claude Code CLI agents through automated software-delivery workflows. An LLM **Orchestrator** sits in the middle of five role-specialised worker agents (PM, Tech Director, Developer, Tester, User) and dispatches them via tmux — using the `/req-*` skill catalogue to drive the full `analyse → design → code → security → cleanup → review → verify → archive` pipeline.

---

## Table of Contents

- [What is Agent Orchestra?](#what-is-agent-orchestra)
- [Why does it exist?](#why-does-it-exist)
- [Architecture at a glance](#architecture-at-a-glance)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quickstart](#quickstart)
- [Interaction model](#interaction-model)
- [The orchestrator protocol](#the-orchestrator-protocol)
- [Built-in workflows](#built-in-workflows)
- [Skill catalogue](#skill-catalogue)
- [Keyboard shortcuts](#keyboard-shortcuts)
- [Configuration](#configuration)
- [Data directory layout](#data-directory-layout)
- [Development](#development)
- [Project history (REQ-driven)](#project-history-req-driven)
- [Known limitations](#known-limitations)

---

## What is Agent Orchestra?

Agent Orchestra is a **Textual**-based local TUI that runs on top of `tmux` and **Claude Code CLI**. It lets you spin up a *group* of agents — each one a real Claude CLI process in its own tmux pane — and have them collaborate on a software task by following a workflow playbook. The work is not scripted in Python: it is dispatched at runtime by an LLM **Orchestrator** agent that reads the latest result, decides who should work next, and tells them exactly what to do (including which `/req-*` skill, if any, they should invoke).

Concretely, when you start a group with the `standard` workflow you get **six** Claude CLI processes running side-by-side:

```
┌─ tmux session: agent-mgmt-<group-id> ─────────────────────────────────┐
│                                                                       │
│  ╔═════════════╗  ╔═════════════╗  ╔═════════════╗  ╔═════════════╗   │
│  ║ Product     ║  ║ Tech        ║  ║ Developer   ║  ║ Tester      ║   │
│  ║ Manager     ║  ║ Director    ║  ║             ║  ║             ║   │
│  ║ (claude CLI)║  ║ (claude CLI)║  ║ (claude CLI)║  ║ (claude CLI)║   │
│  ╚══════▲══════╝  ╚══════▲══════╝  ╚══════▲══════╝  ╚══════▲══════╝   │
│         │ send-keys      │                │                │          │
│         │ capture-pane   │                │                │          │
│         │                │                │                │          │
│  ╔══════▼══════╗  ┌──────┴────────────────┴────────────────┘          │
│  ║    User     ║  │                                                    │
│  ║ (claude CLI)║  │                                                    │
│  ╚══════▲══════╝  │                                                    │
│         │         │                                                    │
│         └─────────┤                                                    │
│                   │                                                    │
│          ╔════════▼═════════╗                                          │
│          ║   Orchestrator   ║  ◀── 6th Claude CLI process. Its system  │
│          ║   (claude CLI)   ║      prompt carries the workflow, the    │
│          ║                  ║      worker roster, and the /req-*       │
│          ║ emits <<DISPATCH ║      skill catalogue. Decides dynamically│
│          ║ role="dev"       ║      what to dispatch next, when to skip │
│          ║  text="...">>    ║      steps, when to loop on tester       │
│          ║                  ║      failures, and when the workflow is  │
│          ║                  ║      complete.                           │
│          ╚══════════════════╝                                          │
└───────────────────────────────────────────────────────────────────────┘
```

On top of the tmux layer sits a **Textual TUI** that:

- Shows every pane's live output in a read-only preview with ANSI colour rendering and scroll-lock
- Lets you click `Enter` on any pane to jump into it for full native terminal interaction (or press Enter when the preview is focused)
- Has a dedicated **input box** that forwards every keystroke — letters, digits, punctuation, arrows, Tab, Ctrl+chars — directly to the focused agent in real time
- Has an 8-button **quick keyboard** (`Continue` `Y` `N` `Esc` `^C` `↑` `↓` `^D`) for one-tap responses to interactive Claude CLI prompts
- Collapses the admin row (`Pause / Resume / Edit / Restart / Delete`) behind a `⋯` toggle to save vertical space
- Shows workflow lifecycle events (dispatch, worker result, stall, complete, abort) in a bottom log

---

## Why does it exist?

Running several Claude Code CLI instances at once is useful but operationally painful: you have to manually cross-pollinate their contexts, paste results between terminals, remember who's supposed to work next, and keep track of stale sessions. Multi-agent frameworks exist but they usually require you to re-plumb your whole codebase onto their runtime.

Agent Orchestra's bet is different:

1. **Keep the agents unchanged.** Each worker is a plain `claude --dangerously-skip-permissions` process. Whatever you already know about Claude Code CLI (slash commands, MCP tools, skill system, tmux attach for debugging) still works.
2. **Let an LLM orchestrator decide the order.** Instead of hardcoding a state machine in Python, the orchestrator *is* a sixth Claude CLI with a carefully-crafted system prompt. It sees each worker's output, chooses the next action, and does its own error recovery.
3. **Use tmux as the transport.** No MCP server, no custom RPC, no supply-chain risk. `send-keys` writes, `capture-pane` reads. If anything goes wrong, you can `tmux attach -t agent-mgmt-<group-id>` and see exactly what each agent sees.
4. **Make interaction native.** The TUI never pretends to be a terminal it isn't. You type in a key-forwarding input box for real-time typing, or hit Enter to `tmux switch-client` into the actual worker pane when you need Tab completion / paste / menus / ANSI cursor apps.

---

## Architecture at a glance

```
                              ┌─────────────────────┐
                              │  Textual TUI (app)  │
                              │                     │
                              │  - AgentPane × N    │
                              │  - GroupPanel       │
                              │  - EventLog         │
                              │  - Modal dialogs    │
                              └──────────┬──────────┘
                                         │ user actions
                                         ▼
                              ┌─────────────────────┐
                              │   Supervisor        │
                              │                     │
                              │  - start/stop_group │
                              │    (parallel via    │
                              │     asyncio.gather) │
                              │  - dispatch_loop    │
                              │    (orchestrator    │
                              │     pane polling)   │
                              │  - force_advance /  │
                              │    abort_workflow   │
                              └──────────┬──────────┘
                                         │
                   ┌─────────────────────┼─────────────────────┐
                   │                     │                     │
                   ▼                     ▼                     ▼
        ┌──────────────────┐  ┌───────────────────┐  ┌───────────────────┐
        │ SessionManager   │  │ orchestrator.py   │  │ workflows.py      │
        │                  │  │                   │  │                   │
        │ - tmux helpers   │  │ Pure functions:   │  │ Playbook data:    │
        │ - send_keys      │  │ - parse_latest_   │  │ - STANDARD        │
        │ - send_raw_keys  │  │     dispatch      │  │ - PROTOTYPE       │
        │ - capture_pane_  │  │ - detect_         │  │ - RESEARCH        │
        │     full (ansi)  │  │     completion    │  │                   │
        │ - start_agent_   │  │ - is_workflow_    │  │ Skill catalogue:  │
        │     session      │  │     complete /    │  │ - AVAILABLE_      │
        │ - _render_       │  │     abort         │  │     SKILLS        │
        │     orchestrator │  │ - validate_       │  │                   │
        │     _prompt      │  │     dispatch_text │  │ Renderers:        │
        │ - _wait_for_     │  │                   │  │ - render_for_     │
        │     pane_ready   │  │                   │  │     orchestrator  │
        │     (F-04 poll)  │  │                   │  │ - render_roster   │
        └──────────┬───────┘  └───────────────────┘  │ - render_skill_   │
                   │                                  │     catalogue    │
                   │ tmux subprocess                  └───────────────────┘
                   ▼
        ┌──────────────────┐
        │ tmux panes       │
        │                  │
        │ Claude CLI × 6   │
        │ per group        │
        └──────────────────┘
                   │
                   ▼
        ┌──────────────────┐
        │ SQLite           │
        │ .agent_management│
        │   /state.db      │
        │                  │
        │ - groups         │
        │ - agents         │
        │ - sessions       │
        │ - role_templates │
        │ - meta           │
        └──────────────────┘
```

**Key points:**

- **No MCP server, no event bus.** The orchestrator communicates with workers exclusively via `tmux send-keys` and `tmux capture-pane`. Earlier versions of the platform used an MCP pub/sub system; REQ-012 v2 deleted it entirely after analysing that "LLMs don't have inboxes" made the event-bus model fundamentally unreliable.
- **Workers are oblivious to the orchestrator.** Their system prompts never mention "Orchestrator", "dispatch", or "[WORKER_RESULT]". They just know: "receive a task, do it, emit `<<TASK_DONE>>` on the final line, stop". The callback from worker → orchestrator is enforced at the Python code level by `Supervisor.dispatch_loop`, never via prompt cooperation.
- **The workflow is a playbook, not a state machine.** The three built-in workflows (`standard`, `prototype`, `research`) are rendered into the orchestrator's system prompt as *typical flows*. The orchestrator is told explicitly: "这是建议，不是硬性要求，根据每次的情况自主编排".
- **Breaking schema changes are destructive.** On startup, if `meta.schema_version` doesn't match the current `SCHEMA_VERSION`, the app shows a reset modal and offers to wipe `.agent_management/`. No migration scripts. This is a local single-user tool.

---

## Requirements

| Dependency | Version | Notes |
|:---|:---|:---|
| Python | 3.13+ | Used via `uv` by default |
| [uv](https://docs.astral.sh/uv/) | latest | Fastest way to install and run |
| tmux | 3.0+ | The platform expects `switch-client` and modern send-keys semantics |
| [Claude Code CLI](https://docs.claude.com/claude-code) | latest | `claude --dangerously-skip-permissions` must work |
| OS | macOS / Linux / Windows | Windows uses Git Bash/MSYS for tmux; tested on Windows 11 + `plink` tmux forwarding |

The Python runtime has only two external dependencies: `textual` and `aiosqlite`. Everything else (ANSI rendering, sqlite, asyncio, subprocess) is stdlib-backed or pulled in transitively by Textual.

---

## Installation

```bash
# Clone the repo (submodule pulls in the .claude/skills toolkit)
git clone --recurse-submodules git@github.com:GOODDAYDAY/agent-orchestra.git
cd agent-orchestra

# Sync dependencies into a local .venv
uv sync
```

Verify the install:

```bash
uv run python -m agent_management --show-config
```

This prints the resolved paths, Claude CLI command, and all tuning constants without starting the TUI. If it completes cleanly, you're ready.

---

## Quickstart

### Start the TUI

**macOS / Linux:**
```bash
bash scripts/start.sh
```

**Windows (Git Bash):**
```bash
scripts/start.bat
```

The start scripts set `AGENT_MGMT_DATA_DIR=<project>/.agent_management/` so all runtime state lives inside the project directory (not in `~/`), and run `uv sync` before launching.

### Create a group

1. Press `g` (or click `+ Group`) to open the New Group dialog
2. Enter a group name (e.g. `sprint-01`)
3. Enter a working directory (path autocomplete is available — tab to accept)
4. Pick a workflow: **Standard**, **Prototype**, or **Research** (see [Built-in workflows](#built-in-workflows))
5. Click **Create**

The platform auto-creates **six** agents: Orchestrator + PM + Tech Director + Developer + Tester + User, all sharing the working directory. They are listed in the group panel.

### Start the group

Click **▶ Start**. The platform:

1. Creates a tmux session named `agent-mgmt-<group-id>`
2. Starts all 5 worker agents **in parallel** (concurrent `tmux new-window` + Claude CLI launch + readiness poll)
3. Waits for all 5 workers to reach `active` status
4. Renders the orchestrator's system prompt with the live worker roster, workflow, completion marker, and skill catalogue
5. Starts the orchestrator agent last
6. Spawns the dispatch loop as a background asyncio task
7. The orchestrator reads its system prompt and emits its first `<<DISPATCH ...>>` within a few seconds

### Watch and intervene

Each AgentPane shows the live output of its worker via a `RichLog` widget with ANSI colour rendering. Four ways to interact:

- **Read-only**: just watch the preview scroll
- **Quick keyboard**: click `Continue`, `Y`, `N`, `Esc`, `^C`, `↑`, `↓`, or `^D` to send a one-shot keystroke
- **Real-time typing**: click the input box (`⌨ click here to type to agent`) and start typing. Every character is forwarded immediately.
- **Full attach**: click anywhere on the preview to focus it, then press **Enter**, or click the **Enter** button in the header. Your terminal switches into the agent's tmux pane for full native interaction. Press **Ctrl+B D** to detach and return to the TUI.

When the orchestrator emits `<<WORKFLOW_COMPLETE>>`, a toast shows and the dispatch loop exits. If the orchestrator emits `<<WORKFLOW_ABORT reason="..."/>>`, the reason is shown in the toast.

---

## Interaction model

Agent Orchestra ships **four distinct interaction modes** on each pane, each appropriate for its use case. The old "type a message and hit Send" model from v1 is gone — it couldn't handle interactive Claude CLI prompts, arrow-key menus, or Tab completion.

### 1. Read-only preview (look)

A Textual `RichLog` widget showing the live `tmux capture-pane -p -e` output of the worker's pane. `rich.text.Text.from_ansi` converts ANSI escape codes into colour/bold/dim rendering.

**Scroll lock:** the viewport auto-follows the bottom by default. If you scroll up manually, auto-follow pauses and a `↓ jump to latest` button appears. Click it (or press `End` while focused) to jump to the bottom and resume auto-follow.

### 2. Button keyboard (one-tap)

Eight buttons arrayed in a single row below the preview:

| Button | Sends |
|:---|:---|
| `Continue` | `continue\n` (Claude CLI's "keep going" command) |
| `Y` | `y\n` (confirms `[Y/n]` prompts) |
| `N` | `n\n` |
| `Esc` | Real Escape keystroke (cancels menus) |
| `^C` | Ctrl+C (interrupt) |
| `↑` | Up arrow (selection menu navigation) |
| `↓` | Down arrow |
| `^D` | Ctrl+D (EOF / exit) |

Each button click produces exactly one `tmux send-keys` call.

### 3. Dedicated input box (real-time typing)

The last row of each AgentPane is a focusable `Static` widget labelled `⌨ click here to type to agent (double-Esc to leave)`. Click it and start typing:

- **Every keystroke** is mapped to a tmux key spec via `frontend.key_forwarding.tmux_args_for_key(event)` and forwarded immediately via `SessionManager.send_raw_keys`. No batching, no compose-and-submit.
- **Punctuation works.** The helper prefers `event.character` for printable input so Shift-modified chars (`!@#$%^&*()`) that Textual reports as `key="exclamation_mark"` still reach the agent as `!`.
- **Tab, arrows, Ctrl+chars, Enter** all forward. Tab does NOT move TUI focus.
- **Double-Esc** leaves the input box. A single Esc is forwarded to the agent (Claude CLI uses it to dismiss menus).
- **Local echo**: typed printable characters are also shown in the widget's label for instant visual feedback.

### 4. Full Attach (escape hatch)

When you need the real thing — Tab completion, paste, mouse selection, ANSI cursor apps, long compose sessions — press **Enter** when the AgentPane's preview has focus, or click the **Enter** button in the header. Under the hood this calls `tmux_attach.grouped_attach` (when you launched the TUI from inside tmux) or `suspend_attach` (when you're outside tmux).

Either path lands you in the worker's pane with full native interaction. Press **Ctrl+B D** to detach and return to the TUI. The pane state is preserved; the TUI resumes polling.

---

## The orchestrator protocol

This is the wire protocol between the Orchestrator agent and the platform. It's **entirely pane text** — no RPC, no MCP, no JSON blobs.

### Dispatch — orchestrator asks a worker to do something

The orchestrator emits a single line of the form:

```
<<DISPATCH role="developer" text="Please invoke /req-3-code with goal: implement the caching layer. After that, emit <<TASK_DONE>>.">>
```

- `role` must be a valid worker role name (`product_manager`, `tech_director`, `developer`, `tester`, `user`) — not `orchestrator`
- `text` is the full prompt to send to the worker; supports `\"` to escape quotes
- **Self-closing form only.** Earlier versions required a `<</DISPATCH>>` closing tag; REQ-016 F-04a dropped that requirement because LLMs often forget or mutate closing tags
- `text` must not contain `<<TASK_DONE>>`, `<<WORKFLOW_COMPLETE>>`, or `<<WORKFLOW_ABORT` — those are reserved platform markers
- `text` should not contain newlines; if it does, the dispatch_loop normalises them to spaces before forwarding

### Completion — worker signals it's done

The worker emits its artifact followed by a final line:

```
<<TASK_DONE>>
```

The supervisor's dispatch_loop polls `tmux capture-pane` every 500 ms and detects the marker via line-start regex match.

**Three completion layers** are tried in order of preference:

| Layer | Trigger | Artifact quality |
|:---|:---|:---|
| **marker** | `<<TASK_DONE>>` line detected | Clean — extracted up to the marker |
| **silence** | Worker pane has no new output for 60 s AND has some content | Possibly incomplete; `via="silence"` flag set in the result block |
| **stall** | No completion signal within 10 min of dispatch | Operator intervention required — a TUI toast offers Force Advance / Abort Workflow |

### Worker result — platform returns the artifact to the orchestrator

```
[WORKER_RESULT role="developer" via="marker"]
<everything the worker produced up to the marker, verbatim>
[/WORKER_RESULT]
```

This is injected into the orchestrator pane via `send_keys`. The orchestrator then decides the next dispatch.

### Workflow completion and abort

When the orchestrator judges the workflow is done:

```
<<WORKFLOW_COMPLETE>>
```

When something is unrecoverable:

```
<<WORKFLOW_ABORT reason="tests keep failing after 3 retries"/>>
```

Both terminate the dispatch loop and post a Textual message to the TUI.

### Platform-to-orchestrator errors

If the orchestrator does something wrong, the platform injects one of:

- `[PLATFORM_ERROR: unknown role 'marketing' — valid roles: developer, pm, ...]`
- `[PLATFORM_ERROR: dispatch text must not contain '<<TASK_DONE>>']`
- `[WORKER_ERROR role="developer" reason="pane vanished"]`
- `[PLATFORM_STALL: no completion signal from role="developer" after 600 seconds]`

The orchestrator sees these as regular text in its pane and is expected to recover (retry, skip, abort — its choice).

### Tester failure loop

Workflows like `standard` carry a `failure_loop_to` on the Tester step. When the Tester's artifact contains `<<TESTS_FAILED>>` in addition to `<<TASK_DONE>>`, the supervisor increments a retry counter and the orchestrator's next dispatch should typically go back to Developer. The counter is informational; the orchestrator makes the final decision about whether to retry, bypass, or abort.

---

## Built-in workflows

Three playbooks are shipped in `backend/workflows.py`. The orchestrator treats them as **typical flows**, not strict state machines — skipping, repeating, and reordering are all explicitly permitted.

### `standard`

Full requirement-to-acceptance pipeline:

```
1. Product Manager   — Produce a complete requirement specification.
2. Tech Director     — Review the spec and produce a technical design.
3. Developer         — Implement the technical design.
4. Tester            — Run the test suite and report results.
                       If <<TESTS_FAILED>>, loop back to step 3 (max 3 retries).
5. User              — Acceptance review by the human (or human stand-in) user.
```

### `prototype`

Two-step playbook for quick experiments:

```
1. Developer         — Implement the prototype.
2. User              — Acceptance review of the prototype.
```

### `research`

Design-only playbook with no coding phase:

```
1. Product Manager   — Frame the research question and the desired outcomes.
2. Tech Director     — Investigate and produce a technical findings document.
3. User              — Acceptance review of the findings.
```

---

## Skill catalogue

The orchestrator has an on-prompt catalogue of `/req-*` skills it can ask any worker to invoke. The catalogue is pure data (`workflows.AVAILABLE_SKILLS`); adding a new skill is a one-line tuple append.

| Skill | Purpose |
|:---|:---|
| `/req-1-analyze` | Expand a brief description into a complete requirement document (requirement.md) with background, functional requirements, acceptance criteria, and change log |
| `/req-2-tech` | Produce a technical design (technical.md): tech stack, architecture, module design, data model, key flows, risks |
| `/req-3-code` | Implement code following the technical design: high-cohesion low-coupling modules, logging, comments, automation scripts |
| `/req-4-security` | Security review: injection attacks, data leakage, authentication issues, configuration vulnerabilities |
| `/req-5-cleanup` | Structural cleanup: detect unused code, dead code, duplicated logic, optimise cohesion/coupling without changing business logic |
| `/req-6-review` | Compare the implementation against the requirement document item by item; flag undeclared changes |
| `/req-7-verify` | Verification: build check, runtime check, automated testing, generate verification scripts |
| `/req-8-done` | Final archive: consistency check, update index.md status to Completed |

**Crucially, skill selection is a runtime decision by the orchestrator LLM.** There is no "PM always runs /req-1" mapping in the workflow dataclass or in the Python code. The orchestrator reads the catalogue, sees the current situation, and decides per dispatch which skill (if any) to tell the worker to invoke. REQ-017 walked back an earlier attempt to hardcode skill↔step mapping — the full rationale is in `requirements/REQ-017-restore-orchestrator-autonomy/requirement.md`.

---

## Keyboard shortcuts

### Global (TUI-level)

| Key | Action |
|:---|:---|
| `n` | New Agent |
| `g` | New Group |
| `t` | Role Templates editor |
| `z` | Debug shell (opens a zsh pane) |
| `c` | Clear all (stops all sessions, wipes event log) |
| `q` | Quit |

### AgentPane-level

| Key / action | Effect |
|:---|:---|
| Click `⋯` in header | Toggle the Pause/Resume/Edit/Restart/Delete admin row visibility |
| Click `Enter` in header **or** focus preview + press Enter | Attach to the worker's tmux pane |
| Click inside read-only preview | Focus the preview for scroll / Enter-attach |
| Click input box | Enter key-forwarding mode |
| Single `Esc` in input box | Forwarded to agent (cancels Claude CLI menus) |
| Double `Esc` in input box | Leave input box (return focus to pane container) |
| End key on focused preview | Jump to latest and re-enable auto-follow |

### Inside a full Attach

| Key | Effect |
|:---|:---|
| `Ctrl+B D` | Detach from the worker pane and return to the TUI |

---

## Configuration

All tunables live in `src/agent_management/shared/config.py`. The primary way to override is via environment variables before launch.

### Environment variables

| Variable | Default | Purpose |
|:---|:---|:---|
| `AGENT_MGMT_DATA_DIR` | `<cwd>/.agent_management/` | Directory for SQLite DB, tmp files, logs |

### Tunable constants (code-level)

| Constant | Default | Purpose |
|:---|:---|:---|
| `CLAUDE_CMD` | `["claude", "--dangerously-skip-permissions"]` | Claude CLI invocation base |
| `TMUX_SESSION_PREFIX` | `"agent-mgmt"` | Prefix for per-group tmux sessions |
| `SESSION_START_TIMEOUT` | `30.0` s | Maximum time to wait for a Claude CLI pane to produce output (readiness poll) |
| `SESSION_STOP_TIMEOUT` | `5.0` s | Graceful shutdown grace period before `kill-pane` |
| `DISPATCH_POLL_INTERVAL` | `0.5` s | How often the supervisor polls orchestrator / worker panes |
| `WORKER_SILENCE_TIMEOUT` | `60.0` s | Silence-layer completion timeout |
| `ORCHESTRATOR_STALL_TIMEOUT` | `600.0` s | Stall-layer timeout (triggers the operator toast) |
| `PANE_REFRESH_INTERVAL` | `0.25` s | Read-only preview refresh rate (4 Hz) |
| `OUTPUT_BUFFER_LINES` | `500` | `RichLog` ring buffer cap |
| `DIRECT_SEND_MAX_LEN` | `200` | Legacy constant (REQ-016 removed the branching; kept for compat) |
| `SCHEMA_VERSION` | `5` | Bumped on breaking schema changes; triggers the destructive-reset modal |

### Check resolved config

```bash
uv run python -m agent_management --show-config
```

---

## Data directory layout

All runtime state lives under `AGENT_MGMT_DATA_DIR` (default: `<project>/.agent_management/`):

```
.agent_management/
├── state.db              # SQLite — groups, agents, sessions, role templates, meta
├── platform.log          # stdlib logging output
└── tmp/                  # Temporary files
    ├── orch_prompt_*.txt # Rendered orchestrator prompts (auto-cleaned after 30s)
    └── agent_msg_*.txt   # Legacy large-payload send helpers (no longer used)
```

### SQLite schema (v5)

```
agents         — id, name, role, working_dir, system_prompt,
                  system_prompt_file, paused, status, created_at, updated_at
groups         — id, name, workflow_id, created_at
group_members  — group_id, agent_id (many-to-many)
sessions       — id, agent_id, group_id, claude_session_id,
                  previous_session_id, tmux_session_name, tmux_pane_id,
                  status, started_at, stopped_at
role_templates — role, display_name, system_prompt
meta           — key/value (schema_version, template_version)
```

**Destructive migration policy**: the app compares `meta.schema_version` at startup. If it doesn't match the current `SCHEMA_VERSION` constant, the user is shown a modal offering to wipe `.agent_management/` and continue, or quit. There are no migration scripts — this is a local single-user tool and migration complexity is not justified.

---

## Development

### Running the test suite

```bash
uv run pytest -q
```

Current totals (as of REQ-017): **454 tests passing in ~8 seconds**.

Test files:

| File | Focus |
|:---|:---|
| `tests/test_orchestrator.py` | Dispatch parser, completion detection, workflow marker regexes |
| `tests/test_workflows.py` | Built-in workflow structure, AVAILABLE_SKILLS catalogue, render helpers |
| `tests/test_repository.py` | SQLite schema, CRUD, role template integrity, schema-mismatch detection |
| `tests/test_models.py` | Domain dataclass defaults and invariants |
| `tests/test_session_manager.py` | Payload sanitiser, orchestrator prompt rendering |
| `tests/test_supervisor_concurrency.py` | start/stop/resume_group parallelism proofs |
| `tests/test_dispatch_integration.py` | End-to-end dispatch loop with FakeSessionManager — happy path, silence layer, stall layer, worker crash, scrollback resilience |
| `tests/test_key_forwarding.py` | Exhaustive Textual key → tmux argv mapping |
| `tests/test_agent_pane.py` | Textual widget tests via `App.run_test()` pilot — admin toggle, quick keyboard, InputBox forwarding |
| `tests/test_tmux_attach.py` | F-03 attach path (grouped / suspend / environment detection / stale session cleanup) |

The integration tests use a `FakeSessionManager` that mirrors the real `SessionManager`'s public API, records `send_keys` calls, and replays scripted `capture_pane_full` responses. No tmux or subprocess is ever started during tests.

### Adding a new workflow

Edit `backend/workflows.py`. Define a new `Workflow` literal with `Step(...)` entries, add it to `BUILT_IN_WORKFLOWS`. The orchestrator template picks it up automatically on next start via the `{{WORKFLOW_DEFINITION}}` placeholder. No other code changes needed. Don't forget to bump `_TEMPLATE_VERSION` in `repository.py` if you also changed the orchestrator prompt — force-update is how the new templates propagate into existing `.agent_management/` state.

### Adding a new `/req-*` skill

Edit `backend/workflows.py` → `AVAILABLE_SKILLS` — append a `(name, description)` tuple. The orchestrator's prompt auto-renders it via `render_skill_catalogue()`. The skill itself is a Claude Code CLI skill directory under `.claude/skills/` — that's a separate submodule (`my-skills`).

### Adding a role

1. Add the enum value to `AgentRole` in `backend/models.py`
2. Add a default template to `repository._DEFAULT_TEMPLATES`
3. Bump `_TEMPLATE_VERSION`
4. Update tests as needed

### Requirement-driven development

This project is itself developed using the `/req` skill suite. Every meaningful change goes through an 8-stage pipeline (`analyse → tech design → code → security → cleanup → requirement review → verify → archive`). The full history of requirement documents lives under `requirements/`:

```
requirements/
├── index.md
├── REQ-001-agent-management-platform/
├── REQ-002-grid-layout/
├── ...
└── REQ-017-restore-orchestrator-autonomy/
    ├── requirement.md   # What we built and why
    ├── technical.md     # How we built it
    └── *.puml / *.svg   # PlantUML diagrams (where generated)
```

Reading the REQ documents in order is the fastest way to understand how and why the current architecture exists.

---

## Project history (REQ-driven)

| REQ | Status | Summary |
|:---|:---|:---|
| REQ-001 | Completed | Original agent management platform — multi-agent Claude CLI orchestration TUI with pub/sub and session resume |
| REQ-002 | Completed | 2-column grid layout + session-ID fix |
| REQ-003 | Completed | Configurable data directory (`AGENT_MGMT_DATA_DIR`) |
| REQ-004 | Completed | Path input autocomplete |
| REQ-005 | Completed | CLI `--help` and `--show-config` flags |
| REQ-006 | Completed | Tech Director role enum value |
| REQ-007 | Completed | Editable role templates, `t` keybinding, auto-fill on role select |
| REQ-008 | Completed | AgentPane focus input + adaptive layout |
| REQ-009 | Completed | Group auto-create agents |
| REQ-010 | Completed | Delete group / agent with cascade |
| REQ-011 | Completed | Native tmux attach / detach |
| REQ-012 v1 | Superseded | Original three-bug patch on the MCP event-bus architecture |
| **REQ-012 v2** | **Completed** | **Architectural pivot: deleted the MCP event bus; introduced the LLM Orchestrator model, `<<DISPATCH>>` protocol, 3 built-in workflows, 3-layer completion detection** |
| REQ-013 | Superseded by REQ-015 | Original design for terminal attach interaction and scroll fix |
| REQ-014 | Completed | Quality hardening on REQ-012 v2: scrollback bug, tmp file leak, pane crash detection, test suite expansion (90 → 189) |
| REQ-015 | Completed | Native-first interaction: delete old Input row; ANSI-rendering preview with scroll lock; 8-button quick keyboard; dedicated input box with pure key forwarding; Enter-on-focused-preview triggers Attach |
| REQ-016 | Completed | 5-issue polish: collapsible admin row, punctuation forwarding fix, concurrent start/stop/resume, dispatch reliability (self-closing parser, cat fallback removed, newline normalisation, diagnostic logging) |
| REQ-017 | Completed | Restore orchestrator autonomy — revert REQ-016's per-step skill hardcoding; introduce AVAILABLE_SKILLS catalogue; orchestrator template emphasises autonomous decision-making; worker templates completely hide the orchestrator abstraction |

---

## Known limitations

- **Single active group at a time.** `Supervisor._active_group_id` is a single field. Running two groups concurrently requires a separate TUI process.
- **Template customisation is overwritten on version bumps.** When `_TEMPLATE_VERSION` increments, built-in role templates are force-overwritten. Users who customised templates must re-apply their edits.
- **No migration scripts.** Schema version mismatch triggers a destructive reset.
- **6-pane layout assumes a wide terminal.** Recommended ≥ 160 columns.
- **Orchestrator dispatch is sequential.** Only one worker in flight at a time; parallel dispatch is Out of Scope.
- **Multi-line dispatch text is collapsed.** The dispatch_loop replaces `\n` with space before `send_keys` to avoid tmux submitting early.
- **Non-Claude CLI workers are not supported.** All panes run `claude --dangerously-skip-permissions`; there is no abstraction layer for swapping runtimes.
- **`textual-terminal`-style embedded terminal not implemented.** Native interaction is achieved via (a) key-forwarding input box for real-time typing, (b) full tmux attach for everything else. An actual embedded VT100 widget was evaluated in REQ-015 and rejected as too invasive.
- **On Windows, tmux must be reachable from Git Bash or WSL.** The platform shells out to `tmux` via `asyncio.create_subprocess_exec` — if tmux isn't on `PATH`, nothing will work.

---

*Agent Orchestra is developed using itself, dogfood-style. If you are reading this in the repo, the README, the requirements docs, the code, and the tests were all produced by Claude Code agents driven by a human operator through the `/req` pipeline.*
