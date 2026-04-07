"""tmux attach utilities for native agent pane interaction.

Provides two attach paths:
  - grouped_attach(): in-tmux path — creates a grouped tmux session and calls
    switch-client so the asyncio event loop (and MCP/SSE) stay live.
  - suspend_attach(): out-of-tmux path — suspends the Textual app, runs a
    blocking attach-session, then resumes.

All tmux subcommands are routed through an injected _runner callable, making
every public function fully unit-testable without a real tmux server.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_management.shared.config import TEMP_DIR

logger = logging.getLogger(__name__)

# Prefix for all grouped view sessions created by this platform
_SESSION_PREFIX = "agmgr-enter-"

# Type alias: injected runner receives tmux sub-args (no "tmux" prefix) and
# returns (returncode, stdout, stderr) — identical to app.py's _tmux() contract.
TmuxRunner = Callable[..., Coroutine[Any, Any, tuple[int, str, str]]]


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

@dataclass
class AttachResult:
    """Result of an attach attempt; drives toast message and MCP health check."""

    ok: bool
    message: str
    need_mcp_check: bool = False


class NestedTmuxError(RuntimeError):
    """Raised when a nested / unexpected tmux environment is detected."""


# ---------------------------------------------------------------------------
# Default runner — wraps asyncio.create_subprocess_exec
# ---------------------------------------------------------------------------

async def _default_runner(*args: str) -> tuple[int, str, str]:
    """Run `tmux <args>` and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "tmux", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


# ---------------------------------------------------------------------------
# Session naming
# ---------------------------------------------------------------------------

def _view_session_name(agent_id: str, app_pid: int) -> str:
    """Return the grouped session name: agmgr-enter-{agent_id[:8]}-{pid}."""
    return f"{_SESSION_PREFIX}{agent_id[:8]}-{app_pid}"


# ---------------------------------------------------------------------------
# Sentinel file helpers (suspend-path race mitigation)
# ---------------------------------------------------------------------------

def _sentinel_path(agent_id: str) -> Path:
    """Return the sentinel file path for a suspend-attach operation."""
    return TEMP_DIR / f"attach_sentinel_{agent_id[:8]}.txt"


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

async def detect_environment(
    app_session_prefix: str = "agent-mgmt-",
    *,
    _runner: TmuxRunner = _default_runner,
) -> str:
    """Detect the tmux environment; return the current session name or empty string.

    Returns the session name when running inside the platform's own tmux session.
    Returns empty string when not inside tmux at all (triggers suspend path).
    Raises NestedTmuxError when inside an unexpected tmux session.
    """
    # 1. Check if we're inside any tmux session at all
    tmux_env = os.environ.get("TMUX", "")
    if not tmux_env:
        logger.debug("Not inside tmux ($TMUX absent) — will use suspend path")
        return ""

    # 2. Query the current session name
    rc, session_name, _ = await _runner("display-message", "-p", "#{session_name}")
    if rc != 0 or not session_name:
        logger.debug(
            "tmux display-message failed (rc=%d) — falling back to suspend path", rc
        )
        return ""

    # 3. Reject nested / unexpected tmux sessions
    if not session_name.startswith(app_session_prefix):
        logger.warning(
            "Nested tmux detected: session=%s does not match prefix=%s",
            session_name,
            app_session_prefix,
        )
        raise NestedTmuxError(
            f"Nested tmux not supported. Current session: {session_name!r}. "
            "Please enter the agent session manually."
        )

    logger.debug("In-tmux confirmed, session=%s", session_name)
    return session_name


# ---------------------------------------------------------------------------
# Pane validation
# ---------------------------------------------------------------------------

async def validate_pane_exists(
    pane_id: str,
    *,
    _runner: TmuxRunner = _default_runner,
) -> bool:
    """Return True if the given tmux pane ID exists and is responsive."""
    if not pane_id:
        logger.debug("validate_pane_exists called with empty pane_id")
        return False
    rc, _, _ = await _runner("display-message", "-p", "-t", pane_id, "")
    exists = rc == 0
    logger.debug("Pane validation, pane_id=%s, exists=%s", pane_id, exists)
    return exists


# ---------------------------------------------------------------------------
# F-05 — Concurrent access check
# ---------------------------------------------------------------------------

