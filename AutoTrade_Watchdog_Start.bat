@echo off
REM Starts the external shadow-loop process-liveness watchdog
REM (scripts/run_loop_watchdog.py) in THIS window, in the foreground -- same
REM convention as AutoTrade_TelegramControl_Start.bat: this window IS the
REM running watchdog, no PID file / Stop button, closing the window (or
REM Ctrl+C) is the only way to stop it.
REM
REM Sends a Telegram alert the moment scripts/run_shadow_loop.py's process
REM disappears (2026-07-23 incident: it died silently with zero alert for
REM ~2 hours). Run this ALONGSIDE AutoTrade_Start.bat, not instead of it.

"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\run_loop_watchdog.py"
