#!/bin/bash
# FTD Mode Switcher - RPi setup script
# Usually invoked by ../install.sh, but can also be run standalone:
#   sudo bash setup.sh
set -e

echo "=== FTD Mode Switcher setup ==="

# Resolve our own location so we can find app.py
SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)

# 1. Install dependencies
echo "[1/6] Installing packages..."
apt update
apt install -y git python3 python3-pip python3-venv

# 2. Create ftd user (separate from pi, owns the repo + app)
echo "[2/6] Creating 'ftd' user..."
if ! id -u ftd >/dev/null 2>&1; then
    useradd -m -s /bin/bash ftd
fi

# 3. Set up directories
echo "[3/6] Creating directories..."
sudo -u ftd mkdir -p /home/ftd/repos /home/ftd/app
REPO=/home/ftd/repos/ftd.git

# 4. Initialize bare repo with both branches and seed files
echo "[4/6] Initializing git repo with seed files..."
if [ ! -d "$REPO" ]; then
    sudo -u ftd git init --bare "$REPO"

    # Use a temporary working dir to seed initial content
    TMPDIR=$(mktemp -d)
    chown ftd:ftd "$TMPDIR"
    sudo -u ftd git clone "$REPO" "$TMPDIR/seed"
    cd "$TMPDIR/seed"
    sudo -u ftd git config user.email "ftd@localhost"
    sudo -u ftd git config user.name "FTD System"

    # Seed file for training branch
    echo "This file confirms TRAINING mode is deployed." | sudo -u ftd tee MODE_TRAINING.txt > /dev/null
    sudo -u ftd git add MODE_TRAINING.txt
    sudo -u ftd git commit -m "Initial training seed"
    sudo -u ftd git branch -M training
    sudo -u ftd git push -u origin training

    # Seed file for development branch
    sudo -u ftd git rm MODE_TRAINING.txt
    echo "This file confirms DEVELOPMENT mode is deployed." | sudo -u ftd tee MODE_DEVELOPMENT.txt > /dev/null
    sudo -u ftd git add MODE_DEVELOPMENT.txt
    sudo -u ftd git commit -m "Initial development seed"
    sudo -u ftd git checkout -b development
    sudo -u ftd git push -u origin development

    # Set HEAD of bare repo to training (default branch)
    sudo -u ftd git --git-dir="$REPO" symbolic-ref HEAD refs/heads/training

    cd /
    rm -rf "$TMPDIR"
    echo "    Repo seeded with training + development branches."
else
    echo "    Repo already exists, skipping init."
fi

# 5. Initialize state file
echo "[5/6] Creating state file..."
STATE=/home/ftd/state.json
if [ ! -f "$STATE" ]; then
    echo '{"current_mode": "training"}' | sudo -u ftd tee "$STATE" > /dev/null
fi

# 6. Install Python app
echo "[6/6] Installing Python app + systemd service..."
if [ ! -d /home/ftd/app/venv ]; then
    sudo -u ftd python3 -m venv /home/ftd/app/venv
fi
sudo -u ftd /home/ftd/app/venv/bin/pip install --quiet --upgrade pip
sudo -u ftd /home/ftd/app/venv/bin/pip install --quiet fastapi uvicorn

# Copy app.py from the same folder as this script
if [ ! -f "$SCRIPT_DIR/app.py" ]; then
    echo "ERROR: app.py not found at $SCRIPT_DIR/app.py"
    exit 1
fi
cp "$SCRIPT_DIR/app.py" /home/ftd/app/app.py
chown ftd:ftd /home/ftd/app/app.py

# Install systemd service
cat > /etc/systemd/system/ftd-mode.service <<'EOF'
[Unit]
Description=FTD Mode Switcher API
After=network.target

[Service]
Type=simple
User=ftd
WorkingDirectory=/home/ftd/app
ExecStart=/home/ftd/app/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ftd-mode.service
systemctl restart ftd-mode.service

# Get IP for convenience
IP=$(hostname -I | awk '{print $1}')

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Web UI:  http://${IP}:8080"
echo "API:     http://${IP}:8080/api/state"
echo ""
echo "Test from another machine:"
echo "  curl http://${IP}:8080/api/state"
echo ""
echo "Service status:"
systemctl status ftd-mode.service --no-pager | head -5

echo ""
echo "Next: set up SSH keys for Windows clients. See README.md."
