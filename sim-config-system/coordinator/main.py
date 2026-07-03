"""Coordinator API. Run: uvicorn coordinator.main:app --host 0.0.0.0 --port 8090

The coordinator is the single git writer and the orchestration brain. Agents are
read-only git clients that poll /agents/{ip}/commands and post results.
"""
import base64
import json
import threading
import time
import uuid
import yaml
from pathlib import Path
from fastapi import FastAPI, HTTPException, Header, Body, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from . import config, db, discovery, git_ops, manifest

# In-flight filesystem-browse requests: req_id -> {"event": Event, "result": dict}
_browse_requests: dict = {}
# In-flight drift-diff requests (PC status "diff"): req_id -> {"event", "result"}
_drift_requests: dict = {}
# In-flight per-file diff requests (click a drifted file): req_id -> {"event", "result"}
_filediff_requests: dict = {}

# Live import/capture progress per PC: pc_ip -> {total_bytes, received_bytes, done}
_import_progress: dict = {}
_capture_progress: dict = {}

# Live deploy/sync state per PC for the current deploy: pc_ip -> {state, at, expected}
# state: "syncing" (command sent, agent working) | "ok" | "fail".
_deploy_progress: dict = {}
# How long the last successful deploy took per PC (seconds) — projects % + ETA.
_last_deploy_dur: dict = {}

# Last error message reported by each agent (cleared when it leaves ERROR).
_agent_errors: dict = {}

# Last install result per PC: pc_ip -> {id, ok, msg, at}
_install_results: dict = {}

# Guard status per PC: pc_ip -> {guard_id -> {pass, detail, at}}. `pass` is None while
# an apply is running (pending re-check).
_guard_results: dict = {}

# PCs the operator asked to self-update; the agent checks this at startup (before
# syncing) and on each poll, then acks to clear it (so no update loop).
_update_pending: set = set()

app = FastAPI(title="Sim Config Coordinator")

_STATIC = Path(__file__).resolve().parent / "static"


@app.get("/")
def dashboard():
    """Operational web UI (single page; calls the JSON API below)."""
    return FileResponse(_STATIC / "index.html")


@app.get("/config")
def ui_config():
    return {"forgejo_url": config.FORGEJO_URL,
            "training_live": config.TRAINING_LIVE, "dev_branch": config.DEV_BRANCH}


@app.get("/manifest/pcs")
def manifest_pcs():
    """Authoritative PC list from the manifest (so the UI can render rows before
    any agent has connected)."""
    out = []
    for ip, spec in manifest.load_manifest()["pcs"].items():
        out.append({"ip": ip, "folder": spec.get("folder"), "role": spec.get("role"),
                    "apps": list((spec.get("apps") or {}).keys())})
    return {"pcs": out}


@app.get("/manifest/raw")
def manifest_raw():
    """Full manifest text for the config panel."""
    p = config.MANIFEST_PATH
    return {"yaml": p.read_text() if p.exists() else ""}


class ManifestRaw(BaseModel):
    yaml: str


