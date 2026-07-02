"""SQLite state: agent registry, version history, dev-session lock, deploy log."""
import json
import sqlite3
import time
from contextlib import contextmanager
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    pc_ip        TEXT PRIMARY KEY,
    folder       TEXT,
    mode         TEXT,              -- UNSEEDED | IDLE | TRAINING | DEV_TRACKING | DEPLOYING | CAPTURING | ERROR
    current_ref  TEXT,
    clean        INTEGER,           -- 1 = matches its ref, 0 = drifted
    last_seen    REAL,
    version      TEXT               -- agent build version (git sha), from heartbeat
);
CREATE TABLE IF NOT EXISTS versions (
    tag        TEXT PRIMARY KEY,    -- v1.0, v1.1, ...
    message    TEXT,
    author     TEXT,
    commit_sha TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS dev_session (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    holder     TEXT,                -- user who owns the lock; NULL = no active session
    started_at REAL
);
CREATE TABLE IF NOT EXISTS dev_versions (
    tag        TEXT PRIMARY KEY,    -- dev-1, dev-2, ... (test snapshots of dev)
    message    TEXT,
    author     TEXT,
    commit_sha TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS deploys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ref        TEXT,
    results    TEXT,                -- JSON: {pc_ip: "ok"|"fail:reason"}
    created_at REAL
);
CREATE TABLE IF NOT EXISTS imports (
    pc_ip       TEXT PRIMARY KEY,   -- last bootstrap import per PC
    folder      TEXT,
    file_count  INTEGER,
    byte_count  INTEGER,
    missing     TEXT,               -- JSON list of missing live dirs (flagged for review)
    imported_at REAL
);
CREATE TABLE IF NOT EXISTS size_reports (
    pc_ip       TEXT PRIMARY KEY,   -- last du -sh report per PC
    folder      TEXT,
    sizes       TEXT,               -- JSON: {app: bytes}
    reported_at REAL
);
CREATE TABLE IF NOT EXISTS dismissed_pcs (
    pc_ip      TEXT PRIMARY KEY,    -- manually removed from PC status; cleared when
    dismissed_at REAL               -- the agent next heartbeats (so it reappears)
);
CREATE TABLE IF NOT EXISTS discovered_hosts (
    ip         TEXT PRIMARY KEY,    -- host found on the LAN (or manually added)
    hostname   TEXT,
    mac        TEXT,
    listed     INTEGER DEFAULT 0,   -- 1 = operator added it to the managed list
    note       TEXT,
    first_seen REAL,
    last_seen  REAL
);
INSERT OR IGNORE INTO dev_session (id, holder, started_at) VALUES (1, NULL, NULL);
"""


@contextmanager
def conn():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        # Migration for DBs created before the agents.version column existed.
        try:
            c.execute("ALTER TABLE agents ADD COLUMN version TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists


# --- agents -------------------------------------------------------------
def upsert_agent(pc_ip, folder, mode, current_ref, clean, version=None):
    # version is COALESCE'd so callers that don't know it (deploy-result) don't wipe it.
    with conn() as c:
        c.execute(
            """INSERT INTO agents (pc_ip, folder, mode, current_ref, clean, last_seen, version)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(pc_ip) DO UPDATE SET
                 folder=excluded.folder, mode=excluded.mode,
                 current_ref=excluded.current_ref, clean=excluded.clean,
                 last_seen=excluded.last_seen,
                 version=COALESCE(excluded.version, agents.version)""",
            (pc_ip, folder, mode, current_ref, int(clean), time.time(), version),
        )
        # A detected agent un-hides a PC that was manually removed from PC status.
        c.execute("DELETE FROM dismissed_pcs WHERE pc_ip=?", (pc_ip,))


def list_agents():
    now = time.time()
    with conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM agents")]
    for r in rows:
        r["online"] = (now - (r["last_seen"] or 0)) < config.HEARTBEAT_TIMEOUT
    return rows


def forget_agent(pc_ip):
    """Remove a PC from PC status: drop its agent row AND mark it dismissed so it
    stays hidden even if it's a manifest PC. The next heartbeat from that IP clears
    the dismissal (see upsert_agent), so a live/returning agent reappears."""
    with conn() as c:
        c.execute("DELETE FROM agents WHERE pc_ip=?", (pc_ip,))
        c.execute("INSERT OR REPLACE INTO dismissed_pcs (pc_ip, dismissed_at) VALUES (?,?)",
                  (pc_ip, time.time()))


def list_dismissed():
    with conn() as c:
        return [r["pc_ip"] for r in c.execute("SELECT pc_ip FROM dismissed_pcs")]


# --- dev-session lock ---------------------------------------------------
def acquire_lock(user):
    with conn() as c:
        row = c.execute("SELECT holder FROM dev_session WHERE id=1").fetchone()
        if row["holder"] and row["holder"] != user:
            return False
        c.execute("UPDATE dev_session SET holder=?, started_at=? WHERE id=1", (user, time.time()))
        return True


def release_lock():
    with conn() as c:
        c.execute("UPDATE dev_session SET holder=NULL, started_at=NULL WHERE id=1")


def lock_holder():
    with conn() as c:
        return c.execute("SELECT holder FROM dev_session WHERE id=1").fetchone()["holder"]


# --- versions -----------------------------------------------------------
def record_version(tag, message, author, commit_sha):
    # OR IGNORE: re-sealing/idempotent calls must not collide on the tag PK.
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO versions (tag, message, author, commit_sha, created_at) VALUES (?,?,?,?,?)",
            (tag, message, author, commit_sha, time.time()),
        )


def list_versions():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM versions ORDER BY created_at DESC")]


def next_version_tag():
    """Compute v(N+1) as a simple minor bump from the latest vMAJOR.MINOR."""
    vers = list_versions()
    if not vers:
        return "v1.0"
    latest = vers[0]["tag"].lstrip("v")
    major, minor = (latest.split(".") + ["0"])[:2]
    return f"v{major}.{int(minor) + 1}"


# --- dev test versions (snapshots of the dev branch) --------------------
def record_dev_version(tag, message, author, commit_sha):
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO dev_versions (tag, message, author, commit_sha, created_at) VALUES (?,?,?,?,?)",
            (tag, message, author, commit_sha, time.time()),
        )


def list_dev_versions():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM dev_versions ORDER BY created_at DESC")]


def next_dev_tag():
    """A dev build is named after the training version it works toward:
    dev-v<next>. One dev build per target version (re-snapshotting the same target
    overwrites it), so no numeric suffix."""
    return "dev-" + next_version_tag()


# --- bootstrap: imports + size reports ----------------------------------
def record_import(pc_ip, folder, file_count, byte_count, missing):
    with conn() as c:
        c.execute(
            """INSERT INTO imports (pc_ip, folder, file_count, byte_count, missing, imported_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(pc_ip) DO UPDATE SET
                 folder=excluded.folder, file_count=excluded.file_count,
                 byte_count=excluded.byte_count, missing=excluded.missing,
                 imported_at=excluded.imported_at""",
            (pc_ip, folder, file_count, byte_count, json.dumps(missing or []), time.time()),
        )


def list_imports():
    with conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM imports")]
    for r in rows:
        r["missing"] = json.loads(r["missing"] or "[]")
    return rows


def record_size_report(pc_ip, folder, sizes):
    with conn() as c:
        c.execute(
            """INSERT INTO size_reports (pc_ip, folder, sizes, reported_at)
               VALUES (?,?,?,?)
               ON CONFLICT(pc_ip) DO UPDATE SET
                 folder=excluded.folder, sizes=excluded.sizes,
                 reported_at=excluded.reported_at""",
            (pc_ip, folder, json.dumps(sizes or {}), time.time()),
        )


def list_size_reports():
    with conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM size_reports")]
    for r in rows:
        r["sizes"] = json.loads(r["sizes"] or "{}")
    return rows


# --- discovered hosts ---------------------------------------------------
def upsert_discovered(ip, hostname, mac):
    """Record/refresh a host from a network scan. Preserves listed/note/first_seen."""
    now = time.time()
    with conn() as c:
        c.execute(
            """INSERT INTO discovered_hosts (ip, hostname, mac, first_seen, last_seen)
               VALUES (?,?,?,?,?)
               ON CONFLICT(ip) DO UPDATE SET
                 hostname=COALESCE(excluded.hostname, discovered_hosts.hostname),
                 mac=COALESCE(excluded.mac, discovered_hosts.mac),
                 last_seen=excluded.last_seen""",
            (ip, hostname, mac, now, now),
        )


def set_listed(ip, listed, note=None):
    """Add/remove a host from the managed list. Allows adding an IP that was never
    discovered (manual entry)."""
    now = time.time()
    with conn() as c:
        cur = c.execute(
            "UPDATE discovered_hosts SET listed=?, note=COALESCE(?, note) WHERE ip=?",
            (1 if listed else 0, note, ip),
        )
        if cur.rowcount == 0 and listed:
            c.execute(
                "INSERT INTO discovered_hosts (ip, listed, note, first_seen, last_seen) VALUES (?,?,?,?,?)",
                (ip, 1, note, now, now),
            )


def list_discovered():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM discovered_hosts ORDER BY ip")]