async def check_concurrent_access(
    agent_id: str,
    app_pid: int,
    *,
    _runner: TmuxRunner = _default_runner,
) -> bool:
    """Return True if another terminal is already attached to this agent's view session."""
    view_session = _view_session_name(agent_id, app_pid)
    rc, out, _ = await _runner("list-clients", "-t", view_session)
    if rc != 0:
        logger.debug(
            "list-clients failed for session=%s (session likely does not exist yet)",
            view_session,
        )
        return False
    client_count = len([line for line in out.splitlines() if line.strip()])
    logger.debug(
        "Concurrent access check, session=%s, clients=%d", view_session, client_count
    )
    return client_count > 0


# ---------------------------------------------------------------------------
# F-03 — Grouped session path (in-tmux, asyncio loop stays live)
# ---------------------------------------------------------------------------

async def grouped_attach(
    agent_id: str,
    tmux_session_name: str,
    app_pid: int,
    pane_id: str = "",
    *,
    _runner: TmuxRunner = _default_runner,
) -> AttachResult:
    """Attach via a grouped tmux session — preferred path when inside tmux.

    Creates agmgr-enter-{agent_id[:8]}-{app_pid} grouped onto the agent's session,
    registers a client-detached hook that kills it when the last client leaves,
    then switches the current terminal into it.  The asyncio event loop is never
    suspended, so MCP/SSE connections remain live throughout.

    pane_id (F-03): when provided, select-pane is called after switch-client to focus
    the specific agent pane rather than whatever window was last active.
    """
    logger.info(
        "Starting grouped attach, agent_id=%s, tmux_session=%s, pane_id=%s",
        agent_id, tmux_session_name, pane_id,
    )
    view_session = _view_session_name(agent_id, app_pid)

    # 1. Ensure the grouped view session exists (create or reuse)
    create_result = await _ensure_view_session(view_session, tmux_session_name, _runner)
    if not create_result.ok:
        return create_result

    # 2. Register cleanup hook so session dies when last client detaches
    await _register_detach_hook(view_session, _runner)

    # 3. Switch the current terminal into the view session
    result = await _switch_to_view_session(view_session, agent_id, _runner)
    if not result.ok:
        return result

    # 4. Select the specific agent pane (F-03) so the user lands on the right window
    if pane_id:
        await _select_target_pane(pane_id, agent_id, _runner)

    return result


async def _ensure_view_session(
    view_session: str,
    target_session: str,
    _runner: TmuxRunner,
) -> AttachResult:
    """Create the grouped view session if it does not already exist."""
    rc_has, _, _ = await _runner("has-session", "-t", view_session)
    if rc_has == 0:
        logger.debug("View session already exists, reusing, session=%s", view_session)
        return AttachResult(ok=True, message="")

    logger.debug(
        "Creating grouped session, view=%s, target=%s", view_session, target_session
    )
    rc, _, err = await _runner(
        "new-session", "-d", "-s", view_session, "-t", target_session
    )
    if rc != 0:
        logger.error(
            "Failed to create grouped session, view=%s, err=%s", view_session, err
        )
        # Truncate raw tmux error output before including in user-facing message (S-01)
        safe_err = err[:120] if err else "unknown error"
        return AttachResult(ok=False, message=f"Failed to create tmux session: {safe_err}")

    return AttachResult(ok=True, message="")


async def _register_detach_hook(view_session: str, _runner: TmuxRunner) -> None:
    """Register a client-detached hook that kills the session when the last client leaves."""
    # The hook checks client count before killing to handle simultaneous detaches.
    # Errors are redirected to /dev/null so they never surface to the user.
    hook_script = (
        f"run-shell 'COUNT=$(tmux list-clients -t {view_session} 2>/dev/null | wc -l); "
        f"[ \"$COUNT\" -le 1 ] && tmux kill-session -t {view_session} 2>/dev/null'"
    )
    rc, _, err = await _runner("set-hook", "-t", view_session, "client-detached", hook_script)
    if rc != 0:
        # Non-fatal: startup cleanup will catch any orphaned sessions
        logger.warning(
            "Failed to register detach hook, session=%s, err=%s "
            "(startup cleanup will catch orphans)",
            view_session,
            err,
        )


