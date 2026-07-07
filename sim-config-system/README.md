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

1. **Forgejo** (needed for deploy/dev; optional for first import→seal) — one script:
   ```bash
   sudo PI_HOST=70.84.68.196 bash deploy/forgejo-setup.sh
   ```
   Brings Forgejo up headless, creates the `sim/sim-config` repo + admin + token, pushes the
   coordinator's working clone, and sets `SIM_GIT_REMOTE`/`SIM_FORGEJO_URL`. It prints the
   `git_remote` to put in each agent's `agent.json`.
   *Before Forgejo:* the coordinator runs in local-init mode (`ensure_repo` does a local
   `git init`, seeds `manifest.yaml`), so import → seal works; pushes are no-ops until the
   remote exists. Deploy/capture need the remote.
2. **Coordinator** (on the Pi):
   ```bash
   pip install -r coordinator/requirements.txt
   export SIM_WORK_CLONE=/srv/sim-config/work     # external SSD
   export SIM_DB=/srv/sim-config/coordinator.db
   export SIM_GIT_REMOTE=http://localhost:3000/sim/sim-config.git   # default; harmless if Forgejo is down
   export SIM_AGENT_TOKEN=<shared-token>
   uvicorn coordinator.main:app --host 0.0.0.0 --port 8090
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

## Web UI

Open `http://<pi-ip>:8090/` for the operational dashboard (served by the
coordinator, single page, no build step): PC status grid, version history with
per-version rollback, deploy button, dev session panel (start/end, per-PC capture,
promote), bootstrap (import / size-report / seal v1.0), and LAN discovery. Enter
your name (top-right) — it's used as the author/lock holder for dev/seal/promote.

## Agent startup / reboot behaviour

What the agent does when it starts depends on what is running and what is live:

| Scenario | Resync? | Apps launched? |
| --- | --- | --- |
| **PC reboot, a training load (`v1.x`) is live** | ✅ yes (diff-only) | ✅ yes |
| **PC reboot, a dev/test load (`dev-N`) is live** | ❌ no | ❌ no — waits for a manual **Deploy** |
| **Agent restarted while the sim is running** | ❌ no (adopts state) | ❌ no |
| **Agent self-update** | ❌ no (adopts state) | ❌ no |

- On a **cold boot** the agent asks the coordinator (`GET /agents/{ip}/enforce`) for
  the `training-live` load and syncs + launches it — but **only if `training-live` is
  a customer training version**. If a **dev/test load** is live, `enforce` returns
  nothing, so the PC stays idle until someone clicks **Deploy** again. Dev loads are
  transient and must not auto-resume on a reboot; only production training loads
  auto-recover.
- Syncs are **diff-only** (`robocopy /MIR`), so re-launching an unchanged load copies
  nothing — the time is the apps' `start_delay` sequence, not the file copy.
- Restarting just the **agent** (sim still up) or a **self-update** never re-syncs or
  restarts the apps — the agent adopts the deployed state and resumes monitoring.
- To make a PC **never** auto-launch on boot (always wait for a manual Deploy), set
  `"enforce_on_start": false` in that PC's `agent.json`.

## API summary

Operator: `GET /pcs`, `GET /versions`, `GET /bootstrap`, `POST /import/{pc}`,
`POST /import/{pc}/size-report`, `POST /seal-baseline`, `POST /deploy`,
`POST /dev/start|capture|end`, `GET /diff`, `POST /promote`, `POST /rollback`.
Discovery: `POST /discover` (ping-sweep the subnet), `GET /discover` (last results),
`POST /discover/add|remove`, `GET /hosts` (curated list). Helper: `deploy/discover.sh`.
Agent-facing: `POST /agents/{ip}/heartbeat`, `GET /agents/{ip}/commands`,
`POST /agents/{ip}/import-result|size-report-result|capture-result|deploy-result`.

## Open questions to resolve before/while building

See `PROJECT_SPEC.md` §16 — mainly transport auth (token vs mTLS) for Phase 1 and the
app quiesce mechanism for Phase 3.
