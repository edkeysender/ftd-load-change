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

# Branch / ref names.
TRAINING_LIVE = "training-live"   # movable pointer = what is deployed now
DEV_BRANCH = "dev"
MASTER = "master"

# Agent considered offline if no heartbeat within this many seconds.
HEARTBEAT_TIMEOUT = 30

# Shared bearer token for agent auth (OPEN QUESTION: replace with mTLS if LAN untrusted).
AGENT_TOKEN = os.environ.get("SIM_AGENT_TOKEN", "change-me")
