# Install the LibreHardwareMonitor sensor libraries so this PC can report CPU (and
# AMD/Intel GPU) temperatures to the HealthCheck tab. The agent downloads lhm.zip into
# $env:SIM_ASSETS; we unpack it next to the agent. Nothing is registered or started -
# health-probe.ps1 loads the DLL on demand.
#
# lhm.zip holds LibreHardwareMonitorLib.dll AND its dependencies (HidSharp,
# System.Memory, BlackSharp.Core, DiskInfoToolkit, RAMSPDToolkit-NDD, ...). Installing
# the main DLL alone fails at load with "Unable to load one or more of the requested
# types", so unpack the lot. Build it with deploy/fetch-lhm.sh.
#
# We do NOT `Expand-Archive -Force` straight over the target: that deletes each existing
# file first, and a DLL currently loaded by a process (the health probe mid-sample, or a
# console that ran Add-Type) is a mapped image Windows will not let us delete or rename.
# Instead unpack to a temp dir and copy only what differs - so re-applying an already
# installed, identical build is a no-op that cannot fail on a lock.
$ErrorActionPreference = 'Stop'

$lhmDir = if ($env:SIM_LHM) { $env:SIM_LHM } else { 'C:\sim-agent\lhm' }
$src = $env:SIM_ASSETS
if (-not $src -or -not (Test-Path $src)) { Write-Output 'no assets delivered'; exit 1 }

$zip = Join-Path $src 'lhm.zip'
if (-not (Test-Path $zip)) {
  Write-Output 'lhm.zip was not delivered - run deploy/fetch-lhm.sh on the coordinator (or upload it under Assets)'
  exit 1
}

$tmp = Join-Path $env:TEMP ('lhm-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
  Expand-Archive -Path $zip -DestinationPath $tmp -Force
  New-Item -ItemType Directory -Force -Path $lhmDir | Out-Null

  $copied = 0; $same = 0; $locked = @()
  foreach ($f in Get-ChildItem -Path $tmp -Filter *.dll) {
    $dst = Join-Path $lhmDir $f.Name
    if (Test-Path $dst) {
      # Same bytes already in place - nothing to do, and no lock can bite us.
      if ((Get-FileHash $dst).Hash -eq (Get-FileHash $f.FullName).Hash) { $same++; continue }
    }
    try { Copy-Item -Path $f.FullName -Destination $dst -Force -ErrorAction Stop; $copied++ }
    catch { $locked += $f.Name }
  }
} finally {
  Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

# Windows blocks loading DLLs that carry the downloaded-from-internet mark.
Get-ChildItem -Path $lhmDir -Filter *.dll | Unblock-File -ErrorAction SilentlyContinue

# --- PawnIO: the kernel driver LHM 0.9.x reads CPU sensors through ---------
# LHM no longer ships WinRing0; it carries PawnIO modules (RyzenSMU, IntelMSR, ...) as
# embedded resources and needs the PawnIO driver present, or every sensor reads null.
# Flags are from the installer's own help text: -install -silent. It refuses to install
# over an existing copy ("a previous installation was found"), so check first.
$pawnKeys = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO',
              'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO')
$pawn = @($pawnKeys | Where-Object { Test-Path $_ }).Count -gt 0
$note = ''
if ($pawn) {
  $note = '; PawnIO already installed'
} else {
  $setup = Join-Path $src 'PawnIO_setup.exe'
  if (-not (Test-Path $setup)) {
    $note = '; PawnIO_setup.exe not delivered - run deploy/fetch-pawnio.sh on the coordinator (CPU temps need it on boards with no ACPI thermal zone)'
  } else {
    try {
      $p = Start-Process -FilePath $setup -ArgumentList '-install', '-silent' -PassThru -WindowStyle Hidden -ErrorAction Stop
      # Never block the agent forever if a dialog appears despite -silent.
      if (-not $p.WaitForExit(180000)) { $p.Kill(); throw 'installer did not finish within 180s' }
      $pawn = @($pawnKeys | Where-Object { Test-Path $_ }).Count -gt 0
      if ($pawn) { $note = '; PawnIO installed' }
      else { $note = "; PawnIO installer exited $($p.ExitCode) but did not register - CPU temps will stay empty" }
    } catch {
      $note = "; PawnIO install failed: $($_.Exception.Message)"
    }
  }
}

if ($locked.Count -gt 0) {
  Write-Output ("in use, could not replace: " + ($locked -join ', ') +
                " - a process has them loaded. Close any PowerShell window that ran Add-Type on them, or restart the agent, then Apply again." + $note)
  exit 1
}
if (-not (Test-Path (Join-Path $lhmDir 'LibreHardwareMonitorLib.dll'))) {
  Write-Output ("unpacked to $lhmDir but LibreHardwareMonitorLib.dll is not among the files" + $note)
  exit 1
}
Write-Output ("sensor dlls in $lhmDir - $copied installed/updated, $same already current" + $note)
# The DLLs are in place; without PawnIO they will read nothing, so say so loudly rather
# than reporting success and leaving the temperature mysteriously blank.
if (-not $pawn) { exit 1 }
exit 0
