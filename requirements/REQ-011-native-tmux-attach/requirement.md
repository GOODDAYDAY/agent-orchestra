# REQ-011 Native tmux Attach / Detach for Agent Panes

> Status: Completed
> Created: 2026-04-07
> Updated: 2026-04-07

## 1. Background

The Agent Management Platform runs multiple Claude Code CLI agents inside tmux panes, displaying their output via `tmux capture-pane` in a Textual TUI. When Claude Code CLI presents interactive prompts — permission dialogs, selection menus, `[Y/n]` confirmations — the TUI can display the output but cannot forward raw keystrokes to the agent's stdin. Users must manually locate the agent's tmux pane outside the TUI to respond.

This creates a friction-heavy workflow: the user must break out of the TUI, find the correct tmux session/window, respond to the prompt, then navigate back to the TUI. The feature requested here provides a one-click "Enter" path per AgentPane that switches the user's terminal directly into the agent's live tmux pane, and Ctrl+B D to return to the TUI seamlessly.

**Key constraint**: The platform runs inside a tmux session (`agent-mgmt-{group_id}`) and uses FastMCP over SSE on port 8765 for inter-agent communication. The `suspend()` path must account for asyncio event loop suspension and its effect on SSE connections.

## 2. Target Users & Scenarios

**Primary user**: Developer running the Agent Management Platform locally inside tmux, agents ask permission dialogs that need human input.

| Scenario | Description |
|:---|:---|
| S-01 In-tmux interaction | User is running TUI inside a tmux session; wants to enter agent pane without disrupting TUI |
| S-02 Out-of-tmux interaction | User launched TUI directly in a terminal (not inside tmux); needs TUI suspend + attach |
| S-03 Nested tmux | TUI is running inside a tmux pane that is itself nested inside another tmux session |
| S-04 Agent not active | User clicks Enter on an agent that has no live tmux pane |
| S-05 Concurrent access | Another terminal is already attached to the same agent's pane |
| S-06 Platform restart after crash | Previous run left orphan grouped sessions in tmux |

## 3. Functional Requirements

### F-01 Enter Button and Entry Guard

**Main flow:**
1. Each AgentPane adds an "Enter" button in the controls row
2. Button is enabled only when agent status is `active` or `paused` (has a live pane)
3. User clicks Enter → button immediately disabled
4. Validate that agent has a `tmux_pane_id` and `tmux list-panes` confirms it exists
5. If validation passes: dispatch `AttachRequested(agent_id)` message to app
6. On completion or failure: re-enable button (timeout: 5 s forced re-enable)

**Error handling:**
- Agent has no `tmux_pane_id` or pane does not exist → toast "Agent has no active pane", re-enable button
- Button disabled during `starting` / `stopping` / `degraded` states

**Edge cases:**
- Agent restarts mid-flow → pane_id changes; current enter operation is cancelled with toast

### F-02 Environment Detection and Path Selection

**Main flow:**
1. Check `os.environ.get("TMUX")` + run `tmux display-message -p '#{session_name}'`
2. If both succeed → **grouped-session path** (F-03)
3. If `$TMUX` absent or `display-message` fails → **suspend path** (F-04)
4. Detection result cached per Enter click (re-validated each click, not at startup)

**Error handling:**
- Nested tmux detected (outer `$TMUX` differs from expected `agent-mgmt-*` prefix) → toast "Nested tmux not supported, please enter the agent session manually" and abort
- tmux binary not found → toast "tmux not found in PATH"

**Edge cases:**
- User detaches and re-attaches the outer tmux session between clicks → re-validation on each click ensures correct path

**Architecture note:** When active MCP/SSE connections are present, the grouped-session path is strongly preferred because it does not suspend the asyncio event loop, avoiding SSE disconnection entirely.

### F-03 Grouped Session Path (In-tmux)

**Main flow:**
1. Resolve agent pane to `session_name:window_index` via `tmux display-message -p`
2. Generate unique grouped session name: `agmgr-enter-{agent_id[:8]}`
3. If a session with that name already exists and has a client attached → go to F-05 (concurrent access check)
4. If session exists but no client → reuse it (skip creation)
5. Create grouped session: `tmux new-session -d -s {view_session} -t {session_name}`
6. Select correct window: `tmux select-window -t {view_session}:{window_idx}`
7. Register cleanup hook: `tmux set-hook -t {view_session} client-detached "run-shell 'CLIENT_COUNT=$(tmux list-clients -t {view_session} 2>/dev/null | wc -l); [ $CLIENT_COUNT -le 1 ] && tmux kill-session -t {view_session} 2>/dev/null'"`
8. Switch client: `tmux switch-client -t {view_session}`
9. Show toast: "Attached to {agent_name} — press Ctrl+B D to return"

**Error handling:**
- `new-session` fails (name conflict after 3 retries with UUID suffix) → toast "Failed to create tmux session"
- `switch-client` fails → kill the grouped session, toast error
- Hook registration fails → proceed without hook (mark session as "needs manual cleanup", rely on F-06 startup scan)

**Edge cases:**
- Multiple clients detach simultaneously → hook checks client count before kill; last-client-out kills the session
- Agent's window is closed while user is attached → session state degrades; return to TUI triggers state refresh

