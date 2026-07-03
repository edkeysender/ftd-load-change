#!/usr/bin/env bash
# Prepare the portable-git install bundle that agents fetch (via the Installs panel)
# when a PC is missing git. Downloads PortableGit for Windows, extracts it here on
# the Pi with p7zip, and repackages it as a plain zip the Go agent can unzip. Also
# registers it in installs.json. Re-run to update the git version.
#
# Usage:  sudo bash deploy/prepare-git-bundle.sh
set -euo pipefail

INSTALLS_DIR="${SIM_INSTALLS_DIR:-/srv/sim-config/installs}"
GIT_VER="${GIT_VER:-2.47.0}"                    # override: GIT_VER=2.48.0 bash prepare-git-bundle.sh
PG_TAG="${PG_TAG:-v${GIT_VER}.windows.1}"
PG_FILE="PortableGit-${GIT_VER}-64-bit.7z.exe"
PG_URL="https://github.com/git-for-windows/git/releases/download/${PG_TAG}/${PG_FILE}"

need() { command -v "$1" >/dev/null 2>&1; }
if ! need 7z; then echo "installing p7zip-full…"; apt-get update && apt-get install -y p7zip-full; fi
if ! need zip; then apt-get install -y zip; fi

mkdir -p "$INSTALLS_DIR"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

echo "downloading $PG_URL"
curl -fL "$PG_URL" -o "$tmp/$PG_FILE"

echo "extracting (p7zip reads the Windows SFX)…"
7z x -y -o"$tmp/pg" "$tmp/$PG_FILE" >/dev/null

echo "zipping bundle -> $INSTALLS_DIR/git-bundle.zip"
( cd "$tmp/pg" && zip -qr "$tmp/git-bundle.zip" . )
mv -f "$tmp/git-bundle.zip" "$INSTALLS_DIR/git-bundle.zip"

# Register (or refresh) the git entry in installs.json.
python3 - "$INSTALLS_DIR" <<'PY'
import json, os, sys
d = sys.argv[1]; f = os.path.join(d, "installs.json")
items = []
if os.path.exists(f):
    try: items = json.load(open(f))
    except Exception: items = []
items = [x for x in items if x.get("id") != "git"]
items.insert(0, {
    "id": "git",
    "name": "Git for Windows (portable)",
    "desc": "Required for deploy; includes git-lfs. Unzips to C:/sim-agent/git — no admin needed.",
    "file": "git-bundle.zip",
    "type": "unzip",
    "target": "C:/sim-agent/git",
})
json.dump(items, open(f, "w"), indent=2)
print("registered git in", f)
PY

chmod -R a+rX "$INSTALLS_DIR"
echo "done. Push it from the dashboard: Configuration -> Installs -> 'install on <pc>'."