@app.put("/manifest/raw")
def manifest_raw_save(req: ManifestRaw):
    """Validate + persist the manifest (global sync config). Takes effect
    immediately (read live); committed into the next version on seal/promote."""
    import yaml as _yaml
    try:
        data = _yaml.safe_load(req.yaml)
    except Exception as e:
        raise HTTPException(400, f"YAML parse error: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("pcs"), dict):
        raise HTTPException(400, "manifest must be a mapping with a 'pcs' mapping")
    for ip, spec in data["pcs"].items():
        if not isinstance(spec, dict) or "folder" not in spec:
            raise HTTPException(400, f"pc {ip}: missing 'folder'")
        if not isinstance(spec.get("apps") or {}, dict):
            raise HTTPException(400, f"pc {ip}: 'apps' must be a mapping")
    git_ops.commit_manifest(req.yaml)
    db.set_meta("manifest_saved_at", time.time())  # a dev version re-imports fresh content
    return {"ok": True, "pcs": list(data["pcs"].keys())}


def _validate_manifest(data):
    if not isinstance(data, dict) or not isinstance(data.get("pcs"), dict):
        raise HTTPException(400, "manifest must be a mapping with a 'pcs' mapping")
    for ip, spec in data["pcs"].items():
        if not isinstance(spec, dict) or "folder" not in spec:
            raise HTTPException(400, f"pc {ip}: missing 'folder'")
        if not isinstance(spec.get("apps") or {}, dict):
            raise HTTPException(400, f"pc {ip}: 'apps' must be a mapping")


@app.get("/manifest/json")
def manifest_json():
    """Parsed manifest for the config-panel builder."""
    return {"manifest": manifest.load_manifest()}


@app.put("/manifest/json")
def manifest_json_save(body: dict = Body(...)):
    """Persist a manifest built by the UI (file browser). Dumped to YAML and
    committed to dev (comments not preserved — use the raw editor to keep them)."""
    _validate_manifest(body)
    git_ops.commit_manifest(
        yaml.safe_dump(body, sort_keys=False, default_flow_style=False, allow_unicode=True))
    db.set_meta("manifest_saved_at", time.time())  # a dev version re-imports fresh content
    return {"ok": True, "pcs": list(body["pcs"].keys())}


@app.get("/manifest/at")
def manifest_at(ref: str):
    """The manifest as of a version/branch, for 'Load from version' in the editor."""
    txt = git_ops.show_file(ref, "manifest.yaml")
    if txt is None:
        raise HTTPException(404, f"no manifest at {ref}")
    return {"ref": ref, "yaml": txt, "manifest": yaml.safe_load(txt)}


@app.on_event("startup")
def _startup():
    db.init()
    git_ops.ensure_repo()  # local git init, or clone if a Forgejo remote is configured


def _auth(token: str | None):
    if token != f"Bearer {config.AGENT_TOKEN}":
        raise HTTPException(401, "bad agent token")


@app.get("/whoami")
def whoami(request: Request, candidates: str = "", authorization: str | None = Header(None)):
    """A zero-config agent calls this once at startup to learn its identity + manifest
    folder. `candidates` is the agent's own list of local IPv4s: a dual-homed PC has
    several, so we pin it to whichever one the manifest actually knows — deterministic
    regardless of which interface routed this request. Fall back to the observed
    source IP (original behaviour) when no candidate matches / none were sent."""
    _auth(authorization)
    src = request.client.host if request.client else ""
    pcs = manifest.load_manifest().get("pcs", {})
    for ip in (c.strip() for c in candidates.split(",")):
        if ip and ip in pcs:
            return {"ip": ip, "folder": (pcs.get(ip) or {}).get("folder")}
    return {"ip": src, "folder": (pcs.get(src) or {}).get("folder")}


def _mirror_cmd(ip: str, ctype: str, ref: str) -> dict:
    """Build a deploy/track command for one PC. Reads the manifest AS OF `ref` so
    each version syncs its own file set; carries resolved apps + folder + git
    remote so the agent needs no local config."""
    return {"type": ctype, "ref": ref,
            "folder": manifest.pc_folder(ip, ref) or manifest.pc_folder(ip),
            "apps": manifest.resolved_apps(ip, ref),
            "git_remote": config.GIT_REMOTE}


def _enqueue_deploy_all():
    ips = list(manifest.load_manifest()["pcs"])
    _deploy_progress.clear()  # fresh deploy — start tracking sync per PC
    now = time.time()
    for ip in ips:
        _deploy_progress[ip] = {"state": "syncing", "at": now,
                                "expected": _last_deploy_dur.get(ip)}  # None until first timing
    manifest.enqueue_all(lambda ip: _mirror_cmd(ip, "deploy", config.TRAINING_LIVE))


# ===================== UI / operator endpoints ==========================
@app.get("/pcs")
def pcs():
    now = time.time()
    dp_view = {}
    for ip, d in _deploy_progress.items():
        v = dict(d)
        if d.get("state") == "syncing":
            v["elapsed"] = now - d.get("at", now)  # server-computed, no clock-sync needed
        dp_view[ip] = v
    agents = db.list_agents()
    for a in agents:
        a["error"] = _agent_errors.get(a["pc_ip"])  # last failure message, if any
    return {"agents": agents, "dev_lock": db.lock_holder(),
            "capture_progress": _capture_progress, "dismissed": db.list_dismissed(),
            "deploy_progress": dp_view}


@app.get("/versions")
def versions():
    live = git_ops.ref_sha(config.TRAINING_LIVE) if (config.WORK_CLONE / ".git").exists() else None
    return {"versions": db.list_versions(), "training_live_sha": live}


@app.get("/bootstrap")
def bootstrap_status():
    """Bootstrap panel data: per-PC import status + last size report + live progress."""
    return {"imports": db.list_imports(), "sizes": db.list_size_reports(),
            "progress": _import_progress}


# ---- host discovery / enrollment list ----------------------------------
def _enrich_hosts(rows):
    """Annotate discovered hosts with manifest/agent cross-references."""
    pcs = manifest.load_manifest()["pcs"]
    agent_ips = {a["pc_ip"] for a in db.list_agents()}
    for r in rows:
        ip = r["ip"]
        r["in_manifest"] = ip in pcs
        r["folder"] = pcs.get(ip, {}).get("folder")
        r["has_agent"] = ip in agent_ips
        r["listed"] = bool(r["listed"])
    return rows


class DiscoverReq(BaseModel):
    cidr: str | None = None   # default: /24 derived from the manifest PCs


@app.post("/discover")
def discover(req: DiscoverReq):
    """Ping-sweep the subnet, record live hosts, and return the list annotated with
    in_manifest / has_agent so you can see which PCs still need an agent."""
    cidr = req.cidr or discovery.default_cidr(list(manifest.load_manifest()["pcs"].keys()))
    if discovery.host_count(cidr) > discovery.MAX_HOSTS:
        raise HTTPException(400, f"range too large (> {discovery.MAX_HOSTS} hosts); use a smaller CIDR")
    found = discovery.scan(cidr)
    for h in found:
        db.upsert_discovered(h["ip"], h.get("hostname"), h.get("mac"))
    return {"cidr": cidr, "found": len(found), "hosts": _enrich_hosts(db.list_discovered())}


@app.get("/discover")
def discover_list():
    """Last known discovery results (no new scan)."""
    return {"hosts": _enrich_hosts(db.list_discovered())}


class HostReq(BaseModel):
    ip: str
    note: str | None = None


@app.post("/discover/add")
def discover_add(req: HostReq):
    """Add a host to the managed list (curate which machines you care about)."""
    db.set_listed(req.ip, True, req.note)
    return {"ip": req.ip, "listed": True}


@app.post("/discover/remove")
def discover_remove(req: HostReq):
    db.set_listed(req.ip, False)
    return {"ip": req.ip, "listed": False}


@app.get("/hosts")
def hosts():
    """The curated managed list (hosts the operator added)."""
    listed = [r for r in _enrich_hosts(db.list_discovered()) if r["listed"]]
    return {"hosts": listed}


@app.post("/import/{pc}")
def import_pc(pc: str):
    """Bootstrap: queue a read-only import. The agent walks its live dirs, applies
    excludes, and uploads the tree to /agents/{pc}/import-result. Nothing on the
    PC is modified."""
    apps = manifest.resolved_apps(pc)
    if not apps:
        raise HTTPException(400, f"{pc} has no folders selected — nothing to import")
    folder = manifest.pc_folder(pc)
    manifest.enqueue(pc, {"type": "import", "folder": folder, "apps": apps})
    return {"queued": True, "folder": folder}


@app.post("/import/{pc}/size-report")
def size_report(pc: str):
    folder = manifest.pc_folder(pc)
    manifest.enqueue(pc, {"type": "size_report", "folder": folder,
                          "apps": manifest.resolved_apps(pc)})
    return {"queued": True, "folder": folder}


class SealReq(BaseModel):
    message: str
    author: str


@app.post("/seal-baseline")
def seal_baseline(req: SealReq):
    sha = git_ops.seal_baseline(req.message, req.author)
    db.record_version("v1.0", req.message, req.author, sha or "")
    _enqueue_deploy_all()
    return {"tag": "v1.0", "sha": sha}


class DeployReq(BaseModel):
    ref: str = config.TRAINING_LIVE


@app.post("/deploy")
def deploy(req: DeployReq):
    if req.ref != config.TRAINING_LIVE:
        git_ops.rollback(req.ref)  # repoint training-live, then deploy it
    git_ops.publish_training_live()  # ensure the remote pointer == coordinator's
    _enqueue_deploy_all()
    return {"deploying": config.TRAINING_LIVE}


# ---- installable prerequisites (git, redists, …) ----------------------
def _load_installs():
    """Read the installs manifest (id/name/desc/file/type/args/target per entry)."""
    f = config.INSTALLS_DIR / "installs.json"
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


@app.get("/installs")
def installs():
    """List installable prerequisites + the last install result per PC."""
    return {"installs": _load_installs(), "results": _install_results}


@app.get("/installs/{iid}/file")
def install_file(iid: str, authorization: str | None = Header(None)):
    """Serve an install asset (the agent downloads this, then runs/unzips it)."""
    _auth(authorization)
    for it in _load_installs():
        if it.get("id") == iid:
            p = config.INSTALLS_DIR / it["file"]
            if p.exists():
                return FileResponse(p, filename=it["file"],
                                    media_type="application/octet-stream")
    raise HTTPException(404, "install not found")


class InstallReq(BaseModel):
    pc: str
    id: str


@app.post("/install")
def install(req: InstallReq):
    """Queue an install on one PC. The agent downloads the asset and either runs it
    (installer exe, with args) or unzips it to target (e.g. portable git)."""
    it = next((x for x in _load_installs() if x.get("id") == req.id), None)
    if not it:
        raise HTTPException(404, "unknown install")
    manifest.enqueue(req.pc, {
        "type": "install", "install_id": it["id"], "name": it.get("name", it["id"]),
        "url": f"/installs/{it['id']}/file", "file": it["file"],
        "install_type": it.get("type", "run"), "args": it.get("args", []),
        "target": it.get("target", "")})
    return {"queued": True, "pc": req.pc, "id": req.id}


@app.post("/agents/{pc_ip}/install-result")
def install_result(pc_ip: str, payload: dict = Body(...), authorization: str | None = Header(None)):
    _auth(authorization)
    _install_results[pc_ip] = {"id": payload.get("id"), "ok": bool(payload.get("ok")),
                               "msg": payload.get("msg", ""), "at": time.time()}
    return {"ok": True}


# ---- guards: per-PC compliance checks + fixes -------------------------
def _load_guards():
    f = config.GUARDS_DIR / "guards.json"
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _guard_cmd(item, kind):
    """Build a guard command: which script to run + (for apply) the assets to fetch."""
    script = item["check"] if kind == "check" else item["apply"]
    assets = [] if kind == "check" else [
        {"name": a, "url": f"/guards/file/{a}"} for a in item.get("assets", [])]
    return {"type": "guard", "guard_id": item["id"], "guard_kind": kind,
            "script_url": f"/guards/file/{script}", "script_name": script, "assets": assets}


@app.get("/guards")
def guards():
    items = _load_guards()
    for it in items:  # flag assets that haven't been uploaded yet
        it["assets_missing"] = [a for a in it.get("assets", [])
                                if not ((config.GUARDS_DIR / a).exists() or (config.INSTALLS_DIR / a).exists())]
    return {"guards": items, "results": _guard_results}


@app.put("/installs/asset/{name}")
async def upload_asset(name: str, request: Request):
    """Upload a guard/install asset (e.g. wallpaper.png) from the dashboard — the raw
    file body is saved into the installs dir under `name`."""
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "bad name")
    config.INSTALLS_DIR.mkdir(parents=True, exist_ok=True)
    data = await request.body()
    (config.INSTALLS_DIR / name).write_bytes(data)
    return {"ok": True, "name": name, "bytes": len(data)}


