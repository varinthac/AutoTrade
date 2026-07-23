# AutoTrade VPS bootstrap -- run this ONCE on a fresh Windows Server VPS to
# get from a blank machine to "repo cloned, venv ready, dependencies
# installed" with a single command. See docs/vps_deployment.md for the
# manual steps this automates, and for everything this script deliberately
# does NOT do (MT5 terminal install/login -- GUI-only, no way to script it
# safely; .env credentials -- never baked into a script; Task Scheduler /
# auto-logon / heartbeat -- one-time interactive setup, not idempotent
# enough to blindly re-run).
#
# Usage (typed manually on the VPS, one line, no clipboard needed):
#   iwr https://raw.githubusercontent.com/varinthac/AutoTrade/main/ops/vps_bootstrap.ps1 -OutFile bootstrap.ps1; .\bootstrap.ps1
#
# Idempotent: safe to re-run -- every step checks whether its target already
# exists/works before doing anything.

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/varinthac/AutoTrade.git"
$RepoPath = "C:\AutoTrade"
$PythonVersion = "3.12.10"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "=== $msg ===" -ForegroundColor Cyan
}

# --- 1. Python -------------------------------------------------------------
Write-Step "Checking Python"
$pythonOk = $false
try {
    $v = & python --version 2>&1
    if ($v -match [regex]::Escape($PythonVersion)) { $pythonOk = $true }
    Write-Host "Found: $v"
} catch { }

if (-not $pythonOk) {
    Write-Host "Installing Python $PythonVersion ..."
    $pyInstaller = "$env:TEMP\python-installer.exe"
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" -OutFile $pyInstaller
    Start-Process -FilePath $pyInstaller -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
    Remove-Item $pyInstaller -ErrorAction SilentlyContinue
    # Refresh PATH in this process from the machine+user environment so the
    # freshly-installed python.exe is visible without opening a new session.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    Write-Host "Python installed: $(python --version)"
} else {
    Write-Host "Python already OK, skipping install."
}

# --- 2. Git ------------------------------------------------------------------
Write-Step "Checking Git"
$gitOk = $false
try {
    & git --version 2>&1 | Out-Null
    $gitOk = $true
    Write-Host "Found: $(git --version)"
} catch { }

if (-not $gitOk) {
    Write-Host "Installing Git for Windows (latest release via GitHub API) ..."
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/git-for-windows/git/releases/latest"
    $asset = $release.assets | Where-Object { $_.name -match "^Git-.*-64-bit\.exe$" } | Select-Object -First 1
    if (-not $asset) { throw "Could not find a 64-bit Git installer asset in the latest git-for-windows release." }
    $gitInstaller = "$env:TEMP\git-installer.exe"
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $gitInstaller
    Start-Process -FilePath $gitInstaller -ArgumentList "/VERYSILENT /NORESTART" -Wait
    Remove-Item $gitInstaller -ErrorAction SilentlyContinue
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    Write-Host "Git installed: $(git --version)"
} else {
    Write-Host "Git already OK, skipping install."
}

# --- 3. Clone (or update) the repo -----------------------------------------
Write-Step "Cloning/updating AutoTrade repo at $RepoPath"
if (Test-Path "$RepoPath\.git") {
    Write-Host "Repo already exists -- pulling latest instead of re-cloning."
    Push-Location $RepoPath
    git pull
    Pop-Location
} else {
    git clone $RepoUrl $RepoPath
}

# --- 4. venv + dependencies from the lockfile -------------------------------
Write-Step "Setting up venv"
Push-Location $RepoPath
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    python -m venv .venv
} else {
    Write-Host "venv already exists, skipping creation."
}

Write-Step "Installing dependencies from requirements-lock.txt"
.\.venv\Scripts\pip install -r requirements-lock.txt
.\.venv\Scripts\pip install -e . --no-deps

Write-Step "Smoke test"
.\.venv\Scripts\python.exe -c "import autotrade; from autotrade.orchestrator.shadow_loop import ShadowLoop; print('Import OK')"
Pop-Location

Write-Step "Done"
Write-Host "Repo + venv ready at $RepoPath." -ForegroundColor Green
Write-Host "Still needed manually (see docs/vps_deployment.md):" -ForegroundColor Yellow
Write-Host "  - Copy .env.example to .env and fill in real credentials"
Write-Host "  - Install the MT5 terminal and log in interactively once"
Write-Host "  - Task Scheduler auto-start entries (Section 6a/6b)"
Write-Host "  - External heartbeat (Section 6c)"
