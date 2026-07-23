# AutoTrade VPS SSH server reinstall -- removes the stale Windows-bundled
# OpenSSH Server (confirmed via `sshd -T`/debug log as "OpenSSH_for_Windows
# 8.1", much older than the 10.0p2 client connecting to it) and installs a
# current release from the upstream Win32-OpenSSH project instead, since an
# extensive diagnosis (config rewritten clean, correct LocalSystem service
# account, correct local security policies, correct password confirmed via
# a fresh RDP login) still left SSH password auth silently rejected before
# ever reaching a Windows LogonUser call -- consistent with a bug inside the
# old bundled sshd's own auth pipeline rather than any config/policy issue.
#
# Uses the GitHub API to resolve the latest release asset by exact name
# (not a guessed URL) -- same pattern as vps_bootstrap.ps1's Git-for-Windows
# install, after an earlier ambiguous `-like` match bug that session hit
# (matched both OpenSSH-Win64.zip and OpenSSH-Win64_Symbols.zip).
#
# Usage (as Administrator, on the VPS):
#   iwr https://raw.githubusercontent.com/varinthac/AutoTrade/main/ops/ssh_reinstall.ps1 -OutFile ssh_reinstall.ps1; .\ssh_reinstall.ps1

$ErrorActionPreference = "Stop"
$InstallDir = "C:\Program Files\OpenSSH"

Write-Host "=== Stopping and removing old sshd/ssh-agent services ===" -ForegroundColor Cyan
Stop-Service sshd, ssh-agent -ErrorAction SilentlyContinue
sc.exe delete sshd | Out-Null
sc.exe delete ssh-agent | Out-Null

Write-Host "`n=== Removing the old Windows Capability (Feature) install ===" -ForegroundColor Cyan
$cap = Get-WindowsCapability -Online | Where-Object { $_.Name -like "OpenSSH.Server*" }
if ($cap) {
    Write-Host "Found: $($cap.Name) -- removing (this can take a minute)..."
    Remove-WindowsCapability -Online -Name $cap.Name | Out-Null
} else {
    Write-Host "No OpenSSH.Server capability registered -- skipping."
}

Write-Host "`n=== Downloading latest Win32-OpenSSH release ===" -ForegroundColor Cyan
$release = Invoke-RestMethod -Uri "https://api.github.com/repos/PowerShell/Win32-OpenSSH/releases/latest"
$asset = $release.assets | Where-Object { $_.name -eq "OpenSSH-Win64.zip" } | Select-Object -First 1
if (-not $asset) { throw "Could not find an exact-match OpenSSH-Win64.zip asset in the latest Win32-OpenSSH release." }
Write-Host "Latest release: $($release.tag_name) -- asset: $($asset.name)"
$zipPath = "$env:TEMP\OpenSSH-Win64.zip"
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath

Write-Host "`n=== Extracting to $InstallDir ===" -ForegroundColor Cyan
if (Test-Path $InstallDir) {
    Rename-Item $InstallDir "$InstallDir-old-$(Get-Date -Format yyyyMMdd-HHmmss)" -ErrorAction SilentlyContinue
}
Expand-Archive -Path $zipPath -DestinationPath "C:\Program Files" -Force
if (Test-Path "C:\Program Files\OpenSSH-Win64") {
    Rename-Item "C:\Program Files\OpenSSH-Win64" $InstallDir
}
Remove-Item $zipPath -ErrorAction SilentlyContinue

Write-Host "`n=== Running the bundled install script ===" -ForegroundColor Cyan
Push-Location $InstallDir
powershell.exe -ExecutionPolicy Bypass -File .\install-sshd.ps1
Pop-Location

Write-Host "`n=== Restoring our Match Group administrators + PasswordAuthentication config ===" -ForegroundColor Cyan
$configPath = "$env:ProgramData\ssh\sshd_config"
$content = Get-Content $configPath -Raw
if ($content -notmatch 'PasswordAuthentication\s+yes') {
    $content += "`nPasswordAuthentication yes`n"
}
if ($content -notmatch 'Match Group administrators') {
    $content += "`nMatch Group administrators`n       AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys`n"
}
Set-Content -Path $configPath -Value $content -Encoding ASCII

Write-Host "`n=== Opening firewall port 22 (if not already) ===" -ForegroundColor Cyan
if (-not (Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    Write-Host "Firewall rule created."
} else {
    Write-Host "Firewall rule already exists."
}

Write-Host "`n=== Starting sshd, setting Automatic ===" -ForegroundColor Cyan
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
Get-Service sshd, ssh-agent | Format-Table -AutoSize

Write-Host "`n=== Version check ===" -ForegroundColor Cyan
& "$InstallDir\sshd.exe" -V 2>&1 | Out-String | Write-Host

Write-Host "`n=== Done. Try SSH from your local machine now. ===" -ForegroundColor Green