@app.get("/guards/file/{name}")
def guard_file(name: str, authorization: str | None = Header(None)):
    """Serve a guard script (from GUARDS_DIR) or asset (from INSTALLS_DIR)."""
    _auth(authorization)
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "bad name")
    for base in (config.GUARDS_DIR, config.INSTALLS_DIR):
        p = base / name
        if p.exists() and p.is_file():
            return FileResponse(p, filename=name, media_type="application/octet-stream")
    raise HTTPException(404, "guard file not found")


class GuardReq(BaseModel):
    pc: str
    id: str


@app.post("/guard/check")
def guard_check(req: GuardReq):
    item = next((x for x in _load_guards() if x["id"] == req.id), None)
    if not item:
        raise HTTPException(404, "unknown guard")
    manifest.enqueue(req.pc, _guard_cmd(item, "check"))
    return {"queued": True}


@app.post("/guard/apply")
def guard_apply(req: GuardReq):
    item = next((x for x in _load_guards() if x["id"] == req.id), None)
    if not item:
        raise HTTPException(404, "unknown guard")
    _guard_results.setdefault(req.pc, {})[req.id] = {"pass": None, "detail": "applying…", "at": time.time()}
    manifest.enqueue(req.pc, _guard_cmd(item, "apply"))
    return {"queued": True}


