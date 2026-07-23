# AutoTrade VPS SSH repair -- rewrites C:\ProgramData\ssh\sshd_config from a
# known-good full template (the earlier ssh_diagnose.ps1 run very likely
# clobbered it down to a near-empty file when its Get-Content/Set-Content
# round-trip hit a missing-path error mid-script -- see the long SSH
# debugging thread in this session's history for the full diagnosis).
#
# This restores the standard Win32-OpenSSH default config (matching
# Microsoft's own installer template) PLUS the `Match Group administrators`
# block that routes admin-group pubkey auth to administrators_authorized_keys
# (required on Windows -- regular .ssh/authorized_keys is NOT honored for
# accounts in the Administrators group), and explicit PasswordAuthentication
# yes so there's no ambiguity.
#
# Usage (as Administrator, on the VPS):
#   iwr https://raw.githubusercontent.com/varinthac/AutoTrade/main/ops/ssh_repair.ps1 -OutFile ssh_repair.ps1; .\ssh_repair.ps1

$ErrorActionPreference = "Stop"
$sshDir = "$env:ProgramData\ssh"
$configPath = "$sshDir\sshd_config"

Write-Host "=== Backing up current (possibly broken) sshd_config ===" -ForegroundColor Cyan
if (Test-Path $configPath) {
    Copy-Item $configPath "$configPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)" -Force
    Write-Host "Backed up."
} else {
    Write-Host "No existing file to back up."
}

New-Item -ItemType Directory -Path $sshDir -Force | Out-Null

Write-Host "`n=== Writing known-good sshd_config ===" -ForegroundColor Cyan
$config = @'
Port 22
AddressFamily any
ListenAddress 0.0.0.0
ListenAddress ::

HostKey __PROGRAMDATA__/ssh/ssh_host_rsa_key
HostKey __PROGRAMDATA__/ssh/ssh_host_ecdsa_key
HostKey __PROGRAMDATA__/ssh/ssh_host_ed25519_key

SyslogFacility AUTH
LogLevel INFO

LoginGraceTime 2m
PermitRootLogin prohibit-password
StrictModes yes
MaxAuthTries 6
MaxSessions 10

PubkeyAuthentication yes
AuthorizedKeysFile	.ssh/authorized_keys

PasswordAuthentication yes
PermitEmptyPasswords no
ChallengeResponseAuthentication no

AllowAgentForwarding yes
AllowTcpForwarding yes
GatewayPorts no
X11Forwarding no
PrintMotd no

Subsystem	sftp	sftp-server.exe

Match Group administrators
       AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
'@
Set-Content -Path $configPath -Value $config -Encoding ASCII
Write-Host "Written to $configPath"

Write-Host "`n=== Checking host keys exist ===" -ForegroundColor Cyan
$hostKeys = @("ssh_host_rsa_key", "ssh_host_ecdsa_key", "ssh_host_ed25519_key")
$missing = $hostKeys | Where-Object { -not (Test-Path "$sshDir\$_") }
if ($missing) {
    Write-Host "Missing host keys: $missing -- regenerating all via ssh-keygen -A"
    & "C:\Windows\System32\OpenSSH\ssh-keygen.exe" -A
} else {
    Write-Host "All host keys present."
}

Write-Host "`n=== Re-checking administrators_authorized_keys ===" -ForegroundColor Cyan
$keysFile = "$sshDir\administrators_authorized_keys"
if (Test-Path $keysFile) {
    Write-Host "Exists. Content:"
    Get-Content $keysFile
    Write-Host "`nACL:"
    icacls $keysFile
} else {
    Write-Host "MISSING -- pubkey auth for Administrators group won't work until this is created." -ForegroundColor Yellow
    Write-Host "(Password auth should still work now regardless.)"
}

Write-Host "`n=== Restarting sshd ===" -ForegroundColor Cyan
Restart-Service sshd
Start-Sleep -Seconds 2
Get-Service sshd | Format-Table -AutoSize

Write-Host "`n=== Effective config (sshd -T) -- verify passwordauthentication/authorizedkeysfile ===" -ForegroundColor Cyan
& "C:\Windows\System32\OpenSSH\sshd.exe" -T | Select-String "passwordauthentication|pubkeyauthentication|authorizedkeysfile|permitrootlogin"

Write-Host "`n=== Done. Try SSH from your local machine now. ===" -ForegroundColor Green
