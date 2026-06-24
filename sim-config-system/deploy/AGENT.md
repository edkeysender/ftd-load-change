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

Pick one PC to start — e.g. the displays box `70.84.68.12`.

1. Copy `dist/simagent.exe` and `agent/agent.example.json` to a folder on the PC,
   e.g. `C:\sim-agent\`.
2. Rename `agent.example.json` → `agent.json` (must sit next to the exe; the agent
   reads `agent.json` from its working directory).
3. Edit `agent.json`:
   - `pc_ip` / `folder` — match this PC's row in `manifest.yaml`
     (`70.84.68.12` / `pc-12-display`).
   - `coordinator_url` — `http://<pi-ip>:8090` (e.g. `http://70.84.68.196:8090`).
   - `token` — the value of `SIM_AGENT_TOKEN` from `/etc/sim-config.env` on the Pi
     (`sudo grep SIM_AGENT_TOKEN /etc/sim-config.env`).
   - `repo_path` / `git_remote` / `git_exe` — ignore for now (deploy only).

## 3. Run it

```powershell
cd C:\sim-agent
.\simagent.exe
```

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

## Deploy (Phase 2) — needs a git remote

Import uploads *to* the coordinator, so it needs no git. Deploy is the reverse: the
agent fetches the repo and mirrors it onto live locations, which requires the repo
to be served over the network (Forgejo, or `git daemon` on the Pi). Stand up Forgejo
(`docker compose up -d`), set `SIM_GIT_REMOTE` in `/etc/sim-config.env`, push the
working clone, then fill in `repo_path` / `git_remote` / `git_exe` in `agent.json`.
