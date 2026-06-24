#!/bin/bash
# One-line installer for the Sim Config Coordinator on the Raspberry Pi.
#
#   curl -fsSL https://raw.githubusercontent.com/edkeysender/ftd-load-change/main/sim-config-system/deploy/pi-install.sh | sudo bash
#
# Clones/updates the repo into /opt/ftd-load-change, then runs pi-setup.sh which
# installs the coordinator as the 'sim-coordinator' systemd service (port 8090,
# local-init mode — no Forgejo required to start). Idempotent; safe to re-run.
set -e

REPO_URL="${FTD_REPO_URL:-https://github.com/edkeysender/ftd-load-change.git}"
INSTALL_DIR="${FTD_INSTALL_DIR:-/opt/ftd-load-change}"
BRANCH="${FTD_BRANCH:-main}"

echo "=== Sim Config Coordinator bootstrap ==="
echo "Repo:   $REPO_URL"
echo "Target: $INSTALL_DIR"
echo ""

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: run as root:  curl -fsSL <url> | sudo bash"
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    echo "[bootstrap] Installing git..."
    apt update
    apt install -y git
fi

if [ -d "$INSTALL_DIR/.git" ]; then
    echo "[bootstrap] Updating existing checkout..."
    cd "$INSTALL_DIR"
    git fetch origin
    git reset --hard "origin/$BRANCH"
else
    echo "[bootstrap] Cloning repo..."
    rm -rf "$INSTALL_DIR"
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

echo ""
echo "[bootstrap] Running coordinator setup..."
echo ""
bash "$INSTALL_DIR/sim-config-system/deploy/pi-setup.sh"
