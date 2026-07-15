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
  guard.go install.go update.go power.go state.go health.go
  agent.example.json go.mod
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
no build step), organised into four top tabs:

- **Loads** — PC status grid (online / mode `TRAINING`\|`TESTING` / running version /
  clean-vs-dirty, with a per-PC *diff* of drifted files, plus a per-PC **actions**
  dropdown: *Update agent* / *Wake on LAN* / *Force shut down* / *Remove from list*);
  **Training Loads** (immutable `v1.x`, deploy any to the whole sim, *Re-deploy live*);
  and **Development Loads** (`dev-v1.x` test builds — *Deploy to sim* / *Redeploy testing*,
  then *Promote to Training Load*, or *Delete* to discard one — the live one is protected);
  and **Compare** (diff any two versions).
- **Load Configuration** — **Sync Configuration** (pick folders per PC from a live tree;
  uncheck a subfolder to *ignore* it; raw-YAML tab). Opens defaulted to the **last dev
  load**'s config. Typical dev loop: deploy the last dev version, tweak files **live** on
  the PCs (no auto-resync happens — see the reboot matrix below), then **Review changes &
  deploy** — asks every PC's agent for its live-vs-deployed **file drift** and lists exactly
  what you changed (click a file for its diff); if it looks right, **Deploy** captures those
  edits from each PC → commits → creates a new `dev-v1.x` → deploys it (two-phase progress
  modal, ending in a link to the new load in **Loads**). The per-PC stepper (Save → Import →
  snapshot) is still available for building a load from scratch. Also **Installs** (per-PC
  compliance guards, see below) and **Global ignore**.
- **HealthCheck** — every PC running an agent, with its BIOS (vendor / version / release
  date) and CPU/GPU temperatures, plus a 30-day history chart per PC (24h / 7d / 30d).
  Each agent samples its own sensors every 5 min and posts them; the coordinator keeps
  `HEALTH_RETENTION_DAYS` (30) and prunes older samples on every write.
- **Sequence Config** — in-browser block builder for the sim startup/shutdown
  `sequenceConfig.json` (import / edit / download; error-code catalogue).

### Installs (guards)

Per-PC compliance items, each a PowerShell check (+ usually an apply) in `coordinator/guards/`,
listed in `guards.json`. Each online PC shows its Windows name next to its IP, and ↻ Recheck
re-runs every check on that PC.

| Guard | Asserts | Apply |
| --- | --- | --- |
| Computer name | name matches `WS-XX-XXX` (X = digit) | — check only, rename needs a reboot |
| Max performance | High/Ultimate plan, no idle timeouts, no hibernation, no device power-down | ✅ |
| Wake-on-LAN ready | NIC holding the coordinator-facing IP wakes on magic packet | ✅ (BIOS/UEFI wake is still manual) |
| Windows Update disabled | `NoAutoUpdate` policy + `wuauserv` disabled | ✅ |
| Notifications disabled | toasts, notification centre, tips, Defender alerts | ✅ |
| Wallpaper | desktop/lock image is the standard | ✅ |
| Git / SSH / VC++ | prerequisites present | ✅ |
| Hardware sensors | a CPU temperature is readable (for HealthCheck) | ✅ (needs the LHM DLLs uploaded) |

A guard may omit `apply` (check-only); the dashboard then shows no Apply button.
Guard scripts run with `SIM_PC_IP` (this PC's coordinator-facing IP), `SIM_LHM` (sensor
DLL dir) and, for apply, `SIM_ASSETS` (downloaded assets) in the environment.

> **Guard scripts must be ASCII.** Windows PowerShell 5.1 reads a BOM-less `.ps1` as
> ANSI, so a UTF-8 `—` decodes to `â€"` — and that last byte is a cp1252 smart quote,
> which PowerShell accepts as a *string terminator*. Use `-`, not `—`.

