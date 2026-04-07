"""Entry point: python -m agent_management [--help]"""
import sys


_HELP = """\
Agent Management Platform

USAGE
  python -m agent_management          Start the TUI
  python -m agent_management --help   Show this help

CONFIGURATION
  All settings can be overridden via environment variables before launch.

  AGENT_MGMT_DATA_DIR
      Directory for runtime data: SQLite database, temp files, logs.
      Default: <cwd>/.agent_management/
      Example: export AGENT_MGMT_DATA_DIR=/tmp/mydata

RUNTIME VALUES (resolved at startup)
  Run with --show-config to print the resolved values without starting the TUI.

STARTUP
  Recommended: use scripts/start.sh — it sets AGENT_MGMT_DATA_DIR automatically
  to <project_dir>/.agent_management/ and handles uv sync.

  Manual override example:
    AGENT_MGMT_DATA_DIR=~/my-data bash scripts/start.sh
"""


def _show_config() -> None:
    """Print resolved runtime configuration and exit."""
    from agent_management.shared.config import (
        BASE_DIR, DB_PATH, TEMP_DIR,
        CLAUDE_CMD, DIRECT_SEND_MAX_LEN,
        DISPATCH_POLL_INTERVAL, PANE_REFRESH_INTERVAL,
        SESSION_START_TIMEOUT, SESSION_STOP_TIMEOUT,
        ORCHESTRATOR_STALL_TIMEOUT, WORKER_SILENCE_TIMEOUT,
        SCHEMA_VERSION,
    )
    import os

    lines = [
        "Agent Management Platform — resolved configuration",
        "=" * 52,
        "",
        "Paths",
        f"  DATA_DIR          {BASE_DIR}",
        f"  DATABASE          {DB_PATH}",
        f"  TEMP_DIR          {TEMP_DIR}",
        "",
        "Claude CLI",
        f"  CLAUDE_CMD        {' '.join(CLAUDE_CMD)}",
        "",
        "Tuning",
        f"  PANE_REFRESH      {PANE_REFRESH_INTERVAL}s  (4 Hz)",
        f"  DIRECT_SEND_MAX   {DIRECT_SEND_MAX_LEN} chars",
        f"  SESSION_START_TO  {SESSION_START_TIMEOUT}s",
        f"  SESSION_STOP_TO   {SESSION_STOP_TIMEOUT}s",
        f"  DISPATCH_POLL     {DISPATCH_POLL_INTERVAL}s",
        f"  WORKER_SILENCE_TO {WORKER_SILENCE_TIMEOUT}s",
        f"  ORCH_STALL_TO     {ORCHESTRATOR_STALL_TIMEOUT}s",
        "",
        "Schema",
        f"  SCHEMA_VERSION    {SCHEMA_VERSION}",
        "",
        "Environment overrides",
        f"  AGENT_MGMT_DATA_DIR = {os.environ.get('AGENT_MGMT_DATA_DIR', '(not set — using cwd default)')}",
    ]
    print("\n".join(lines))


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("--help", "-h"):
        print(_HELP)
        return
    if args and args[0] == "--show-config":
        _show_config()
        return

    from agent_management.frontend.app import AgentManagementApp
    app = AgentManagementApp()
    app.run()


if __name__ == "__main__":
    sys.exit(main())
