# PASS (exit 0) if the Recycle Bin icon is hidden from the desktop. A clean sim desktop
# shows only the managed shortcuts, not the bin. Per-user setting (HideDesktopIcons), so
# resolve the console user's hive - the agent may run as SYSTEM, whose HKCU is not the
# operator's. Keep in step with recyclebin-apply.ps1.
$ErrorActionPreference = 'SilentlyContinue'

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

$hive = Get-UserHive
if (-not $hive) { Write-Output 'cannot tell: nobody is logged on, so the per-user desktop-icon setting is not loaded'; exit 1 }

# {645FF040-5081-101B-9F08-00AA002F954E} is the Recycle Bin; 1 = hidden.
$clsid = '{645FF040-5081-101B-9F08-00AA002F954E}'
$path = "$hive\Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\NewStartPanel"
$v = (Get-ItemProperty -Path $path -Name $clsid).$clsid
if ($v -eq 1) { Write-Output 'Recycle Bin is hidden from the desktop'; exit 0 }
Write-Output 'Recycle Bin is shown on the desktop'; exit 1
