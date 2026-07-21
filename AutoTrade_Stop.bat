@echo off
REM Requests a GRACEFUL stop of the running AutoTrade shadow loop. Any open
REM positions are left untouched (broker-side SL/TP still active, but
REM Watchman stops managing them until restarted) -- use
REM AutoTrade_EmergencyStop.bat instead if you need positions closed.

"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\autotrade_control.py" stop

pause