@app.post("/guard/check-all")
def guard_check_all(payload: dict = Body(...)):
    pc = payload.get("pc")
    for item in _load_guards():
        manifest.enqueue(pc, _guard_cmd(item, "check"))
    return {"queued": True, "count": len(_load_guards())}


@app.post("/agents/{pc_ip}/guard-result")
def guard_result(pc_ip: str, payload: dict = Body(...), authorization: str | None = Header(None)):
    _auth(authorization)
    gid, kind, ok = payload.get("id"), payload.get("kind"), bool(payload.get("ok"))
    detail = payload.get("detail", "")
    slot = _guard_results.setdefault(pc_ip, {})
    if kind == "check":
        slot[gid] = {"pass": ok, "detail": detail, "at": time.time()}
    else:  # apply: mark pending + re-check to refresh pass/fail
        slot[gid] = {"pass": None, "detail": ("applied — re-checking" if ok else "apply failed: " + detail),
                     "at": time.time()}
        item = next((x for x in _load_guards() if x["id"] == gid), None)
        if item:
            manifest.enqueue(pc_ip, _guard_cmd(item, "check"))
    return {"ok": True}


# ---- remote agent self-update -----------------------------------------
@app.get("/agent/binary")
def agent_binary(authorization: str | None = Header(None)):
    """Serve the current agent build (agents download this on `update`)."""
    _auth(authorization)
    if not config.AGENT_BINARY.exists():
        raise HTTPException(404, "agent binary not built — run deploy/build-agent.sh")
    return FileResponse(config.AGENT_BINARY, filename="simagent.exe",
                        media_type="application/octet-stream")


