#!/bin/bash
# Agent Management Platform — startup script
# Handles missing uv, missing venv, and missing dependencies automatically

set -e
set -x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# 1. Check uv
if ! command -v uv &>/dev/null; then
    echo "ERROR: uv is not installed."
    echo ""
    echo "Install uv with:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo ""
    echo "Then restart your shell and run this script again."
    exit 1
fi

echo "$(uv --version) found."

# 2. Change to project root
cd "$PROJECT_DIR"

# 3. Sync dependencies (creates .venv if missing; no-op if up to date)
echo "Syncing dependencies..."
uv sync --quiet

# 4. Launch (unset VIRTUAL_ENV to suppress uv mismatch warning)
# Data directory defaults to .agent_management/ inside the project.
# Override by setting AGENT_MGMT_DATA_DIR before calling this script.
echo "Starting Agent Management Platform..."
unset VIRTUAL_ENV
export AGENT_MGMT_DATA_DIR="${AGENT_MGMT_DATA_DIR:-$PROJECT_DIR/.agent_management}"
exec uv run python -m agent_management "$@"
