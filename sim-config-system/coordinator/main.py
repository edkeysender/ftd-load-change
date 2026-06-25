"""Coordinator API. Run: uvicorn coordinator.main:app --host 0.0.0.0 --port 8090

The coordinator is the single git writer and the orchestration brain. Agents are
read-only git clients that poll /agents/{ip}/commands and post results.
"""
import base64
from fastapi import FastAPI, HTTPException, Header, Body
from pydantic import BaseModel
from . import config, db, discovery, git_ops, manifest

app = FastAPI(title="Sim Config Coordinator")


@app.on_event("startup")
def _startup():
    db.init()
    git_ops.ensure_repo()  # local git init, or clone if a Forgejo remote is configured


def _auth(token: str | None):
    if token != f"Bearer {config.AGENT_TOKEN}":
        raise HTTPException(401, "bad agent token")


def _mirror_cmd(ip: str, ctype: str, ref: str) -> dict:
    """Build a deploy/track command for one PC. Carries the resolved apps (with
    merged excludes) + folder so the agent can mirror worktree->live and restart
    without needing the manifest locally."""
    return {"type": ctype, "ref": ref,
            "folder": manifest.pc_folder(ip),
            "apps": manifest.resolved_apps(ip)}


def _enqueue_deploy_all():
    manifest.enqueue_all(lambda ip: _mirror_cmd(ip, "deploy", config.TRAINING_LIVE))


# ===================== UI / operator endpoints ==========================
@app.get("/pcs")
def pcs():
    return {"agents": db.list_agents(), "dev_lock": db.lock_holder()}


@app.get("/versions")
def versions():
    live = git_ops.head_sha(config.TRAINING_LIVE) if (config.WORK_CLONE / ".git").exists() else None
    return {"versions": db.list_versions(), "training_live_sha": live}


@app.get("/bootstrap")
def bootstrap_status():
    """Bootstrap panel data: per-PC import status + last size report."""
    return {"imports": db.list_imports(), "sizes": db.list_size_reports()}


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
    folder = manifest.pc_folder(pc)
    manifest.enqueue(pc, {"type": "import", "folder": folder,
                          "apps": manifest.resolved_apps(pc)})
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
    _enqueue_deploy_all()
    return {"deploying": config.TRAINING_LIVE}


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


class CaptureReq(BaseModel):
    pc: str


@app.post("/dev/capture")
def dev_capture(req: CaptureReq):
    if not db.lock_holder():
        raise HTTPException(409, "no active dev session")
    manifest.enqueue(req.pc, {"type": "capture", "folder": manifest.pc_folder(req.pc)})
    return {"capturing": req.pc}


@app.get("/diff")
def diff(base: str, head: str = config.DEV_BRANCH):
    return {"changed": git_ops.changed_files(base, head),
            "compare_url": git_ops.compare_url(base, head)}


class PromoteReq(BaseModel):
    message: str
    author: str


@app.post("/promote")
def promote(req: PromoteReq):
    if db.lock_holder() and db.lock_holder() != req.author:
        raise HTTPException(409, f"dev session held by {db.lock_holder()}")
    tag = db.next_version_tag()
    sha = git_ops.promote(req.message, req.author, tag)
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


@app.post("/agents/{pc_ip}/heartbeat")
def heartbeat(pc_ip: str, hb: Heartbeat, authorization: str | None = Header(None)):
    _auth(authorization)
    db.upsert_agent(hb.pc_ip, hb.folder, hb.mode, hb.current_ref, hb.clean)
    return {"ok": True}


@app.get("/agents/{pc_ip}/commands")
def commands(pc_ip: str, authorization: str | None = Header(None)):
    _auth(authorization)
    # TODO: convert to true long-poll (block up to ~25s waiting for a command).
    return {"command": manifest.pop(pc_ip)}


@app.post("/agents/{pc_ip}/import-result")
def import_result(pc_ip: str, payload: dict = Body(...), authorization: str | None = Header(None)):
    """Phase 1 bootstrap upload. Stage the PC's tree into the working clone
    (idempotent: the folder is replaced). No commit happens until /seal-baseline."""
    _auth(authorization)
    folder = payload["folder"]
    files = {p: base64.b64decode(b) for p, b in payload.get("files", {}).items()}
    git_ops.stage_import_bundle(folder, files)
    db.record_import(pc_ip, folder, len(files),
                     sum(len(b) for b in files.values()),
                     payload.get("missing", []))
    return {"staged_files": len(files), "folder": folder}


@app.post("/agents/{pc_ip}/size-report-result")
def size_report_result(pc_ip: str, payload: dict = Body(...), authorization: str | None = Header(None)):
    """Per-app byte sizes for the bootstrap panel (review before sealing)."""
    _auth(authorization)
    db.record_size_report(pc_ip, payload.get("folder", ""), payload.get("sizes", {}))
    return {"ok": True}


@app.post("/agents/{pc_ip}/capture-result")
def capture_result(pc_ip: str, payload: dict = Body(...), authorization: str | None = Header(None)):
    _auth(authorization)
    # payload: {folder, message, author, files: {rel_path: base64}}
    files = {p: base64.b64decode(b) for p, b in payload["files"].items()}
    sha = git_ops.apply_capture_bundle(payload["folder"], files,
                                        payload["message"], payload["author"])
    return {"committed": sha}


@app.post("/agents/{pc_ip}/deploy-result")
def deploy_result(pc_ip: str, payload: dict = Body(...), authorization: str | None = Header(None)):
    _auth(authorization)
    db.upsert_agent(pc_ip, payload.get("folder", ""), payload.get("mode", "TRAINING"),
                    payload.get("ref"), payload.get("clean", True))
    return {"ok": True}
