# PASS (exit 0) if the desktop wallpaper is our managed image.
$ErrorActionPreference = 'SilentlyContinue'
$managed = 'C:\ProgramData\sim\wallpaper.png'
if (-not (Test-Path $managed)) { Write-Output 'no managed wallpaper applied'; exit 1 }
$cur = (Get-ItemProperty 'HKCU:\Control Panel\Desktop' -Name Wallpaper).Wallpaper
if ($cur -eq $managed) { Write-Output 'wallpaper matches standard'; exit 0 }
Write-Output "current wallpaper is '$cur'"; exit 1