async def _switch_to_view_session(
    view_session: str,
    agent_id: str,
    _runner: TmuxRunner,
) -> AttachResult:
    """Switch the current tmux client into the view session."""
    rc, _, err = await _runner("switch-client", "-t", view_session)
    if rc != 0:
        logger.error(
            "switch-client failed, session=%s, err=%s — killing orphaned session",
            view_session,
            err,
        )
        # Clean up the session we just created so it does not become an orphan
        await _runner("kill-session", "-t", view_session)
        # Truncate raw tmux error output before including in user-facing message (S-01)
        safe_err = err[:120] if err else "unknown error"
        return AttachResult(ok=False, message=f"Failed to switch to session: {safe_err}")

    logger.info(
        "Grouped attach successful, agent_id=%s, view_session=%s", agent_id, view_session
    )
    return AttachResult(
        ok=True,
        message="Attached — press Ctrl+B D to return",
        need_mcp_check=False,
    )


async def _select_target_pane(
    pane_id: str,
    agent_id: str,
    _runner: TmuxRunner,
) -> None:
    """Focus the specific agent pane after switching to the grouped view session.

    Non-fatal: if select-pane fails (pane was renumbered), the user lands on the
    active window instead of the specific agent pane.  A warning is logged.
    """
    rc, _, err = await _runner("select-pane", "-t", pane_id)
    if rc != 0:
        logger.warning(
            "select-pane failed for agent_id=%s, pane_id=%s, err=%s — "
            "landed on active window instead",
            agent_id, pane_id, err,
        )
    else:
        logger.debug("Pane selected, agent_id=%s, pane_id=%s", agent_id, pane_id)


# ---------------------------------------------------------------------------
# F-04 — Suspend path (out-of-tmux) with sentinel file
# ---------------------------------------------------------------------------

async def suspend_attach(
    agent_id: str,
    tmux_session_name: str,
    app_pid: int,
    pane_id: str = "",
    suspend_fn: Callable[[], Any] = None,
    *,
    _runner: TmuxRunner = _default_runner,
) -> AttachResult:
    """Attach by suspending the Textual TUI and running a blocking tmux attach.

    Writes a sentinel file before suspending so that a subsequent crash/restart
    can detect and clean up incomplete state.  The sentinel is removed in the
    finally block regardless of outcome.

    Returns AttachResult with need_mcp_check=True because the asyncio event loop
    was blocked during the attach; the MCP/SSE connection may have timed out.
    """
    logger.info(
        "Starting suspend attach, agent_id=%s, tmux_session=%s",
        agent_id,
        tmux_session_name,
    )

    # 1. Write sentinel so a crash during attach can be detected on next startup
    sentinel = _sentinel_path(agent_id)
    sentinel.write_text(tmux_session_name)
    logger.debug("Sentinel written, path=%s, session=%s", sentinel, tmux_session_name)

    try:
        # 2. Give the asyncio event loop one tick to drain queued callbacks
        await asyncio.sleep(0)

        # 3. Suspend TUI, attach (blocking), then resume (F-03: pass pane_id)
        await _suspend_and_attach(tmux_session_name, pane_id, suspend_fn)

    finally:
        # 4. Always remove sentinel, even if attach raised
        sentinel.unlink(missing_ok=True)
        logger.debug("Sentinel removed, path=%s", sentinel)

    logger.info("Suspend attach complete, agent_id=%s", agent_id)
    return AttachResult(
        ok=True,
        message="Returned from agent pane.",
        need_mcp_check=True,  # asyncio was blocked; SSE may have disconnected
    )


async def _suspend_and_attach(
    tmux_session_name: str,
    pane_id: str,
    suspend_fn: Callable[[], Any],
) -> None:
    """Suspend the TUI, run a blocking tmux attach-session, then resume.

    pane_id (F-03): after attach returns (user pressed Ctrl+B D), a select-pane
    call is issued so that if the user re-attaches via the same path they land on
    the correct pane.  The post-detach select-pane is best-effort.
    """
    logger.debug("Suspending TUI, attaching to session=%s, pane_id=%s", tmux_session_name, pane_id)
    async with suspend_fn():
        # This blocks the asyncio event loop intentionally — the user is
        # interacting with the tmux pane directly until they press Ctrl+B D.
        subprocess.run(  # noqa: S603
            ["tmux", "attach-session", "-t", tmux_session_name],
            check=False,
        )
        # F-03: After user detaches, select the target pane so future attaches land correctly
        if pane_id:
            subprocess.run(  # noqa: S603
                ["tmux", "select-pane", "-t", pane_id],
                check=False,
            )
    logger.debug("TUI resumed after attach from session=%s", tmux_session_name)


