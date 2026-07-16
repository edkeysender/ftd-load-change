#!/usr/bin/env bash
# Download the PawnIO installer into the installs dir, so the `sensors` guard can put it
# on PCs and HealthCheck can read CPU temperatures.
#
# Why this is needed: LibreHardwareMonitor 0.9.x no longer ships WinRing0. It carries
# PawnIO *modules* (RyzenSMU, AMDFamily17, IntelMSR, ...) as embedded resources and reads
# CPU sensors through the PawnIO kernel driver, which is installed separately. Without it
# LHM enumerates the sensors and every value reads null.
#
# PawnIO is open source and the official build is Authenticode-signed by namazso.eu
# (verified: Status=Valid, GLOBALTRUST codesigning). Boards that expose an ACPI thermal
# zone never need any of this - the probe uses WMI there.
#
# Usage:  sudo bash deploy/fetch-pawnio.sh
set -euo pipefail
INSTALLS_DIR="${SIM_INSTALLS_DIR:-/srv/sim-config/installs}"
url='https://github.com/namazso/PawnIO.Setup/releases/latest/download/PawnIO_setup.exe'

mkdir -p "$INSTALLS_DIR"
echo "downloading PawnIO_setup.exe"
curl -fL "$url" -o "$INSTALLS_DIR/PawnIO_setup.exe"
chmod a+r "$INSTALLS_DIR/PawnIO_setup.exe"

echo "done -> $INSTALLS_DIR/PawnIO_setup.exe"
echo
echo "NOTE: this is a kernel driver. sensors-apply.ps1 installs it silently"
echo "      ('PawnIO_setup.exe -install -silent') only on PCs where it is not already"
echo "      present, and only when you Apply the Hardware sensors guard."
