# AutoTrade VPS SSH diagnosis -- run ONCE on the VPS via RDP to find out why
# both key-based and password-based SSH auth have been failing with
# "Permission denied" despite a correctly-populated administrators_authorized_keys
# and a confirmed-correct password. See docs/vps_deployment.md for the SSH
# setup this is diagnosing.
#
# Turns on verbose sshd logging (LogLevel DEBUG3), restarts the service, then
# dumps the real Windows Security-log failure reason for the most recent
# failed logon -- this is more reliable than `sshd.exe -d -d -d` on this
# Win32-OpenSSH build, since that re-execs into a child sshd-session.exe
# whose debug output never reaches the parent console.
#
# Usage (as Administrator, on the VPS):
#   iwr https://raw.githubusercontent.com/varinthac/AutoTrade/main/ops/ssh_diagnose.ps1 -OutFile ssh_diagnose.ps1; .\ssh_diagnose.ps1
#
# After running this, attempt an SSH connection from your LOCAL machine,
# then re-run just the last section (Get-WinEvent) to see the real reason.

Write-Host "=== sshd / sshd-session service status ===" -ForegroundColor Cyan
Get-Service sshd, ssh-agent -ErrorAction SilentlyContinue | Format-Table -AutoSize

Write-Host "`n=== sshd_config: auth-relevant lines ===" -ForegroundColor Cyan
Select-String -Path "$env:ProgramData\ssh\sshd_config" -Pattern `
    'PasswordAuthentication|PubkeyAuthentication|Match|AllowUsers|AllowGroups|DenyUsers|AuthenticationMethods|LogLevel'

Write-Host "`n=== administrators_authorized_keys: existence + ACL ===" -ForegroundColor Cyan
$keysFile = "$env:ProgramData\ssh\administrators_authorized_keys"
if (Test-Path $keysFile) {
    Write-Host "File exists. Content (public key fingerprint-safe to show):"
    Get-Content $keysFile
    Write-Host "`nACL:"
    icacls $keysFile
} else {
    Write-Host "MISSING: $keysFile" -ForegroundColor Red
}

Write-Host "`n=== Enabling verbose sshd logging (LogLevel DEBUG3) ===" -ForegroundColor Cyan
$configPath = "$env:ProgramData\ssh\sshd_config"
$content = Get-Content $configPath
if ($content -match '^\s*LogLevel\s') {
    $content = $content -replace '^\s*LogLevel\s.*$', 'LogLevel DEBUG3'
} else {
    $content += "LogLevel DEBUG3"
}
Set-Content -Path $configPath -Value $content
Restart-Service sshd
Write-Host "sshd restarted with DEBUG3 logging. Now attempt an SSH connection from your LOCAL machine, then re-run just this block:" -ForegroundColor Yellow

Write-Host "`n=== Most recent OpenSSH Operational log entries ===" -ForegroundColor Cyan
try {
    Get-WinEvent -LogName "OpenSSH/Operational" -MaxEvents 20 -ErrorAction Stop |
        Sort-Object TimeCreated |
        Format-Table TimeCreated, Id, Message -Wrap -AutoSize
} catch {
    Write-Host "OpenSSH/Operational log not available or empty: $_" -ForegroundColor Yellow
}

Write-Host "`n=== Most recent Security-log failed logon (4625) ===" -ForegroundColor Cyan
try {
    Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4625} -MaxEvents 5 -ErrorAction Stop |
        ForEach-Object { $_.Message } |
        Out-String | Write-Host
} catch {
    Write-Host "No 4625 events found yet (or Security log inaccessible): $_" -ForegroundColor Yellow
}
