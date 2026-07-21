@echo off
REM Starts the AutoTrade live shadow loop (--adapter demo) in a new console
REM window. See scripts/autotrade_control.py for the "start" subcommand's
REM full behavior (refuses if the kill switch is active, double-launch guard
REM inside run_shadow_loop.py itself).

"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\autotrade_control.py" start

pause
