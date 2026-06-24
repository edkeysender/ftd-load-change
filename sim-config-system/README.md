# Sim Config Version Management — Scaffold

Starter scaffold for the system described in `PROJECT_SPEC.md`. Training load = git
`master` (+ immutable `v1.x` tags); development load = `dev` branch. A Raspberry Pi
hosts Forgejo + a coordinator + UI; each sim PC runs a read-only agent.

> **Implementer note:** decisions are locked in `PROJECT_SPEC.md` §2. Build in phase
> order (§15). Stubs marked `TODO` are the intended fill-in points. The agent never
> pushes to git — the coordinator is the sole writer.

## Layout

```
manifest.yaml          # source of truth, pre-filled from launcher.json
docker-compose.yml      # Forgejo on the Pi (data on external SSD)
repo/                   # monorepo skeleton (one folder per PC) -> push to Forgejo as sim-config
coordinator/            # FastAPI: SQLite state + git wrapper + REST API (Pi)
  config.py db.py git_ops.py manifest.py main.py requirements.txt
agent/                  # Go Windows service: poll loop + state machine (each PC)
  main.go config.go client.go handlers.go fsops.go agent.example.yaml go.mod
```

## Bring-up order

1. **Forgejo** — `docker compose up -d`, create an empty `sim/sim-config` repo, push `repo/` and `manifest.yaml` to it.
   *Optional for a first local run:* if `SIM_GIT_REMOTE` is unreachable, the coordinator
   initialises a local `master` repo automatically (`ensure_repo`) and seeds `manifest.yaml`
   from this folder, so you can exercise import → seal before Forgejo exists. Pushes become
   no-ops until a remote is reachable.
2. **Coordinator** (on the Pi):
   ```bash
   pip install -r coordinator/requirements.txt
   export SIM_WORK_CLONE=/srv/sim-config/work     # external SSD
   export SIM_DB=/srv/sim-config/coordinator.db
   export SIM_GIT_REMOTE=http://localhost:3000/sim/sim-config.git   # default; harmless if Forgejo is down
   export SIM_AGENT_TOKEN=<shared-token>
   uvicorn coordinator.main:app --host 0.0.0.0 --port 8080
   ```
3. **Agent** (each PC): copy `agent.example.yaml` → `agent.yaml`, fill it in, then
   `GOOS=windows go build -o simagent.exe ./agent` and install as a Windows service.

## First run (Phase 1, read-only on PCs)

Agents connect as `UNSEEDED`. For each PC: `POST /import/{pc}/size-report` (review GB
per app via `GET /bootstrap`, prune `manifest.yaml` excludes) then `POST /import/{pc}`.
The agent walks each app's `live` dirs read-only, applies the merged excludes, and uploads
the tree to `/agents/{ip}/import-result`, which the coordinator stages into its working
clone (re-import overwrites). When all three are staged: `POST /seal-baseline` → commits
across all folders, tags `v1.0`, branches `dev`, sets `training-live`. Nothing on the PCs'
live locations is modified during import.

Keep the existing `launcher.json` rsync flow running in parallel until the new deploy
path is trusted.

## API summary

Operator: `GET /pcs`, `GET /versions`, `GET /bootstrap`, `POST /import/{pc}`,
`POST /import/{pc}/size-report`, `POST /seal-baseline`, `POST /deploy`,
`POST /dev/start|capture|end`, `GET /diff`, `POST /promote`, `POST /rollback`.
Agent-facing: `POST /agents/{ip}/heartbeat`, `GET /agents/{ip}/commands`,
`POST /agents/{ip}/import-result|size-report-result|capture-result|deploy-result`.

## Open questions to resolve before/while building

See `PROJECT_SPEC.md` §16 — mainly transport auth (token vs mTLS) for Phase 1 and the
app quiesce mechanism for Phase 3.