@app.post("/agents/{pc_ip}/update")
def agent_update(pc_ip: str):
    """Mark an agent to self-update. It checks this at startup (before sync) and
    on each poll, updates + relaunches, then acks to clear the flag."""
    _update_pending.add(pc_ip)
    return {"requested": True}


@app.post("/agents/update")
def agents_update_all():
    """Mark every PC in the manifest to self-update."""
    for ip in manifest.load_manifest()["pcs"]:
        _update_pending.add(ip)
    return {"requested": True}


@app.post("/agents/{pc_ip}/forget")
def agent_forget(pc_ip: str):
    """Drop a PC's agent record from PC status. If that PC's agent is still alive
    it re-registers on its next heartbeat (~10s); stale/renamed IPs stay gone."""
    db.forget_agent(pc_ip)
    return {"forgotten": pc_ip}


@app.get("/agents/{pc_ip}/update-pending")
def update_pending(pc_ip: str, authorization: str | None = Header(None)):
    _auth(authorization)
    return {"pending": pc_ip in _update_pending}


@app.post("/agents/{pc_ip}/ack-update")
def ack_update(pc_ip: str, authorization: str | None = Header(None)):
    """Agent confirms it has consumed the update request (clears the flag)."""
    _auth(authorization)
    _update_pending.discard(pc_ip)
    return {"ok": True}


class DevStartReq(BaseModel):
    user: str


@app.post("/dev/start")
def dev_start(req: DevStartReq):
    if not db.acquire_lock(req.user):
        raise HTTPException(409, f"dev session held by {db.lock_holder()}")
    manifest.enqueue_all(lambda ip: _mirror_cmd(ip, "track", config.DEV_BRANCH))
    return {"locked_by": req.user}


@app.post("/dev/end")
def dev_end():
    db.release_lock()
    return {"released": True}


@app.get("/dev/versions")
def dev_versions():
    """Admin: list of dev test snapshots (dev-N), with the currently-live one
    flagged by sha so you can see what the sim is testing."""
    live = git_ops.ref_sha(config.TRAINING_LIVE) if (config.WORK_CLONE / ".git").exists() else None
    return {"versions": db.list_dev_versions(), "training_live_sha": live}


