#!/bin/bash
# Re-run after you push changes to GitHub. Pulls latest and re-runs setup.
set -e
INSTALL_DIR="${FTD_INSTALL_DIR:-/opt/ftd-load-change}"

if [ "$EUID" -ne 0 ]; then
    echo "Run with sudo."
    exit 1
fi

cd "$INSTALL_DIR"
git pull
bash rpi/setup.sh
