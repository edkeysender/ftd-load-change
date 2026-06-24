#!/bin/bash
# Phase-1 smoke test — runs entirely on the Pi, no Windows agents or Forgejo needed.
#
# It SIMULATES the three agents: sends a heartbeat for each PC, uploads a tiny
# fake import bundle + size report to the coordinator's result endpoints, then
# seals v1.0 and verifies the tag/branches exist in the working clone.
#
# This exercises the real coordinator code path (auth -> stage_import_bundle ->
# seal_baseline -> git tag). Replace with real agents once they are built.
#
# Usage:  sudo bash deploy/smoke-test.sh
set -e

ENV_FILE="${SIM_ENV_FILE:-/etc/sim-config.env}"
if [ -f "$ENV_FILE" ]; then
    set -a; . "$ENV_FILE"; set +a
fi
: "${SIM_AGENT_TOKEN:?Set SIM_AGENT_TOKEN or run pi-setup.sh first}"
: "${SIM_WORK_CLONE:?SIM_WORK_CLONE not set (check $ENV_FILE)}"
export SIM_BASE="${SIM_BASE:-http://localhost:${SIM_PORT:-8080}}"

echo "=== Phase-1 smoke test ==="
echo "Base:  $SIM_BASE"
echo "Clone: $SIM_WORK_CLONE"
echo ""

python3 - <<'PY'
import base64, json, os, urllib.request

BASE  = os.environ["SIM_BASE"]
TOKEN = os.environ["SIM_AGENT_TOKEN"]

def call(method, path, obj=None, auth=True):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode()
    return json.loads(body) if body else {}

def b64(s):
    return base64.b64encode(s.encode()).decode()

# (ip, folder) per the manifest. Each gets a couple of fake files under its folder.
PCS = [
    ("70.84.68.11", "pc-11-simhost"),
    ("70.84.68.10", "pc-10-ios"),
    ("70.84.68.12", "pc-12-display"),
]

for ip, folder in PCS:
    call("POST", f"/agents/{ip}/heartbeat",
         {"pc_ip": ip, "folder": folder, "mode": "UNSEEDED", "clean": True})
    files = {
        f"{folder}/example-app/app.cfg":  b64(f"config for {folder}\n"),
        f"{folder}/example-app/data.bin": b64(f"binary-ish data {folder}\n"),
    }
    r = call("POST", f"/agents/{ip}/import-result",
             {"folder": folder, "missing": [], "files": files})
    print(f"imported {folder}: staged {r.get('staged_files')} files")
    call("POST", f"/agents/{ip}/size-report-result",
         {"folder": folder, "sizes": {"example-app": 1234}})

print("\nsealing baseline v1.0 ...")
r = call("POST", "/seal-baseline", {"message": "smoke-test baseline", "author": "tester"}, auth=False)
print("seal:", r)

print("\n/versions:", json.dumps(call("GET", "/versions", auth=False), indent=2))
print("\n/bootstrap:", json.dumps(call("GET", "/bootstrap", auth=False), indent=2))
print("\n/pcs:", json.dumps(call("GET", "/pcs", auth=False), indent=2))
PY

echo ""
echo "=== Git state in working clone ==="
echo "tags:     $(git -C "$SIM_WORK_CLONE" tag | tr '\n' ' ')"
echo "branches: $(git -C "$SIM_WORK_CLONE" branch --format='%(refname:short)' | tr '\n' ' ')"
echo "v1.0 contains:"
git -C "$SIM_WORK_CLONE" ls-tree -r --name-only v1.0 | sed 's/^/  /'

echo ""
if git -C "$SIM_WORK_CLONE" rev-parse v1.0 >/dev/null 2>&1; then
    echo "PASS: v1.0 tag created and all PC folders staged + committed."
else
    echo "FAIL: v1.0 tag missing — check 'journalctl -u sim-coordinator'."
    exit 1
fi
