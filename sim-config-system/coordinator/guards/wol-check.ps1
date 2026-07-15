# PASS (exit 0) if the NIC carrying this PC's coordinator-facing IP can be woken by
# a magic packet - i.e. the "Wake on LAN" action in PC status will actually work:
#   - Wake on Magic Packet: enabled
#   - "Allow the computer to turn off this device to save power": NOT unticked.
#     Counter-intuitive, but Windows treats wake as a sub-option of it: clear the parent
#     and wake is never armed, which is the classic reason WoL fails from a powered-off
#     PC. The power guard exempts this NIC for the same reason.
# NOTE: Windows is only half of it. WoL must also be enabled in the PC's BIOS/UEFI
# (often "Power On by PCIe/PME"); nothing here can see or set that.
# Keep in step with wol-apply.ps1.
$ErrorActionPreference = 'SilentlyContinue'

# The NIC that owns the IP the coordinator knows us by - a sim PC often has several
# (spare ports, virtual switches), and waking the wrong one wakes nothing.
$ip = $env:SIM_PC_IP
if (-not $ip) { Write-Output 'SIM_PC_IP not set - cannot tell which NIC to check (agent too old?)'; exit 1 }
$idx = (Get-NetIPAddress -IPAddress $ip -ErrorAction SilentlyContinue).InterfaceIndex
if (-not $idx) { Write-Output "no network adapter on this PC holds $ip"; exit 1 }
$ad = Get-NetAdapter -InterfaceIndex $idx -ErrorAction SilentlyContinue
if (-not $ad) { Write-Output "no adapter for interface index $idx"; exit 1 }

$pm = Get-NetAdapterPowerManagement -Name $ad.Name -ErrorAction SilentlyContinue
if (-not $pm) { Write-Output "$($ad.Name): driver exposes no power management - cannot verify WoL"; exit 1 }

$bad = @()
if ($pm.WakeOnMagicPacket -ne 'Enabled') { $bad += "Wake on Magic Packet is $($pm.WakeOnMagicPacket)" }
# 'Unsupported' is fine (the driver exposes no such knob); only an explicit Disabled is a
# problem, because then Windows never arms wake on this NIC.
if ($pm.AllowComputerToTurnOffDevice -eq 'Disabled') {
  $bad += 'device power-down is unticked, so Windows will not arm wake on this NIC'
}

if ($bad.Count -eq 0) {
  Write-Output "$($ad.Name) ($($ad.MacAddress)) on $ip - wake on magic packet enabled"
  exit 0
}
Write-Output ("$($ad.Name) on $ip - " + ($bad -join '; '))
exit 1
