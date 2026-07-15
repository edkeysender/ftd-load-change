#!/usr/bin/env bash
# Build installs/lhm.zip - the LibreHardwareMonitor sensor libraries the `sensors`
# guard pushes to PCs, so HealthCheck can read CPU (and AMD/Intel GPU) temperatures.
# Re-run to refresh to the newest release.
#
# We take the .NET FRAMEWORK build (the plain LibreHardwareMonitor.zip asset), NOT the
# .NET 10 one: the agent loads the DLL via `Add-Type` in Windows PowerShell 5.1, which
# runs on .NET Framework 4.x and cannot load a .NET 10 assembly.
#
# We ship EVERY root-level DLL from the upstream archive, not just
# LibreHardwareMonitorLib.dll: it references HidSharp, System.Memory, BlackSharp.Core,
# DiskInfoToolkit, RAMSPDToolkit-NDD and System.Runtime.CompilerServices.Unsafe, and
# missing any of them fails at load with the unhelpful "Unable to load one or more of
# the requested types". Taking all of them also survives upstream adding a dependency.
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
[ -f "$tmp/x/LibreHardwareMonitorLib.dll" ] || {
  echo "LibreHardwareMonitorLib.dll missing from $TAG - asset layout changed?" >&2; exit 1; }

# Repack the DLLs alone (drop the GUI exe and localised resources). python3 rather than
# `zip` - the coordinator already requires python3, `zip` may not be installed.
mkdir -p "$INSTALLS_DIR"
python3 - "$tmp/x" "$INSTALLS_DIR/lhm.zip" <<'PY'
import glob, os, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
dlls = sorted(glob.glob(os.path.join(src, "*.dll")))   # root level only
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
    for f in dlls:
        z.write(f, os.path.basename(f))
print("  packed %d dll(s) -> %s" % (len(dlls), dst))
PY
chmod a+r "$INSTALLS_DIR/lhm.zip"

# The old two-file layout, if a previous version of this script left it behind.
rm -f "$INSTALLS_DIR/LibreHardwareMonitorLib.dll" "$INSTALLS_DIR/HidSharp.dll"

echo "done -> $INSTALLS_DIR/lhm.zip (from $TAG)"
echo
echo "NOTE: these only help if the PC lets the sensor driver load. Windows 11 with"
echo "      Memory Integrity (Core Isolation) ON blocks it, and the agent must run"
echo "      elevated. The HealthCheck note on each PC says which is in the way."
