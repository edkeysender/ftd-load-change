#!/bin/bash
# Wipe ALL coordinator state for a fresh, clean bootstrap of the real PCs.
#
# DESTRUCTIVE — removes the working clone + SQLite DB and recreates the Forgejo
# repo empty, so the next seal produces a clean real v1.0 (no test leftovers).
# The manifest is re-seeded from the repo copy on coordinator startup.
#
# Usage:  sudo bash deploy/reset-state.sh --yes
set -e

if [ "$EUID" -ne 0 ]; then echo "ERROR: run as root: sudo bash $0 --yes"; exit 1; fi
if [ "$1" != "--yes" ]; then
  echo "This DELETES the working clone, the DB, and the Forgejo repo contents."
  echo "Re-run to confirm:  sudo bash $0 --yes"
  exit 1
fi

ENV_FILE=/etc/sim-config.env
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }
WORK="${SIM_WORK_CLONE:-/var/lib/sim-config/work}"
DB="${SIM_DB:-/var/lib/sim-config/coordinator.db}"
PORT="${SIM_PORT:-8090}"
FORGEJO_USER="${FORGEJO_USER:-sim}"
PI_HOST="${PI_HOST:-$(hostname -I | awk '{print $1}')}"

echo "[1/5] Stopping coordinator..."
systemctl stop sim-coordinator 2>/dev/null || true

echo "[2/5] Wiping working clone + DB..."
rm -rf "$WORK" "$DB"

echo "[3/5] Recreating Forgejo repo (empty)..."
if [ -n "${FORGEJO_TOKEN:-}" ]; then
  curl -fsS -X DELETE "http://localhost:3000/api/v1/repos/$FORGEJO_USER/sim-config" \
    -H "Authorization: token $FORGEJO_TOKEN" >/dev/null 2>&1 || true
  curl -fsS -X POST "http://localhost:3000/api/v1/user/repos" \
    -H "Authorization: token $FORGEJO_TOKEN" -H 'Content-Type: application/json' \
    -d '{"name":"sim-config","private":false,"auto_init":false}' >/dev/null 2>&1 \
    && echo "      recreated" || echo "      (Forgejo not reachable / token missing — skipped)"
else
  echo "      no FORGEJO_TOKEN in $ENV_FILE — skipping Forgejo reset"
fi

echo "[4/5] Restarting coordinator (re-inits local repo + re-seeds manifest)..."
systemctl start sim-coordinator
printf "      waiting"
for _ in $(seq 1 40); do
  if curl -fsS "http://localhost:${PORT}/pcs" >/dev/null 2>&1; then break; fi
  printf "."; sleep 0.5
done
echo ""

echo "[5/5] Re-attaching origin remote to the fresh working clone..."
if [ -n "${FORGEJO_TOKEN:-}" ] && [ -d "$WORK/.git" ]; then
  AUTH_REMOTE="http://$FORGEJO_USER:$FORGEJO_TOKEN@$PI_HOST:3000/$FORGEJO_USER/sim-config.git"
  git -C "$WORK" remote remove origin 2>/dev/null || true
  git -C "$WORK" remote add origin "$AUTH_REMOTE"
  echo "      origin -> $PI_HOST:3000/$FORGEJO_USER/sim-config.git"
fi

echo ""
echo "=== Clean slate ready ==="
echo "Manifest now lists only the real PCs:"
curl -s "http://localhost:${PORT}/manifest/pcs" | python3 -m json.tool 2>/dev/null | grep '"ip"' || true
echo ""
echo "Next: run agents on the 3 real PCs, then size-report -> import -> seal."
