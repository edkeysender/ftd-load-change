"""Manifest loading and the per-agent command queue.

Commands are pulled by agents via GET /agents/{ip}/commands (long-poll).
For the scaffold the queue is in-memory; persist to SQLite if the coordinator
must survive restarts mid-deploy.
"""
import threading
import yaml
from collections import defaultdict, deque
from . import config

_pending = defaultdict(deque)            # pc_ip -> deque of command dicts
_events = defaultdict(threading.Event)   # pc_ip -> wakeup for the long-poll


def load_manifest():
    with open(config.MANIFEST_PATH) as f:
        return yaml.safe_load(f)


def load_manifest_at(ref):
    """The manifest as of a git ref (so deploying a version uses THAT version's
    sync config). Falls back to the working-tree manifest if the ref has none."""
    from . import git_ops
    txt = git_ops.show_file(ref, "manifest.yaml")
    return yaml.safe_load(txt) if txt else load_manifest()


def _manifest(ref=None):
    return load_manifest_at(ref) if ref else load_manifest()


def pc_folder(pc_ip, ref=None):
    return _manifest(ref).get("pcs", {}).get(pc_ip, {}).get("folder")


def pc_transport(pc_ip, ref=None):
    """How the coordinator reaches this PC: "agent" (a Windows PC running the Go agent,
    the default and every pre-existing entry) or "ssh" (an agentless Linux device the
    coordinator drives itself).

    Read per-ref like everything else here, so a device that was an agent PC at v1.0 and
    an SSH device at v1.3 deploys correctly at both.
    """
    spec = _manifest(ref).get("pcs", {}).get(pc_ip) or {}
    return (spec.get("transport") or "agent").strip().lower()


def ssh_pcs(ref=None):
    """IPs in the manifest that are managed over SSH."""
    return [ip for ip, spec in (_manifest(ref).get("pcs") or {}).items()
            if ((spec or {}).get("transport") or "agent").strip().lower() == "ssh"]


def load_global_ignore():
    """The global-ignore glob list — applies to every app on every PC. Reads the
    writable file, falling back to the bundled seed. Always a list of strings."""
    from . import config
    for path in (config.GLOBAL_IGNORE, config.SEED_GLOBAL_IGNORE):
        try:
            if path and path.exists():
                data = yaml.safe_load(path.read_text()) or []
                return [str(p) for p in data if p]
        except Exception:
            pass
    return []


def resolved_apps(pc_ip: str, ref=None) -> dict:
    """Apps for a PC with the global-ignore + manifest defaults merged into each
    app's own excludes (in that order, dedup, order preserved). Read from `ref`'s
    manifest when given. Empty if the PC isn't in that manifest."""
    m = _manifest(ref)
    pc = m.get("pcs", {}).get(pc_ip)
    if not pc:
        return {}
    defaults = m.get("defaults", {}).get("exclude", []) or []
    global_ignore = load_global_ignore()
    apps = {}
    for name, spec in (pc.get("apps") or {}).items():
        spec = dict(spec)
        merged, seen = [], set()
        for pat in global_ignore + defaults + (spec.get("exclude") or []):
            if pat not in seen:
                seen.add(pat)
                merged.append(pat)
        spec["exclude"] = merged
        spec.setdefault("live", spec.get("live") or [])
        apps[name] = spec
    return apps


def enqueue(pc_ip, command: dict):
    _pending[pc_ip].append(command)
    _events[pc_ip].set()  # wake any long-poll waiting on this agent


def enqueue_all(command_fn):
    """command_fn(pc_ip) -> command dict, queued for every PC in the manifest."""
    for pc_ip in load_manifest()["pcs"]:
        enqueue(pc_ip, command_fn(pc_ip))


def pop(pc_ip):
    return _pending[pc_ip].popleft() if _pending[pc_ip] else None


def wait_command(pc_ip, timeout=25.0):
    """Long-poll: return the next command for an agent, blocking up to `timeout`
    seconds until one is enqueued. Returns None on timeout. Replaces the old
    busy-poll so agents don't hammer the coordinator."""
    ev = _events[pc_ip]
    cmd = pop(pc_ip)
    if cmd is not None:
        return cmd
    ev.clear()
    cmd = pop(pc_ip)          # re-check after clear to avoid a lost wakeup
    if cmd is not None:
        return cmd
    ev.wait(timeout)
    return pop(pc_ip)
