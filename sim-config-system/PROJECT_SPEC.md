# Simulator Config Version Management — Build Spec

> **For the implementer (Claude Code):** This is a complete design for a system to be built from scratch. The architectural decisions in "Locked Decisions" are settled — do not re-litigate them. Build in the phase order under "Build Phases." Where the spec says "from the launcher config," it refers to the existing `launcher.json` the user will provide (the per-IP `run` + `settings.dirsToSync` file); use it as the source of truth for IPs, app paths, launch order, and current sync mappings.

---

## 1. Problem & Goal

We operate a 3-PC flight simulator. Each PC runs a fixed set of applications whose configuration must be **identical and deterministic after every restart** — this is the **training load**. We also need a **development load** where changes are preserved so an engineer can test them, and once satisfied, **promote** dev into training as a new immutable version with the ability to **roll back**.

Today this is handled by a hand-maintained rsync-style launcher (`launcher.json`): per-PC it copies "training" source folders into the apps' live locations on boot and restarts apps on file change. There is no versioning, no diff view, no rollback, and no central UI.

**Goal:** Replace the ad-hoc flow with a versioned system. Training load = git `master`; development load = git `dev` branch. A Raspberry Pi hosts the repo, a coordinator service, and a web UI showing what is synced to each PC, what changed in dev, full version history, and one-click deploy / promote / rollback.

## 2. Locked Decisions (do not change)

1. **One whole-sim version.** A single version number (`v1.0`, `v1.1`, …) describes the state of all three PCs together. One monorepo, one folder per PC. Never tag per-PC.
2. **Manual capture.** Dev changes are committed only when a user explicitly clicks "Capture" — never auto-committed on file writes.
3. **Multi-user, coordinator-mediated commits.** More than one engineer runs dev sessions and promotes loads. **Agents never push to git.** The coordinator holds the only writable clone and is the sole committer/merger/tagger, serialized by a dev-session lock. This removes push races when dev edits land on multiple PCs in one session.
4. **Sync scope = binaries + required files, minus junk (denylist).** For each app we version the **whole app folder** (executable, DLLs, data, config) and exclude only junk (logs, caches, temp, crash dumps, lockfiles, Navdata). A denylist is intentional: anything unclassified defaults to *included*, so a missed dependency does not break a deploy.
5. **Prepar3D (P3D) = config only.** P3D's multi-GB install/scenery is **not** versioned. Only its config dirs are tracked (same as today's rsync). Full P3D / full-machine recovery remains the job of the existing **Clonezilla/DRBL PXE** disaster-recovery system. This system does not back up or image machines.
6. **Single git track.** With P3D's bulk excluded, all remaining app folders are small enough for plain git; no separate binary "baseline mirror" or git-LFS is needed.

## 3. System Context

Three Windows PCs on the sim LAN, plus a Raspberry Pi. IPs and app inventory come from `launcher.json`:

| IP | Role | Apps (launch order by `startDelay`) | Versioned config sync (today) |
|----|------|--------------------------------------|-------------------------------|
| `70.84.68.11` | Sim host | ImmersiveDisplayPRO → **Prepar3D** → **ProSimA322 System** → ProsimHardwareGateway → HardwareGateway → ControlLoadingSystem (acl) | P3D: `ProgramData/Lockheed Martin`, `AppData/Roaming/Lockheed Martin`. ProSimA322: load dir + `ProgramData/ProSim-AR` (excl. `Navdata`) |
| `70.84.68.10` | Instructor station (IOS) | ProSimIOS | `ProSimIOS` load + `ProgramData/ProSim-AR` |
| `70.84.68.12` | Displays | CPT In/Outboard, FO In/Outboard, Eicas Upper/Lower, ISIS, CDU CPT, CDU FO, ProSimAudio2 | `ProSimDisplay-training` |

