#!/usr/bin/env bash
# Download the LibreHardwareMonitor sensor DLLs into the installs dir so the `sensors`
# guard can push them to PCs and HealthCheck can read CPU (and AMD/Intel GPU)
# temperatures. Re-run to refresh to the newest release.
#
# We take the .NET FRAMEWORK build (the plain LibreHardwareMonitor.zip asset), NOT the
# .NET 10 one: the agent loads the DLL via `Add-Type` in Windows PowerShell 5.1, which
# runs on .NET Framework 4.x and cannot load a .NET 10 assembly.
#
# Usage:  sudo bash deploy/fetch-lhm.sh
set -euo pipefail
INSTALLS_DIR="${SIM_INSTALLS_DIR:-/srv/sim-config/installs}"
TAG="${LHM_TAG:-}"   # pin a release, e.g. LHM_TAG=v0.9.6; default = latest

api='https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases'
if [ -z "$TAG" ]; then
  TAG=$(curl -fsSL "$api/latest" | grep -m1 '"tag_name"' | cut -d'"' -f4)
fi
[ -n "$TAG" ] || { echo "could not resolve a LibreHardwareMonitor release tag" >&2; exit 1; }

url="https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/$TAG/LibreHardwareMonitor.zip"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "downloading LibreHardwareMonitor $TAG (.NET Framework build)"
curl -fL "$url" -o "$tmp/lhm.zip"
unzip -o -q "$tmp/lhm.zip" -d "$tmp/x"

# LibreHardwareMonitorLib.dll reads the sensors; HidSharp.dll is its USB/HID dependency.
mkdir -p "$INSTALLS_DIR"
for f in LibreHardwareMonitorLib.dll HidSharp.dll; do
  [ -f "$tmp/x/$f" ] || { echo "$f missing from $TAG - asset layout changed?" >&2; exit 1; }
  cp "$tmp/x/$f" "$INSTALLS_DIR/$f"
done
chmod a+r "$INSTALLS_DIR"/LibreHardwareMonitorLib.dll "$INSTALLS_DIR"/HidSharp.dll

echo "done -> $INSTALLS_DIR (LibreHardwareMonitorLib.dll, HidSharp.dll) from $TAG"
echo
echo "NOTE: these only help if the PC lets the sensor driver load. Windows 11 with"
echo "      Memory Integrity (Core Isolation) ON blocks it, and temperatures stay"
echo "      empty - the HealthCheck note on each PC will say so."
