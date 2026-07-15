# Install the LibreHardwareMonitor sensor libraries so this PC can report CPU (and
# AMD/Intel GPU) temperatures to the HealthCheck tab. The agent downloads lhm.zip into
# $env:SIM_ASSETS; we unpack it next to the agent. Nothing is registered or started -
# health-probe.ps1 loads the DLL on demand.
#
# lhm.zip holds LibreHardwareMonitorLib.dll AND its dependencies (HidSharp,
# System.Memory, BlackSharp.Core, DiskInfoToolkit, RAMSPDToolkit-NDD, ...). Installing
# the main DLL alone fails at load with "Unable to load one or more of the requested
# types", so unpack the lot. Build it with deploy/fetch-lhm.sh.
$ErrorActionPreference = 'Stop'

$lhmDir = if ($env:SIM_LHM) { $env:SIM_LHM } else { 'C:\sim-agent\lhm' }
$src = $env:SIM_ASSETS
if (-not $src -or -not (Test-Path $src)) { Write-Output 'no assets delivered'; exit 1 }

$zip = Join-Path $src 'lhm.zip'
if (-not (Test-Path $zip)) {
  Write-Output 'lhm.zip was not delivered - run deploy/fetch-lhm.sh on the coordinator (or upload it under Assets)'
  exit 1
}

New-Item -ItemType Directory -Force -Path $lhmDir | Out-Null
Expand-Archive -Path $zip -DestinationPath $lhmDir -Force
# Windows blocks loading DLLs that carry the downloaded-from-internet mark.
Get-ChildItem -Path $lhmDir -Filter *.dll | Unblock-File

$n = @(Get-ChildItem -Path $lhmDir -Filter *.dll).Count
if (-not (Test-Path (Join-Path $lhmDir 'LibreHardwareMonitorLib.dll'))) {
  Write-Output "unpacked $n dll(s) to $lhmDir but LibreHardwareMonitorLib.dll is not among them"
  exit 1
}
Write-Output "unpacked $n sensor dll(s) to $lhmDir"
exit 0
