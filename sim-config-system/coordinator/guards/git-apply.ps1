# Install portable git by unzipping the bundle to C:\sim-agent\git.
$ErrorActionPreference = 'Stop'
$zip = Join-Path $env:SIM_ASSETS 'git-bundle.zip'
if (-not (Test-Path $zip)) { Write-Output 'git-bundle.zip not provided (run prepare-git-bundle.sh on the Pi)'; exit 1 }
$dest = 'C:\sim-agent\git'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Expand-Archive -Path $zip -DestinationPath $dest -Force
if (Test-Path (Join-Path $dest 'cmd\git.exe')) { Write-Output 'installed'; exit 0 }
Write-Output 'extracted but cmd\git.exe missing'; exit 1