Key behaviors the system must preserve from the launcher:
- Each app launches with its `cmd` (+ optional `arguments`) after its `startDelay` seconds, in order.
- `restartOnFileChange`: when the app's versioned files change, restart the app (mainly used in dev so testers see changes live).
- The current `dirsToSync` source→destination mappings define where each app reads its live config (e.g. `C:/ProgramData/ProSim-AR`, `C:/Users/sim/AppData/Roaming/Lockheed Martin`). These become the "worktree → live" mirror step.
- Note: some display apps run from `D:/prosim/ProSim-training/ProSimDisplay/...` while the sync source is `D:/Rsync/ProSimDisplay-training/`. The bootstrap step must reconcile actual run paths with sync paths from the real config; flag mismatches rather than guessing.

## 4. Architecture

```
                    Raspberry Pi
   ┌─────────────────────────────────────────────────┐
   │  Forgejo (bare repo)   ← web diff / history / UI │
   │  Coordinator service   ← ONLY git writer         │
   │    • working clone of the monorepo               │
   │    • SQLite: PC registry, session lock, versions │
   │    • REST API + web UI                            │
   └─────────────────────────────────────────────────┘
        ▲ deploy (ref)        ▲ capture (file bundle)
        │ heartbeat/poll      │ upload
   ┌────┴────┐   ┌────┴────┐   ┌────┴────┐
   │ Agent   │   │ Agent   │   │ Agent   │   (Windows service per PC)
   │ .11     │   │ .10     │   │ .12     │
   └─────────┘   └─────────┘   └─────────┘
```

**Roles & write boundaries:**
- **Forgejo** (Docker on the Pi): bare repo = source of truth; provides the web diff, history, branch/tag views, and one-click revert UI for free.
- **Coordinator** (Pi): holds a full *writable working clone*. It is the **only** process that commits, merges, tags, and pushes. Serializes all writes via a dev-session lock in SQLite. Serves the operational web UI and the REST API. Issues commands to agents and records version metadata + who promoted what.
- **Agents** (one per PC, Windows service): **read-only git clients** for deploy (fetch + sparse checkout + mirror to live + restart apps) and **file-bundle uploaders** for capture. They never push. Communication is **pull-based**: agents poll the coordinator for pending commands and POST heartbeats + results, so no inbound ports are opened on the sim PCs.

## 5. Repository Layout

One monorepo. One folder per PC. Repo tree mirrors live destination structure so deploy is a straight mirror copy.

```
sim-config/
├── manifest.yaml                 # PC↔IP map, app→path mappings, exclude rules, launch order
├── pc-11-simhost/
│   ├── prepar3d/                 # CONFIG ONLY (Lockheed Martin dirs); install excluded
│   ├── prosim-a322/              # full app folder (binaries + config), minus junk/Navdata
│   ├── gateways/                 # a320ProsimHardwareGateway, hardwareGateway
│   └── acl/                      # control loading
├── pc-10-ios/
│   └── prosim-ios/
└── pc-12-display/
    ├── displays/                 # CPT/FO In+Outboard, Eicas U/L, ISIS
    ├── cdu/                      # CPT, FO
    └── audio/                    # ProSimAudio2
```

Each agent runs `git sparse-checkout set <its folder>` so a PC pulls only what it runs. Git is content-addressed: the seven identical `ProSimA322-Display.exe` copies dedup to one stored blob while each folder's distinct config is tracked separately.

## 6. Versioning Model

Three kinds of ref, distinct jobs:
- `master` — integration branch, never deployed directly.
- `v1.0`, `v1.1`, … — **annotated tags, immutable**, one per promotion. The version history.
- `training-live` — a **movable branch** that training-mode agents track. "What is deployed right now."
- `dev` — development branch, branched from the current version.

```
# promote dev → training, cut a new version
git checkout master && git merge --squash dev && git commit -m "v1.2: <summary>"
git tag -a v1.2 -m "<summary>"
git branch -f training-live v1.2        # point live at the new version

# rollback — no rebuild, just move the pointer and redeploy
git branch -f training-live v1.0
```

Versions never move; only `training-live` moves. **Rollback = move the pointer + redeploy.** All of the above runs only in the coordinator's working clone.

## 7. Sync Scope Policy & Manifest