# ---------------------------------------------------------------------------
# F-06 — Session lifecycle: startup cleanup and shutdown cleanup
# ---------------------------------------------------------------------------

async def cleanup_stale_sessions(
    app_pid: int,
    *,
    _runner: TmuxRunner = _default_runner,
) -> None:
    """Kill agmgr-enter-* sessions left by a previously crashed platform instance.

    A session is stale when its embedded PID belongs to a process that is no
    longer running (and is not the current process).  Called on TUI startup.
    Also removes any leftover sentinel files from incomplete attach operations.
    """
    logger.debug("Starting stale session cleanup, current_pid=%d", app_pid)
    rc, out, _ = await _runner("list-sessions", "-F", "#{session_name}")
    if rc != 0:
        logger.debug("tmux list-sessions failed — skipping stale session cleanup")
        return

    killed = 0
    for session_name in out.splitlines():
        if not session_name.startswith(_SESSION_PREFIX):
            continue
        if _is_stale_session(session_name, app_pid):
            rc_kill, _, _ = await _runner("kill-session", "-t", session_name)
            if rc_kill == 0:
                killed += 1
                logger.info("Killed stale session, name=%s", session_name)
            else:
                logger.debug("Session already gone, name=%s", session_name)

    # Remove leftover sentinel files from incomplete previous attach operations
    for sentinel in TEMP_DIR.glob("attach_sentinel_*.txt"):
        sentinel.unlink(missing_ok=True)
        logger.debug("Removed orphaned sentinel, path=%s", sentinel)

    logger.info("Stale session cleanup complete, killed=%d", killed)


async def cleanup_all_sessions(
    app_pid: int,
    *,
    _runner: TmuxRunner = _default_runner,
) -> None:
    """Kill all agmgr-enter-* sessions owned by this platform instance.

    Called on clean TUI shutdown to leave no orphaned view sessions.
    """
    logger.debug("Killing all owned view sessions on shutdown, pid=%d", app_pid)
    rc, out, _ = await _runner("list-sessions", "-F", "#{session_name}")
    if rc != 0:
        return

    killed = 0
    for session_name in out.splitlines():
        if not session_name.startswith(_SESSION_PREFIX):
            continue
        if _owns_session(session_name, app_pid):
            await _runner("kill-session", "-t", session_name)
            killed += 1
            logger.info("Killed owned view session on shutdown, name=%s", session_name)

    logger.info("Shutdown session cleanup complete, killed=%d", killed)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _is_stale_session(session_name: str, current_pid: int) -> bool:
    """Return True if this session belongs to a dead process (not current_pid)."""
    pid = _extract_pid_from_session_name(session_name)
    if pid is None:
        return False  # unknown format — leave it alone
    if pid == current_pid:
        return False  # belongs to us, not stale
    return not _is_pid_alive(pid)


def _owns_session(session_name: str, app_pid: int) -> bool:
    """Return True if this session was created by the given platform PID."""
    return _extract_pid_from_session_name(session_name) == app_pid


def _extract_pid_from_session_name(session_name: str) -> int | None:
    """Extract the PID suffix from 'agmgr-enter-{agent[:8]}-{pid}'.

    Returns None if the session name does not match the expected format.
    """
    try:
        return int(session_name.rsplit("-", 1)[1])
    except (ValueError, IndexError):
        return None


def _is_pid_alive(pid: int) -> bool:
    """Return True if the process with the given PID is still running.

    Cross-platform: Linux/macOS use os.kill(pid, 0); Windows raises OSError
    [WinError 87] for non-existent PIDs, which we map to "dead".
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        # Windows raises OSError(WinError 87 — invalid parameter) when the PID
        # does not exist. Treat any non-permission OSError as dead.
        if isinstance(exc, PermissionError):
            return True
        return False
    except PermissionError:
        # Process exists but we lack permission to signal it
        return True
