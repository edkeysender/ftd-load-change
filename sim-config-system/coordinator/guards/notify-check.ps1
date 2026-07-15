# PASS (exit 0) if Windows notifications are switched off - a toast popping over a
# fullscreen sim (or stealing focus mid-exercise) is exactly what we don't want.
# Checks the same settings notify-apply.ps1 writes; keep the two tables in step.
$ErrorActionPreference = 'SilentlyContinue'

# The agent may run as SYSTEM, whose HKCU is the SYSTEM profile - NOT the operator's.
# Resolve the console user's hive so we check what the person at the PC actually sees.
function Get-UserHive {
  $me = [Security.Principal.WindowsIdentity]::GetCurrent()
  if (-not $me.IsSystem) { return 'HKCU:' }
  $u = (Get-CimInstance Win32_ComputerSystem).UserName   # DOMAIN\user at the console
  if (-not $u) { return $null }                          # nobody logged on
  try {
    $sid = (New-Object System.Security.Principal.NTAccount($u)).Translate(
             [System.Security.Principal.SecurityIdentifier]).Value
  } catch { return $null }
  if (-not (Test-Path "Registry::HKEY_USERS\$sid")) { return $null }
  return "Registry::HKEY_USERS\$sid"
}

$settings = @(
  @{ Root='USER'; Path='SOFTWARE\Microsoft\Windows\CurrentVersion\PushNotifications';        Name='ToastEnabled';                      Value=0; What='toasts' },
  @{ Root='USER'; Path='SOFTWARE\Microsoft\Windows\CurrentVersion\Notifications\Settings';   Name='NOC_GLOBAL_SETTING_TOASTS_ENABLED'; Value=0; What='global toasts' },
  @{ Root='USER'; Path='SOFTWARE\Policies\Microsoft\Windows\Explorer';                       Name='DisableNotificationCenter';         Value=1; What='notification centre' },
  @{ Root='USER'; Path='SOFTWARE\Microsoft\Windows\CurrentVersion\ContentDeliveryManager';   Name='SubscribedContent-338389Enabled';   Value=0; What='tips & suggestions' },
  @{ Root='USER'; Path='SOFTWARE\Microsoft\Windows\CurrentVersion\ContentDeliveryManager';   Name='SoftLandingEnabled';                Value=0; What='spotlight tips' },
  @{ Root='HKLM'; Path='SOFTWARE\Policies\Microsoft\Windows Defender Security Center\Notifications'; Name='DisableNotifications';      Value=1; What='Defender alerts' }
)

$hive = Get-UserHive
if (-not $hive) { Write-Output 'cannot tell: nobody is logged on, so the per-user notification settings are not loaded'; exit 1 }

$bad = @()
foreach ($s in $settings) {
  $path = if ($s.Root -eq 'HKLM') { "HKLM:\$($s.Path)" } else { "$hive\$($s.Path)" }
  $cur = (Get-ItemProperty -Path $path -Name $s.Name).$($s.Name)
  if ($cur -ne $s.Value) { $bad += $s.What }
}
if ($bad.Count -eq 0) { Write-Output 'notifications disabled (toasts, notification centre, tips, Defender alerts)'; exit 0 }
Write-Output ('still enabled: ' + ($bad -join ', ')); exit 1
