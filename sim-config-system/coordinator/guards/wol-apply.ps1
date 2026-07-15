# Enable Wake-on-LAN on the NIC that carries this PC's coordinator-facing IP, so the
# Wake on LAN action in PC status can power it back on. Needs admin. No reboot.
# Wake on *magic packet only*: pattern wake is left off, or broadcast chatter on the
# sim LAN would keep waking the PC by itself. Keep in step with wol-check.ps1.
#
# Reminder: WoL also has to be enabled in BIOS/UEFI ("Power On by PCIe/PME" or
# similar). This script cannot set that - do it once by hand per PC.
$ErrorActionPreference = 'SilentlyContinue'

$ip = $env:SIM_PC_IP
if (-not $ip) { Write-Output 'SIM_PC_IP not set - cannot tell which NIC to configure (agent too old?)'; exit 1 }
$idx = (Get-NetIPAddress -IPAddress $ip -ErrorAction SilentlyContinue).InterfaceIndex
if (-not $idx) { Write-Output "no network adapter on this PC holds $ip"; exit 1 }
$ad = Get-NetAdapter -InterfaceIndex $idx -ErrorAction SilentlyContinue
if (-not $ad) { Write-Output "no adapter for interface index $idx"; exit 1 }

$did = @()
try {
  # Magic packet on; pattern wake off so LAN broadcast chatter can't wake the PC by
  # itself. We deliberately do NOT untick "allow the computer to turn off this device":
  # Windows treats wake as a sub-option of it, so clearing the parent stops wake being
  # armed - the usual reason WoL never works from a powered-off PC. The power guard
  # exempts this NIC to match.
  Set-NetAdapterPowerManagement -Name $ad.Name -WakeOnMagicPacket Enabled -WakeOnPattern Disabled `
    -ErrorAction Stop
  $did += 'magic packet on, pattern wake off'
} catch {
  Write-Output "$($ad.Name): $($_.Exception.Message)"
  exit 1
}

# Undo a previous power-apply (or hand tweak) that unticked the parent - wake cannot be
# armed while it is off. 'Unsupported' drivers just ignore this.
if ((Get-NetAdapterPowerManagement -Name $ad.Name -ErrorAction SilentlyContinue).AllowComputerToTurnOffDevice -eq 'Disabled') {
  Set-NetAdapterPowerManagement -Name $ad.Name -AllowComputerToTurnOffDevice Enabled -ErrorAction SilentlyContinue
  $did += 're-armed device power-down (required for wake)'
}

# Some drivers only honour their own advanced keyword; set it too when present.
foreach ($kw in @('*WakeOnMagicPacket', '*WakeOnPattern')) {
  $p = Get-NetAdapterAdvancedProperty -Name $ad.Name -RegistryKeyword $kw -ErrorAction SilentlyContinue
  if ($p) {
    $v = if ($kw -eq '*WakeOnMagicPacket') { 1 } else { 0 }
    Set-NetAdapterAdvancedProperty -Name $ad.Name -RegistryKeyword $kw -RegistryValue $v -ErrorAction SilentlyContinue
    $did += "$kw=$v"
  }
}

# Verify rather than trust: re-read what the driver actually accepted.
$pm = Get-NetAdapterPowerManagement -Name $ad.Name -ErrorAction SilentlyContinue
if ($pm -and $pm.WakeOnMagicPacket -ne 'Enabled') {
  Write-Output "$($ad.Name): driver did not accept Wake on Magic Packet (still $($pm.WakeOnMagicPacket))"
  exit 1
}
Write-Output ("$($ad.Name) ($($ad.MacAddress)) on $ip - " + ($did -join '; ') + '. Check BIOS/UEFI wake-on-LAN too.')
exit 0