def _dev_readiness():
    """Per-PC readiness for creating a dev version: has each PC in the load had its
    content imported to dev since the last manifest change? Gates 'Deploy as Dev' so
    a version can't be cut with a folder picked but never imported."""
    pcs = manifest.load_manifest().get("pcs", {})
    imports = {i["pc_ip"]: i for i in db.list_imports()}
    agents = {a["pc_ip"]: a for a in db.list_agents()}
    saved = float(db.get_meta("manifest_saved_at") or 0)
    dev_exists = git_ops.ref_sha(config.DEV_BRANCH) is not None  # need v1.0 sealed first
    rows, all_ready = [], True
    for ip, spec in pcs.items():
        if not (spec.get("apps") or {}):
            continue  # a PC with no folders selected isn't part of this load
        imp = imports.get(ip)
        imported_at = imp["imported_at"] if imp else None
        # A dev version captures fresh content, so an import only counts if it
        # happened AFTER the load was last saved. saved==0 -> save the config first.
        ready = bool(imported_at) and saved > 0 and imported_at >= saved
        stale = bool(imported_at) and saved > 0 and imported_at < saved
        if not ready:
            all_ready = False
        rows.append({"ip": ip, "folder": spec.get("folder"),
                     "online": bool(agents.get(ip, {}).get("online")),
                     "imported_at": imported_at, "stale": stale, "ready": ready})
    return {"pcs": rows, "ready": all_ready and bool(rows) and dev_exists,
            "manifest_saved_at": saved}


@app.get("/dev/readiness")
def dev_readiness():
    return _dev_readiness()


class SnapshotReq(BaseModel):
    message: str
    author: str


@app.post("/dev/snapshot")
def dev_snapshot(req: SnapshotReq):
    """Freeze the current dev tip as a named test version (dev-vN) to deploy to the
    sim for testing before promoting to a customer training version. Gated on
    readiness so content is never skipped."""
    r = _dev_readiness()
    if not r["ready"]:
        missing = [p["ip"] for p in r["pcs"] if not p["ready"]]
        raise HTTPException(409, "import content first for: " + ", ".join(missing))
    # Only the load's PC folders belong in the version — drop leftovers (e.g. a PC
    # that was removed from the load but whose content still lingers on dev).
    pcs = manifest.load_manifest().get("pcs", {})
    folders = {spec.get("folder") for spec in pcs.values() if (spec.get("apps") or {})}
    git_ops.prune_to_folders(folders)
    tag = db.next_dev_tag()
    sha = git_ops.snapshot_dev(tag, req.message, req.author)
    db.record_dev_version(tag, req.message, req.author, sha or "")
    return {"tag": tag, "sha": sha}


class CaptureReq(BaseModel):
    pc: str


@app.post("/dev/capture")
def dev_capture(req: CaptureReq):
    if not db.lock_holder():
        raise HTTPException(409, "no active dev session")
    manifest.enqueue(req.pc, {"type": "capture", "ref": config.DEV_BRANCH,
                              "folder": manifest.pc_folder(req.pc),
                              "apps": manifest.resolved_apps(req.pc),
                              "git_remote": config.GIT_REMOTE})
    return {"capturing": req.pc}


@app.get("/diff")
def diff(base: str, head: str = config.DEV_BRANCH):
    return {"changed": git_ops.changed_files(base, head),
            "compare_url": git_ops.compare_url(base, head)}


class PromoteReq(BaseModel):
    message: str
    author: str
    from_ref: str | None = None  # a specific dev-N build to promote (default: dev tip)


@app.post("/promote")
def promote(req: PromoteReq):
    tag = db.next_version_tag()
    sha = git_ops.promote(req.message, req.author, tag, req.from_ref)
    db.record_version(tag, req.message, req.author, sha or "")
    _enqueue_deploy_all()
    return {"tag": tag, "sha": sha}


class RollbackReq(BaseModel):
    tag: str


@app.post("/rollback")
def rollback(req: RollbackReq):
    git_ops.rollback(req.tag)
    _enqueue_deploy_all()
    return {"training_live": req.tag}


# ===================== agent-facing endpoints ===========================
class Heartbeat(BaseModel):
    pc_ip: str
    folder: str
    mode: str
    current_ref: str | None = None
    clean: bool = True
    version: str | None = None
    error: str | None = None


@app.post("/agents/{pc_ip}/heartbeat")
def heartbeat(pc_ip: str, hb: Heartbeat, authorization: str | None = Header(None)):
    _auth(authorization)
    db.upsert_agent(hb.pc_ip, hb.folder, hb.mode, hb.current_ref, hb.clean, hb.version)
    _agent_errors[hb.pc_ip] = hb.error if (hb.mode == "ERROR" and hb.error) else None
    dp = _deploy_progress.get(hb.pc_ip)
    if dp and dp.get("state") == "syncing" and hb.mode == "ERROR":
        _deploy_progress[hb.pc_ip] = {"state": "fail", "at": time.time(),
                                      "error": hb.error}  # deploy failed
    return {"ok": True}


