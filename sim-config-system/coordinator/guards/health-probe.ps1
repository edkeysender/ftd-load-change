# Prints this PC's BIOS + CPU/GPU temperatures as one JSON object on stdout.
# Run by the agent every few minutes; the coordinator keeps 30 days of history.
# Read-only - this only reads sensors, it changes nothing.
#
# Source order per value: built-in first (no install), then LibreHardwareMonitor
# for whatever is still missing (its DLLs are put on the PC by the `sensors`
# guard: Installs -> Hardware sensors -> Apply).
#   CPU: ACPI thermal zone via WMI  -> LHM. Most desktop boards expose NO thermal
#        zone, so LHM is usually what actually answers for CPU.
#   GPU: nvidia-smi (NVIDIA driver) -> LHM (covers AMD/Intel too).
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

$out = [ordered]@{
  bios_vendor = $null; bios_version = $null; bios_date = $null
  cpu_name = $null; gpu_name = $null
  cpu_c = $null; gpu_c = $null
  cpu_src = $null; gpu_src = $null
  note = $null
}

# Why LHM enumerated sensors but every value was null. Checked in the order they actually
# bite: LHM 0.9.x reads CPU sensors through the PawnIO kernel driver (it no longer ships
# WinRing0 - it carries PawnIO modules like RyzenSMU/IntelMSR as embedded resources), so
# a missing PawnIO is by far the most common cause and is checked before the exotic ones.
function Get-NoSensorReason {
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if (-not $admin) { return 'the agent is not running as administrator (the sensor driver needs it).' }
  $pawn = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO',
            'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO') |
          Where-Object { Test-Path $_ }
  if (-not $pawn) {
    return 'the PawnIO driver is not installed - LibreHardwareMonitor reads CPU sensors through it. Apply the Hardware sensors guard (Load Configuration > Installs) to install it.'
  }
  $dg = Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\Microsoft\Windows\DeviceGuard
  if ($dg -and $dg.SecurityServicesRunning -contains 2) {
    return 'Memory Integrity (Core Isolation) is ON and may be blocking the sensor driver. Turn it off in Windows Security > Device security > Core isolation, then reboot.'
  }
  return 'its sensor driver could not load on this PC.'
}

# ---- static inventory -------------------------------------------------
$b = Get-CimInstance Win32_BIOS
if ($b) {
  $out.bios_vendor = $b.Manufacturer
  # SMBIOSBIOSVersion is the vendor's build string (e.g. F31h) - what you compare
  # against the vendor's download page; Version is the raw SMBIOS id.
  $out.bios_version = if ($b.SMBIOSBIOSVersion) { $b.SMBIOSBIOSVersion } else { $b.Version }
  if ($b.ReleaseDate) { $out.bios_date = ([datetime]$b.ReleaseDate).ToString('yyyy-MM-dd') }
}
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
if ($cpu -and $cpu.Name) { $out.cpu_name = $cpu.Name.Trim() }
$vc = Get-CimInstance Win32_VideoController |
      Where-Object { $_.AdapterCompatibility -notmatch 'Microsoft' } | Select-Object -First 1
if ($vc -and $vc.Name) { $out.gpu_name = $vc.Name.Trim() }

# ---- built-in: CPU via the ACPI thermal zone --------------------------
$tz = Get-CimInstance -Namespace root/WMI -ClassName MSAcpi_ThermalZoneTemperature
if ($tz) {
  $dk = ($tz | Measure-Object -Property CurrentTemperature -Maximum).Maximum  # deci-kelvin
  if ($dk -gt 0) {
    $c = [math]::Round(($dk / 10) - 273.15, 1)
    if ($c -gt 0 -and $c -lt 150) { $out.cpu_c = $c; $out.cpu_src = 'wmi' }
  }
}

# ---- built-in: GPU via nvidia-smi -------------------------------------
$smi = (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue).Source
if (-not $smi) {
  foreach ($p in @("$env:ProgramFiles\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
                   "$env:windir\System32\nvidia-smi.exe")) {
    if (Test-Path $p) { $smi = $p; break }
  }
}
if ($smi) {
  $t = & $smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>$null | Select-Object -First 1
  if ("$t".Trim() -match '^\d+$') { $out.gpu_c = [double]"$t".Trim(); $out.gpu_src = 'nvidia-smi' }
  $n = & $smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1
  if ($n) { $out.gpu_name = "$n".Trim() }
}

# ---- fallback: LibreHardwareMonitor for whatever is still missing ------
if (($null -eq $out.cpu_c) -or ($null -eq $out.gpu_c)) {
  $lhmDir = if ($env:SIM_LHM) { $env:SIM_LHM } else { 'C:\sim-agent\lhm' }
  $dll = Join-Path $lhmDir 'LibreHardwareMonitorLib.dll'
  if (Test-Path $dll) {
    try {
      Add-Type -Path $dll
      $comp = New-Object LibreHardwareMonitor.Hardware.Computer
      $comp.IsCpuEnabled = $true
      $comp.IsGpuEnabled = $true
      $comp.Open()
      $tempType = [LibreHardwareMonitor.Hardware.SensorType]::Temperature
      foreach ($hw in $comp.Hardware) {
        $hw.Update()
        $kind = [string]$hw.HardwareType     # Cpu | GpuNvidia | GpuAmd | GpuIntel | ...
        if ($kind -ne 'Cpu' -and $kind -notlike 'Gpu*') { continue }
        # Prefer the package/core sensor; otherwise the hottest temperature we saw.
        $best = $null
        foreach ($s in $hw.Sensors) {
          if ($s.SensorType -ne $tempType -or $null -eq $s.Value) { continue }
          $v = [double]$s.Value
          # LHM reports 0 for temperature sensors its driver could not actually poll.
          # A CPU/GPU at 0 C is not a reading - treat implausible values as absent rather
          # than publishing a number that looks real on the dashboard.
          if ($v -lt 5 -or $v -gt 150) { continue }
          $preferred = if ($kind -eq 'Cpu') { $s.Name -match 'Package' } else { $s.Name -match 'Core' }
          if ($preferred) { $best = $v; break }
          if ($null -eq $best -or $v -gt $best) { $best = $v }
        }
        if ($null -eq $best) { continue }
        $best = [math]::Round($best, 1)
        if ($kind -eq 'Cpu') {
          if ($null -eq $out.cpu_c) { $out.cpu_c = $best; $out.cpu_src = 'lhm' }
          if (-not $out.cpu_name) { $out.cpu_name = $hw.Name }
        } else {
          if ($null -eq $out.gpu_c) { $out.gpu_c = $best; $out.gpu_src = 'lhm' }
          if (-not $out.gpu_name) { $out.gpu_name = $hw.Name }
        }
      }
      $comp.Close()
      if ($null -eq $out.cpu_c) {
        # LHM loaded and enumerated sensors but every value came back null: its kernel
        # driver (WinRing0, needed for MSR access) did not load. Name the actual reason
        # - guessing "not admin" sends people down the wrong path.
        $out.note = 'LibreHardwareMonitor read no CPU temperature: ' + (Get-NoSensorReason)
      }
    } catch {
      $out.note = "LibreHardwareMonitor failed: $($_.Exception.Message)"
    }
  } elseif ($null -eq $out.cpu_c) {
    $out.note = 'No CPU temperature source: this board exposes no ACPI thermal zone and LibreHardwareMonitor is not installed here (Installs -> Hardware sensors -> Apply).'
  }
}

$out | ConvertTo-Json -Compress
