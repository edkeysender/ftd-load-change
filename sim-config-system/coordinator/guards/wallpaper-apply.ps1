# Make ONE image appear at every stage of sign-in - lock screen, the sign-in prompt,
# and the desktop - instead of Windows migrating through three (Spotlight lock image ->
# blurred acrylic sign-in background -> desktop wallpaper). Needs admin (machine policy).
$ErrorActionPreference = 'Stop'
$src = Join-Path $env:SIM_ASSETS 'wallpaper.png'
if (-not (Test-Path $src)) { Write-Output 'wallpaper.png not provided (upload it in the dashboard)'; exit 1 }

# Desktop copy (kept on D: for parity with the existing check); a second copy in a
# machine-readable location the lock/sign-in screen can load BEFORE any user logs in.
$dir = 'D:\libraries\Pictures'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$dest = Join-Path $dir 'wallpaper.png'
Copy-Item $src $dest -Force

$lockDir = 'C:\ProgramData\sim'
New-Item -ItemType Directory -Force -Path $lockDir | Out-Null
$login = Join-Path $lockDir 'login.png'
Copy-Item $src $login -Force

$did = @()

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
$did += 'desktop set'

# --- Lock screen image, enforced (PersonalizationCSP is the reliable Win10/11 path) ---
$csp = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\PersonalizationCSP'
New-Item -Path $csp -Force | Out-Null
Set-ItemProperty $csp -Name LockScreenImageStatus -Value 1 -Type DWord
Set-ItemProperty $csp -Name LockScreenImagePath -Value $login
Set-ItemProperty $csp -Name LockScreenImageUrl  -Value $login
# Belt-and-braces policy + stop the user (or Windows) rotating it.
$pers = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization'
New-Item -Path $pers -Force | Out-Null
Set-ItemProperty $pers -Name LockScreenImage -Value $login
Set-ItemProperty $pers -Name NoChangingLockScreen -Value 1 -Type DWord
$did += 'lock screen locked to image'

# --- Kill Windows Spotlight (the rotating lock/desktop image source, machine-wide) ---
$cloud = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent'
New-Item -Path $cloud -Force | Out-Null
Set-ItemProperty $cloud -Name DisableWindowsSpotlightFeatures -Value 1 -Type DWord
Set-ItemProperty $cloud -Name DisableSpotlightCollectionOnDesktop -Value 1 -Type DWord
$did += 'spotlight off'

# --- Sign-in screen: show the image crisp, not a blurred acrylic pane over an accent ---
$sys = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System'
New-Item -Path $sys -Force | Out-Null
Set-ItemProperty $sys -Name DisableAcrylicBackgroundOnLogon -Value 1 -Type DWord
Set-ItemProperty $sys -Name DisableLogonBackgroundImage -Value 0 -Type DWord  # 0 = show the image
$did += 'sign-in blur off'

if ($applied) { Write-Output (($did -join '; ') + '. Lock/sign-in changes show at the next sign-out.'); exit 0 }
Write-Output (($did -join '; ') + '; but SystemParametersInfo failed for the live desktop'); exit 1
