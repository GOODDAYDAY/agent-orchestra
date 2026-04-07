@echo off
:: Agent Management Platform — startup script (Windows)
:: Handles missing uv, missing venv, and missing dependencies automatically

setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..

:: 1. Check uv
where uv >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv is not installed.
    echo.
    echo Install uv with:
    echo   winget install --id astral-sh.uv
    echo   or: pip install uv
    echo.
    echo Then restart your shell and run this script again.
    exit /b 1
)

for /f "tokens=*" %%v in ('uv --version') do echo %%v found.

:: 2. Change to project root
cd /d "%PROJECT_DIR%"

:: 3. Sync dependencies
echo Syncing dependencies...
uv sync --quiet
if errorlevel 1 (
    echo ERROR: dependency sync failed.
    exit /b 1
)

:: 4. Launch
echo Starting Agent Management Platform...
uv run python -m agent_management %*
