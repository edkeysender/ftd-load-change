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
no build step), organised into five top tabs:

- **Fleet** — the single per-PC view: every computer that runs an agent, one expandable
  row each. Summary tiles across the top (PCs / online / seeded / unseeded / config issues).
  The collapsed row shows online, mode (`TRAINING`\|`TESTING`\|`UNSEEDED`), running ref,
  clean-vs-dirty (with a *diff* of drifted files), last-seen, agent build, and a per-PC
  **actions** dropdown (*Update agent* / *Wake on LAN* / *Force shut down* / *Remove*).
  Expand a row for its **Hardware & temperature** (BIOS, CPU/GPU names, live CPU/GPU temps
  with a 24h/7d/30d history chart) and its **Configuration checks** (the guard checklist,
  `N/M checks pass`, with per-item Apply and *Recheck this PC*). Agents sample sensors every
  5 min; the coordinator keeps `HEALTH_RETENTION_DAYS` (30) and prunes on every write.
  A PC whose CPU or GPU has spent **at least `HEALTH_HOT_SUSTAIN_MIN` (20) cumulative
  minutes at/above its hot threshold** (`HEALTH_CPU_HOT_C` 95 °C — the Ryzen/Intel Tjmax
  ceiling, since X3D chips boost to ~90 °C by design — / `HEALTH_GPU_HOT_C` 88 °C)
  in the last 24h is flagged **overheating** — a red row with a `🌡 hot: CPU 2h30m` chip,
  a red temp tile, and a count in the **Overheating** summary tile. A momentary spike is
  ignored; only sustained heat is flagged.
- **Loads** — **Training Loads** (immutable `v1.x`, deploy any to the whole sim,
  *Re-deploy live*); **Development Loads** (`dev-v1.x` test builds — *Deploy to sim* /
  *Redeploy testing*, then *Promote to Training Load*, or *Delete* to discard one — the live
  one is protected); and **Compare** (diff any two versions).
- **Load Configuration** — **Sync Configuration** (pick folders per PC from a live tree;
  uncheck a subfolder to *ignore* it; raw-YAML tab). Opens defaulted to the **last dev
  load**'s config. Typical dev loop: deploy the last dev version, tweak files **live** on
  the PCs (no auto-resync happens — see the reboot matrix below), then **Review changes &
  deploy** — asks every PC's agent for its live-vs-deployed **file drift** and lists exactly
  what you changed (click a file for its diff); if it looks right, **Deploy** captures those
  edits from each PC → commits → creates a new `dev-v1.x` → deploys it (two-phase progress
  modal, ending in a link to the new load in **Loads**). The per-PC stepper (Save → Import →
  snapshot) is still available for building a load from scratch. Also **Global ignore**.
- **Installs** — the **shared assets** pushed to every PC (wallpaper, git bundle, sensor
  DLLs, PawnIO installer, VC++ redists — upload/replace here). The per-PC compliance
  *checks* live under each computer in **Fleet**.
- **Sequence Config** — in-browser block builder for the sim startup/shutdown
  `sequenceConfig.json` (import / edit / download; error-code catalogue).

### Installs (guards)

Per-PC compliance items, each a PowerShell check (+ usually an apply) in `coordinator/guards/`,
listed in `guards.json`. They surface under each computer in the **Fleet** tab (`N/M checks
pass`, per-item Apply, *Recheck this PC*); their shared assets are managed in the **Installs**
tab.

| Guard | Asserts | Apply |
| --- | --- | --- |
| Computer name | name matches `WS-XX-XXX` (X = digit), optional `-role` suffix (e.g. `WS-25-024-display`), case-insensitive | — check only, rename needs a reboot |
| Clock in sync | shows the PC's system time; within 60s of the **coordinator's** clock | ✅ (resync, else set from the coordinator) |
| Max performance | High/Ultimate plan, no idle timeouts, no hibernation, no device power-down | ✅ |
| Wake-on-LAN ready | NIC holding the coordinator-facing IP wakes on magic packet | ✅ (BIOS/UEFI wake is still manual) |
| Windows Update disabled | the `NoAutoUpdate` / no-online-check **policies** (not the service — see below) | ✅ |
| Notifications disabled | toasts, notification centre, tips, Defender alerts | ✅ |
| Windows activated | Windows reports a licensed state | — check only, activate by hand (key/KMS) |
| Wallpaper and login image | one managed image on desktop + lock + sign-in; Spotlight and sign-in blur off (no three-image flash during login) | ✅ |
| Recycle Bin hidden | Recycle Bin icon hidden from the desktop | ✅ (applies at next sign-in) |
| Git / SSH / VC++ | prerequisites present | ✅ |
| Hardware sensors | a CPU temperature is readable (for HealthCheck) | ✅ (unpacks the LHM DLLs + installs the PawnIO driver) |

