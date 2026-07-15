# Max performance, never sleep. Sets the High/Ultimate performance plan, zeroes every
# idle timeout (AC and DC), turns hibernation and USB selective suspend off, and
# unticks "allow the computer to turn off this device to save power" on every device.
# Needs admin. Does not reboot. Keep in step with power-check.ps1.
$ErrorActionPreference = 'SilentlyContinue'

$HIGH     = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'
$ULTIMATE = 'e9a42b02-d5df-448d-aa00-03f14749eb61'
$did = @()

# Ultimate Performance is hidden until duplicated; fall back to High if the SKU
# doesn't offer it (duplicatescheme is a no-op if it already exists).
powercfg /duplicatescheme $ULTIMATE 2>$null | Out-Null
powercfg /setactive $ULTIMATE 2>$null
$active = (powercfg /getactivescheme) -join ' '
if ($active -notmatch [regex]::Escape($ULTIMATE)) {
  powercfg /setactive $HIGH 2>$null
  $active = (powercfg /getactivescheme) -join ' '
  $did += 'plan: High performance'
} else {
  $did += 'plan: Ultimate performance'
}

# 0 = never, for both mains and battery.
foreach ($t in @('standby-timeout', 'hibernate-timeout', 'monitor-timeout', 'disk-timeout')) {
  powercfg /change "$t-ac" 0 2>$null
  powercfg /change "$t-dc" 0 2>$null
}
$did += 'idle timeouts: never (sleep, hibernate, monitor, disk)'

# Hibernation also brings fast startup, which skips a real shutdown - not wanted on a sim.
powercfg /hibernate off 2>$null
$did += 'hibernation: off'

# USB selective suspend off, on the active scheme (catches USB controls/yokes dropping out).
$SUB_USB = '2a737441-1930-4402-8d77-b2bebba308a3'
$USB_SUSPEND = '48e6b7a6-50f5-4782-a5d4-53bb8f07e226'
powercfg /setacvalueindex SCHEME_CURRENT $SUB_USB $USB_SUSPEND 0 2>$null
powercfg /setdcvalueindex SCHEME_CURRENT $SUB_USB $USB_SUSPEND 0 2>$null
powercfg /setactive SCHEME_CURRENT 2>$null
$did += 'USB selective suspend: off'

# Untick "allow the computer to turn off this device to save power" on every device that
# offers it - including the Wake-on-LAN NIC: waking a real sim PC with this unticked
# proved wake still works, so there is nothing to exempt. Not all devices expose the
# setting and some refuse the write, so count rather than fail - power-check.ps1 only
# insists on the network/USB/input ones (see its header).
$devs = Get-CimInstance -Namespace root\wmi -ClassName MSPower_DeviceEnable
$changed = 0; $failed = 0
foreach ($d in @($devs | Where-Object { $_.Enable })) {
  try {
    $d.Enable = $false
    Set-CimInstance -InputObject $d -ErrorAction Stop
    $changed++
  } catch { $failed++ }
}
$did += "device power saving: disabled on $changed device(s)$(if ($failed) { ", $failed refused" })"

Write-Output ($did -join '; ')
exit 0
