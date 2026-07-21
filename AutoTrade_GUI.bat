@echo off
REM Launches the AutoTrade desktop GUI (start/stop/emergency-stop plus a
REM .env settings editor). See scripts/autotrade_gui.py for full behavior.

"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\autotrade_gui.py"

pause