@app.get("/agents/{pc_ip}/commands")
def commands(pc_ip: str, authorization: str | None = Header(None)):
    _auth(authorization)
    return {"command": manifest.wait_command(pc_ip)}  # long-poll up to ~25s


@app.get("/agents/{pc_ip}/enforce")
def enforce(pc_ip: str, authorization: str | None = Header(None)):
    """Current training-live deploy command for this PC (used by the agent at
    startup to sync + launch before anything runs). Null until v1.0 is sealed or
    the PC isn't in the manifest."""
    _auth(authorization)
    if pc_ip not in manifest.load_manifest()["pcs"]:
        return {"command": None}
    if not git_ops.ref_sha(config.TRAINING_LIVE):
        return {"command": None}
    return {"command": _mirror_cmd(pc_ip, "deploy", config.TRAINING_LIVE)}


@app.post("/agents/{pc_ip}/import-result")
def import_result(pc_ip: str, payload: dict = Body(...), authorization: str | None = Header(None)):
    """Phase 1 bootstrap upload, streamed in batches to bound memory. batch_index
    0 clears the folder; final=true records the import. No commit until /seal."""
    _auth(authorization)
    folder = payload["folder"]
    files = {p: base64.b64decode(b) for p, b in payload.get("files", {}).items()}
    # Before v1.0 there's no dev branch: stage into the working tree for the seal.
    # After v1.0, import lands on dev and is committed, so it can feed a dev version.
    dev_mode = git_ops.ref_sha(config.DEV_BRANCH) is not None
    if payload.get("batch_index", 0) == 0:
        if dev_mode:
            git_ops.dev_import_begin(folder)
        else:
            git_ops.clear_folder(folder)
        _import_progress[pc_ip] = {"folder": folder, "total_bytes": payload.get("total_bytes", 0),
                                   "received_bytes": 0, "received_files": 0, "done": False}
    git_ops.write_files(files)
    prog = _import_progress.setdefault(pc_ip, {"folder": folder, "total_bytes": payload.get("total_bytes", 0),
                                               "received_bytes": 0, "received_files": 0, "done": False})
    prog["received_bytes"] += sum(len(b) for b in files.values())
    prog["received_files"] += len(files)
    if payload.get("final", True):
        if dev_mode:
            git_ops.dev_import_commit(folder, f"import content for {pc_ip}", "import")
        prog["done"] = True
        n, b = git_ops.folder_stats(folder)
        db.record_import(pc_ip, folder, n, b, payload.get("missing", []))
    return {"staged_files": len(files), "batch": payload.get("batch_index", 0)}


@app.post("/agents/{pc_ip}/size-report-result")
def size_report_result(pc_ip: str, payload: dict = Body(...), authorization: str | None = Header(None)):
    """Per-app byte sizes for the bootstrap panel (review before sealing)."""
    _auth(authorization)
    db.record_size_report(pc_ip, payload.get("folder", ""), payload.get("sizes", {}))
    return {"ok": True}


# ---- filesystem browser (config panel) ----------------------------------
@app.get("/agents/{pc_ip}/browse")
def browse(pc_ip: str, path: str = ""):
    """Operator endpoint: ask the agent to list `path` (or drives if empty) and
    block until it answers. Powers the config-panel file tree."""
    req_id = uuid.uuid4().hex
    ev = threading.Event()
    _browse_requests[req_id] = {"event": ev, "result": None}
    try:
        manifest.enqueue(pc_ip, {"type": "browse", "req_id": req_id, "path": path})
        if not ev.wait(12) or _browse_requests[req_id]["result"] is None:
            raise HTTPException(504, "agent did not respond (offline or busy)")
        return _browse_requests[req_id]["result"]
    finally:
        _browse_requests.pop(req_id, None)


@app.post("/agents/{pc_ip}/browse-result")
def browse_result(pc_ip: str, payload: dict = Body(...), authorization: str | None = Header(None)):
    _auth(authorization)
    req = _browse_requests.get(payload.get("req_id"))
    if req is not None:
        req["result"] = {"path": payload.get("path", ""),
                         "entries": payload.get("entries", []),
                         "error": payload.get("error") or None}
        req["event"].set()
    return {"ok": True}


