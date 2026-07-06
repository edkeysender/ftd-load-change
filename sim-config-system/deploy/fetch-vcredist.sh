#!/usr/bin/env bash
# Download the latest VC++ 2015-2022 redistributables into the installs dir so the
# vcredist guard can push them to PCs. aka.ms/vs/17/release always points at the
# current release. Re-run to refresh to the newest.
#
# Usage:  sudo bash deploy/fetch-vcredist.sh
set -euo pipefail
INSTALLS_DIR="${SIM_INSTALLS_DIR:-/srv/sim-config/installs}"
mkdir -p "$INSTALLS_DIR"
echo "downloading vc_redist.x64.exe"
curl -fL 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -o "$INSTALLS_DIR/vc_redist.x64.exe"
echo "downloading vc_redist.x86.exe"
curl -fL 'https://aka.ms/vs/17/release/vc_redist.x86.exe' -o "$INSTALLS_DIR/vc_redist.x86.exe"
chmod a+r "$INSTALLS_DIR"/vc_redist.x*.exe
echo "done -> $INSTALLS_DIR (vc_redist.x64.exe, vc_redist.x86.exe)"
