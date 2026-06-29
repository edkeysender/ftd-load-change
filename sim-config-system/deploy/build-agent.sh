#!/bin/bash
# Cross-compile the Windows agent (simagent.exe) on the Raspberry Pi.
# The agent is pure-stdlib Go, so this needs no network and produces one static
# .exe with no DLLs to ship.
#
# Usage:  bash deploy/build-agent.sh
set -e

SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
AGENT_DIR="$REPO_DIR/agent"
DIST="$REPO_DIR/dist"
PORT="${SIM_PORT:-8090}"

if ! command -v go >/dev/null 2>&1; then
    echo "[build] Go not found; installing golang-go (needs sudo)..."
    sudo apt update
    sudo apt install -y golang-go
fi
echo "[build] $(go version)"

mkdir -p "$DIST"
VER=$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo dev)
echo "[build] Cross-compiling -> windows/amd64 (version $VER) ..."
cd "$AGENT_DIR"
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath \
  -ldflags "-X main.Version=$VER" -o "$DIST/simagent.exe" .

echo ""
echo "Built: $DIST/simagent.exe"
ls -la "$DIST/simagent.exe"
echo ""
echo "Next steps (see deploy/AGENT.md):"
echo " 1. Copy $DIST/simagent.exe and agent/agent.example.json to the Windows PC."
echo " 2. Rename the json to agent.json (same folder as the exe); set pc_ip, folder,"
echo "    and token (from /etc/sim-config.env on this Pi)."
echo " 3. On the PC, run in a console:   .\\simagent.exe"
echo " 4. On the Pi, trigger a read-only import:"
echo "      curl -X POST http://localhost:${PORT}/import/<pc_ip>"
echo " 5. Verify staged tree:"
echo "      sudo git -C /var/lib/sim-config/work status"
