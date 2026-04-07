"""Acceptance tests for tmux_attach.py — all tmux calls are injected via _runner."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from agent_management.frontend.tmux_attach import (
    AttachResult,
    NestedTmuxError,
    _SESSION_PREFIX,
    _extract_pid_from_session_name,
    _is_pid_alive,
    _view_session_name,
    check_concurrent_access,
    cleanup_all_sessions,
    cleanup_stale_sessions,
    detect_environment,
    grouped_attach,
    suspend_attach,
    validate_pane_exists,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner(responses: dict | None = None):
    """Build a mock _runner that maps args-tuples to (rc, stdout, stderr)."""
    resp: dict = responses or {}

    async def runner(*args: str) -> tuple[int, str, str]:
        return resp.get(args, (0, "", ""))

    return runner


@asynccontextmanager
async def _mock_suspend():
    """No-op async context manager simulating app.suspend()."""
    yield


# ---------------------------------------------------------------------------
# F-02 / AC-03 / AC-04 / AC-05 — Environment detection
# ---------------------------------------------------------------------------

class TestDetectEnvironment:
    """AC-03: in-tmux path taken; AC-04: out-of-tmux path; AC-05: nested tmux error."""

    @pytest.mark.asyncio
    async def test_AC04_out_of_tmux_when_no_tmux_env(self):
        """AC-04: No $TMUX → returns empty string (suspend path)."""
        runner = _make_runner()
        with patch.dict(os.environ, {}, clear=True):
            if "TMUX" in os.environ:
                del os.environ["TMUX"]
            result = await detect_environment(_runner=runner)
        assert result == ""

    @pytest.mark.asyncio
    async def test_AC03_in_tmux_returns_session_name(self):
        """AC-03: $TMUX set + matching session → returns session name."""
        runner = _make_runner({
            ("display-message", "-p", "#{session_name}"): (0, "agent-mgmt-abc12345", ""),
        })
        with patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default,12345,0"}):
            result = await detect_environment(_runner=runner)
        assert result == "agent-mgmt-abc12345"

    @pytest.mark.asyncio
    async def test_AC05_nested_tmux_raises(self):
        """AC-05: session name doesn't match prefix → NestedTmuxError raised."""
        runner = _make_runner({
            ("display-message", "-p", "#{session_name}"): (0, "other-session", ""),
        })
        with patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default,12345,0"}):
            with pytest.raises(NestedTmuxError):
                await detect_environment(_runner=runner)

    @pytest.mark.asyncio
    async def test_display_message_failure_falls_back_to_out_of_tmux(self):
        """display-message fails → falls back to suspend path (out-of-tmux)."""
        runner = _make_runner({
            ("display-message", "-p", "#{session_name}"): (1, "", "no server"),
        })
        with patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default,12345,0"}):
            result = await detect_environment(_runner=runner)
        assert result == ""


# ---------------------------------------------------------------------------
# F-01 / F-03 — Pane validation
# ---------------------------------------------------------------------------

class TestValidatePaneExists:
    """F-01: validate tmux_pane_id before attaching."""

    @pytest.mark.asyncio
    async def test_returns_true_when_pane_alive(self):
        runner = _make_runner({
            ("display-message", "-p", "-t", "%1", ""): (0, "%1", ""),
        })
        assert await validate_pane_exists("%1", _runner=runner) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_pane_gone(self):
        runner = _make_runner({
            ("display-message", "-p", "-t", "%1", ""): (1, "", "no pane"),
        })
        assert await validate_pane_exists("%1", _runner=runner) is False

    @pytest.mark.asyncio
    async def test_returns_false_for_empty_pane_id(self):
        runner = _make_runner()
        assert await validate_pane_exists("", _runner=runner) is False


# ---------------------------------------------------------------------------
# F-03 — Grouped attach (in-tmux path)
# ---------------------------------------------------------------------------

