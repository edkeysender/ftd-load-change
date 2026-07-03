# PASS (exit 0) if the OpenSSH server is running.
$ErrorActionPreference = 'SilentlyContinue'
$svc = Get-Service sshd -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq 'Running') { Write-Output 'sshd running'; exit 0 }
Write-Output 'sshd not installed/running'; exit 1
