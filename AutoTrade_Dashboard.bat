@echo off
REM MANUAL USE ONLY (as of 2026-08-04 -- the VPS dashboard is on-demand via Telegram /dashboard).
REM Launches the read-only AutoTrade trade dashboard (Flask) at
REM http://127.0.0.1:8765 -- local machine only, never exposed off this PC.
REM Use this for local testing/development. See scripts/run_dashboard.py for full behavior.

"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\run_dashboard.py"

pause
