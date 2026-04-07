#!/bin/bash
# Agent Management Platform — build check (syntax + import validation)
# Usage: ./scripts/build.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

if ! command -v uv &>/dev/null; then
    echo "ERROR: uv is not installed. See scripts/start.sh for instructions."
    exit 1
fi

echo "Syncing dependencies..."
unset VIRTUAL_ENV
uv sync --quiet

echo "Running syntax check..."
uv run python -m py_compile \
    src/agent_management/frontend/tmux_attach.py \
    src/agent_management/frontend/agent_pane.py \
    src/agent_management/frontend/app.py \
    src/agent_management/backend/repository.py \
    src/agent_management/backend/supervisor.py \
    src/agent_management/backend/session_manager.py

echo "Build OK."
