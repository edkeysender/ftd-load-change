#!/bin/bash
# FTD Mode Switcher — bootstrap installer
# One-line install:
#   curl -fsSL https://raw.githubusercontent.com/edkeysender/ftd-load-change/main/install.sh | sudo bash
set -e

REPO_URL="${FTD_REPO_URL:-https://github.com/edkeysender/ftd-load-change.git}"
INSTALL_DIR="${FTD_INSTALL_DIR:-/opt/ftd-load-change}"
BRANCH="${FTD_BRANCH:-main}"

echo "=== FTD Mode Switcher bootstrap ==="
echo "Repo:   $REPO_URL"
echo "Target: $INSTALL_DIR"
echo ""

# Must run as root (we need to write to /opt, /etc/systemd, etc.)
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: This installer must run as root. Re-run with:"
    echo "  curl -fsSL <url> | sudo bash"
    exit 1
fi

# Install git first if it's not there yet (chicken/egg — we need it to clone)
if ! command -v git >/dev/null 2>&1; then
    echo "[bootstrap] Installing git..."
    apt update
    apt install -y git
fi

# Clone or update the repo
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

# Hand off to the real setup script
echo ""
echo "[bootstrap] Running RPi setup..."
echo ""
cd "$INSTALL_DIR/rpi"
bash setup.sh
