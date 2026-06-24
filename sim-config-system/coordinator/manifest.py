"""Manifest loading and the per-agent command queue.

Commands are pulled by agents via GET /agents/{ip}/commands (long-poll).
For the scaffold the queue is in-memory; persist to SQLite if the coordinator
must survive restarts mid-deploy.
"""
import yaml
from collections import defaultdict, deque
from . import config

_pending = defaultdict(deque)  # pc_ip -> deque of command dicts


def load_manifest():
    with open(config.MANIFEST_PATH) as f:
        return yaml.safe_load(f)


def pc_folder(pc_ip):
    return load_manifest()["pcs"][pc_ip]["folder"]


def resolved_apps(pc_ip: str) -> dict:
    """Apps for a PC with the default exclude set merged into each app's own
    excludes (defaults first, dedup, order preserved). This is what the agent
    receives so it never needs the manifest defaults locally."""
    m = load_manifest()
    defaults = m.get("defaults", {}).get("exclude", []) or []
    apps = {}
    for name, spec in m["pcs"][pc_ip]["apps"].items():
        spec = dict(spec)
        merged, seen = [], set()
        for pat in defaults + (spec.get("exclude") or []):
            if pat not in seen:
                seen.add(pat)
                merged.append(pat)
        spec["exclude"] = merged
        spec.setdefault("live", spec.get("live") or [])
        apps[name] = spec
    return apps


def enqueue(pc_ip, command: dict):
    _pending[pc_ip].append(command)


def enqueue_all(command_fn):
    """command_fn(pc_ip) -> command dict, queued for every PC in the manifest."""
    for pc_ip in load_manifest()["pcs"]:
        _pending[pc_ip].append(command_fn(pc_ip))


def pop(pc_ip):
    return _pending[pc_ip].popleft() if _pending[pc_ip] else None
