@echo off
REM Agent Management Platform — build check (Windows)
REM Usage: scripts\build.bat

where uv >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv is not installed.
    exit /b 1
)

cd /d "%~dp0.."

echo Syncing dependencies...
set VIRTUAL_ENV=
uv sync --quiet

echo Running syntax check...
uv run python -m py_compile ^
    src/agent_management/frontend/tmux_attach.py ^
    src/agent_management/frontend/agent_pane.py ^
    src/agent_management/frontend/app.py ^
    src/agent_management/backend/repository.py ^
    src/agent_management/backend/supervisor.py ^
    src/agent_management/backend/session_manager.py

echo Build OK.
