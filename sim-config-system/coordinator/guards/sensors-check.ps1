# PASS (exit 0) if this PC can report a CPU temperature to the HealthCheck tab -
# either from the built-in ACPI thermal zone, or via the LibreHardwareMonitor DLLs
# that sensors-apply.ps1 puts in place. GPU temps are reported separately (nvidia-smi
# covers NVIDIA cards without any of this), so this checks the CPU path only.
$ErrorActionPreference = 'SilentlyContinue'

$tz = Get-CimInstance -Namespace root/WMI -ClassName MSAcpi_ThermalZoneTemperature
if ($tz) {
  $dk = ($tz | Measure-Object -Property CurrentTemperature -Maximum).Maximum
  if ($dk -gt 0) {
    $c = [math]::Round(($dk / 10) - 273.15, 1)
    if ($c -gt 0 -and $c -lt 150) { Write-Output "CPU temp readable from the ACPI thermal zone ($c C)"; exit 0 }
  }
}

$lhmDir = if ($env:SIM_LHM) { $env:SIM_LHM } else { 'C:\sim-agent\lhm' }
$dll = Join-Path $lhmDir 'LibreHardwareMonitorLib.dll'
if (-not (Test-Path $dll)) {
  Write-Output "no ACPI thermal zone on this board and LibreHardwareMonitor is not installed - click Apply (upload LibreHardwareMonitorLib.dll + HidSharp.dll above first)"
  exit 1
}
try {
  Add-Type -Path $dll
  $comp = New-Object LibreHardwareMonitor.Hardware.Computer
  $comp.IsCpuEnabled = $true
  $comp.Open()
  $tempType = [LibreHardwareMonitor.Hardware.SensorType]::Temperature
  $val = $null
  foreach ($hw in $comp.Hardware) {
    $hw.Update()
    foreach ($s in $hw.Sensors) {
      if ($s.SensorType -eq $tempType -and $null -ne $s.Value) { $val = [math]::Round([double]$s.Value, 1); break }
    }
    if ($null -ne $val) { break }
  }
  $comp.Close()
  if ($null -ne $val) { Write-Output "CPU temp readable via LibreHardwareMonitor ($val C)"; exit 0 }
  # Sensors enumerated but every value null = the driver did not load. Say why.
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  $dg = Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\Microsoft\Windows\DeviceGuard
  if (-not $admin) {
    Write-Output 'LibreHardwareMonitor loaded but read nothing: the agent is not running as administrator (its sensor driver needs it)'
  } elseif ($dg -and $dg.SecurityServicesRunning -contains 2) {
    Write-Output 'LibreHardwareMonitor loaded but read nothing: Memory Integrity (Core Isolation) is ON and blocks its sensor driver - turn it off in Windows Security > Device security > Core isolation, then reboot'
  } else {
    Write-Output 'LibreHardwareMonitor loaded but read nothing: its sensor driver could not load on this PC'
  }
  exit 1
} catch {
  Write-Output "LibreHardwareMonitor failed to load: $($_.Exception.Message)"
  exit 1
}