A guard may omit `apply` (check-only); the dashboard then shows no Apply button.
Guard scripts run with `SIM_PC_IP` (this PC's coordinator-facing IP), `SIM_LHM` (sensor
DLL dir), `SIM_COORDINATOR` (its base URL — the time guard reads the coordinator's clock
from the `Date` header of any response) and, for apply, `SIM_ASSETS` (downloaded assets).

> **A guard must only assert what its Apply can hold.** Two of these were rewritten after
> failing forever: *Max performance* demanded power-saving off on all 37 devices (Windows
> enables it by default on nearly all of them, and some refuse the write), and *Windows
> Update disabled* demanded `wuauserv` stay disabled — the Update Medic Service re-enables
> it at every boot, so it flipped to fail on each restart. Both now assert the durable
> subset (network/USB/input devices; the policy keys) and report the rest.
>
> Guards must also not contradict each other: *Max performance* exempts the Wake-on-LAN
> NIC, because Windows treats "allow this device to wake the computer" as a sub-option of
> "allow the computer to turn off this device", so clearing the parent silently disarms WoL.

> **Guard scripts must be ASCII.** Windows PowerShell 5.1 reads a BOM-less `.ps1` as
> ANSI, so a UTF-8 `—` decodes to `â€"` — and that last byte is a cp1252 smart quote,
> which PowerShell accepts as a *string terminator*. Use `-`, not `—`.

**CPU/GPU temperatures.** Windows has no dependable built-in CPU temp source, so
`health-probe.ps1` tries in order: ACPI thermal zone via WMI (absent on most desktop
boards) → LibreHardwareMonitor. GPU: `nvidia-smi` (any NVIDIA driver) → LibreHardwareMonitor
(covers AMD/Intel). A PC with no source reports `no reading` **plus the reason** — it never
invents a value.

To enable the LHM fallback, run `sudo bash deploy/fetch-lhm.sh` and `sudo bash
deploy/fetch-pawnio.sh` on the Pi (pi-setup.sh already does), then **Apply** the
*Hardware sensors* guard: it unpacks the DLLs next to the agent (`C:\sim-agent\lhm`),
unblocks them, and installs PawnIO silently.

Two non-obvious things, both learned the hard way:

- `lhm.zip` carries **all** of the upstream root DLLs, not just `LibreHardwareMonitorLib.dll`.
  It references `HidSharp`, `System.Memory`, `BlackSharp.Core`, `DiskInfoToolkit`,
  `RAMSPDToolkit-NDD` and `System.Runtime.CompilerServices.Unsafe`; a missing one fails at
  load with the unhelpful *"Unable to load one or more of the requested types"*.
- **LHM 0.9.x does not ship WinRing0.** It carries PawnIO *modules* (`RyzenSMU.bin`,
  `AMDFamily17.bin`, `IntelMSR.bin`, …) as embedded resources and reads sensors through the
  separately-installed **PawnIO** kernel driver (open source, Authenticode-signed by
  namazso.eu). Without it LHM enumerates every sensor and reads `null` — or `0`, which is
  worse, because it looks like a reading. `fetch-pawnio.sh` pulls the official installer;
  `sensors-apply.ps1` runs it as `-install -silent` only where it is absent (its own help
  text documents those flags; it refuses to install over an existing copy).

Three things must ALL be true for a CPU temperature, and the guard names whichever is missing:
1. `lhm.zip` unpacked on the PC (*Hardware sensors* → Apply);
2. **PawnIO installed** (same Apply);
3. the **agent running elevated** — the driver needs an admin token. Install the agent as a
   scheduled task with *Run with highest privileges* (see `deploy/AGENT.md`).

