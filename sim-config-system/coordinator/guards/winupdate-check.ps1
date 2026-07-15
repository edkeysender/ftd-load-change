# PASS (exit 0) if Windows Update cannot run on this PC. A sim must never reboot or
# swap a driver mid-session, so we want BOTH belts:
#   1. policy  HKLM\...\WindowsUpdate\AU\NoAutoUpdate = 1  (no automatic download/install)
#   2. service wuauserv startup = Disabled                 (Win11 ignores the policy alone)
$ErrorActionPreference = 'SilentlyContinue'

$au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
$noAuto = (Get-ItemProperty -Path $au -Name NoAutoUpdate).NoAutoUpdate
$svc = Get-Service -Name wuauserv
$start = (Get-CimInstance Win32_Service -Filter "Name='wuauserv'").StartMode  # Disabled | Manual | Auto

$bad = @()
if ($noAuto -ne 1) { $bad += 'NoAutoUpdate policy not set' }
if ($start -ne 'Disabled') { $bad += "wuauserv startup is $start" }
if ($svc -and $svc.Status -eq 'Running') { $bad += 'wuauserv is running' }

if ($bad.Count -eq 0) { Write-Output 'Windows Update disabled (policy + wuauserv disabled)'; exit 0 }
Write-Output ($bad -join '; '); exit 1
