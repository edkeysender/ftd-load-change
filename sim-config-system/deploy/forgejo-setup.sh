#!/bin/bash
# Stage 4 — stand up Forgejo on the Pi and connect the coordinator to it.
#
# Brings up Forgejo headless (no web wizard), creates an admin user + access
# token + the sim-config repo, then points the coordinator's working clone at it
# (push) and sets SIM_GIT_REMOTE (anonymous read, for agents). Idempotent.
#
# Usage:  sudo bash deploy/forgejo-setup.sh
#         sudo PI_HOST=70.84.68.196 bash deploy/forgejo-setup.sh   # force the IP agents will use
#
# The repo is created PUBLIC so agents clone read-only without credentials on the
# trusted sim LAN. The coordinator pushes via an authenticated origin (token).
set -e

if [ "$EUID" -ne 0 ]; then echo "ERROR: run as root: sudo bash $0"; exit 1; fi

SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE=/etc/sim-config.env
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }

PI_HOST="${PI_HOST:-$(hostname -I | awk '{print $1}')}"
FORGEJO_DATA="${FORGEJO_DATA:-$([ -d /mnt/ssd ] && echo /mnt/ssd/forgejo || echo /var/lib/forgejo)}"
FORGEJO_USER="${FORGEJO_USER:-sim}"
FORGEJO_PASS="${FORGEJO_PASS:-$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 20)}"
WORK_CLONE="${SIM_WORK_CLONE:-/var/lib/sim-config/work}"
REPO_NAME=sim-config
CT=forgejo
INI=/data/gitea/conf/app.ini

echo "=== Forgejo bring-up ==="
echo "Pi host (agents reach here): $PI_HOST"
echo "Data dir:                    $FORGEJO_DATA"
echo "Admin user:                  $FORGEJO_USER"
echo ""

set_env() {  # upsert KEY=VALUE in the coordinator env file
  local k="$1" v="$2"
  if grep -q "^$k=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^$k=.*|$k=$v|" "$ENV_FILE"
  else
    echo "$k=$v" >> "$ENV_FILE"
  fi
}

# 1. Docker (engine only; we use `docker run`, no compose needed) -------
if ! command -v docker >/dev/null 2>&1; then
  echo "[1/7] Installing Docker engine..."
  apt update && apt install -y docker.io
fi
# git-lfs on the Pi (the coordinator commits/pushes LFS blobs)
command -v git-lfs >/dev/null 2>&1 || { apt update && apt install -y git-lfs; }
git lfs install --system 2>/dev/null || true
systemctl enable --now docker 2>/dev/null || true
echo "[1/7] Docker: $(docker --version)"

# 2. Start Forgejo (plain docker run; data persists in the volume) -------
echo "[2/7] Starting Forgejo (data=$FORGEJO_DATA)..."
mkdir -p "$FORGEJO_DATA"
chown -R 1000:1000 "$FORGEJO_DATA"
docker rm -f "$CT" >/dev/null 2>&1 || true
docker run -d --name "$CT" --restart unless-stopped \
  -p 3000:3000 -p 2222:22 \
  -v "$FORGEJO_DATA:/data" \
  -e USER_UID=1000 -e USER_GID=1000 \
  -e FORGEJO__database__DB_TYPE=sqlite3 \
  -e FORGEJO__security__INSTALL_LOCK=true \
  -e FORGEJO__service__DISABLE_REGISTRATION=true \
  -e FORGEJO__service__REQUIRE_SIGNIN_VIEW=false \
  -e FORGEJO__security__LOGIN_REMEMBER_DAYS=3650 \
  -e FORGEJO__server__LFS_START_SERVER=true \
  -e FORGEJO__server__DOMAIN="$PI_HOST" \
  -e FORGEJO__server__HTTP_PORT=3000 \
  -e FORGEJO__server__ROOT_URL="http://$PI_HOST:3000/" \
  -e FORGEJO__server__SSH_PORT=2222 \
  codeberg.org/forgejo/forgejo:7

# 3. Wait until the API answers -----------------------------------------
echo -n "[3/7] Waiting for Forgejo"
ready=
for _ in $(seq 1 60); do
  if curl -fsS "http://localhost:3000/api/v1/version" >/dev/null 2>&1; then ready=1; break; fi
  echo -n "."; sleep 2
