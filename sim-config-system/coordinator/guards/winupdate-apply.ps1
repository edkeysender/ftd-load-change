# Disable Windows Update: set the AU policy, then stop and disable wuauserv.
# Nothing here reboots the PC. Needs admin (HKLM + service control).
$ErrorActionPreference = 'Stop'

$au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
New-Item -Path $au -Force | Out-Null
# 1 = never check for updates. AUOptions is what the (legacy) UI reads; set both so
# the policy is unambiguous to every consumer.
New-ItemProperty -Path $au -Name NoAutoUpdate -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $au -Name AUOptions -Value 1 -PropertyType DWord -Force | Out-Null

# The policy alone is not enough on Win11 - the Update Orchestrator can still pull
# updates - so take the service out too.
Stop-Service -Name wuauserv -Force -ErrorAction SilentlyContinue
Set-Service -Name wuauserv -StartupType Disabled

$start = (Get-CimInstance Win32_Service -Filter "Name='wuauserv'").StartMode
Write-Output "Windows Update disabled: NoAutoUpdate=1, wuauserv startup=$start"
exit 0
