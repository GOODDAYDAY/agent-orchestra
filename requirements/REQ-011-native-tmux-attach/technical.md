# REQ-011 Technical Design: Native tmux Attach / Detach

> Status: Completed
> Requirement: requirement.md
> Created: 2026-04-07
> Updated: 2026-04-07

## 1. Technology Stack

| Module | Technology | Rationale |
|:---|:---|:---|
| tmux control | `asyncio.create_subprocess_exec` | Non-blocking tmux commands; no thread overhead |
| Terminal attach (in-tmux) | `tmux new-session -t` (grouped session) + `tmux switch-client` | Asyncio loop stays alive; SSE connections unaffected |
| Terminal attach (out-of-tmux) | `app.suspend()` context manager + `subprocess.run()` blocking | Textual's built-in suspend/resume; falls back to SIGTSTP |
| Hook race mitigation | `asyncio.sleep(0)` drain + sentinel file | Gives event loop a tick before blocking; sentinel survives SIGKILL |
| Session leak detection | PID embedded in session name | `os.kill(pid, 0)` liveness check on startup for orphan detection |
| MCP/SSE health check | Single `asyncio.wait_for` probe on `_mcp_task` state | Passive check only; no polling loop |

## 2. Design Principles

- **Minimal surface area**: new code lives in one new file (`tmux_attach.py`); existing modules receive targeted additions only
- **No asyncio suspension on the hot path**: grouped-session path (in-tmux) is the default and does not call `app.suspend()`; the event loop and SSE connections remain live
- **Testability via injection**: all tmux subcommands flow through a `_runner` callable parameter, defaulting to `asyncio.create_subprocess_exec`; tests swap in a coroutine stub
- **Graceful degradation**: every tmux command is wrapped in try/except; failures produce a toast, never a crash; the Enter button always re-enables (5 s forced timeout)

## 3. Architecture Overview

Two code paths share a single entry point (`on_agent_pane_attach_requested`) and a single result type (`AttachResult`):

```
AgentPane (Enter button)
    │ AttachRequested(agent_id) message
    ▼
App.on_agent_pane_attach_requested()
    │
    ├─ validate pane exists
    ├─ detect environment (TMUX + display-message)
    │
    ├─[in-tmux]──▶ _do_grouped_attach()        ← asyncio loop stays live
    │                  tmux new-session -t
    │                  tmux switch-client
    │                  client-detached hook
    │
    └─[out-of-tmux]─▶ _do_suspend_attach()     ← event loop suspended briefly
                        asyncio.sleep(0)
                        app.suspend()
                        subprocess.run(attach)
                        app.resume()
                        _check_mcp_alive()
```

See `tech-architecture.puml` for the component diagram.

## 4. Module Design

### 4.1 `frontend/tmux_attach.py` (new, ~120 lines)

**Responsibility**: all tmux attach logic, pure async functions, no Textual imports.

**Public interface**:
```python
@dataclass
class AttachResult:
    ok: bool
    message: str          # toast text
    need_mcp_check: bool  # True only after suspend path

async def grouped_attach(
    agent_id: str,
    tmux_pane_id: str,
    app_pid: int,
    *,
    _runner = _default_runner,
) -> AttachResult: ...

async def suspend_attach(
    tmux_pane_id: str,
    app_pid: int,
    suspend_fn: Callable,
    *,
    _runner = _default_runner,
) -> AttachResult: ...

async def cleanup_stale_sessions(app_pid: int, *, _runner = _default_runner) -> None: ...
async def cleanup_all_sessions(app_pid: int, *, _runner = _default_runner) -> None: ...
```

**Session naming**: `agmgr-enter-{agent_id[:8]}-{app_pid}`
Including `app_pid` allows startup scan to distinguish sessions from a live platform vs. a crashed one.

**Hook script** (registered via `tmux set-hook client-detached`):
```sh
CLIENT_COUNT=$(tmux list-clients -t {session} 2>/dev/null | wc -l)
[ "$CLIENT_COUNT" -le 1 ] && tmux kill-session -t {session} 2>/dev/null
```

**Race mitigation** (suspend path only):
```python
await asyncio.sleep(0)          # drain pending Textual callbacks
sentinel.write_text(session_name)
try:
    async with suspend_fn():
        subprocess.run(["tmux", "attach-session", "-t", session_name])
finally:
    sentinel.unlink(missing_ok=True)
```

### 4.2 `frontend/agent_pane.py` (modified, +35 lines)

**Additions**:
- `AttachRequested(agent_id: str)` Textual `Message` class (inner class of `AgentPane`)
- Enter `Button` in the controls row (id `enter-agent`)
- `on_button_pressed` handler: disables button, posts `AttachRequested`, schedules 5 s forced re-enable via `set_timer`
- `watch_status`: disables Enter when status is `starting / stopping / degraded / not_started`

**Reuse notes**: reuses existing `_status_class` map and controls-row container pattern from current pane layout.

