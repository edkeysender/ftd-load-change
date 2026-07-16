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

# Bake the coordinator URL + token into the binary so the agent needs no config
# file: drop the exe on a PC, run it, and it auto-detects its identity via /whoami.
ENV_FILE=/etc/sim-config.env
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }
PI_HOST="${PI_HOST:-$(hostname -I | awk '{print $1}')}"
COORD="http://${PI_HOST}:${SIM_PORT:-8090}"
TOKEN="${SIM_AGENT_TOKEN:-}"

echo "[build] Cross-compiling -> windows/amd64 (version $VER, coordinator $COORD) ..."
cd "$AGENT_DIR"
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath \
  -ldflags "-X main.Version=$VER -X main.DefaultCoordinator=$COORD -X main.DefaultToken=$TOKEN" \
  -o "$DIST/simagent.exe" .

echo ""
echo "Built: $DIST/simagent.exe"
ls -la "$DIST/simagent.exe"
echo ""
WORK_CLONE="${SIM_WORK_CLONE:-/srv/sim-config/work}"
echo "Next steps (see deploy/AGENT.md):"
echo " 1. Copy ONLY $DIST/simagent.exe to the Windows PC — no config file needed."
echo "    The coordinator URL ($COORD) and token are baked in; the agent auto-detects"
echo "    its identity (pc_ip + folder) from the coordinator via /whoami."
echo "    (Create agent.json from agent/agent.example.json only to override a default,"
echo "     e.g. \"enforce_on_start\": false.)"
echo " 2. Run it. For CPU temperatures it must run ELEVATED — install it as a scheduled"
echo "    task with highest privileges (deploy/AGENT.md). For a quick test:  .\\simagent.exe"
echo " 3. It registers on its next heartbeat (~10s) and shows up in the dashboard's"
echo "    Fleet tab as UNSEEDED. Update a running fleet later with 'Update all agents'."
echo " 4. Import / deploy from the dashboard (Load Configuration). First read-only import"
echo "    can also be triggered by:  curl -X POST http://localhost:${PORT}/import/<pc_ip>"
echo "    then verify the staged tree:  sudo git -C ${WORK_CLONE} status"
