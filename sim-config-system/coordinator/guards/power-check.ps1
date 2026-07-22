# PASS (exit 0) if this PC is set to full performance and can never nod off:
#   - active power plan is High performance (or Ultimate)
#   - sleep / hibernate / monitor / disk idle timeouts are all 0 (= never), AC and DC
#   - no NETWORK, USB or INPUT device may be powered down to save power
# Keep in step with power-apply.ps1.
#
# Why only those device classes: Windows ships with power management ON for nearly
# every device (37 of them on a typical box - Management Engine, GNA accelerator, I2C
# controllers...). Most cannot be turned off, don't matter to a sim, and some silently
# refuse the write - so demanding all of them makes a guard that can never pass.
# These three classes are the ones that actually break a session: a NIC that drops,
# a USB controller that suspends a yoke/throttle, an input device that stops reporting.
$ErrorActionPreference = 'SilentlyContinue'

$HIGH     = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'
$ULTIMATE = 'e9a42b02-d5df-448d-aa00-03f14749eb61'
# powercfg subgroup/setting GUIDs (stable across Windows versions).
$SUB_SLEEP = '238c9fa8-0aad-41ed-83f4-97be242c8f20'
$SUB_VIDEO = '7516b95f-f776-4464-8c53-06167f40cc99'
$SUB_DISK  = '0012ee47-9041-4b5d-9b77-535fba8b1442'
$STANDBY   = '29f6c1db-86da-48c5-9fdb-f2b67b1f44da'
$HIBERNATE = '9d7815a6-7ee4-497e-8888-515a05f02364'
$VIDEOIDLE = '3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e'
$DISKIDLE  = '6738e2c4-e8a5-4a42-b16a-e040e769756e'
# USB + input devices: MSPower_DeviceEnable is the real backing for these. NICs are
# checked separately through the netadapter API (see below) - MSPower disagrees for them.
$USB_CLASSES = @('USB', 'HIDClass', 'Mouse', 'Keyboard')

# Returns @(acIndex, dcIndex) for a setting on the active scheme, or @($null,$null).
function Get-Idx($sub, $setting) {
  $o = powercfg /query SCHEME_CURRENT $sub $setting 2>$null
  if (-not $o) { return @($null, $null) }
  $ac = ([regex]::Match(($o -join "`n"), 'Current AC Power Setting Index:\s*(0x[0-9a-fA-F]+)'))
  $dc = ([regex]::Match(($o -join "`n"), 'Current DC Power Setting Index:\s*(0x[0-9a-fA-F]+)'))
  return @(
    $(if ($ac.Success) { [Convert]::ToInt64($ac.Groups[1].Value, 16) } else { $null }),
    $(if ($dc.Success) { [Convert]::ToInt64($dc.Groups[1].Value, 16) } else { $null })
  )
}

$bad = @()

$active = (powercfg /getactivescheme) -join ' '
$m = [regex]::Match($active, 'GUID:\s*([0-9a-fA-F-]{36})')
$guid = if ($m.Success) { $m.Groups[1].Value.ToLower() } else { '' }
if ($guid -ne $HIGH -and $guid -ne $ULTIMATE) {
  $nm = [regex]::Match($active, '\(([^)]+)\)')
  $bad += "power plan is '$(if ($nm.Success) { $nm.Groups[1].Value } else { $guid })', not High/Ultimate performance"
}

foreach ($s in @(@{N='sleep'; S=$SUB_SLEEP; G=$STANDBY}, @{N='hibernate'; S=$SUB_SLEEP; G=$HIBERNATE},
                 @{N='monitor'; S=$SUB_VIDEO; G=$VIDEOIDLE}, @{N='disk'; S=$SUB_DISK; G=$DISKIDLE})) {
  $v = Get-Idx $s.S $s.G
  if ($null -eq $v[0]) { continue }              # setting not present on this box
  if ($v[0] -ne 0) { $bad += "$($s.N) idle timeout (AC) is $($v[0])s, not never" }
  if ($null -ne $v[1] -and $v[1] -ne 0) { $bad += "$($s.N) idle timeout (DC) is $($v[1])s, not never" }
}

# Class + name lookup once - a Win32_PnPEntity query per device would take ~40 round trips.
$cls = @{}; $nm = @{}
Get-CimInstance Win32_PnPEntity | ForEach-Object { $cls[$_.DeviceID] = $_.PNPClass; $nm[$_.DeviceID] = $_.Name }

# --- USB + input devices: MSPower_DeviceEnable.Enable is their real setting. ---
# Enable = $true means "allow the computer to turn off this device to save power".
$still = @(Get-CimInstance -Namespace root\wmi -ClassName MSPower_DeviceEnable |
           Where-Object { $_.Enable -and $cls[($_.InstanceName -replace '_\d+$','')] -in $USB_CLASSES })
if ($still.Count -gt 0) {
  # Name the devices, not their class - "USB, USB, USB" tells nobody what to go fix.
  $names = ($still | Select-Object -First 3 | ForEach-Object {
              $n = $nm[($_.InstanceName -replace '_\d+$','')]
              if ($n) { $n } else { $_.InstanceName } }) -join '; '
  $more = if ($still.Count -gt 3) { ", +$($still.Count - 3) more" } else { '' }
  $bad += "$($still.Count) USB/input device(s) may still be powered down ($names$more)"
}

# --- NICs: check the SAME setting Device Manager and Set-NetAdapterPowerManagement use. ---
# MSPower_DeviceEnable disagrees for network adapters - it reflects a different WMI view
# that lags the real checkbox, and it enumerates ghost "#3"-style instances left by past
# driver installs. So a NIC the operator (or power-apply) already disabled kept reading as
# powered-down. Trust AllowComputerToTurnOffDevice on real, present adapters instead;
# 'Unsupported' (no such knob) is fine, only 'Enabled' fails.
#
# Only enforce on adapters that are actually Up (carrying the link). A Disconnected or
# Disabled radio powering down cannot interrupt a session, and boxes with a multi-radio
# Wi-Fi card expose several idle instances that read 'Enabled' and can never be satisfied
# (e.g. a wired sim PC with an unused Wi-Fi 7 card showing three disconnected radios).
$hotNics = @()
foreach ($a in @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' })) {
  $pm = Get-NetAdapterPowerManagement -Name $a.Name -ErrorAction SilentlyContinue
  if ($pm -and $pm.AllowComputerToTurnOffDevice -eq 'Enabled') { $hotNics += $a.InterfaceDescription }
}
if ($hotNics.Count -gt 0) {
  $bad += "$($hotNics.Count) NIC(s) may still be powered down ($($hotNics -join '; '))"
}

if ($bad.Count -eq 0) {
  Write-Output 'max performance: plan OK, no idle timeouts, no network/USB/input device powers down'
  exit 0
}
Write-Output ($bad -join '; ')
exit 1
