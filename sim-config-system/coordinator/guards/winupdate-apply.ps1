# Stop Windows Update installing anything on its own: set the policies that survive a
# reboot, then best-effort stop the service.
#
# The policies are the part that holds. Disabling wuauserv is done too, but Windows'
# Update Medic Service puts it back at the next boot, so winupdate-check.ps1 does not
# require it - see its header. Needs admin. Does not reboot.
$ErrorActionPreference = 'SilentlyContinue'

$au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
$wu = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'
New-Item -Path $au -Force | Out-Null

$did = @()
# 1 = never check for updates (the "Disabled" setting of Configure Automatic Updates).
New-ItemProperty -Path $au -Name NoAutoUpdate -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $au -Name AUOptions   -Value 1 -PropertyType DWord -Force | Out-Null
# Don't contact Microsoft's update servers at all.
New-ItemProperty -Path $wu -Name DoNotConnectToWindowsUpdateInternetLocations -Value 1 -PropertyType DWord -Force | Out-Null
# Never let a quality update swap a driver under a running sim.
New-ItemProperty -Path $wu -Name ExcludeWUDriversInQualityUpdate -Value 1 -PropertyType DWord -Force | Out-Null
$did += 'policy: no automatic updates, no online update check, no driver updates'

# Best effort - Windows restores this at boot, which is why the check ignores it.
Stop-Service -Name wuauserv -Force
Set-Service -Name wuauserv -StartupType Disabled
# Recovery tab -> all three failure actions to "Take No Action". Belt-and-braces only:
# this stops the SCM restarting wuauserv if it *crashes*, not the Update Medic Service
# (WaaSMedicSvc) that re-enables it at boot - so, like the disable above, it is not
# enforced by the check. `sc.exe`, not the `sc` PowerShell alias (= Set-Content).
& sc.exe failure wuauserv reset= 0 actions= '""' | Out-Null
$svc = (Get-CimInstance Win32_Service -Filter "Name='wuauserv'").StartMode
$did += "wuauserv stopped, startup=$svc, recovery=take-no-action (Windows may re-enable it at boot - the policy is what holds)"

$edition = (Get-CimInstance Win32_OperatingSystem).Caption
if ($edition -match 'Home') {
  Write-Output (($did -join '; ') + "; WARNING: $edition ignores these policies - this PC needs Pro/Enterprise to be protected")
  exit 1
}
Write-Output ($did -join '; ')
exit 0