done
echo ""
[ -n "$ready" ] || { echo "Forgejo did not come up; check: docker logs $CT"; exit 1; }

# 4. Admin user (idempotent) --------------------------------------------
echo "[4/7] Ensuring admin user '$FORGEJO_USER'..."
if docker exec -u git "$CT" forgejo --config "$INI" admin user create \
      --admin --username "$FORGEJO_USER" --password "$FORGEJO_PASS" \
      --email "$FORGEJO_USER@sim.local" --must-change-password=false >/dev/null 2>&1; then
  echo "      created (password stored in $ENV_FILE)"
  set_env FORGEJO_PASS "$FORGEJO_PASS"
else
  echo "      already exists, keeping current password"
fi

# 5. Access token (generate once, then reuse from env) -------------------
if [ -z "${FORGEJO_TOKEN:-}" ]; then
  echo "[5/7] Generating access token..."
  FORGEJO_TOKEN=$(docker exec -u git "$CT" forgejo --config "$INI" admin user generate-access-token \
      --username "$FORGEJO_USER" --raw --scopes "write:repository,write:user" \
      --token-name coordinator 2>/dev/null | tr -d '[:space:]') || true
  if [ -z "$FORGEJO_TOKEN" ]; then
    echo "      ERROR: could not generate a token (one named 'coordinator' may already exist)."
    echo "      Delete it in the Forgejo UI (user > Settings > Applications) and re-run, or"
    echo "      set FORGEJO_TOKEN=<token> in $ENV_FILE manually."
    exit 1
  fi
  set_env FORGEJO_TOKEN "$FORGEJO_TOKEN"
else
  echo "[5/7] Reusing FORGEJO_TOKEN from $ENV_FILE"
fi

# 6. Create the repo (idempotent) ---------------------------------------
echo "[6/7] Ensuring repo $FORGEJO_USER/$REPO_NAME (public)..."
curl -fsS -X POST "http://localhost:3000/api/v1/user/repos" \
  -H "Authorization: token $FORGEJO_TOKEN" -H "Content-Type: application/json" \
  -d "{\"name\":\"$REPO_NAME\",\"private\":false,\"auto_init\":false}" >/dev/null 2>&1 \
  && echo "      created" || echo "      already exists (or create skipped)"

# 7. Wire the coordinator working clone + env ----------------------------
echo "[7/7] Connecting coordinator working clone..."
AUTH_REMOTE="http://$FORGEJO_USER:$FORGEJO_TOKEN@$PI_HOST:3000/$FORGEJO_USER/$REPO_NAME.git"
ANON_REMOTE="http://$PI_HOST:3000/$FORGEJO_USER/$REPO_NAME.git"
if [ -d "$WORK_CLONE/.git" ]; then
  git -C "$WORK_CLONE" remote remove origin 2>/dev/null || true
  git -C "$WORK_CLONE" remote add origin "$AUTH_REMOTE"
  git -C "$WORK_CLONE" push origin --all  2>&1 | sed 's/^/      /' || true
  git -C "$WORK_CLONE" push origin --tags 2>&1 | sed 's/^/      /' || true
else
  echo "      WARN: no working clone at $WORK_CLONE yet — run pi-setup.sh / seal first, then re-run."
fi
set_env SIM_GIT_REMOTE  "$ANON_REMOTE"
set_env SIM_FORGEJO_URL "http://$PI_HOST:3000/$FORGEJO_USER/$REPO_NAME"
set_env PI_HOST "$PI_HOST"
systemctl restart sim-coordinator 2>/dev/null || true

echo ""
echo "=== Forgejo ready ==="
echo "Web:           http://$PI_HOST:3000/   (login: $FORGEJO_USER, password in $ENV_FILE)"
echo "Repo:          http://$PI_HOST:3000/$FORGEJO_USER/$REPO_NAME"
echo "Agent git_remote (put in agent.json): $ANON_REMOTE"
echo ""
echo "Now rebuild/restart agents with that git_remote, then deploy from the UI or:"
echo "  curl -X POST http://localhost:8090/deploy"