Memory Integrity (Core Isolation) blocked the *old* WinRing0-based approach and is still
reported as a possible cause, but PawnIO is signed and is not on the vulnerable-driver
blocklist, so it is no longer expected to matter.

Temperatures outside 5–150 °C are treated as absent by the probe **and** rejected at the
coordinator on ingest — LHM reports `0` for sensors it could not poll, and a 0 °C CPU
charted beside a real one reads as a healthy chip.

Enter your name (top-right) — it's recorded as the author of seals, promotes and dev builds.

## Troubleshooting the coordinator

The coordinator runs on the Pi as the systemd service **`sim-coordinator`** (port 8090,
as root, no sandbox). Everything below is run on the Pi.

**Dashboard won't load / `http://<pi-ip>:8090/` refuses the connection.** The service is
down. Check it, then restart:

```bash
systemctl status sim-coordinator --no-pager | head -20   # Active: failed / inactive?
sudo systemctl reset-failed sim-coordinator              # clear a start-limit lockout
sudo systemctl restart sim-coordinator
sudo ss -tlnp | grep 8090                                # confirm it's listening
```

**See why it died** — the Python traceback is in the journal:

```bash
sudo journalctl -u sim-coordinator -n 80 --no-pager
```

Common startup failures and what they mean:

| Symptom in the journal | Cause | Fix |
| --- | --- | --- |
| `sqlite3.OperationalError: attempt to write a readonly database` | The filesystem holding `/var/lib/sim-config` was **read-only** when it started - usually a **transient** blip (NVMe/SD hiccup under heavy I/O), occasionally a full or failing disk. | Verify it's writable *now* (below); if so just `reset-failed` + `restart`. If a big disk copy is running, wait for it to finish first. |
| `Start request repeated too quickly` / `status=... failed` and no traceback | The service crash-looped and systemd **gave up**. (The unit now sets `StartLimitIntervalSec=0` so it keeps retrying; older installs don't.) | `sudo systemctl reset-failed sim-coordinator && sudo systemctl restart sim-coordinator` |
| `Address already in use` on port 8090 | Something else grabbed the port. | `sudo ss -tlnp | grep 8090` to find it; stop it or change `SIM_PORT` in `/etc/sim-config.env`. |
| `ModuleNotFoundError` / `SyntaxError` in `coordinator/*.py` | A bad pull / broken venv. | `git -C /opt/ftd-load-change pull`, reinstall deps: `/opt/ftd-load-change/sim-config-system/.venv/bin/pip install -r coordinator/requirements.txt`, restart. |

**Diagnose a "readonly database" — is the disk actually writable right now?**

```bash
mount | grep -E '\bro\b'                                  # any real fs mounted read-only?
df -h /var/lib                                            # disk full?
sudo touch /var/lib/sim-config/_t && sudo rm /var/lib/sim-config/_t && echo "WRITE OK"
sudo dmesg | grep -iE 'read-only|ext4-fs error|I/O error|remount' | tail   # disk errors?
```

If `WRITE OK` and no fs shows `ro`, the fault was transient and already cleared - just
`reset-failed` + `restart`. If a filesystem shows `ro`, remount it (`sudo mount -o
remount,rw /`); if `dmesg` shows I/O errors, the underlying disk is failing - reimage/replace
(the DB at `/var/lib/sim-config/coordinator.db` and the git clone both live there).

**Applying pushed changes** (the running service does **not** hot-reload):

- **Backend** (`coordinator/*.py`): `git pull` then `sudo systemctl restart sim-coordinator`.
  Symptom of forgetting: a newly-added route returns 404.
- **Frontend** (`coordinator/static/index.html`): just Ctrl-F5 in the browser - uvicorn
  serves the file fresh, no restart needed.
- **Agent** (`agent/*.go`): rebuild on the Pi with `sudo bash deploy/build-agent.sh`, then
  *Update all agents* in the dashboard (agents self-update).

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
`POST /agents/{ip}/health/refresh` (manual temp sample), `GET /agent/binary`.
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
