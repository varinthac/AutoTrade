@echo off
REM Thai PDF Generation Script
REM This script generates the Thai PDF from the HTML source

echo Generating Thai PDF...
echo.

REM Run Chrome in headless mode to generate PDF
"C:\Program Files\Google\Chrome\Application\chrome.exe" --headless --disable-gpu --no-pdf-header-footer --print-to-pdf="D:\AutoTrade\trading_system_summary_th.pdf" --print-to-pdf-no-header "D:\AutoTrade\trading_system_summary_th.html"

echo.
echo Waiting for PDF generation to complete...
timeout /t 3 /nobreak

REM Check if PDF was created
if exist "D:\AutoTrade\trading_system_summary_th.pdf" (
    echo PDF created successfully!
    echo File: D:\AutoTrade\trading_system_summary_th.pdf
    dir "D:\AutoTrade\trading_system_summary_th.pdf"
) else (
    echo ERROR: PDF was not created
    exit /b 1
)

pause
