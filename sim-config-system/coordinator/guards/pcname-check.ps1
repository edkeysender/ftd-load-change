# PASS (exit 0) if this PC's name follows the standard WS-XX-XXX (X = a digit),
# optionally with a -role suffix, e.g. WS-01-002 or WS-25-024-display. Case-insensitive
# (Windows names are). Check-only: renaming a PC needs a reboot, so it's done by hand.
$ErrorActionPreference = 'SilentlyContinue'
$name = $env:COMPUTERNAME
if ($name -match '^WS-\d{2}-\d{3}(-[A-Za-z0-9]+)*$') { Write-Output "name '$name' matches WS-XX-XXX[-role]"; exit 0 }
Write-Output "name '$name' does not match WS-XX-XXX[-role] (X = digit, e.g. WS-01-002 or WS-25-024-display)"; exit 1
