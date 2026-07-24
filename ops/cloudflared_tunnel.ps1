# AutoTrade dashboard exposure via Cloudflare Tunnel -- outbound-only
# connection from the VPS to Cloudflare's edge, so the dashboard (bound to
# 127.0.0.1:8765, see scripts/run_dashboard.py's own module docstring) can
# be reached at https://trade.kylerlink.com with a real Cloudflare-issued
# HTTPS certificate, WITHOUT opening any inbound port or needing a second
# dedicated IP -- deliberately avoiding a repeat of this session's multi-hour
# SSH/shared-IP/NAT saga (see docs/vps_deployment.md and this session's own
# ops/ssh_*.ps1 scripts for that history).
#
# Prerequisite: trade.kylerlink.com's DNS zone must already be on Cloudflare
# (nameservers pointed there) -- confirmed by the user before this script
# was written. Remove any existing A/CNAME record for trade.kylerlink.com
# in the Cloudflare dashboard BEFORE running this -- `tunnel route dns`
# will fail if a conflicting record already exists.
#
# This script automates everything EXCEPT the interactive browser login
# (`cloudflared tunnel login`) -- that step opens a URL you must open
# yourself and approve in a browser, picking the kylerlink.com zone. Run
# this script, watch the console for that URL/prompt, complete it, then
# the rest continues automatically.
#
# Usage (as Administrator, on the VPS):
#   iwr https://raw.githubusercontent.com/varinthac/AutoTrade/main/ops/cloudflared_tunnel.ps1 -OutFile cloudflared_tunnel.ps1; .\cloudflared_tunnel.ps1

$ErrorActionPreference = "Stop"
$InstallDir = "C:\Program Files\cloudflared"
$TunnelName = "autotrade-dashboard"
$Hostname = "trade.kylerlink.com"
$DashboardPort = 8765

Write-Host "=== Installing cloudflared ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$exePath = "$InstallDir\cloudflared.exe"
if (-not (Test-Path $exePath)) {
    Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $exePath
    Write-Host "Downloaded cloudflared to $exePath"
} else {
    Write-Host "cloudflared already present, skipping download."
}
& $exePath --version

Write-Host "`n=== Logging in to Cloudflare (INTERACTIVE) ===" -ForegroundColor Yellow
Write-Host "A URL will be printed below -- open it in a browser, log in to Cloudflare, and select the kylerlink.com zone to authorize." -ForegroundColor Yellow
$certPath = "$env:USERPROFILE\.cloudflared\cert.pem"
if (-not (Test-Path $certPath)) {
    & $exePath tunnel login
} else {
    Write-Host "Already logged in (cert.pem exists), skipping."
}

Write-Host "`n=== Creating tunnel '$TunnelName' ===" -ForegroundColor Cyan
$existing = & $exePath tunnel list 2>&1 | Select-String $TunnelName
if (-not $existing) {
    & $exePath tunnel create $TunnelName
} else {
    Write-Host "Tunnel '$TunnelName' already exists, skipping creation."
}

Write-Host "`n=== Routing DNS: $Hostname -> tunnel ===" -ForegroundColor Cyan
& $exePath tunnel route dns $TunnelName $Hostname

Write-Host "`n=== Writing tunnel config ===" -ForegroundColor Cyan
$credsFile = Get-ChildItem "$env:USERPROFILE\.cloudflared\*.json" | Select-Object -First 1
if (-not $credsFile) { throw "No tunnel credentials JSON found under $env:USERPROFILE\.cloudflared -- tunnel creation may have failed." }

$configYaml = @"
tunnel: $TunnelName
credentials-file: $($credsFile.FullName)

ingress:
  - hostname: $Hostname
    service: http://127.0.0.1:$DashboardPort
  - service: http_status:404
"@
Set-Content -Path "$InstallDir\config.yml" -Value $configYaml -Encoding ASCII
Write-Host "Written to $InstallDir\config.yml"

Write-Host "`n=== Installing cloudflared as a Windows service (survives reboot) ===" -ForegroundColor Cyan
Push-Location $InstallDir
& $exePath service install
Pop-Location
Set-Service -Name cloudflared -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name cloudflared -ErrorAction SilentlyContinue
Get-Service -Name cloudflared -ErrorAction SilentlyContinue | Format-Table -AutoSize

Write-Host "`n=== Done ===" -ForegroundColor Green
Write-Host "Once scripts/run_dashboard.py is running on port $DashboardPort, https://$Hostname should reach it." -ForegroundColor Green
Write-Host "Remember: the dashboard itself still has NO auth yet -- that's a separate change (webapp_auth.py) needed before this URL is safe to share." -ForegroundColor Yellow