Policy: version the whole app folder, exclude junk. P3D is config-only. Standard exclude set: `**/*.log`, `**/logs/**`, `**/cache/**`, `**/Cache/**`, `**/*.tmp`, `**/*.lock`, `**/crashes/**`, plus P3D `**/Navdata/**`.

`manifest.yaml` shape (Claude Code: generate the full file from `launcher.json`; this is the pattern):

```yaml
pcs:
  "70.84.68.12":
    folder: pc-12-display
    apps:
      display-cpt-inboard:
        live: D:/prosim/ProSim-training/ProSimDisplay/CPTInboard   # whole folder
        repo: pc-12-display/displays/CPTInboard
        run:  D:/prosim/ProSim-training/ProSimDisplay/CPTInboard/ProSimA322-Display.exe
        start_delay: 5
        restart_on_change: true
        exclude: ["**/*.log", "**/cache/**", "**/*.tmp", "**/*.lock"]
      # ...FO Outboard, Eicas U/L, ISIS, CDU CPT/FO, ProSimAudio2 (start_delay 60)
  "70.84.68.11":
    folder: pc-11-simhost
    apps:
      prepar3d:                 # CONFIG ONLY — install stays local, recovered via Clonezilla
        live: [C:/ProgramData/Lockheed Martin, C:/Users/sim/AppData/Roaming/Lockheed Martin]
        repo: pc-11-simhost/prepar3d
        run:  "D:/Prepar3D/Lockheed Martin/Prepar3D v5/Prepar3D.exe"
        start_delay: 10
        exclude: ["**/Navdata/**", "**/*.log", "**/Cache/**"]
      prosim-a322:
        live: [D:/prosim/ProSimA322-training, C:/ProgramData/ProSim-AR]
        repo: pc-11-simhost/prosim-a322
        run:  D:/prosim/ProSimA322-training/ProSimA322-System.exe
        start_delay: 40
        exclude: ["ProSim-AR/Navdata/**", "**/*.log"]
      # ...gateways (55, 65), acl (70), ImmersiveDisplayPRO (1, no sync)
```

## 8. Agent (Windows service)

**Tech:** Go, single static `.exe`, runs as a Windows service. Bundles a portable `git.exe`. Talks to the coordinator over HTTPS (REST). Pull-based: long-poll `/commands`, POST `/heartbeat` and result endpoints. Absorbs the existing launcher's responsibilities (launch `cmd` with `start_delay` ordering; `restart_on_change` file watching; worktree→live mirror replacing `dirsToSync`).

**State machine:**
```
UNSEEDED ──connect──> (waits for import; nothing enforced, nothing touched)
IDLE
DEPLOYING   : fetch → sparse checkout training-live → mirror worktree→live (excludes) → restart apps in order → report
TRAINING    : steady state; heartbeats clean/dirty vs training-live; re-enforces on restart
DEV_TRACKING: checkout dev → mirror→live → restart; restart_on_change active so testers see changes live
CAPTURING   : quiesce apps → mirror live→worktree subset (excludes) → diff vs dev → bundle changed files → upload
ERROR       : report; retry to prior state
```

**Deploy (training):** on poll, agent fetches, sparse-checks-out `training-live`, mirrors its folder to live locations (applying excludes), restarts apps in `start_delay` order, reports per-app success. Local edits are overwritten on every deploy — this is the determinism guarantee, now backed by a commit hash.

**Capture (dev):** agent **quiesces (gracefully stops) the relevant apps first** (they hold config files open while running — snapshotting live risks half-written files), mirrors live→worktree subset with excludes, computes changed files vs current `dev`, and **uploads a bundle** of changed files for its folder. It does **not** commit. The coordinator applies the bundle into its working clone and commits to `dev`, attributed to the session user, under the lock.

## 9. Coordinator (Pi)

**Tech:** Python FastAPI + SQLite. Owns the single writable git clone. Serves REST API + web UI. SQLite tables: `agents` (registry, last heartbeat, mode, current ref, clean/dirty), `versions` (tag, message, author, timestamp, parent), `dev_session` (lock holder, started_at), `deploys` (ref, per-PC results, timestamp).

