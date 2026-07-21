@echo off
REM Launches the read-only AutoTrade trade dashboard (Flask) at
REM http://127.0.0.1:8765 -- local machine only, never exposed off this PC.
REM See scripts/run_dashboard.py for full behavior.

"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\run_dashboard.py"

pause
