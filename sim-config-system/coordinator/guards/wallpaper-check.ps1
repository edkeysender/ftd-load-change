# PASS (exit 0) if a single managed image appears through the whole sign-in - desktop,
# lock screen, and sign-in prompt - with no Spotlight rotation or acrylic blur to make it
# look like several images migrating. Mirrors wallpaper-apply.ps1; keep the two in step.
$ErrorActionPreference = 'SilentlyContinue'

$managed = 'D:\libraries\Pictures\wallpaper.png'
$login = 'C:\ProgramData\sim\login.png'
if (-not (Test-Path $managed)) { Write-Output 'standard wallpaper not on this PC (click Apply)'; exit 1 }

$bad = @()

# Desktop background.
$cur = (Get-ItemProperty 'HKCU:\Control Panel\Desktop' -Name Wallpaper).Wallpaper
if ($cur -ne $managed) { $bad += "desktop wallpaper is '$cur'" }

# Lock screen enforced to our image (PersonalizationCSP is what actually holds).
$csp = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\PersonalizationCSP'
if ((Get-ItemProperty $csp -Name LockScreenImageStatus).LockScreenImageStatus -ne 1 -or
    (Get-ItemProperty $csp -Name LockScreenImagePath).LockScreenImagePath -ne $login) {
  $bad += 'lock screen image not enforced'
}

# Spotlight off (or the lock/desktop image rotates on its own).
if ((Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' -Name DisableWindowsSpotlightFeatures).DisableWindowsSpotlightFeatures -ne 1) {
  $bad += 'Windows Spotlight still on'
}

# Sign-in acrylic blur off (or the sign-in screen shows a blurred variant first).
if ((Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System' -Name DisableAcrylicBackgroundOnLogon).DisableAcrylicBackgroundOnLogon -ne 1) {
  $bad += 'sign-in acrylic blur still on'
}

if ($bad.Count -eq 0) { Write-Output 'one managed image on desktop, lock screen and sign-in (spotlight + blur off)'; exit 0 }
Write-Output ($bad -join '; '); exit 1