@app.get("/agents/{pc_ip}/drift")
def drift(pc_ip: str):
    """PC status 'diff': ask the agent which files differ between the deployed
    version and live, and block until it answers."""
    req_id = uuid.uuid4().hex
    ev = threading.Event()
    _drift_requests[req_id] = {"event": ev, "result": None}
    try:
        manifest.enqueue(pc_ip, {
            "type": "drift", "req_id": req_id,
            "folder": manifest.pc_folder(pc_ip, config.TRAINING_LIVE) or manifest.pc_folder(pc_ip),
            "apps": manifest.resolved_apps(pc_ip, config.TRAINING_LIVE),
            "git_remote": config.GIT_REMOTE})
        if not ev.wait(25) or _drift_requests[req_id]["result"] is None:
            raise HTTPException(504, "agent did not respond (offline or busy)")
        return _drift_requests[req_id]["result"]
    finally:
        _drift_requests.pop(req_id, None)


@app.post("/agents/{pc_ip}/drift-result")
def drift_result(pc_ip: str, payload: dict = Body(...), authorization: str | None = Header(None)):
    _auth(authorization)
    req = _drift_requests.get(payload.get("req_id"))
    if req is not None:
        req["result"] = {"entries": payload.get("entries") or []}
        req["event"].set()
    return {"ok": True}


@app.get("/agents/{pc_ip}/filediff")
def filediff(pc_ip: str, app: str, path: str):
    """Read one drifted file (version vs live) so the UI can show a line diff."""
    req_id = uuid.uuid4().hex
    ev = threading.Event()
    _filediff_requests[req_id] = {"event": ev, "result": None}
    try:
        manifest.enqueue(pc_ip, {
            "type": "filediff", "req_id": req_id, "diff_app": app, "diff_path": path,
            "apps": manifest.resolved_apps(pc_ip, config.TRAINING_LIVE),
            "git_remote": config.GIT_REMOTE})
        if not ev.wait(20) or _filediff_requests[req_id]["result"] is None:
            raise HTTPException(504, "agent did not respond (offline or busy)")
        return _filediff_requests[req_id]["result"]
    finally:
        _filediff_requests.pop(req_id, None)


@app.post("/agents/{pc_ip}/filediff-result")
def filediff_result(pc_ip: str, payload: dict = Body(...), authorization: str | None = Header(None)):
    _auth(authorization)
    req = _filediff_requests.get(payload.get("req_id"))
    if req is not None:
        req["result"] = payload.get("diff") or {}
        req["event"].set()
    return {"ok": True}


@app.post("/agents/{pc_ip}/capture-result")
def capture_result(pc_ip: str, payload: dict = Body(...), authorization: str | None = Header(None)):
    """Apply a dev-capture diff to dev, streamed in batches. batch 0 checks out
    dev; the final batch applies deletions + commits (attributed to the lock
    holder). Serialized with other git writes so multi-PC captures don't race."""
    _auth(authorization)
    files = {p: base64.b64decode(b) for p, b in payload.get("files", {}).items()}
    folder = payload.get("folder", "")
    if payload.get("batch_index", 0) == 0:
        git_ops.capture_begin()
        _capture_progress[pc_ip] = {"folder": folder, "total_bytes": payload.get("total_bytes", 0),
                                    "received_bytes": 0, "done": False}
    git_ops.capture_write(files)
    prog = _capture_progress.setdefault(pc_ip, {"folder": folder, "total_bytes": payload.get("total_bytes", 0),
                                                "received_bytes": 0, "done": False})
    prog["received_bytes"] += sum(len(b) for b in files.values())
    result = {"batch": payload.get("batch_index", 0)}
    if payload.get("final", True):
        author = db.lock_holder() or "dev"
        message = payload.get("message") or f"dev capture from {pc_ip} ({folder})"
        result["committed"] = git_ops.capture_commit(payload.get("deleted") or [], message, author)
        prog["done"] = True
    return result


@app.post("/agents/{pc_ip}/deploy-result")
def deploy_result(pc_ip: str, payload: dict = Body(...), authorization: str | None = Header(None)):
    _auth(authorization)
    db.upsert_agent(pc_ip, payload.get("folder", ""), payload.get("mode", "TRAINING"),
                    payload.get("ref"), payload.get("clean", True))
    dp = _deploy_progress.get(pc_ip)
    if dp and dp.get("state") == "syncing":
        _last_deploy_dur[pc_ip] = time.time() - dp["at"]  # remember for the next ETA
    _deploy_progress[pc_ip] = {"state": "ok", "at": time.time()}  # this PC finished syncing
    return {"ok": True}
