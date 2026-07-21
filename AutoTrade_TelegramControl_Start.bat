@echo off
REM Starts the Telegram inbound control listener (scripts/run_telegram_control.py)
REM in THIS window, in the foreground -- unlike AutoTrade_Start.bat, this does
REM not open a new console, this window IS the running listener.
REM
REM Closing this window (or Ctrl+C) is the only way to stop it -- there is no
REM PID file / Stop button for this process, by design: it does nothing but
REM listen for Telegram messages and reply, so there is nothing for a separate
REM stop mechanism to protect against.

"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\run_telegram_control.py"