**API:**
```
GET  /pcs                          # per-agent: online, mode, current ref, clean/dirty, last deploy/capture
POST /import/{pc}                  # bootstrap: pull current state up (read-only on PC), stage into working clone
POST /import/{pc}/size-report      # du -sh per app folder before commit (see Bootstrap)
POST /seal-baseline   {message}    # commit all PC folders, tag v1.0, branch dev, set training-live
POST /deploy          {ref|tag}    # set training-live→ref; instruct agents; collect per-PC results
POST /dev/start                    # acquire dev-session lock; agents → DEV_TRACKING on dev
POST /dev/capture     {pc}         # agent captures; coordinator commits bundle to dev (serialized by lock)
POST /dev/end                      # release lock
GET  /diff            base..head   # changed-file list + deep link to Forgejo compare view
POST /promote         {message}    # squash-merge dev→master, tag v(N+1), return new version
POST /rollback        {tag}        # branch -f training-live→tag, then deploy
# agent-facing
GET  /agents/{id}/commands         # long-poll for pending command
POST /agents/{id}/heartbeat
POST /agents/{id}/capture-result   # uploads the changed-file bundle
POST /agents/{id}/deploy-result
```

The dev-session lock is what makes multi-user safe: captures from `.11` and `.12` arrive and are committed one at a time onto `dev`. Forgejo renders the actual side-by-side diff, so `/diff` returns metadata + a link.

## 10. Web UI (served by coordinator)

Operational dashboard (link out to Forgejo for deep diff/history):
- **PC status grid:** each PC's online state, mode (TRAINING/DEV/UNSEEDED), current version/ref, clean-or-dirty flag, last deploy/capture time.
- **Version history:** list of `v1.x` tags with message, author, timestamp; "currently live" badge on whichever `training-live` points at; per-version "Rollback to this" button.
- **Dev panel:** Start/End dev session (shows lock holder), per-PC "Capture" button, changed-files summary, "Promote to training" button (prompts for version message).
- **Bootstrap panel:** per-PC "Import current state" + size report, then "Create master (v1.0)" once all imported and clean.
- **Deploy:** "Deploy v1.x" with per-PC progress and pass/fail.

## 11. Bootstrap / First Run (read-only on PCs)

First run never writes to live locations and restarts nothing — safe against a working sim. The current `*-training` / `Rsync` source dirs in `launcher.json` are the canonical training load; ingest those.

1. **Enroll:** install each agent, point it at the coordinator URL. All show `UNSEEDED`. Pi: empty repo + bootstrap manifest (source→repo map from `launcher.json`, with the standard excludes).
2. **Import (per PC):** "Import current state" copies the app folders up to the coordinator, applying excludes, into that PC's repo folder. Idempotent — re-import overwrites. Prefer the staging/`-training` dirs (curated) over live `C:/ProgramData` copies (derived); diff live vs staging and flag drift.
3. **Size report:** agent runs `du -sh` (or PowerShell equivalent) on every app folder it is about to import, so the user sees GB per app **before** the first commit and can drop anything surprising into `exclude`.
4. **Review & prune:** browse staged tree, add volatile junk to `exclude`, re-import. Coordinator emits a draft manifest listing every top-level dir found.
5. **Seal v1.0:** once all three PCs are imported and clean, "Create master" → one commit across all folders, tag `v1.0`, branch `dev`, set `training-live` → `v1.0`.
6. **Go managed:** agents move `UNSEEDED → TRAINING`, enforcing `v1.0`.
7. **Keep the existing rsync flow running in parallel** for the first few weeks as a zero-cost fallback until the new deploy path is trusted.

## 12. Core Flows

**Deploy:** coordinator sets `training-live`→ref → agents poll, fetch, checkout, mirror→live, restart in order → report per-PC.
**Dev → Promote:** `/dev/start` (lock) → agents track `dev` → engineer edits via live apps → per-PC `/dev/capture` (coordinator commits to dev) → review diff in Forgejo → `/promote` (squash-merge, tag `v(N+1)`) → `/dev/end` → deploy.
**Rollback:** `/rollback {tag}` → coordinator points `training-live` at the older tag → deploy.