### F-04 Suspend Path (Out-of-tmux) with MCP Reconnection

**Main flow:**
1. Before suspend: check active MCP SSE connection state; if connected, set `_mcp_reconnect_needed = True`
2. Call `app.suspend()` to release terminal control
3. Execute `subprocess.run(["tmux", "attach-session", "-t", view_session])` (blocking)
4. On return (after Ctrl+B D): call `app.resume()`
5. Call `app.refresh(layout=True)` + run `tput reset` to restore TUI rendering
6. If `_mcp_reconnect_needed`: check SSE connection, reconnect if disconnected; show "Reconnecting MCP…" toast while in progress
7. Trigger agent state refresh (force one `capture-pane` poll cycle)

**Error handling:**
- `app.suspend()` raises → fall back to `os.kill(os.getpid(), signal.SIGTSTP)`; on resume catch `SIGCONT`
- `tmux attach-session` fails (session gone) → app auto-resumes, toast error
- MCP reconnect times out after 10 s → toast "MCP reconnection failed, please restart the platform"; agents marked `degraded`
- SIGHUP received during attach (terminal closed) → catch signal, call `app.resume()` gracefully

**Edge cases:**
- Textual workers continue running during suspend (asyncio tasks keep going) → supervisor poll may enqueue state changes; resume triggers a full state reconciliation
- Terminal resized while attached → SIGWINCH on resume handled by Textual

### F-05 Concurrent Access Check

**Main flow:**
1. Before switching into a grouped session, query: `tmux list-clients -t {view_session}`
2. If one or more clients already attached → show confirmation dialog: "Agent pane already being accessed by another client. Enter anyway?"
3. User confirms → proceed with switch
4. User cancels → re-enable Enter button, abort

**Error handling:**
- `list-clients` fails → proceed without check (log warning)

### F-06 Session Lifecycle Management and Startup Cleanup

**Main flow:**
1. All grouped sessions created by this platform use the prefix `agmgr-enter-`
2. On TUI startup: `tmux list-sessions` → filter by prefix → kill any stale sessions (from previous crash)
3. On TUI exit (normal shutdown): enumerate and kill all `agmgr-enter-*` sessions
4. Session name format: `agmgr-enter-{agent_id[:8]}` (single-instance assumption for MVP)

**Error handling:**
- tmux not running on startup → skip cleanup silently
- Kill fails (session already gone) → ignore

### F-07 Post-Return State Refresh

**Main flow:**
1. After returning from any attach (both paths), trigger one immediate `capture-pane` poll for the agent
2. Update agent status in TUI from database (in case agent state changed while user was attached)
3. No visual diff highlighting (deferred to v2)

**Error handling:**
- Capture fails → show last known output, no crash

## 4. Non-functional Requirements

| Category | Requirement |
|:---|:---|
| Performance | Enter-to-attached transition ≤ 300 ms (grouped path) |
| Compatibility | tmux ≥ 2.4 required (for `set-hook`); warn and disable Enter button if lower |
| Reliability | No orphan `agmgr-enter-*` sessions after normal or crash exit |
| Safety | Hook scripts use `2>/dev/null` to prevent hook errors from surfacing; all tmux commands wrapped in try/except |
| MCP stability | SSE connection must be verified usable within 10 s of TUI resume |

## 5. Out of Scope

- Nested tmux support (S-03): show error, do not attempt
- Multi-machine / remote tmux server scenarios
- SSH forwarding edge cases
- Visual diff highlighting of changed pane content after return (deferred)
- Keyboard shortcut for Enter (deferred to v2)
- "Start & Enter" combined flow for stopped agents (deferred)
- Audit logging of attach/detach events (deferred)
- Configurable enter behavior (deferred)

## 6. Acceptance Criteria

| ID | Feature | Condition | Expected Result |
|:---|:---|:---|:---|
| AC-01 | F-01 | Agent is `active`, user clicks Enter | Button disables, transition starts within 300 ms |
| AC-02 | F-01 | Agent is `not_started` or `stopped` | Enter button is disabled / not clickable |
| AC-03 | F-02 | TUI running inside tmux | Grouped-session path taken, TUI continues running |
| AC-04 | F-02 | TUI not inside tmux | Suspend path taken |
| AC-05 | F-02 | Nested tmux detected | Toast shown, no crash, no attach attempted |
| AC-06 | F-03 | User presses Ctrl+B D | Returns to TUI; `agmgr-enter-*` session is gone within 5 s |
| AC-07 | F-03 | Enter clicked twice rapidly | Second click is ignored (button disabled) |
| AC-08 | F-04 | After suspend+resume, MCP was disconnected | SSE reconnection attempted; agents usable within 10 s |
| AC-09 | F-04 | After suspend+resume | TUI renders correctly, no visual artifacts |
| AC-10 | F-05 | Another client already attached | Confirmation dialog shown before entering |
| AC-11 | F-06 | TUI restarted after crash | All `agmgr-enter-*` stale sessions cleaned up on startup |
| AC-12 | F-07 | Return from attach | AgentPane content refreshed within 2 s |

## 7. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-07 | Initial version | ALL | - |
