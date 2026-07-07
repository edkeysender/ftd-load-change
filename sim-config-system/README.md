# Sim Config Version Management

Versioned config management for the flight simulator described in `PROJECT_SPEC.md`.
Training load = git `master` (+ immutable `v1.x` tags); development load = `dev` branch
(snapshotted as `dev-v1.x` test builds). A Raspberry Pi hosts Forgejo + a coordinator +
dashboard; each sim PC runs a pull-based agent.

> **Invariant:** the agent never pushes to git — the coordinator is the sole git writer.
> Agents are read-only git clients that pull and mirror; all commits/tags/pushes happen
> on the coordinator. Design rationale is in `PROJECT_SPEC.md`.

## Layout

```
manifest.yaml           # source of truth, pre-filled from launcher.json
global-ignore.yaml      # global exclude globs merged into every app (all PCs)
docker-compose.yml      # (unused) legacy Forgejo compose; forgejo-setup.sh uses plain `docker run`
repo/                   # monorepo skeleton (one folder per PC) -> pushed to Forgejo as sim-config
coordinator/            # FastAPI: SQLite state + git wrapper + REST API (Pi)
  config.py db.py git_ops.py manifest.py discovery.py main.py requirements.txt
  static/index.html     # single-page dashboard
  guards/               # per-PC compliance guard scripts (*-check/*-apply .ps1) + guards.json
agent/                  # Go Windows agent: poll loop + state machine (each PC)
  main.go config.go client.go handlers.go deploy.go fsops.go capture.go browse.go
  guard.go install.go update.go agent.example.json go.mod
deploy/                 # setup/build scripts: pi-setup, forgejo-setup, build-agent,
                        #   prepare-git-bundle, fetch-vcredist, discover, AGENT.md
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
3. **Agent** (each PC): build once on the Pi with `sudo bash deploy/build-agent.sh`
   (cross-compiles and **bakes the coordinator URL + shared token** into `simagent.exe`),
   copy the exe to the PC, and run it — **no config file needed**. It auto-detects its
   identity (`pc_ip` + folder) from the coordinator via `/whoami`. Create `agent.json`
   (from `agent.example.json`) only to override a default, e.g. `"enforce_on_start": false`.
   Update agents later from the dashboard ("Update all agents") — they self-update.

## First run (Phase 1, read-only on PCs)

Agents connect as `UNSEEDED`. For each PC: `POST /import/{pc}/size-report` (review GB
per app via `GET /bootstrap`, prune `manifest.yaml` excludes) then `POST /import/{pc}`.
The agent walks each app's `live` dirs read-only, applies the merged excludes, and uploads
the tree to `/agents/{ip}/import-result`, which the coordinator stages into its working
clone (re-import overwrites). When every PC is staged: `POST /seal-baseline` → commits
across all folders, tags `v1.0`, branches `dev`, sets `training-live`. Nothing on the PCs'
live locations is modified during import.

Keep the existing `launcher.json` rsync flow running in parallel until the new deploy
path is trusted.

## Web UI

Open `http://<pi-ip>:8090/` for the dashboard (served by the coordinator, single page,
no build step), organised into three top tabs:

- **Loads** — PC status grid (online / mode `TRAINING`\|`TESTING` / running version /
  clean-vs-dirty, with a per-PC *diff* of drifted files, plus *update* / *remove*);
  **Training Loads** (immutable `v1.x`, deploy any to the whole sim, *Re-deploy live*);
  and **Development Loads** (`dev-v1.x` test builds — *Deploy to sim* / *Redeploy testing*,
  then *Promote to Training Load*); and **Compare** (diff any two versions).
- **Configuration** — **Sync Configuration** (pick folders per PC from a live tree;
  uncheck a subfolder to *ignore* it; raw-YAML tab) with the gated dev-version stepper
  (Save → Import each PC → Deploy as Dev); **PC Images** (first-run import → seal `v1.0`,
  hidden after); **Installs** (per-PC compliance guards — wallpaper, Git, SSH, VC++ — with
  check/Apply and asset upload); and **Global ignore**.
- **Sequence Config** — in-browser block builder for the sim startup/shutdown
  `sequenceConfig.json` (import / edit / download; error-code catalogue).

Enter your name (top-right) — it's recorded as the author of seals, promotes and dev builds.

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

Operator: `GET /pcs`, `GET /versions`, `GET /dev/versions`, `GET /dev/readiness`,
`GET /bootstrap`, `GET /config`, `GET|PUT /manifest/json`, `PUT /manifest/raw`,
`GET /manifest/at?ref`, `GET /manifest/pcs`, `POST /import/{pc}`,
`POST /import/{pc}/size-report`, `POST /seal-baseline`, `POST /deploy`,
`POST /dev/snapshot`, `GET /diff`, `POST /promote` (+ `GET /promote/status`),
`POST /rollback`.
Guards / installs / global ignore: `GET /guards`, `POST /guard/check|apply|check-all`,
`GET /installs`, `POST /install`, `PUT|GET /installs/asset/{name}`, `GET|PUT /global-ignore`.
Agent-facing: `GET /whoami`, `POST /agents/{ip}/heartbeat`, `GET /agents/{ip}/commands`,
`GET /agents/{ip}/enforce`, `GET /agents/{ip}/browse|drift|filediff`,
`POST /agents/{ip}/{import|size-report|capture|deploy|drift|filediff|guard|install}-result`,
`POST /agents/{ip}/update|forget`, `GET /agent/binary`.
Discovery (endpoints still exist; the panel is hidden — agents self-register via heartbeat):
`POST|GET /discover`, `POST /discover/add|remove`, `GET /hosts`. Helper: `deploy/discover.sh`.

## Open questions to resolve before/while building

See `PROJECT_SPEC.md` §16 — mainly transport auth (token vs mTLS) for Phase 1 and the
app quiesce mechanism for Phase 3.
