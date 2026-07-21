@echo off
REM EMERGENCY STOP -- halts trading AND closes every open position at
REM market (via scripts/kill_switch.py). Destructive: requires an explicit
REM Y confirmation below so an accidental double-click never immediately
REM flattens real positions.

choice /C YN /M "Close ALL open positions and halt trading?"
if errorlevel 2 goto :cancelled

"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\autotrade_control.py" emergency-stop --confirm
goto :end

:cancelled
echo Emergency stop cancelled -- no positions were closed.

:end
pause
