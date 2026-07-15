# Install the LibreHardwareMonitor sensor DLLs so this PC can report CPU (and
# AMD/Intel GPU) temperatures to the HealthCheck tab. The agent downloads the
# uploaded assets into $env:SIM_ASSETS; we just copy them next to the agent.
# Nothing is registered or started - health-probe.ps1 loads the DLL on demand.
$ErrorActionPreference = 'Stop'

$lhmDir = if ($env:SIM_LHM) { $env:SIM_LHM } else { 'C:\sim-agent\lhm' }
$src = $env:SIM_ASSETS
if (-not $src -or -not (Test-Path $src)) { Write-Output 'no assets delivered'; exit 1 }

$dlls = Get-ChildItem -Path $src -Filter *.dll
if (-not $dlls) { Write-Output 'no .dll assets uploaded - upload LibreHardwareMonitorLib.dll + HidSharp.dll under Assets above'; exit 1 }

New-Item -ItemType Directory -Force -Path $lhmDir | Out-Null
foreach ($d in $dlls) { Copy-Item -Path $d.FullName -Destination (Join-Path $lhmDir $d.Name) -Force }
# Windows blocks loading DLLs that carry the downloaded-from-internet mark.
Get-ChildItem -Path $lhmDir -Filter *.dll | Unblock-File

Write-Output ("installed {0} to {1}: {2}" -f $dlls.Count, $lhmDir, (($dlls | ForEach-Object { $_.Name }) -join ', '))
exit 0