class TestGroupedAttach:
    """F-03: grouped session creation, switch-client, hook registration."""

    @pytest.mark.asyncio
    async def test_AC03_creates_session_and_switches(self):
        """AC-03: creates grouped session + calls switch-client → ok=True."""
        agent_id = "abcdef1234567890"
        app_pid = 999
        session_name = "agent-mgmt-abc12345"
        view = _view_session_name(agent_id, app_pid)

        runner = _make_runner({
            ("has-session", "-t", view): (1, "", ""),  # doesn't exist yet
            ("new-session", "-d", "-s", view, "-t", session_name): (0, "", ""),
            ("set-hook", "-t", view, "client-detached",
             f"run-shell 'COUNT=$(tmux list-clients -t {view} 2>/dev/null | wc -l); "
             f"[ \"$COUNT\" -le 1 ] && tmux kill-session -t {view} 2>/dev/null'"): (0, "", ""),
            ("switch-client", "-t", view): (0, "", ""),
        })
        result = await grouped_attach(agent_id, session_name, app_pid, _runner=runner)
        assert result.ok is True
        assert "Ctrl+B D" in result.message
        assert result.need_mcp_check is False

    @pytest.mark.asyncio
    async def test_reuses_existing_session_without_recreation(self):
        """F-03: if session already exists, skips new-session."""
        agent_id = "abcdef1234567890"
        app_pid = 999
        session_name = "agent-mgmt-abc12345"
        view = _view_session_name(agent_id, app_pid)

        calls: list[tuple] = []

        async def tracking_runner(*args: str) -> tuple[int, str, str]:
            calls.append(args)
            if args == ("has-session", "-t", view):
                return (0, "", "")  # already exists
            if args[0] == "switch-client":
                return (0, "", "")
            if args[0] == "set-hook":
                return (0, "", "")
            return (0, "", "")

        result = await grouped_attach(agent_id, session_name, app_pid, _runner=tracking_runner)
        assert result.ok is True
        assert not any(args[0] == "new-session" for args in calls)

    @pytest.mark.asyncio
    async def test_returns_error_when_new_session_fails(self):
        """F-03: new-session failure → ok=False, error message."""
        agent_id = "abcdef1234567890"
        app_pid = 999
        session_name = "agent-mgmt-abc12345"
        view = _view_session_name(agent_id, app_pid)

        runner = _make_runner({
            ("has-session", "-t", view): (1, "", ""),
            ("new-session", "-d", "-s", view, "-t", session_name): (1, "", "duplicate session"),
        })
        result = await grouped_attach(agent_id, session_name, app_pid, _runner=runner)
        assert result.ok is False
        assert "Failed" in result.message

    @pytest.mark.asyncio
    async def test_kills_session_when_switch_client_fails(self):
        """F-03: switch-client failure → session killed, ok=False."""
        agent_id = "abcdef1234567890"
        app_pid = 999
        session_name = "agent-mgmt-abc12345"
        view = _view_session_name(agent_id, app_pid)

        kill_calls: list = []

        async def runner(*args: str) -> tuple[int, str, str]:
            if args == ("has-session", "-t", view):
                return (1, "", "")
            if args[0] == "new-session":
                return (0, "", "")
            if args[0] == "set-hook":
                return (0, "", "")
            if args[0] == "switch-client":
                return (1, "", "no client")
            if args[0] == "kill-session":
                kill_calls.append(args)
                return (0, "", "")
            return (0, "", "")

        result = await grouped_attach(agent_id, session_name, app_pid, _runner=runner)
        assert result.ok is False
        assert len(kill_calls) == 1  # orphaned session cleaned up


# ---------------------------------------------------------------------------
# F-05 — Concurrent access check
# ---------------------------------------------------------------------------

class TestCheckConcurrentAccess:
    """F-05: detect when another client is already attached."""

    @pytest.mark.asyncio
    async def test_AC10_returns_true_when_client_attached(self):
        """AC-10: another client attached → returns True."""
        agent_id = "abcdef1234567890"
        app_pid = 999
        view = _view_session_name(agent_id, app_pid)

        runner = _make_runner({
            ("list-clients", "-t", view): (0, "/dev/pts/1: other-session", ""),
        })
        result = await check_concurrent_access(agent_id, app_pid, _runner=runner)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_no_session(self):
        """No existing session → list-clients fails → returns False."""
        agent_id = "abcdef1234567890"
        app_pid = 999
        view = _view_session_name(agent_id, app_pid)

        runner = _make_runner({
            ("list-clients", "-t", view): (1, "", "no session"),
        })
        result = await check_concurrent_access(agent_id, app_pid, _runner=runner)
        assert result is False


# ---------------------------------------------------------------------------
# F-04 — Suspend attach (out-of-tmux path)
# ---------------------------------------------------------------------------

class TestSuspendAttach:
    """F-04: suspend TUI + attach, sentinel file lifecycle, MCP check flag."""

    @pytest.mark.asyncio
    async def test_AC04_calls_suspend_fn_and_returns_need_mcp_check(self, tmp_path: Path):
        """AC-04 / AC-08: suspend_fn called, need_mcp_check=True on success."""
        suspend_called = []

        @asynccontextmanager
        async def mock_suspend_fn():
            suspend_called.append(True)
            yield

        with patch("agent_management.frontend.tmux_attach.TEMP_DIR", tmp_path):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = None
                result = await suspend_attach(
                    agent_id="abcdef12",
                    tmux_session_name="agent-mgmt-abc12345",
                    app_pid=999,
                    suspend_fn=mock_suspend_fn,
                )

        assert suspend_called == [True]
        assert result.ok is True
        assert result.need_mcp_check is True

    @pytest.mark.asyncio
    async def test_sentinel_cleaned_up_after_attach(self, tmp_path: Path):
        """F-04: sentinel file removed after successful attach."""
        with patch("agent_management.frontend.tmux_attach.TEMP_DIR", tmp_path):
            with patch("subprocess.run"):
                await suspend_attach(
                    agent_id="abcdef12",
                    tmux_session_name="agent-mgmt-abc12345",
                    app_pid=999,
                    suspend_fn=_mock_suspend,
                )

        # No sentinel files should remain
        sentinels = list(tmp_path.glob("attach_sentinel_*.txt"))
        assert sentinels == []

    @pytest.mark.asyncio
    async def test_sentinel_cleaned_up_even_if_attach_raises(self, tmp_path: Path):
        """F-04: sentinel removed even if subprocess.run raises."""
        @asynccontextmanager
        async def crashing_suspend():
            yield  # attach happens here, then raises

        with patch("agent_management.frontend.tmux_attach.TEMP_DIR", tmp_path):
            with patch("subprocess.run", side_effect=RuntimeError("crash")):
                with pytest.raises(RuntimeError):
                    await suspend_attach(
                        agent_id="abcdef12",
                        tmux_session_name="agent-mgmt-abc12345",
                        app_pid=999,
                        suspend_fn=crashing_suspend,
                    )

        sentinels = list(tmp_path.glob("attach_sentinel_*.txt"))
        assert sentinels == []


