# Turn Windows notifications off: toasts, the notification centre, tips/suggestions
# and Defender alerts. Registry only - nothing is uninstalled and no reboot is forced.
# Keep the table in step with notify-check.ps1.
$ErrorActionPreference = 'Stop'

# The agent may run as SYSTEM, whose HKCU is the SYSTEM profile - writing there would
# "succeed" and change nothing the operator sees. Resolve the console user's hive.
function Get-UserHive {
  $me = [Security.Principal.WindowsIdentity]::GetCurrent()
  if (-not $me.IsSystem) { return 'HKCU:' }
  $u = (Get-CimInstance Win32_ComputerSystem).UserName
  if (-not $u) { return $null }
  try {
    $sid = (New-Object System.Security.Principal.NTAccount($u)).Translate(
             [System.Security.Principal.SecurityIdentifier]).Value
  } catch { return $null }
  if (-not (Test-Path "Registry::HKEY_USERS\$sid")) { return $null }
  return "Registry::HKEY_USERS\$sid"
}

$settings = @(
  @{ Root='USER'; Path='SOFTWARE\Microsoft\Windows\CurrentVersion\PushNotifications';        Name='ToastEnabled';                      Value=0 },
  @{ Root='USER'; Path='SOFTWARE\Microsoft\Windows\CurrentVersion\Notifications\Settings';   Name='NOC_GLOBAL_SETTING_TOASTS_ENABLED'; Value=0 },
  @{ Root='USER'; Path='SOFTWARE\Policies\Microsoft\Windows\Explorer';                       Name='DisableNotificationCenter';         Value=1 },
  @{ Root='USER'; Path='SOFTWARE\Microsoft\Windows\CurrentVersion\ContentDeliveryManager';   Name='SubscribedContent-338389Enabled';   Value=0 },
  @{ Root='USER'; Path='SOFTWARE\Microsoft\Windows\CurrentVersion\ContentDeliveryManager';   Name='SoftLandingEnabled';                Value=0 },
  @{ Root='HKLM'; Path='SOFTWARE\Policies\Microsoft\Windows Defender Security Center\Notifications'; Name='DisableNotifications';      Value=1 }
)

$hive = Get-UserHive
if (-not $hive) { Write-Output 'nobody is logged on - log in as the sim user, then Apply again (the per-user hive must be loaded)'; exit 1 }

$n = 0
foreach ($s in $settings) {
  $path = if ($s.Root -eq 'HKLM') { "HKLM:\$($s.Path)" } else { "$hive\$($s.Path)" }
  if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }
  New-ItemProperty -Path $path -Name $s.Name -Value $s.Value -PropertyType DWord -Force | Out-Null
  $n++
}
# Toasts stop immediately; the notification-centre policy is read by Explorer at
# logon, so it fully applies after the next sign-in. We don't restart Explorer here
# - that would flash the desktop on a PC that may be mid-session.
Write-Output "$n notification settings written (hive: $hive) - notification centre applies at next sign-in"
exit 0
