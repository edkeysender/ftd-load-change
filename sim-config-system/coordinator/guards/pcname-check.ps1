# PASS (exit 0) if this PC's name follows the standard WS-XX-XXX (X = a digit),
# e.g. WS-01-002. Check-only: renaming a PC needs a reboot, so it's done by hand.
$ErrorActionPreference = 'SilentlyContinue'
$name = $env:COMPUTERNAME
if ($name -cmatch '^WS-\d{2}-\d{3}$') { Write-Output "name '$name' matches WS-XX-XXX"; exit 0 }
Write-Output "name '$name' does not match WS-XX-XXX (X = digit, e.g. WS-01-002)"; exit 1
