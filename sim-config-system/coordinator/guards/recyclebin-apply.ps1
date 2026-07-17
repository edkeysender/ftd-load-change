# Hide the Recycle Bin icon from the desktop (both the modern and classic desktop-icon
# views). Registry only - we do NOT restart Explorer, which would flash the desktop on a
# PC that may be mid-session; the icon disappears at the next sign-in (or an Explorer
# refresh). Keep in step with recyclebin-check.ps1.
$ErrorActionPreference = 'Stop'

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

$hive = Get-UserHive
if (-not $hive) { Write-Output 'nobody is logged on - log in as the sim user, then Apply again (the per-user hive must be loaded)'; exit 1 }

# 1 = hidden. Set both views so it stays hidden whichever the shell reads.
$clsid = '{645FF040-5081-101B-9F08-00AA002F954E}'
foreach ($view in @('NewStartPanel', 'ClassicStartMenu')) {
  $path = "$hive\Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\$view"
  if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }
  New-ItemProperty -Path $path -Name $clsid -Value 1 -PropertyType DWord -Force | Out-Null
}
Write-Output "Recycle Bin set to hidden (hive: $hive) - the icon disappears at the next sign-in"
exit 0