**CPU/GPU temperatures.** Windows has no dependable built-in CPU temp source, so
`health-probe.ps1` tries in order: ACPI thermal zone via WMI (absent on most desktop
boards) → LibreHardwareMonitor. GPU: `nvidia-smi` (any NVIDIA driver) → LibreHardwareMonitor
(covers AMD/Intel). To enable the fallback, upload `LibreHardwareMonitorLib.dll` +
`HidSharp.dll` under **Installs → Assets**, then **Apply** the *Hardware sensors* guard —
it copies them next to the agent (`C:\sim-agent\lhm`) and unblocks them. LHM reads sensors
through a kernel driver, so the agent must run **as administrator** or temps come back
empty. A PC with no source reports `no reading` plus the reason — it never invents a value.

Enter your name (top-right) — it's recorded as the author of seals, promotes and dev builds.

## Agent startup / reboot behaviour

**Sync is on-demand only.** The agent NEVER resyncs or launches on start — for a
training OR a dev load. The only thing that mirrors files or (re)starts apps is an
explicit **Deploy / Redeploy** from the dashboard.

| Scenario | Resync? | Apps launched? |
| --- | --- | --- |
| **Any agent/PC start** — training or dev load live | ❌ no | ❌ no |
| **Explicit Deploy / Redeploy** (dashboard) | ✅ yes (diff-only) | ✅ yes |

- On start the agent asks the coordinator (`GET /agents/{ip}/enforce`) which load is
  live, **adopts** that ref, and computes its **clean/dirty drift** — so PC status
  shows the current **mode** and **state** — but it does **not** touch a single file.
  Live edits survive reboots; a powered-off sim stays off until someone clicks Deploy.
- A PC that has **never been deployed** here stays `UNSEEDED` until its first Deploy.
- **Deploy / Redeploy** is the only sync path: `robocopy /MIR` diff-only mirror
  (unchanged files copy nothing) followed by the apps' `start_delay` launch sequence.
- To keep a PC from even adopting/reporting on boot, set `"enforce_on_start": false`
  in that PC's `agent.json`.

## API summary

Operator: `GET /pcs`, `GET /versions`, `GET /dev/versions`, `GET /dev/readiness`,
`GET /bootstrap`, `GET /config`, `GET|PUT /manifest/json`, `PUT /manifest/raw`,
`GET /manifest/at?ref`, `GET /manifest/pcs`, `POST /import/{pc}`,
`POST /import/{pc}/size-report`, `POST /seal-baseline`, `POST /deploy`,
`POST /dev/snapshot`, `DELETE /dev/versions/{tag}`, `GET /diff`,
`POST /promote` (+ `GET /promote/status`), `POST /rollback`.
HealthCheck: `GET /health` (per-PC BIOS + latest temps + 24h stats),
`GET /health/history?pc=&days=` (bucketed series; bucket scales with the span).
Guards / installs / global ignore: `GET /guards`, `POST /guard/check|apply|check-all`,
`GET /installs`, `POST /install`, `PUT|GET /installs/asset/{name}`, `GET|PUT /global-ignore`.
Agent-facing: `GET /whoami`, `POST /agents/{ip}/heartbeat`, `GET /agents/{ip}/commands`,
`GET /agents/{ip}/enforce`, `GET /agents/{ip}/browse|drift|filediff`,
`POST /agents/{ip}/{import|size-report|capture|deploy|drift|filediff|guard|install}-result`,
`POST /agents/{ip}/update|forget|shutdown|wake`, `POST /agents/{ip}/health`,
`GET /agent/binary`.
Wake-on-LAN uses the PC's last-known MAC (reported on every heartbeat); force
shutdown is queued for the agent (`shutdown /s /f /t 0`). The heartbeat also
carries the PC's Windows name, shown next to its IP under **Installs** and
checked against the `WS-XX-XXX` standard (X = a digit) by the `pcname` guard —
that one is **check-only** (a guard may omit `apply`; rename by hand, it needs a
reboot), so the dashboard shows no Apply button for it.
Discovery (endpoints still exist; the panel is hidden — agents self-register via heartbeat):
`POST|GET /discover`, `POST /discover/add|remove`, `GET /hosts`. Helper: `deploy/discover.sh`.

## Open questions to resolve before/while building

See `PROJECT_SPEC.md` §16 — mainly transport auth (token vs mTLS) for Phase 1 and the
app quiesce mechanism for Phase 3.
