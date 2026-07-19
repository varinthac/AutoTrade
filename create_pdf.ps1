# PowerShell script to create PDF
$ErrorActionPreference = "Continue"

Write-Host "Creating PDF document..." -ForegroundColor Cyan

# Try running the Python script
$pythonScript = "D:\AutoTrade\create_pdf.py"
$pdfFile = "D:\AutoTrade\trading_system_summary.pdf"

# Check if Python is available
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) {
    $python = Get-Command python3 -ErrorAction SilentlyContinue
}

if ($null -ne $python) {
    Write-Host "Found Python: $($python.Source)" -ForegroundColor Green
    & python $pythonScript 2>&1
} else {
    Write-Host "Python not found in PATH" -ForegroundColor Yellow
    Write-Host "Creating basic PDF..." -ForegroundColor Yellow

    # Create a minimal PDF directly
    $pdfContent = @"
%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /Resources 4 0 R /MediaBox [0 0 612 792] /Contents 5 0 R >>
endobj
4 0 obj
<< /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>
endobj
5 0 obj
<< /Length 1200 >>
stream
BT
/F1 20 Tf
50 720 Td
(Automated Gold & Forex Trading System) Tj
0 -40 Td
/F1 12 Tf
(Decision Brief for Non-Technical Stakeholders) Tj
0 -45 Td
/F1 11 Tf
(SYSTEM OVERVIEW) Tj
0 -30 Td
/F1 10 Tf
(An automated trading system for Gold and Forex that works like a 5-person team) Tj
0 -25 Td
(- The Brain: AI Council debates every trade with 3 analytical voices) Tj
0 -20 Td
(- The Shield: Portfolio-level risk checkpoint) Tj
0 -20 Td
(- The CFO: Intelligent position sizing) Tj
0 -20 Td
(- The Watchman: Active position monitoring) Tj
0 -20 Td
(- The Auditor: Daily review and approval) Tj
0 -40 Td
/F1 11 Tf
(SAFETY FIRST) Tj
0 -30 Td
/F1 10 Tf
(1. Historical backtest on unseen data) Tj
0 -20 Td
(2. Paper trading with live conditions) Tj
0 -20 Td
(3. Gradual live ramp starting at 0.25 percent) Tj
0 -40 Td
/F1 11 Tf
(COST BREAKDOWN) Tj
0 -30 Td
/F1 10 Tf
(Infrastructure: approximately 15 to 60 dollars per month) Tj
0 -25 Td
(Cloud server: 10 to 30 per month) Tj
0 -20 Td
(AI service: 5 to 30 per month) Tj
0 -20 Td
(Market data: Free) Tj
0 -40 Td
(Note: Separate from trading capital and trading losses) Tj
ET
endstream
endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000203 00000 n
0000000290 00000 n
trailer
<< /Size 6 /Root 1 0 R >>
startxref
1541
%%EOF
"@

    [System.IO.File]::WriteAllText($pdfFile, $pdfContent)
}

# Check if PDF was created
if (Test-Path $pdfFile) {
    $fileSize = (Get-Item $pdfFile).Length
    Write-Host ""
    Write-Host "✓ PDF created successfully!" -ForegroundColor Green
    Write-Host "Location: $pdfFile" -ForegroundColor Green
    Write-Host "Size: $fileSize bytes" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "✗ PDF creation failed" -ForegroundColor Red
    Write-Host ""
    Write-Host "HTML version is available at:" -ForegroundColor Yellow
    Write-Host "  D:\AutoTrade\trading_system_summary.html" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "To create PDF:" -ForegroundColor Yellow
    Write-Host "  1. Open the HTML file in a web browser" -ForegroundColor Yellow
    Write-Host "  2. Press Ctrl+P to print" -ForegroundColor Yellow
    Write-Host "  3. Select 'Print to PDF' or 'Save as PDF'" -ForegroundColor Yellow
}