# ---------------------------------------------------------------------------
# F-06 / AC-11 — Session lifecycle management
# ---------------------------------------------------------------------------

class TestCleanupStaleSessions:
    """AC-11: stale sessions (dead PIDs) killed on startup."""

    @pytest.mark.asyncio
    async def test_AC11_kills_sessions_with_dead_pids(self, tmp_path: Path):
        """AC-11: session with dead PID → killed; live PID → spared."""
        current_pid = os.getpid()
        dead_pid = 99999999  # highly unlikely to be alive

        session_list = "\n".join([
            f"agmgr-enter-aabbccdd-{dead_pid}",
            f"agmgr-enter-eeff1122-{current_pid}",  # current process, not stale but same PID
            "unrelated-session",
        ])

        kill_calls: list[str] = []

        async def runner(*args: str) -> tuple[int, str, str]:
            if args == ("list-sessions", "-F", "#{session_name}"):
                return (0, session_list, "")
            if args[0] == "kill-session":
                kill_calls.append(args[2])  # args: ("kill-session", "-t", session_name)
                return (0, "", "")
            return (0, "", "")

        with patch("agent_management.frontend.tmux_attach.TEMP_DIR", tmp_path):
            await cleanup_stale_sessions(current_pid, _runner=runner)

        assert f"agmgr-enter-aabbccdd-{dead_pid}" in kill_calls
        # current_pid session should not be killed (it's our own)
        assert f"agmgr-enter-eeff1122-{current_pid}" not in kill_calls

    @pytest.mark.asyncio
    async def test_skips_cleanup_when_tmux_not_running(self, tmp_path: Path):
        """F-06: tmux list-sessions fails → no crash, silent skip."""
        runner = _make_runner({
            ("list-sessions", "-F", "#{session_name}"): (1, "", "no server running"),
        })
        with patch("agent_management.frontend.tmux_attach.TEMP_DIR", tmp_path):
            # Should not raise
            await cleanup_stale_sessions(os.getpid(), _runner=runner)


class TestCleanupAllSessions:
    """F-06: on TUI exit, kill all sessions owned by this PID."""

    @pytest.mark.asyncio
    async def test_kills_own_sessions_on_shutdown(self):
        """F-06: cleanup_all_sessions kills sessions with matching PID."""
        app_pid = 12345
        other_pid = 99999

        session_list = "\n".join([
            f"agmgr-enter-aabbccdd-{app_pid}",
            f"agmgr-enter-eeff1122-{other_pid}",
            "unrelated-session",
        ])

        kill_calls: list[str] = []

        async def runner(*args: str) -> tuple[int, str, str]:
            if args == ("list-sessions", "-F", "#{session_name}"):
                return (0, session_list, "")
            if args[0] == "kill-session":
                kill_calls.append(args[2])  # args: ("kill-session", "-t", session_name)
                return (0, "", "")
            return (0, "", "")

        await cleanup_all_sessions(app_pid, _runner=runner)

        assert f"agmgr-enter-aabbccdd-{app_pid}" in kill_calls
        assert f"agmgr-enter-eeff1122-{other_pid}" not in kill_calls


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_view_session_name_format(self):
        name = _view_session_name("abcdef1234567890", 42)
        assert name == f"{_SESSION_PREFIX}abcdef12-42"

    def test_view_session_name_truncates_agent_id(self):
        name = _view_session_name("x" * 32, 1)
        assert len(name.split("-")[-2]) == 8  # exactly 8 chars of agent_id

    def test_extract_pid_from_session_name_valid(self):
        pid = _extract_pid_from_session_name("agmgr-enter-abcd1234-99999")
        assert pid == 99999

    def test_extract_pid_from_session_name_invalid(self):
        pid = _extract_pid_from_session_name("agmgr-enter-abcd1234")
        assert pid is None

    def test_is_pid_alive_current_process(self):
        assert _is_pid_alive(os.getpid()) is True

    def test_is_pid_alive_dead_process(self):
        assert _is_pid_alive(99999999) is False