## 13. Tech Stack Summary

- **Pi storage:** external SSD (not the SD card) for Forgejo bare repo + coordinator working clone.
- **Git host:** Forgejo via Docker.
- **Coordinator:** Python FastAPI + SQLite + a working git clone; serves REST + web UI (React or server-rendered).
- **Agent:** Go static binary, Windows service (e.g. `kardianos/service` or NSSM), bundled portable git; pull-based comms; mirror via robocopy-equivalent honoring excludes.
- **Transport:** HTTPS on the sim LAN; shared-token or mTLS auth (see Open Questions).

## 14. Non-Goals

- **Not a backup or imaging tool.** Full-machine / full-install recovery = existing Clonezilla/DRBL. Do not duplicate it.
- **Not versioning P3D's install or scenery** — config only.
- **No auto-commit** of dev changes — capture is always manual.
- **No per-PC versioning** — one whole-sim version only.
- **No agent-side pushes** to git — coordinator is the sole writer.

## 15. Build Phases

**Phase 1 — Repo + manifest + agent import/size-report (P0).** Monorepo skeleton, `manifest.yaml` generated from `launcher.json`, agent skeleton (Go service) implementing enroll, `UNSEEDED`, import (read-only mirror up), and `du -sh` size report. Coordinator skeleton with working clone + SQLite + `/import` + `/seal-baseline`.
*Acceptance:* Given a fresh install, when all three PCs import and the user seals, then a `v1.0` tag exists containing all three folders minus excluded junk, and nothing on any PC's live locations was modified.

**Phase 2 — Deploy + enforce (P0).** `training-live` pointer, `/deploy`, agent `DEPLOYING/TRAINING` states, worktree→live mirror, app restart in `start_delay` order, clean/dirty heartbeat.
*Acceptance:* Given `v1.0` is live, when a user edits a live config and a deploy runs, then the edit is reverted and apps restart in order; the UI flags the PC dirty before deploy and clean after.

**Phase 3 — Dev capture + promote + rollback (P0).** `dev` branch, `/dev/start` lock, agent `DEV_TRACKING/CAPTURING` (quiesce → bundle upload), coordinator commit-to-dev, `/promote` (squash + tag), `/rollback`.
*Acceptance:* Given a dev session with edits captured on `.11` and `.12`, when promoted, then `v1.1` is tagged containing both PCs' changes with no push conflict, and rollback to `v1.0` redeploys the prior state.

**Phase 4 — Web UI (P1).** PC status grid, version history with rollback buttons, dev panel, bootstrap panel, deploy progress.

**Phase 5 — Forgejo integration (P1).** Wire `/diff` and version history to Forgejo compare/history views; optionally back user identity with Forgejo accounts for promote attribution.

**P2 / future:** decide whether ImmersiveDisplayPRO config joins the repo; optional restic snapshot of P3D bulk if the Pi later becomes its distribution point; tablet-friendly UI.

## 16. Open Questions

- **Transport auth (eng):** shared bearer token over the sim LAN vs mTLS for coordinator↔agent? Blocking for Phase 1 if the LAN isn't trusted.
- **Pi reachability (eng):** Pi's IP/route on the `70.84.68.x` subnet; firewall rules so agents can reach it outbound.
- **Quiesce mechanism (eng):** can ProSim/gateway apps be stopped gracefully (clean file handles) or must the agent force-kill? Affects capture integrity. Blocking for Phase 3.
- **Promote attribution (product):** simple name field vs real Forgejo accounts? Non-blocking; can start with a name field.
- **Path reconciliation (eng):** confirm display run paths (`ProSim-training/ProSimDisplay`) vs sync source (`ProSimDisplay-training`) against the live machine during bootstrap.
- **Service framework (eng):** `kardianos/service` (native Go) vs NSSM wrapper for the Windows service.
