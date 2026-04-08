"""Platform-wide configuration constants."""
import os
from pathlib import Path

# Base directory for all platform data.
# Override via AGENT_MGMT_DATA_DIR env var; default is .agent_management/ in the
# project root (set by start.sh) so data lives alongside the code, not in ~/.
_data_dir_env = os.environ.get("AGENT_MGMT_DATA_DIR", "")
BASE_DIR: Path = Path(_data_dir_env).resolve() if _data_dir_env else Path.cwd() / ".agent_management"
BASE_DIR.mkdir(parents=True, exist_ok=True)

# SQLite database path
DB_PATH: Path = BASE_DIR / "state.db"

# Temp directory for large payload injection via cat and rendered orchestrator prompts
TEMP_DIR: Path = BASE_DIR / "tmp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Claude CLI command — always use --dangerously-skip-permissions
CLAUDE_CMD: list[str] = ["claude", "--dangerously-skip-permissions"]

# tmux session name prefix
TMUX_SESSION_PREFIX: str = "agent-mgmt"

# Max payload length to send via tmux send-keys directly (chars)
# Longer payloads are written to a temp file and injected via cat
DIRECT_SEND_MAX_LEN: int = 200

# Claude session startup timeout (seconds) — agent is marked degraded if exceeded
SESSION_START_TIMEOUT: float = 30.0

# Graceful shutdown timeout before tmux kill-pane (seconds)
SESSION_STOP_TIMEOUT: float = 5.0

# TUI pane refresh interval (seconds) = 4 Hz
PANE_REFRESH_INTERVAL: float = 0.25

# REQ-012 v2: orchestrator dispatch loop tuning
# How often the supervisor polls the orchestrator/worker panes for new content (seconds).
DISPATCH_POLL_INTERVAL: float = 0.5
# If a worker pane has no new output for this long after a dispatch, treat it as
# completed-by-silence (the secondary fallback in F-09).
WORKER_SILENCE_TIMEOUT: float = 60.0
# If neither marker nor silence fires within this long after a dispatch,
# raise the stall fallback (the tertiary fallback in F-09).
ORCHESTRATOR_STALL_TIMEOUT: float = 600.0

# REQ-012 v2: schema version. Bumped whenever the SQLite schema changes in a
# breaking way; on mismatch the application shows a destructive-reset modal.
SCHEMA_VERSION: int = 5

# REQ-015: AgentPane preview tuning.
# OUTPUT_POLL_INTERVAL_MS — read-only preview refresh interval (currently
# unused at runtime; the existing PANE_REFRESH_INTERVAL drives refresh, this
# constant is reserved for future per-pane fine-grained polling and tests).
OUTPUT_POLL_INTERVAL_MS: int = 500
# OUTPUT_BUFFER_LINES — RichLog ring buffer cap; oldest lines are dropped
# silently when the buffer is full.
OUTPUT_BUFFER_LINES: int = 500
