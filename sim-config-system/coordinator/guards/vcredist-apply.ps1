# Silently install the x64 and x86 VC++ redistributables provided as assets.
$ErrorActionPreference = 'Stop'
$installers = @('vc_redist.x64.exe', 'vc_redist.x86.exe')
$ok = @(); $bad = @()
foreach ($name in $installers) {
  $f = Join-Path $env:SIM_ASSETS $name
  if (-not (Test-Path $f)) { $bad += "$name not uploaded"; continue }
  $p = Start-Process -FilePath $f -ArgumentList '/install', '/quiet', '/norestart' -Wait -PassThru
  # 0 = installed, 1638 = a newer version already present, 3010 = success (reboot needed)
  if ($p.ExitCode -in 0, 1638, 3010) { $ok += "$name(exit $($p.ExitCode))" }
  else { $bad += "$name failed exit $($p.ExitCode)" }
}
if ($bad.Count) { Write-Output ("done: " + ($ok -join ', ') + " | errors: " + ($bad -join ', ')); exit 1 }
Write-Output ("installed: " + ($ok -join ', ')); exit 0
