# Bring this PC's clock in line with the coordinator's.
#
# First the proper way: make sure the Windows Time service runs and resync it. On an
# isolated sim LAN that usually achieves nothing (no NTP peer is reachable), so if the
# clock is still off we set it directly from the coordinator's Date header - the
# coordinator is the sim's reference clock, so matching it is the point.
#
# Needs admin (Set-Date + service control). No reboot. Keep in step with time-check.ps1.
$ErrorActionPreference = 'SilentlyContinue'

$tol = 60
if ($env:SIM_TIME_TOLERANCE) { $tol = [int]$env:SIM_TIME_TOLERANCE }
$did = @()

# 1. Windows Time service: running and automatic, then resync.
Set-Service -Name w32time -StartupType Automatic
Start-Service -Name w32time
w32tm /resync /force 2>$null | Out-Null
$did += 'w32time: enabled + resync requested'

# 2. Measure against the coordinator.
function Get-Skew {
  if (-not $env:SIM_COORDINATOR) { return $null }
  # GET first - the coordinator's static root answers HEAD with 405 (see time-check.ps1).
  foreach ($method in @('Get', 'Head')) {
    try {
      $r = Invoke-WebRequest -Uri $env:SIM_COORDINATOR -Method $method -UseBasicParsing -TimeoutSec 8
      $d = $r.Headers['Date']
      if ($d) {
        $ref = [datetime]::Parse($d, [Globalization.CultureInfo]::InvariantCulture,
                                 [Globalization.DateTimeStyles]::AdjustToUniversal)
        return @([math]::Round(((Get-Date).ToUniversalTime() - $ref).TotalSeconds), $ref)
      }
    } catch { }
  }
  return $null
}

$s = Get-Skew
if ($null -eq $s) {
  Write-Output (($did -join '; ') + '; could not reach the coordinator to compare clocks')
  exit 1
}

if ([math]::Abs($s[0]) -le $tol) {
  Write-Output (($did -join '; ') + "; clock already within $($s[0])s of the coordinator")
  exit 0
}

# 3. Still off: take the coordinator's time. Date header resolution is 1s, which is far
# inside the tolerance we care about here.
try {
  Set-Date -Date $s[1].ToLocalTime() -ErrorAction Stop | Out-Null
  $did += "clock set from the coordinator (was $($s[0])s out)"
} catch {
  Write-Output (($did -join '; ') + "; could not set the clock: $($_.Exception.Message)")
  exit 1
}

$after = Get-Skew
if ($null -ne $after -and [math]::Abs($after[0]) -le $tol) {
  Write-Output (($did -join '; ') + "; now {0:yyyy-MM-dd HH:mm:ss} ({1}s off)" -f (Get-Date), $after[0])
  exit 0
}
Write-Output (($did -join '; ') + '; clock still out after setting it - is something else resetting it?')
exit 1
