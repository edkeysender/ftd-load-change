"""Coordinator configuration. Override via environment variables."""
import os
from pathlib import Path

# Writable working clone of the monorepo. The coordinator is the ONLY git writer.
WORK_CLONE = Path(os.environ.get("SIM_WORK_CLONE", "/srv/sim-config/work"))

# Bare repo / Forgejo remote the coordinator pushes to.
GIT_REMOTE = os.environ.get("SIM_GIT_REMOTE", "http://localhost:3000/sim/sim-config.git")

# Forgejo base URL, used to build deep links for diffs/history in the UI.
FORGEJO_URL = os.environ.get("SIM_FORGEJO_URL", "http://localhost:3000/sim/sim-config")

# Manifest path the coordinator reads (lives at the working-clone root once seeded).
MANIFEST_PATH = Path(os.environ.get("SIM_MANIFEST", str(WORK_CLONE / "manifest.yaml")))

# Seed files copied into the working clone on first run so v1.0 is self-describing
# when bootstrapping locally (no Forgejo yet). Default to the files bundled with
# this project, one directory up from the coordinator package.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEED_MANIFEST = Path(os.environ.get("SIM_SEED_MANIFEST", str(_PROJECT_ROOT / "manifest.yaml")))
SEED_GITIGNORE = Path(os.environ.get("SIM_SEED_GITIGNORE", str(_PROJECT_ROOT / "repo" / ".gitignore")))
SEED_GITATTRIBUTES = Path(os.environ.get("SIM_SEED_GITATTRIBUTES", str(_PROJECT_ROOT / "repo" / ".gitattributes")))

# Built agent binary served to agents for remote self-update (build-agent.sh output).
AGENT_BINARY = Path(os.environ.get("SIM_AGENT_BINARY", str(_PROJECT_ROOT / "dist" / "simagent.exe")))

# Directory of installable prerequisites (git bundle, redists, …) that agents can
# download + run/unzip on a PC. Holds the asset files and an installs.json manifest.
INSTALLS_DIR = Path(os.environ.get("SIM_INSTALLS_DIR", "/srv/sim-config/installs"))

# Guards: per-PC compliance items (check + apply scripts). Definitions + .ps1 scripts
# ship with the code here; large binary assets (wallpaper.png, git-bundle.zip) live in
# INSTALLS_DIR and are referenced by name.
GUARDS_DIR = Path(os.environ.get("SIM_GUARDS_DIR", str(Path(__file__).resolve().parent / "guards")))

# SQLite state DB.
DB_PATH = Path(os.environ.get("SIM_DB", "/srv/sim-config/coordinator.db"))

# Global ignore: glob patterns merged into EVERY app's excludes (all PCs, all
# versions) — junk like logs/temp that should never be versioned or deployed. Lives
# outside the git checkout so it survives updates; seeded from the bundled default.
GLOBAL_IGNORE = Path(os.environ.get("SIM_GLOBAL_IGNORE", "/srv/sim-config/global-ignore.yaml"))
SEED_GLOBAL_IGNORE = _PROJECT_ROOT / "global-ignore.yaml"

# Branch / ref names.
TRAINING_LIVE = "training-live"   # movable pointer = what is deployed now
DEV_BRANCH = "dev"
MASTER = "master"

# Agent considered offline if no heartbeat within this many seconds.
HEARTBEAT_TIMEOUT = 30

# HealthCheck: how long temperature history is kept (older samples are pruned on
# every write) and how often each agent samples its sensors.
HEALTH_RETENTION_DAYS = int(os.environ.get("SIM_HEALTH_RETENTION_DAYS", "30"))
HEALTH_SAMPLE_SECONDS = int(os.environ.get("SIM_HEALTH_SAMPLE_SECONDS", "300"))

# Overheating alert (Fleet highlights a PC in red): a chip counts as overheating when it
# spends at least HEALTH_HOT_SUSTAIN_MIN cumulative minutes at/above its HOT threshold
# within the last 24h — a momentary spike is ignored, a sustained cook is flagged.
# CPU 95 C = the AMD Ryzen (and Intel) Tjmax throttle ceiling: modern chips, especially
# Ryzen X3D, boost to ~90 C by design, so only being pegged at the 95 C limit is a real
# alert. Tune per-site via the env vars if your hardware runs hotter/cooler.
HEALTH_CPU_HOT_C = float(os.environ.get("SIM_HEALTH_CPU_HOT_C", "95"))
HEALTH_GPU_HOT_C = float(os.environ.get("SIM_HEALTH_GPU_HOT_C", "88"))
HEALTH_HOT_SUSTAIN_MIN = int(os.environ.get("SIM_HEALTH_HOT_SUSTAIN_MIN", "20"))

# Shared bearer token for agent auth (OPEN QUESTION: replace with mTLS if LAN untrusted).
AGENT_TOKEN = os.environ.get("SIM_AGENT_TOKEN", "change-me")

# ---- Agentless Linux devices (SSH transport) ---------------------------------
# Operator token guarding the SSH surface (enrollment + browsing a Linux box's filesystem
# as root). The rest of the operator API is unauthenticated on a trusted LAN, but these
# routes are not: when this is unset they refuse service rather than allowing access.
OPERATOR_TOKEN = os.environ.get("SIM_OPERATOR_TOKEN", "")

# Key for encrypting stored secrets (see secretbox.py). Unset => passwords are never
# persisted, which is a supported configuration, not an error.
SECRET_KEY = os.environ.get("SIM_SECRET_KEY", "")

# Per-device SSH keypairs the coordinator generates and installs. Derived from DB_PATH so
# it follows SIM_DATA_DIR on a real Pi (/var/lib/sim-config) instead of the /srv defaults
# above, which pi-setup.sh does not set.
SSH_DIR = Path(os.environ.get("SIM_SSH_DIR", str(DB_PATH.parent / "ssh")))

# Liveness polling. There is no heartbeat from an agentless device, so the coordinator
# probes it. Two missed polls must exceed HEARTBEAT_TIMEOUT so online/offline behaves
# exactly as it does for an agent PC.
SSH_POLL_SECONDS = int(os.environ.get("SIM_SSH_POLL_SECONDS", "15"))
SSH_CONNECT_TIMEOUT = int(os.environ.get("SIM_SSH_CONNECT_TIMEOUT", "10"))

# Minimum gap between drift (clean/dirty) checks for one SSH device; 0 disables them.
# A check is a local walk plus one SFTP walk — sub-second for a normal config tree — so
# this is a floor, not a fixed period: ssh_ops widens it to 10x whatever the last check
# actually cost, letting a big tree back off by itself.
SSH_DRIFT_SECONDS = int(os.environ.get("SIM_SSH_DRIFT_SECONDS", "30"))

# Deploy mirrors repo -> device WITH deletions, as root. These bound the blast radius of
# a misconfigured live path or an empty repo folder: a plan exceeding either limit is
# refused unless the operator explicitly confirms it.
SSH_DELETE_LIMIT = int(os.environ.get("SIM_SSH_DELETE_LIMIT", "500"))
SSH_DELETE_PCT = int(os.environ.get("SIM_SSH_DELETE_PCT", "50"))
SSH_DEPLOY_WORKERS = int(os.environ.get("SIM_SSH_DEPLOY_WORKERS", "4"))
