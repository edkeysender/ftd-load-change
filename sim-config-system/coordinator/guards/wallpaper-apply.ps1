# Copy the standard wallpaper to the PC and set it as the desktop background AND the
# login / lock-screen image. Desktop is applied live via SystemParametersInfo (works
# for the session the agent runs in); login image is a machine policy (needs admin).
$ErrorActionPreference = 'Stop'
$src = Join-Path $env:SIM_ASSETS 'wallpaper.png'
if (-not (Test-Path $src)) { Write-Output 'wallpaper.png not provided (upload it in the dashboard)'; exit 1 }

$dir = 'D:\libraries\Pictures'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$dest = Join-Path $dir 'wallpaper.png'
Copy-Item $src $dest -Force

# --- Desktop background (live, current session) ---
Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name Wallpaper -Value $dest
Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name WallpaperStyle -Value 10  # 10 = fill
Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name TileWallpaper -Value 0
Add-Type -TypeDefinition @"
using System.Runtime.InteropServices;
public class SimWp {
  [DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Auto)]
  public static extern bool SystemParametersInfo(int uAction, int uParam, string lpvParam, int fuWinIni);
}
"@
# SPI_SETDESKWALLPAPER = 20 ; SPIF_UPDATEINIFILE(1) | SPIF_SENDWININICHANGE(2) = 3
$applied = [SimWp]::SystemParametersInfo(20, 0, $dest, 3)

# --- Login / lock-screen image (machine-wide policy; needs admin) ---
$loginNote = ''
try {
  $ls = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization'
  New-Item -Path $ls -Force | Out-Null
  Set-ItemProperty $ls -Name LockScreenImage -Value $dest
} catch { $loginNote = " (login image needs admin: $($_.Exception.Message))" }

if ($applied) { Write-Output "wallpaper set to $dest$loginNote"; exit 0 }
Write-Output "copied to $dest but SystemParametersInfo failed$loginNote"; exit 1