### 4.3 `frontend/app.py` (modified, +80 lines)

**Additions**:
- `on_agent_pane_attach_requested(msg)`: validates pane, detects env, dispatches to correct path, re-enables button on completion
- `_do_grouped_attach(agent_id, pane_id)`: thin wrapper calling `tmux_attach.grouped_attach()`
- `_do_suspend_attach(agent_id, pane_id)`: calls `tmux_attach.suspend_attach()`, then `_check_mcp_alive()` if `need_mcp_check`
- `_check_mcp_alive()`: `asyncio.wait_for(self._mcp_task, 0)` probe; if task is done/cancelled, shows "MCP connection lost" toast
- `on_mount` addition: `await cleanup_stale_sessions(os.getpid())`
- `on_unmount` addition: `await cleanup_all_sessions(os.getpid())`

**Reuse notes**: reuses existing `self._tmux()` helper and `self.notify()` toast pattern; `_mcp_task` already tracked.

## 5. Data Model

No schema changes. `Session.tmux_pane_id` already stores `{session}:{window}.{pane}` — sufficient to locate the target pane for grouped session creation.

Sentinel file path: `{TEMP_DIR}/attach_sentinel_{agent_id[:8]}.txt` (cleaned on startup alongside stale sessions).

## 6. Key Flows

### 6.1 In-tmux Path (Grouped Session)

```
User clicks Enter
    → button disabled
    → validate pane (list-panes)
    → detect env: $TMUX present + display-message OK
    → check concurrent: list-clients on existing session
        → if clients: show ConfirmDeleteDialog
    → new-session -d -s agmgr-enter-{id}-{pid} -t {session}
    → select-window
    → set-hook client-detached (kill-if-last)
    → switch-client -t agmgr-enter-{id}-{pid}
    → toast "Attached — Ctrl+B D to return"
    → button re-enabled
    [User presses Ctrl+B D]
    → TUI unchanged (asyncio never suspended)
    → hook fires async: kills grouped session
    → force capture-pane poll → AgentPane refresh
```

### 6.2 Out-of-tmux Path (Suspend + Attach)

```
User clicks Enter
    → button disabled
    → validate pane
    → detect env: $TMUX absent
    → asyncio.sleep(0)           # drain loop
    → write sentinel file
    → app.suspend()              # releases terminal
        subprocess.run(tmux attach-session)   # blocking
        [User interacts natively]
        [User presses Ctrl+B D]
    → app.resume()
    → sentinel.unlink()
    → tput reset (restore rendering)
    → _check_mcp_alive()
        → if task gone: toast "MCP connection lost, please restart"
    → force capture-pane poll
    → button re-enabled
```

See `tech-sequence.puml` for sequence diagrams.

## 7. Environment Detection Logic

```python
tmux_env = os.environ.get("TMUX", "")
if not tmux_env:
    return Path.OUT_OF_TMUX

rc, out, _ = await run("tmux", "display-message", "-p", "#{session_name}")
if rc != 0:
    return Path.OUT_OF_TMUX

session_name = out.strip()
if not session_name.startswith("agent-mgmt-"):
    # nested tmux: outer session is not our session
    raise NestedTmuxError()

return Path.IN_TMUX
```

Nested tmux (S-03) → toast + abort, no attach attempted.

## 8. Shared Modules & Reuse Strategy

| Shared Component | Used By | Notes |
|:---|:---|:---|
| `self._tmux()` helper in `app.py` | `tmux_attach.py` receives `_runner` instead | keeps app helper for other uses; attach module is self-contained |
| `TEMP_DIR` from `shared/config.py` | `tmux_attach.py` (sentinel path) | single source of truth for temp directory |
| `ConfirmDeleteDialog` from `dialogs.py` | `app.py` concurrent access check | reuse existing confirmation dialog |
| `AgentStatus` enum | `agent_pane.py` button enable/disable | no duplication |

## 9. Risks & Notes

| Risk | Mitigation |
|:---|:---|
| SSE disconnect on suspend path | Passive check after resume; toast if dropped; grouped path preferred when in tmux |
| Hook fires after `switch-client` returns | Hook is async by design; grouped session removal is best-effort; startup cleanup catches survivors |
| `app.suspend()` raises (Textual version) | `except Exception: os.kill(os.getpid(), signal.SIGTSTP)` fallback; `SIGCONT` handler calls `app.resume()` |
| Terminal resize during attach | SIGWINCH on resume handled by Textual automatically |
| Multiple Enter clicks before disable | Button is disabled immediately on first click; second click is a no-op |
| tmux < 2.4 (no `set-hook`) | Check version at startup; disable Enter button with tooltip if version unsupported |

## 10. Change Log

| Version | Date | Changes | Affected Scope | Reason |
|:---|:---|:---|:---|:---|
| v1 | 2026-04-07 | Initial proposal | ALL | - |
