@echo off
REM Agent Management Platform — run test suite (Windows)
REM Usage: scripts\test.bat [pytest-args...]

where uv >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv is not installed.
    exit /b 1
)

cd /d "%~dp0.."

echo Running tests...
set VIRTUAL_ENV=
uv run pytest %*
