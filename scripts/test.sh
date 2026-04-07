#!/bin/bash
# Agent Management Platform — run test suite
# Usage: ./scripts/test.sh [pytest-args...]
# Example: ./scripts/test.sh -k tmux_attach -v

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

if ! command -v uv &>/dev/null; then
    echo "ERROR: uv is not installed. See scripts/start.sh for instructions."
    exit 1
fi

echo "Running tests..."
unset VIRTUAL_ENV
exec uv run pytest "$@"
