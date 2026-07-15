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
  Set-NetAdapterPowerManagement -Name $ad.Name -WakeOnMagicPacket Enabled -WakeOnPattern Disabled `
    -AllowComputerToTurnOffDevice Disabled -ErrorAction Stop
  $did += 'magic packet on, pattern wake off, NIC power-down off'
} catch {
  Write-Output "$($ad.Name): $($_.Exception.Message)"
  exit 1
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
