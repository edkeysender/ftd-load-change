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
# Runs on a separate port (8090) from the old mode-switcher (8089), so both can
# coexist while you trust the new path.
set -e

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: run as root:  sudo bash $0"
    exit 1
fi

# Resolve repo dir = parent of this deploy/ folder.
SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

PORT="${SIM_PORT:-8090}"
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
apt install -y python3 python3-venv python3-pip git git-lfs unzip curl
git lfs install --system 2>/dev/null || git lfs install || true   # enable LFS filters

echo "[2/5] Creating data dir + venv..."
mkdir -p "$DATA_DIR/work"
# Sensor assets for the `sensors` guard / HealthCheck CPU temperatures: the
# LibreHardwareMonitor DLLs and the PawnIO driver they read sensors through. Fetched
# rather than vendored (third-party binaries stay out of git); both non-fatal, as they
# need the internet and everything else works without them - re-run the scripts later.
SIM_INSTALLS_DIR="$DATA_DIR/installs" bash "$SCRIPT_DIR/fetch-lhm.sh" \
    || echo "  (LibreHardwareMonitor fetch failed - run deploy/fetch-lhm.sh later for CPU temps)"
SIM_INSTALLS_DIR="$DATA_DIR/installs" bash "$SCRIPT_DIR/fetch-pawnio.sh" \
    || echo "  (PawnIO fetch failed - run deploy/fetch-pawnio.sh later for CPU temps)"
if [ ! -d "$REPO_DIR/.venv" ]; then
    python3 -m venv "$REPO_DIR/.venv"
fi
# Per-device SSH keypairs for agentless Linux devices (coordinator/config.py SSH_DIR).
mkdir -p "$DATA_DIR/ssh"
chmod 700 "$DATA_DIR/ssh"

"$REPO_DIR/.venv/bin/pip" install --quiet --upgrade pip
# paramiko/cryptography ship aarch64 wheels, so this is normally wheel-only. On a 32-bit
# or unusual image pip may need to build them, which requires toolchain headers.
if ! "$REPO_DIR/.venv/bin/pip" install --quiet -r "$REPO_DIR/coordinator/requirements.txt"; then
    echo "    pip install failed - installing build dependencies and retrying..."
    apt install -y python3-dev libffi-dev build-essential
    "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/coordinator/requirements.txt"
fi

echo "[3/5] Writing env file ($ENV_FILE)..."
# Secrets are generated once and never rotated by re-running setup.
#   SIM_AGENT_TOKEN    - shared bearer token every agent sends
#   SIM_OPERATOR_TOKEN - guards the SSH surface (enrolling a Linux device, browsing it
#                        as root); the dashboard prompts for it once
#   SIM_SECRET_KEY     - Fernet key encrypting a remembered root password at rest
gen_token() { head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32; }
gen_fernet_key() { head -c 32 /dev/urandom | base64 | tr '+/' '-_'; }

# Append a key to the env file only if it isn't set yet, so an existing install picks up
# newly-introduced settings without losing its current ones.
ensure_env() {   # ensure_env KEY VALUE COMMENT
    if grep -q "^$1=" "$ENV_FILE" 2>/dev/null; then
        return 1
    fi
    { [ -n "$3" ] && echo "# $3"; echo "$1=$2"; } >> "$ENV_FILE"
    return 0
}

if [ ! -f "$ENV_FILE" ]; then
    TOKEN=$(gen_token)
    cat > "$ENV_FILE" <<EOF
# Coordinator environment. Edit and 'systemctl restart sim-coordinator' to apply.
SIM_PORT=$PORT
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
    echo "    $ENV_FILE already exists, keeping its current settings."
fi

OP_TOKEN=$(gen_token)
# Note the if-form: ensure_env returns 1 when the key is already present, and under
# `set -e` a bare `ensure_env ... && echo ...` would abort the script on every re-run.
if ensure_env SIM_OPERATOR_TOKEN "$OP_TOKEN" \
    "Operator token for the SSH device surface (dashboard asks for it once)."; then
    echo "    Generated operator token (also stored in $ENV_FILE):"
    echo "      $OP_TOKEN"
fi
if ensure_env SIM_SECRET_KEY "$(gen_fernet_key)" \
    "Encrypts stored SSH passwords. Changing it makes existing ones unreadable."; then
    echo "    Generated secret key for encrypting stored SSH passwords."
fi
chmod 600 "$ENV_FILE"

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
# Keep retrying through a transient fault (e.g. a filesystem that briefly
# remounts read-only under heavy I/O). Without this, 5 fast crashes trip
# systemd's default start-limit and the service stays dead until a manual
# restart - turning a momentary blip into a multi-hour outage.
RestartSec=5
StartLimitIntervalSec=0

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
