#!/bin/bash
# Scan the sim LAN for computers and print them (with in_manifest / has_agent
# flags). Optionally pass a CIDR; otherwise a /24 is derived from the manifest.
#
# Usage:  bash deploy/discover.sh                 # auto /24
#         bash deploy/discover.sh 70.84.68.0/24   # explicit
#
# Add/remove a host to the managed list:
#   curl -s -X POST $BASE/discover/add    -H 'Content-Type: application/json' -d '{"ip":"70.84.68.20","note":"spare"}'
#   curl -s -X POST $BASE/discover/remove -H 'Content-Type: application/json' -d '{"ip":"70.84.68.20"}'
#   curl -s $BASE/hosts          # the curated list
set -e

ENV_FILE="${SIM_ENV_FILE:-/etc/sim-config.env}"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
BASE="${SIM_BASE:-http://localhost:${SIM_PORT:-8090}}"

CIDR="${1:-}"
body='{}'
[ -n "$CIDR" ] && body="{\"cidr\":\"$CIDR\"}"

echo "Scanning via $BASE/discover (cidr=${CIDR:-auto}) — takes a few seconds..."
curl -s -X POST "$BASE/discover" -H 'Content-Type: application/json' -d "$body" | python3 -m json.tool
