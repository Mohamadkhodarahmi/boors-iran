@echo off
REM One-command start of the local system (scheduler + local API on http://127.0.0.1:8008).
REM First run: `uv sync` installs Python 3.14 + dependencies; `init-db` creates tables.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set UV_LINK_MODE=copy
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo uv is not installed. Install it from https://docs.astral.sh/uv/ and run this file again.
  exit /b 1
)

uv sync --quiet || exit /b 1
uv run python -m bors_analyser init-db || exit /b 1
uv run python -m bors_analyser run %*
