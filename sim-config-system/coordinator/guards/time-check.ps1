# Shows this PC's current system time, and PASSES (exit 0) only if it agrees with the
# coordinator's clock to within SIM_TIME_TOLERANCE seconds (default 60).
#
# The coordinator is the reference on purpose: it stamps every version, deploy, guard
# result and health sample, so a PC whose clock disagrees with IT is the one that makes
# logs impossible to line up across the sim - regardless of who is "actually" right.
# We read its clock from the Date header of any HTTP response (RFC 1123, always UTC),
# which needs no extra endpoint and no auth.
$ErrorActionPreference = 'SilentlyContinue'

$tol = 60
if ($env:SIM_TIME_TOLERANCE) { $tol = [int]$env:SIM_TIME_TOLERANCE }

$now = Get-Date
$stamp = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f $now, (Get-TimeZone).Id
$src = ((w32tm /query /source) -join '').Trim()   # e.g. time.windows.com / Local CMOS Clock

# --- the coordinator's clock, via the Date header --------------------------
$skew = $null
if ($env:SIM_COORDINATOR) {
  # GET first: the coordinator's static root answers HEAD with 405, so HEAD-first would
  # burn a failed round trip every time. HEAD stays as a fallback for a leaner endpoint.
  foreach ($method in @('Get', 'Head')) {
    try {
      $r = Invoke-WebRequest -Uri $env:SIM_COORDINATOR -Method $method -UseBasicParsing -TimeoutSec 8
      $d = $r.Headers['Date']
      if ($d) {
        $ref = [datetime]::Parse($d, [Globalization.CultureInfo]::InvariantCulture,
                                 [Globalization.DateTimeStyles]::AdjustToUniversal)
        $skew = [math]::Round(((Get-Date).ToUniversalTime() - $ref).TotalSeconds)
        break
      }
    } catch { }
  }
}

if ($null -ne $skew) {
  $dir = if ($skew -gt 0) { 'ahead of' } else { 'behind' }
  $abs = [math]::Abs($skew)
  if ($abs -le $tol) {
    Write-Output "system time $stamp - in sync (${abs}s $dir the coordinator; time source: $src)"
    exit 0
  }
  Write-Output "system time $stamp - clock is ${abs}s $dir the coordinator (tolerance ${tol}s; time source: $src)"
  exit 1
}

# --- no reference reachable: fall back to whether Windows syncs at all ------
# Free-running clocks drift minutes a month, so an unsynced PC fails even though we
# cannot measure it right now. Say plainly that we could not compare.
if ($src -match 'Local CMOS Clock|Free-running') {
  Write-Output "system time $stamp - could not reach the coordinator to compare, and this PC syncs from nothing (source: $src)"
  exit 1
}
Write-Output "system time $stamp - could not reach the coordinator to compare; time source: $src"
exit 1
