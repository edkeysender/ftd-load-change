# PASS (exit 0) if Windows Update cannot install anything on its own. A sim must never
# reboot or swap a driver mid-session.
#
# We assert the POLICY, not the service state. Disabling wuauserv looks tidier but does
# not survive a reboot: the Windows Update Medic Service (WaaSMedicSvc) exists precisely
# to repair tampering with Windows Update and re-enables it at boot, so a guard demanding
# "wuauserv is Disabled" flips back to fail on every restart and can never be satisfied.
# The policy keys under HKLM\SOFTWARE\Policies survive reboots and are what actually stop
# updates on Pro/Enterprise. The service state is reported below, not enforced.
#
# NOTE: Windows Home ignores these policies. Sim PCs are expected to be Pro/Enterprise;
# the edition is printed so a Home box is obvious rather than silently unprotected.
$ErrorActionPreference = 'SilentlyContinue'

$au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
$wu = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'

$bad = @()
# 1 = "Configure Automatic Updates: Disabled" - never check, download or install.
if ((Get-ItemProperty -Path $au -Name NoAutoUpdate).NoAutoUpdate -ne 1) { $bad += 'NoAutoUpdate policy not set' }
if ((Get-ItemProperty -Path $au -Name AUOptions).AUOptions -ne 1) { $bad += 'AUOptions is not 1 (never check)' }
# Belt and braces: do not reach out to Microsoft's update servers at all.
if ((Get-ItemProperty -Path $wu -Name DoNotConnectToWindowsUpdateInternetLocations).DoNotConnectToWindowsUpdateInternetLocations -ne 1) {
  $bad += 'still allowed to contact Windows Update online'
}

$edition = (Get-CimInstance Win32_OperatingSystem).Caption
if ($edition -match 'Home') { $bad += "$edition ignores Windows Update policy - updates cannot be policy-blocked on this edition" }

$svc = (Get-CimInstance Win32_Service -Filter "Name='wuauserv'").StartMode
if ($bad.Count -eq 0) {
  Write-Output "Windows Update disabled by policy (wuauserv startup=$svc, not enforced - Windows resets it at boot)"
  exit 0
}
Write-Output ($bad -join '; ')
exit 1
