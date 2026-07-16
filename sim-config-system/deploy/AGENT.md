# Running a real agent (Phase 1 import against live config)

The agent is a single static `simagent.exe` (pure Go stdlib, no DLLs). For
**import** it needs no git and no Forgejo — it walks each app's `live` dirs
read-only and uploads the tree to the coordinator. Deploy (Phase 2) additionally
needs a git remote; see the bottom of this file.

> Import never writes to live locations and never restarts apps. It is safe to
> run against a working sim.

## 1. Build the agent (on the Pi)

```bash
bash /opt/ftd-load-change/sim-config-system/deploy/build-agent.sh
# -> /opt/ftd-load-change/sim-config-system/dist/simagent.exe
```

## 2. Stage it on a Windows PC

Copy **only** `dist/simagent.exe` to a folder on the PC, e.g. `C:\sim-agent\`.
**No config file is needed** — `build-agent.sh` bakes the coordinator URL and the
shared token into the exe, and the agent auto-detects its identity (`pc_ip` +
`folder`) from the coordinator via `/whoami` on first contact. The identity comes
from the PC's own IP matched against `manifest.yaml`, so it always agrees with the
central config on the Pi.

Create an `agent.json` (next to the exe, from `agent/agent.example.json`) **only** to
override a default — e.g. `{"enforce_on_start": false}` for a passive agent during
testing, or a `git_exe` path. Everything else is discovered; you never put the URL,
token, or identity in a file.

## 3. Run it

```powershell
cd C:\sim-agent
.\simagent.exe
```

### Running it elevated (needed for CPU temperatures)

Double-clicking or running it from a normal shell leaves the agent **unelevated**, and
HealthCheck's CPU temperature stays empty: LibreHardwareMonitor reads MSRs through a
kernel driver that only loads with an admin token. (Everything else — deploy, import,
guards, GPU temps via nvidia-smi — works fine unelevated.)

Install it as a scheduled task that starts at logon with highest privileges. Run this
**once per PC, from an elevated PowerShell**:

```powershell
$exe = 'C:\sim-agent\simagent.exe'
$me  = "$env:USERDOMAIN\$env:USERNAME"
Register-ScheduledTask -TaskName 'sim-agent' -Force `
  -Action    (New-ScheduledTaskAction -Execute $exe) `
  -Trigger   (New-ScheduledTaskTrigger -AtLogOn -User $me) `
  -Principal (New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Highest) `
  -Settings  (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0)
Stop-Process -Name simagent -Force -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName 'sim-agent'
```

It runs as the logged-in user (who must be a local admin), **not** as SYSTEM — deliberate:
the notifications guard and other per-user settings write the console user's registry
hive, which a SYSTEM-run agent would miss.

It logs `agent ... started in state UNSEEDED` and heartbeats every 10s. Confirm
it shows up online on the Pi:

```bash
curl http://localhost:8090/pcs        # this PC should appear with "online": true
```

## 4. Trigger a read-only import (from the Pi)

```bash
curl -X POST http://localhost:8090/import/70.84.68.12
```

The agent picks up the command on its next poll, walks the display app folders,
applies the excludes, and uploads. Watch the agent console for
`[import] bundled N files ... done`.

## 5. Verify what was captured

```bash
sudo git -C /var/lib/sim-config/work status                       # pc-12-display now populated
sudo git -C /var/lib/sim-config/work ls-files -o --exclude-standard | grep pc-12-display | head
```

Also get a size report first if you want to see GB-per-app before importing:

```bash
curl -X POST http://localhost:8090/import/70.84.68.12/size-report
curl http://localhost:8090/bootstrap        # shows sizes + import status per PC
```

**Watch for the path-reconciliation flag:** if the display apps actually read from
a different folder than `manifest.yaml` lists (the spec notes a possible
`ProSim-training/ProSimDisplay` vs `ProSimDisplay-training` mismatch), the import
will report those `live` dirs as missing in `/bootstrap` → adjust `manifest.yaml`.

## Sealing v1.0 for real

Once **all three** PCs have run a real import (repeat steps 2–5 for `70.84.68.11`
and `70.84.68.10`), seal the real baseline:

```bash
curl -X POST http://localhost:8090/seal-baseline \
  -H 'Content-Type: application/json' \
  -d '{"message":"initial real baseline","author":"yourname"}'
```

## Dev capture / promote (Phase 3) — needs a git remote

Drive it from the web UI Dev panel, or via the API:

```bash
curl -s -X POST http://localhost:8090/dev/start   -H 'Content-Type: application/json' -d '{"user":"yourname"}'
# engineer edits live config through the running apps on a PC...
curl -s -X POST http://localhost:8090/dev/capture -H 'Content-Type: application/json' -d '{"pc":"70.84.68.12"}'
#   agent: checkout dev -> quiesce apps -> mirror live->worktree -> diff vs dev -> upload bundle.
#   coordinator commits the bundle to dev (attributed to the lock holder, serialized).
curl -s "http://localhost:8090/diff?base=v1.0&head=dev"            # changed-file list + Forgejo link
curl -s -X POST http://localhost:8090/promote     -H 'Content-Type: application/json' -d '{"message":"tuned X","author":"yourname"}'
curl -s -X POST http://localhost:8090/dev/end
```

Capture **quiesces (stops) the apps** on that PC to release file handles, then
relaunches them — expect a brief restart on the captured PC.

## Deploy (Phase 2) — needs a git remote

Import uploads *to* the coordinator, so it needs no git. Deploy is the reverse: the
agent fetches the repo and mirrors it onto live locations, which requires the repo
to be served over the network. Stand up Forgejo + wire the coordinator in one step:

```bash
sudo PI_HOST=70.84.68.196 bash /opt/ftd-load-change/sim-config-system/deploy/forgejo-setup.sh
```

It prints the `git_remote` URL (e.g. `http://70.84.68.196:3000/sim/sim-config.git`).
Put that in each PC's `agent.json` as `git_remote`, set `repo_path` (e.g. `D:/sim-config`)
and `git_exe` (`git` if on PATH, or a bundled portable git), then deploy from the web UI
or `curl -X POST http://localhost:8090/deploy`.
