@echo off
REM MANUAL USE ONLY (as of 2026-08-04 -- the scheduled watchdog task has been deleted).
REM Starts the external shadow-loop process-liveness watchdog
REM (scripts/run_loop_watchdog.py) in THIS window, in the foreground -- same
REM convention as AutoTrade_TelegramControl_Start.bat: this window IS the
REM running watchdog, no PID file / Stop button, closing the window (or
REM Ctrl+C) is the only way to stop it.
REM
REM Sends a Telegram alert the moment scripts/run_shadow_loop.py's process
REM disappears. Run this ALONGSIDE AutoTrade_Start.bat for manual testing, not as a substitute.
REM The scheduled heartbeat task (via Task Scheduler) is now the primary auto-recovery mechanism.

"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\run_loop_watchdog.py"
