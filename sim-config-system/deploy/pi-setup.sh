#!/bin/bash
# Coordinator setup for the Raspberry Pi.
#
# Installs the FastAPI coordinator (the ONLY git writer) as a systemd service.
# Runs in LOCAL-INIT mode by default: no Forgejo required — the coordinator does
# `git init` on a local working clone so you can exercise import -> seal before
# wiring Forgejo. Add Forgejo later by setting SIM_GIT_REMOTE in /etc/sim-config.env.
#
# Usage (standalone):   sudo bash deploy/pi-setup.sh
# Or via the repo:      sudo bash /opt/ftd-load-change/sim-config-system/deploy/pi-setup.sh
#
# Runs on a separate port (8080) from the old mode-switcher (8089), so both can
# coexist while you trust the new path.
set -e

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: run as root:  sudo bash $0"
    exit 1
fi

# Resolve repo dir = parent of this deploy/ folder.
SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

PORT="${SIM_PORT:-8080}"
DATA_DIR="${SIM_DATA_DIR:-/var/lib/sim-config}"      # SSD recommended for real use
ENV_FILE=/etc/sim-config.env
SERVICE=/etc/systemd/system/sim-coordinator.service

echo "=== Sim Config Coordinator setup ==="
echo "Repo:    $REPO_DIR"
echo "Data:    $DATA_DIR"
echo "Port:    $PORT"
echo ""

echo "[1/5] Installing packages..."
apt update
apt install -y python3 python3-venv python3-pip git

echo "[2/5] Creating data dir + venv..."
mkdir -p "$DATA_DIR/work"
if [ ! -d "$REPO_DIR/.venv" ]; then
    python3 -m venv "$REPO_DIR/.venv"
fi
"$REPO_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$REPO_DIR/.venv/bin/pip" install --quiet -r "$REPO_DIR/coordinator/requirements.txt"

echo "[3/5] Writing env file ($ENV_FILE)..."
if [ ! -f "$ENV_FILE" ]; then
    TOKEN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
    cat > "$ENV_FILE" <<EOF
# Coordinator environment. Edit and 'systemctl restart sim-coordinator' to apply.
SIM_WORK_CLONE=$DATA_DIR/work
SIM_DB=$DATA_DIR/coordinator.db
# LOCAL-INIT mode: empty remote => coordinator does 'git init' (no Forgejo needed).
# To use Forgejo later, set this to its repo URL, e.g.
#   SIM_GIT_REMOTE=http://localhost:3000/sim/sim-config.git
SIM_GIT_REMOTE=
# Shared bearer token agents must send (Authorization: Bearer <token>).
SIM_AGENT_TOKEN=$TOKEN
EOF
    chmod 600 "$ENV_FILE"
    echo "    Generated agent token (also stored in $ENV_FILE):"
    echo "      $TOKEN"
else
    echo "    $ENV_FILE already exists, leaving it as-is."
fi

echo "[4/5] Installing systemd service ($SERVICE)..."
cat > "$SERVICE" <<EOF
[Unit]
Description=Sim Config Coordinator (git writer + REST API)
After=network.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$REPO_DIR/.venv/bin/uvicorn coordinator.main:app --host 0.0.0.0 --port $PORT
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable sim-coordinator.service
systemctl restart sim-coordinator.service

echo "[5/5] Done."
IP=$(hostname -I | awk '{print $1}')
echo ""
echo "=== Coordinator running ==="
echo "API:        http://${IP}:${PORT}/pcs"
echo "Bootstrap:  http://${IP}:${PORT}/bootstrap"
echo ""
echo "Smoke-test the full Phase-1 flow on the Pi alone:"
echo "  sudo bash $SCRIPT_DIR/smoke-test.sh"
echo ""
systemctl status sim-coordinator.service --no-pager | head -6
