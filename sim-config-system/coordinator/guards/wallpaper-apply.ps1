# Apply the standard desktop wallpaper AND the lock-screen / login image.
# Desktop is per-user (HKCU); lock screen is machine policy (HKLM, needs admin).
$ErrorActionPreference = 'Stop'
$src = Join-Path $env:SIM_ASSETS 'wallpaper.png'
if (-not (Test-Path $src)) { Write-Output 'wallpaper.png not provided (put it in the coordinator installs dir)'; exit 1 }
$dir = 'C:\ProgramData\sim'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$managed = Join-Path $dir 'wallpaper.png'
Copy-Item $src $managed -Force

# --- Desktop wallpaper (current user) ---
Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name Wallpaper -Value $managed
Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name WallpaperStyle -Value 10  # 10 = fill
Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name TileWallpaper -Value 0
rundll32.exe user32.dll, UpdatePerUserSystemParameters 1, True

# --- Lock screen / login image (machine-wide) ---
try {
  $ls = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization'
  New-Item -Path $ls -Force | Out-Null
  Set-ItemProperty $ls -Name LockScreenImage -Value $managed
} catch { Write-Output "desktop set; lock screen needs admin: $($_.Exception.Message)"; exit 0 }

Write-Output 'wallpaper + login image applied'; exit 0
