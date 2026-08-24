"""Fleet operations for agentless Linux devices.

Everything a Windows PC's Go agent does for itself — browse, import, deploy, drift,
filediff — done here by the coordinator over SSH instead. The results are deliberately
shaped exactly like the agent's, so the dashboard, the version history and the deploy
progress UI can't tell the two apart.

What is NOT here, by design: starting or stopping services. An SSH device is files-only;
`run` / `start_delay` in the manifest are ignored for it.
"""
import os
import posixpath
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, db, git_ops, globmatch, manifest, secretbox, ssh_transport
from .ssh_transport import SSHError

# Same batch budget the Go agent uses (agent/handlers.go), so memory behaviour during an
# import is identical whichever transport pulled the files.
_BATCH_BUDGET = 48 << 20

# Devices with an import currently running, so a second one can't interleave on the same
# repo folder.
_importing: set = set()
_importing_guard = threading.Lock()

_KEY_COMMENT = "sim-coordinator"


# --- live-path safety ---------------------------------------------------
# Deploy mirrors WITH DELETIONS, as root. A wrong live path is the one way this feature
# can destroy a machine, so paths are validated when the manifest is saved AND again
# immediately before the first delete.
_FORBIDDEN_ROOTS = ("/proc", "/sys", "/dev")


def validate_live_path(path: str) -> str:
    """Return the normalized path, or raise ValueError explaining why it's not allowed."""
    raw = (path or "").strip().replace("\\", "/")
    if not raw.startswith("/"):
        raise ValueError(f"{path!r}: must be an absolute path")
    norm = posixpath.normpath(raw)
    if ".." in norm.split("/"):
        raise ValueError(f"{path!r}: must not contain '..'")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        raise ValueError("'/': refusing to manage the whole filesystem")
    if len(parts) < 2:
        raise ValueError(
            f"{norm!r}: refusing to manage a top-level directory. Deploy deletes files "
            f"that aren't in the version, so pick something more specific "
            f"(e.g. {norm}/myapp)."
        )
    if norm.startswith(_FORBIDDEN_ROOTS):
        raise ValueError(f"{norm!r}: {norm.split('/')[1]} is a kernel filesystem, not config")
    return norm


def validate_manifest_pc(pc_ip: str, spec: dict):
    """Validation hook for a `transport: ssh` manifest entry. Returns a list of warnings;
    raises ValueError on anything unsafe."""
    warnings = []
    for app_name, app in (spec.get("apps") or {}).items():
        for live in (app.get("live") or []):
            try:
                validate_live_path(live)
            except ValueError as e:
                raise ValueError(f"{pc_ip} / {app_name}: {e}")
        if app.get("run"):
            warnings.append(
                f"{pc_ip} / {app_name}: 'run' is ignored for SSH devices — the "
                f"coordinator only syncs files, it never starts programs."
            )
    try:
        enrolled = db.ssh_device(pc_ip) is not None
    except Exception:
        enrolled = True   # can't check -> don't invent a warning, and never block the save
    if not enrolled:
        # Not an error: a manifest restored from an older version can legitimately name a
        # device that hasn't been (re-)enrolled here yet.
        warnings.append(f"{pc_ip} is marked transport: ssh but is not enrolled — add it in Fleet.")
    return warnings


# --- device access ------------------------------------------------------
def is_ssh(pc_ip: str) -> bool:
    return db.ssh_device(pc_ip) is not None


def _folder(pc_ip: str):
    """The device's monorepo folder, or None. Never raises: a device can be enrolled
    before it is in the manifest at all (browse-before-configure), and the manifest file
    may not exist yet on a brand-new install."""
    try:
        return manifest.pc_folder(pc_ip)
    except Exception:
        return None


def _device(pc_ip: str) -> dict:
    dev = db.ssh_device(pc_ip)
    if dev is None:
        raise SSHError(f"{pc_ip} is not an enrolled SSH device")
    return dev


def _password_for(dev) -> str | None:
    if not dev.get("secret"):
        return None
    return secretbox.decrypt(dev["secret"])


def _opener(dev):
    def open_session():
        key_path = dev.get("key_path") if dev.get("auth") == "key" else None
        password = None if key_path else _password_for(dev)
        if not key_path and password is None:
            raise SSHError(
                f"{dev['pc_ip']} has no usable credentials (key auth failed and no password "
                f"was remembered). Re-add the device or use 'Re-install key'."
            )
        client, _ = ssh_transport._connect(
            dev["pc_ip"], dev.get("port") or 22, dev.get("user") or "root",
            password=password, key_path=key_path, expected_fp=dev.get("fingerprint"),
        )
        return ssh_transport.Session(client)
    return open_session


def _session(pc_ip: str):
    dev = _device(pc_ip)
    return ssh_transport.session(pc_ip, _opener(dev))


# --- enrollment ---------------------------------------------------------
def enroll(ip, port, user, password, remember=False, label=None) -> dict:
    """Verify the password, install a dedicated key, and record the device.

    Nothing is persisted until key auth has been proven on a fresh connection, so a
    failure anywhere leaves no half-enrolled device behind.
    """
    ip, user = (ip or "").strip(), (user or "root").strip()
    if not ip:
        raise SSHError("an IP address is required")
    if not password:
        raise SSHError("a password is required to enroll (the key is installed with it)")
    if remember and not secretbox.available():
        raise SSHError(
            "cannot remember the password: SIM_SECRET_KEY is not set in /etc/sim-config.env. "
            "Re-run deploy/pi-setup.sh, or add the device without remembering it."
        )

    info = ssh_transport.probe(ip, port, user, password=password, accept_new=True)
    fp = info["fingerprint"]

    priv, pub_line = ssh_transport.generate_keypair(ip, f"{_KEY_COMMENT}@{_hostname()}")
    try:
        installed = ssh_transport.install_key(ip, port, user, password, pub_line,
                                              expected_fp=fp)
        ssh_transport.verify_key_auth(ip, port, user, priv, fp)
    except Exception:
        # Never leave a key we can't authenticate with lying around.
        if not db.ssh_device(ip):
            ssh_transport.delete_keypair(ip)
        raise

    db.upsert_ssh_device(
        ip, label=(label or info.get("os_pretty") or None), port=port, user=user,
        auth="key", key_path=str(priv),
        secret=secretbox.encrypt(password) if remember else None,
        fingerprint=fp, os_pretty=info.get("os_pretty"), kernel=info.get("kernel"),
        caps=info.get("caps"),
    )
    if not remember:
        db.clear_ssh_secret(ip)
    db.touch_ssh_device(ip, ok=True)
    db.touch_agent(ip, folder=manifest.pc_folder(ip))
    return {"ok": True, "ip": ip, "user": info.get("user") or user, "uid": info.get("uid"),
            "os_pretty": info.get("os_pretty"), "kernel": info.get("kernel"),
            "arch": info.get("arch"), "caps": info.get("caps"), "fingerprint": fp,
            "key_installed": "already present" if installed["already_present"] else "added",
            "created_ssh_dir": installed["created_ssh_dir"],
            "password_stored": bool(remember)}


def _hostname():
    import socket
    try:
        return socket.gethostname()
    except Exception:
        return "coordinator"


def test(pc_ip: str) -> dict:
    """Probe now and record the outcome (the Fleet 'Test connection' action)."""
    dev = _device(pc_ip)
    try:
        info = ssh_transport.probe(
            pc_ip, dev.get("port") or 22, dev.get("user") or "root",
            key_path=dev.get("key_path") if dev.get("auth") == "key" else None,
            password=None if dev.get("auth") == "key" else _password_for(dev),
            expected_fp=dev.get("fingerprint"))
    except SSHError as e:
        db.touch_ssh_device(pc_ip, ok=False, error=str(e))
        ssh_transport.drop(pc_ip)
        raise
    db.upsert_ssh_device(pc_ip, port=dev.get("port") or 22, user=dev.get("user") or "root",
                         auth=dev.get("auth") or "key",
                         os_pretty=info.get("os_pretty"), kernel=info.get("kernel"),
                         caps=info.get("caps"))
    db.touch_ssh_device(pc_ip, ok=True)
    db.touch_agent(pc_ip, folder=_folder(pc_ip))
    return {"ok": True, **info}


def reinstall_key(pc_ip: str, password: str | None = None) -> dict:
    """Re-install our public key — for when authorized_keys was wiped or the device was
    rebuilt. Uses the remembered password if one was stored."""
    dev = _device(pc_ip)
    pw = password or _password_for(dev)
    if not pw:
        raise SSHError("no password stored for this device — supply one to re-install the key")
    priv, pub_line = ssh_transport.generate_keypair(pc_ip, f"{_KEY_COMMENT}@{_hostname()}")
    installed = ssh_transport.install_key(pc_ip, dev.get("port") or 22,
                                          dev.get("user") or "root", pw, pub_line,
                                          expected_fp=dev.get("fingerprint"))
    ssh_transport.verify_key_auth(pc_ip, dev.get("port") or 22, dev.get("user") or "root",
                                 priv, dev.get("fingerprint"))
    db.upsert_ssh_device(pc_ip, port=dev.get("port") or 22, user=dev.get("user") or "root",
                         auth="key", key_path=str(priv))
    db.touch_ssh_device(pc_ip, ok=True)
    ssh_transport.drop(pc_ip)   # force the pool to pick up the new credentials
    return {"ok": True,
            "key_installed": "already present" if installed["already_present"] else "added"}


def reauth(pc_ip: str, user=None, port=None, password=None, remember=False,
           accept_host_key=False) -> dict:
    """Re-authenticate an enrolled device: change the login, port or host key in place.

    Covers what 'Re-install key' cannot — a device rebuilt with a new host key (which
    correctly refuses to connect until someone confirms it), a changed password, or
    moving to an account with different filesystem permissions. Without this the only
    route is Remove + re-add, which throws away the device's label and history.

    The keypair is per-device, so the same key is installed for the new account. The old
    account keeps its copy in authorized_keys — we can no longer be sure of reaching it
    to clean up, and the caller is told so.
    """
    dev = _device(pc_ip)
    prev_user = dev.get("user") or "root"
    user = (user or prev_user).strip()
    port = int(port or dev.get("port") or 22)
    if not password:
        raise SSHError("a password is required to re-authenticate — it installs the key")
    if remember and not secretbox.available():
        raise SSHError(
            "cannot remember the password: SIM_SECRET_KEY is not set in /etc/sim-config.env."
        )
    # Only skip the pinned host key when the operator explicitly accepted the change.
    expected = None if accept_host_key else dev.get("fingerprint")
    info = ssh_transport.probe(pc_ip, port, user, password=password,
                               expected_fp=expected, accept_new=True)
    fp = info["fingerprint"]
    changed_host_key = bool(dev.get("fingerprint")) and fp != dev.get("fingerprint")

    priv, pub_line = ssh_transport.generate_keypair(pc_ip, f"{_KEY_COMMENT}@{_hostname()}")
    installed = ssh_transport.install_key(pc_ip, port, user, password, pub_line,
                                          expected_fp=fp)
    ssh_transport.verify_key_auth(pc_ip, port, user, priv, fp)

    db.update_ssh_auth(pc_ip, user=user, port=port, auth="key", key_path=str(priv),
                       fingerprint=fp,
                       secret=secretbox.encrypt(password) if remember else None)
    db.upsert_ssh_device(pc_ip, port=port, user=user, auth="key",
                         os_pretty=info.get("os_pretty"), kernel=info.get("kernel"),
                         caps=info.get("caps"))
    db.touch_ssh_device(pc_ip, ok=True)
    ssh_transport.drop(pc_ip)     # force the pool to reconnect with the new credentials

    note = None
    if user != prev_user:
        note = (f"Switched from {prev_user} to {user}. The coordinator's key is still in "
                f"{prev_user}'s authorized_keys on the device — remove it there if you "
                f"want it gone.")
    return {"ok": True, "user": user, "port": port,
            "os_pretty": info.get("os_pretty"), "kernel": info.get("kernel"),
            "fingerprint": fp, "host_key_changed": changed_host_key,
            "key_installed": "already present" if installed["already_present"] else "added",
            "password_stored": bool(remember), "note": note}


def forget(pc_ip: str) -> dict:
    """Un-enrol: close the connection, drop the credentials, delete the key.

    The key stays in the device's authorized_keys — we can no longer authenticate to
    remove it, and silently keeping a password around just to do so would be worse. The
    UI says so.
    """
    ssh_transport.drop(pc_ip)
    key_path = db.forget_ssh_device(pc_ip)
    if key_path:
        try:
            os.unlink(key_path)
        except OSError:
            pass
        pub = str(key_path) + ".pub"
        try:
            os.unlink(pub)
        except OSError:
            pass
    ssh_transport.delete_keypair(pc_ip)
    return {"ok": True, "note": "The coordinator's key was left in the device's "
                                "authorized_keys; remove it there if you want it gone."}


def device_view() -> dict:
    """Non-secret device details for /pcs, keyed by IP. Never includes `secret`."""
    out = {}
    for d in db.list_ssh_devices():
        out[d["pc_ip"]] = {
            "label": d.get("label"), "port": d.get("port") or 22,
            "user": d.get("user") or "root", "auth": d.get("auth"),
            "os_pretty": d.get("os_pretty"), "kernel": d.get("kernel"),
            "caps": d.get("caps") or {}, "fingerprint": d.get("fingerprint"),
            "added_at": d.get("added_at"), "last_ok": d.get("last_ok"),
            "last_error": d.get("last_error"),
            "password_stored": bool(d.get("secret")),
        }
    return out


# --- browse -------------------------------------------------------------
def browse(pc_ip: str, path: str = "") -> dict:
    """Same contract as the agent's browse, so the dashboard's file tree is unchanged.

    An empty path lists roots. The Windows agent returns drive letters there; a Linux box
    has exactly one root, so the tree shows a single expandable '/' node.
    """
    if not path:
        return {"path": "", "entries": [{"name": "/", "path": "/", "is_dir": True, "size": 0}],
                "error": None}
    try:
        with _session(pc_ip) as sess:
            entries, truncated = sess.listdir(path)
    except Exception as e:
        # Browsing is exploratory — an unreadable directory, an offline device or a bad
        # stored secret should render inline in the tree, not 500 the dashboard.
        return {"path": path, "entries": [], "error": str(e) or e.__class__.__name__}
    err = None
    if truncated:
        err = f"showing the first {len(entries)} of {truncated} entries"
    return {"path": path, "entries": [{"name": e["name"], "path": e["path"],
                                       "is_dir": e["is_dir"], "size": e["size"]}
                                      for e in entries],
            "error": err}


# --- import -------------------------------------------------------------
def start_import(pc_ip: str) -> dict:
    """Queue-equivalent for an SSH device: run the import on a background thread.

    Must not block the request — a multi-GB pull would hold a worker for minutes while
    the dashboard's 5-second poll starves.
    """
    apps = manifest.resolved_apps(pc_ip)
    if not apps:
        raise SSHError(f"{pc_ip} has no folders selected — nothing to import")
    folder = manifest.pc_folder(pc_ip)
    with _importing_guard:
        if pc_ip in _importing:
            raise SSHError(f"an import is already running for {pc_ip}")
        _importing.add(pc_ip)
    threading.Thread(target=_run_import, args=(pc_ip, folder, apps), daemon=True).start()
    return {"queued": True, "folder": folder}


def start_capture(pc_ip: str, message: str | None = None, author: str = "dev") -> dict:
    """Dev capture for an SSH device.

    For a files-only device a capture IS a re-import onto dev: walk what's live now and
    commit it, with deletions handled by dev_import_commit's `git add -A -- <folder>`.
    Reported through _capture_progress so the dev stepper shows it like any other capture.
    """
    apps = manifest.resolved_apps(pc_ip)
    if not apps:
        raise SSHError(f"{pc_ip} has no folders selected — nothing to capture")
    folder = manifest.pc_folder(pc_ip)
    with _importing_guard:
        if pc_ip in _importing:
            raise SSHError(f"a transfer is already running for {pc_ip}")
        _importing.add(pc_ip)
    threading.Thread(
        target=_run_import, args=(pc_ip, folder, apps),
        kwargs={"capture": True,
                "message": message or f"dev capture from {pc_ip} ({folder})",
                "author": author},
        daemon=True).start()
    return {"capturing": pc_ip, "folder": folder}


def _run_import(pc_ip: str, folder: str, apps: dict, capture: bool = False,
                message: str | None = None, author: str = "import"):
    from . import main as _main   # deferred: main imports this module at startup
    prog_map = _main._capture_progress if capture else _main._import_progress
    try:
        with _session(pc_ip) as sess:
            files, missing, exec_paths = {}, [], []
            total = 0
            plan = []
            # One walk, not two: listdir_attr already returns sizes, so the same pass
            # gives both the progress total and the work list. (The Go agent walks twice
            # because it needs a separate size pass.)
            for app_name, app in apps.items():
                repo = app.get("repo")
                if not repo:
                    continue
                labels = globmatch.live_labels(app.get("live") or [])
                for live in (app.get("live") or []):
                    if not sess.isdir(live):
                        missing.append(f"{app_name}: {live}")
                        continue
                    for rel, full, size, _mtime, mode in sess.walk(
                            live, app.get("exclude") or [], labels[live]):
                        plan.append((f"{repo}/{rel}", full, size, mode))
                        total += size

            prog_map[pc_ip] = {"folder": folder, "total_bytes": total,
                               "received_bytes": 0, "received_files": 0, "done": False}
            batch_index, pending = 0, 0
            for key, full, size, mode in plan:
                files[key] = sess.read_file(full)
                if mode & 0o111:
                    exec_paths.append(key)
                pending += len(files[key])
                if pending >= _BATCH_BUDGET:
                    _main._import_batch(pc_ip, folder, files, batch_index=batch_index,
                                        final=False, total_bytes=total, progress=prog_map)
                    files, pending = {}, 0
                    batch_index += 1
            _main._import_batch(
                pc_ip, folder, files, batch_index=batch_index, final=True,
                total_bytes=total, missing=missing, progress=prog_map,
                message=message, author=author,
                on_commit=lambda: _restore_exec_bits(exec_paths),
            )
        db.touch_ssh_device(pc_ip, ok=True)
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        prog = prog_map.setdefault(pc_ip, {"folder": folder})
        prog["error"] = msg
        prog["done"] = True
        _main._agent_errors[pc_ip] = msg
        db.touch_ssh_device(pc_ip, ok=False, error=msg)
    finally:
        with _importing_guard:
            _importing.discard(pc_ip)


def _restore_exec_bits(keys):
    """git records the executable bit, but git_ops.write_files writes plain 0644 — so
    without this every script and binary imported from a Linux box comes back
    non-executable on the next deploy, breaking the device."""
    for key in keys:
        p = config.WORK_CLONE / key
        try:
            if p.is_file():
                os.chmod(p, 0o755)
        except OSError:
            pass


def size_report(pc_ip: str) -> dict:
    """Per-app byte totals for the bootstrap panel."""
    apps = manifest.resolved_apps(pc_ip)
    folder = manifest.pc_folder(pc_ip)
    sizes = {}
    with _session(pc_ip) as sess:
        for app_name, app in apps.items():
            labels = globmatch.live_labels(app.get("live") or [])
            total = 0
            for live in (app.get("live") or []):
                if not sess.isdir(live):
                    continue
                for _rel, _full, size, _mtime, _mode in sess.walk(
                        live, app.get("exclude") or [], labels[live]):
                    total += size
            sizes[app_name] = total
    db.record_size_report(pc_ip, folder or "", sizes)
    return {"ok": True, "folder": folder, "sizes": sizes}


# --- mirror planning (shared by deploy, drift and preview) --------------
def _app_pairs(pc_ip: str, ref: str, worktree):
    """Yield (app_name, src_dir, live_dir, label, excludes) for every syncable unit.

    A unit is one `live` directory of one app. Apps with no repo, no live dirs, or whose
    repo subtree is absent from the version are skipped — never treated as "delete
    everything", which is the guard agent/deploy.go MirrorToLive relies on too.
    """
    apps = manifest.resolved_apps(pc_ip, ref)
    for app_name, app in apps.items():
        repo = app.get("repo")
        live_dirs = app.get("live") or []
        if not repo or not live_dirs:
            continue
        labels = globmatch.live_labels(live_dirs)
        for live in live_dirs:
            label = labels[live]
            src = worktree / repo
            if label:
                src = src / label
            yield app_name, src, live, label, (app.get("exclude") or [])


def plan_mirror(sess, src, dst: str, label: str, excludes) -> dict:
    """Compute what deploying `src` onto `dst` would do — robocopy /MIR semantics.

    Excluded paths are neither copied nor deleted, which is what lets a device keep its
    logs, caches and runtime state across a deploy (see agent/deploy.go robocopyExcludes).
    """
    local = {rel: (full, size, mtime, mode)
             for rel, full, size, mtime, mode in globmatch.walk_local(src, label, excludes)}
    remote = {rel: (full, size, mtime, mode)
              for rel, full, size, mtime, mode in sess.walk(dst, excludes, label)}

    prefix = len(label) + 1 if label else 0
    copy, chmod, delete_files = [], [], []
    unchanged = copy_bytes = 0
    for rel, (full, size, mtime, mode) in local.items():
        sub = rel[prefix:] if prefix else rel
        target = posixpath.join(dst, sub)
        want_mode = 0o755 if mode & 0o111 else 0o644
        r = remote.get(rel)
        # 2s tolerance matches robocopy's timestamp granularity, so a filesystem that
        # rounds mtimes doesn't make every file look changed.
        if r and r[1] == size and abs((r[2] or 0) - mtime) <= 2:
            # Content matches — but the executable bit is the one permission git records,
            # and on Linux losing it breaks the file. Repair it with a chmod instead of
            # re-uploading, which would mean re-sending whole binaries over a bit.
            if bool((r[3] or 0) & 0o111) != bool(mode & 0o111):
                chmod.append({"dst": target, "rel": sub, "mode": want_mode})
            else:
                unchanged += 1
            continue
        # `existed` / `live_mtime` are carried so drift can label new-vs-changed without
        # a stat round trip per file.
        copy.append({"src": full, "dst": target, "rel": sub,
                     "size": size, "mode": want_mode,
                     "mtime": mtime, "existed": r is not None,
                     "live_mtime": (r[2] or 0) if r else 0})
        copy_bytes += size
    for rel, (full, _size, mtime, _mode) in remote.items():
        if rel not in local:
            delete_files.append({"dst": full, "rel": rel[prefix:] if prefix else rel,
                                 "live_mtime": mtime or 0})
    return {"copy": copy, "chmod": chmod, "delete_files": delete_files,
            "unchanged": unchanged, "copy_bytes": copy_bytes,
            "remote_total": len(remote)}


def _check_delete_budget(plans, confirm_delete: bool):
    """Refuse a plan that would delete implausibly much.

    This is the guard that catches the dangerous case: an empty or un-imported repo
    folder makes every live file look 'extra', and a blind mirror would empty a working
    directory. Cheap to override deliberately, impossible to hit by accident.
    """
    if confirm_delete:
        return
    deletes = sum(len(p["delete_files"]) for p in plans)
    total = sum(p["remote_total"] for p in plans)
    if deletes == 0:
        return
    pct = (deletes * 100 // total) if total else 100
    if deletes > config.SSH_DELETE_LIMIT or pct > config.SSH_DELETE_PCT:
        raise SSHError(
            f"refusing to deploy: it would delete {deletes} of {total} files on the device "
            f"({pct}%), over the {config.SSH_DELETE_LIMIT}-file / {config.SSH_DELETE_PCT}% "
            f"safety limit. Check the version really contains this device's content "
            f"(has it been imported?), then confirm if that is genuinely what you want."
        )


def _check_writable(sess, pc_ip: str, dsts):
    """Fail before touching anything if the SSH user can't write where it must.

    Deploy replaces files in place; discovering half way through that the account has no
    write access leaves the device in a mixed state, and the raw SFTP error
    ("[Errno 13] Permission denied") names neither the path nor the user.
    """
    user = (db.ssh_device(pc_ip) or {}).get("user") or "the SSH user"
    for dst in sorted(set(dsts)):
        target = dst if sess.isdir(dst) else posixpath.dirname(dst)
        reason = sess.check_writable(target)
        if reason:
            raise SSHError(
                f"{user} cannot write to {target} on {pc_ip} ({reason}). "
                f"Either give {user} ownership of that directory "
                f"(sudo chown -R {user} {target}), or remove the device and re-add it "
                f"as a user that can write there, such as root."
            )


def _apply_mirror(sess, plan, dst: str):
    copied = 0
    for item in plan["copy"]:
        with open(item["src"], "rb") as f:
            data = f.read()
        sess.write_file(item["dst"], data, mode=item["mode"], mtime=item["mtime"])
        copied += 1
    for item in plan.get("chmod") or []:
        try:
            sess.sftp.chmod(item["dst"], item["mode"])
        except IOError:
            pass
    deleted = 0
    dirs = set()
    for item in plan["delete_files"]:
        # Resolve before unlinking and require the result to still be inside the live
        # directory, so a symlink can't redirect a delete somewhere else on the device.
        target = item["dst"]
        parent = sess.normalize(posixpath.dirname(target))
        root = sess.normalize(dst)
        if not (parent == root or parent.startswith(root.rstrip("/") + "/")):
            continue
        try:
            sess.remove(target)
            deleted += 1
            dirs.add(parent)
        except IOError:
            pass
    # Prune directories emptied by those deletions, deepest first. rmdir fails harmlessly
    # on a directory that still holds excluded content, which is exactly what we want.
    for d in sorted(dirs, key=lambda p: p.count("/"), reverse=True):
        while d != dst and d.startswith(dst.rstrip("/") + "/"):
            if not sess.rmdir(d):
                break
            d = posixpath.dirname(d)
    return copied, deleted


# --- deploy -------------------------------------------------------------
def preview(pc_ip: str, ref: str | None = None) -> dict:
    """Dry run: what a deploy would copy and delete. Same planner as the real thing, so
    the preview can never disagree with what actually happens."""
    ref = ref or config.TRAINING_LIVE
    worktree = git_ops.deploy_worktree(ref)
    if worktree is None:
        raise SSHError(f"{ref} does not exist yet — nothing to deploy")
    copy = delete = bytes_ = 0
    skipped, entries = [], []
    with _session(pc_ip) as sess:
        for app_name, src, live, label, excludes in _app_pairs(pc_ip, ref, worktree):
            if not src.is_dir():
                skipped.append(f"{app_name}: not present in {ref}")
                continue
            try:
                dst = validate_live_path(live)
            except ValueError as e:
                raise SSHError(f"{app_name}: {e}")
            p = plan_mirror(sess, src, dst, label, excludes)
            copy += len(p["copy"]) + len(p.get("chmod") or [])
            delete += len(p["delete_files"])
            bytes_ += p["copy_bytes"]
            for c in p["copy"][:200]:
                entries.append({"app": app_name, "kind": "copy", "path": c["rel"]})
            for c in (p.get("chmod") or [])[:50]:
                entries.append({"app": app_name, "kind": "chmod", "path": c["rel"]})
            for d in p["delete_files"][:200]:
                entries.append({"app": app_name, "kind": "delete", "path": d["rel"]})
    return {"ref": ref, "copy": copy, "delete": delete, "bytes": bytes_,
            "skipped_apps": skipped, "entries": entries[:400]}


def deploy(pc_ip: str, ref: str | None = None, confirm_delete: bool = False) -> dict:
    """Mirror a version onto the device. Blocking — callers run it on a worker thread."""
    from . import main as _main   # deferred: main imports this module at startup
    ref = ref or config.TRAINING_LIVE
    folder = manifest.pc_folder(pc_ip, ref) or manifest.pc_folder(pc_ip) or ""
    try:
        worktree = git_ops.deploy_worktree(ref)
        if worktree is None:
            raise SSHError(f"{ref} does not exist yet — nothing to deploy")
        copied = deleted = 0
        skipped = []
        with _session(pc_ip) as sess:
            pairs, plans = [], []
            for app_name, src, live, label, excludes in _app_pairs(pc_ip, ref, worktree):
                if not src.is_dir():
                    # The version has no content for this app. Skipping is essential:
                    # mirroring an absent source would delete the live directory.
                    skipped.append(f"{app_name}: not present in {ref}")
                    continue
                try:
                    dst = validate_live_path(live)
                except ValueError as ve:
                    raise SSHError(f"{app_name}: {ve}")
                plan = plan_mirror(sess, src, dst, label, excludes)
                pairs.append((dst, plan))
                plans.append(plan)
            _check_delete_budget(plans, confirm_delete)
            _check_writable(sess, pc_ip, [d for d, _p in pairs])
            for dst, plan in pairs:
                c, d = _apply_mirror(sess, plan, dst)
                copied += c
                deleted += d
        db.touch_ssh_device(pc_ip, ok=True)
        _main._record_deploy_result(pc_ip, folder, "TRAINING", ref, True, kind="ssh")
        return {"ok": True, "copied": copied, "deleted": deleted, "skipped_apps": skipped}
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        db.touch_ssh_device(pc_ip, ok=False, error=msg)
        _main._record_deploy_result(pc_ip, folder, "ERROR", ref, False, error=msg, kind="ssh")
        return {"ok": False, "error": msg}


def deploy_async_all(ips, ref: str):
    """Fan a deploy out across SSH devices. Bounded concurrency; each device is also
    serialized by its own session lock."""
    ips = list(ips)
    if not ips:
        return

    def run():
        # Materialise the ref once, here rather than in the caller: checking out and
        # running `lfs pull` on a large load takes long enough to hold up the HTTP
        # response to /deploy if done on the request thread.
        git_ops.deploy_worktree(ref)
        with ThreadPoolExecutor(max_workers=config.SSH_DEPLOY_WORKERS) as pool:
            for ip in ips:
                pool.submit(deploy, ip, ref)

    threading.Thread(target=run, daemon=True).start()


# --- drift / filediff ---------------------------------------------------
def drift(pc_ip: str) -> dict:
    """Which files differ between the deployed version and live.

    Same planner as deploy with every write suppressed, so 'diff' and the next deploy can
    never disagree. Entry shape matches agent/deploy.go DriftEntry, including `path`
    being relative to the live directory (not label-prefixed).
    """
    ref = config.TRAINING_LIVE
    worktree = git_ops.deploy_worktree(ref)
    if worktree is None:
        return {"entries": []}
    entries = []
    with _session(pc_ip) as sess:
        for app_name, src, live, label, excludes in _app_pairs(pc_ip, ref, worktree):
            if not src.is_dir():
                continue
            p = plan_mirror(sess, src, _norm_live(live), label, excludes)
            for c in p["copy"]:
                entries.append({"app": app_name,
                                "kind": "changed" if c["existed"] else "new",
                                "path": c["rel"], "mtime": c["live_mtime"]})
            for c in p.get("chmod") or []:   # right content, wrong executable bit
                entries.append({"app": app_name, "kind": "changed",
                                "path": c["rel"], "mtime": 0})
            for d in p["delete_files"]:
                entries.append({"app": app_name, "kind": "extra",
                                "path": d["rel"], "mtime": d["live_mtime"]})
            # A huge drift means "resync", not "read the list" — same cap as the agent.
            if len(entries) >= 1000:
                entries = entries[:1000]
                break
    # We just measured the truth, so keep the Fleet clean/dirty pill honest. This makes
    # the dashboard's "diff" button double as a refresh, instead of showing changed files
    # next to a row that still claims the device is clean.
    _record_clean(pc_ip, not entries)
    return {"entries": entries}


def _record_clean(pc_ip: str, clean: bool):
    """Update just the clean flag of a deployed device, leaving mode and ref alone.

    Only meaningful once something has been deployed: a device that was never deployed
    has nothing to be clean or dirty against, and must not be promoted to TRAINING here.
    """
    row = next((a for a in db.list_agents() if a["pc_ip"] == pc_ip), None)
    if not row or row.get("mode") != "TRAINING":
        return
    if bool(row.get("clean")) == clean:
        return
    db.upsert_agent(pc_ip, row.get("folder") or "", "TRAINING", row.get("current_ref"),
                    clean, kind="ssh")


def _norm_live(live: str) -> str:
    try:
        return validate_live_path(live)
    except ValueError:
        return posixpath.normpath((live or "/").replace("\\", "/"))


_DIFF_CAP = 512 * 1024


def filediff(pc_ip: str, app: str, path: str) -> dict:
    """One drifted file, version vs live, for the line-diff modal. Mirrors the caps and
    binary detection in agent/handlers.go doFileDiff."""
    ref = config.TRAINING_LIVE
    res = {"app": app, "path": path, "version": "", "live": "",
           "binary": False, "too_big": False, "error": None}
    worktree = git_ops.deploy_worktree(ref)
    if worktree is None:
        res["error"] = "no deployed version"
        return res
    apps = manifest.resolved_apps(pc_ip, ref)
    spec = apps.get(app)
    if not spec:
        res["error"] = "app not in the deployed manifest"
        return res
    labels = globmatch.live_labels(spec.get("live") or [])
    with _session(pc_ip) as sess:
        for live in (spec.get("live") or []):
            src = worktree / (spec.get("repo") or "")
            if labels[live]:
                src = src / labels[live]
            src_file = src / path.replace("\\", "/")
            live_file = posixpath.join(_norm_live(live), path.replace("\\", "/"))
            s_exists = src_file.is_file()
            l_stat = sess.stat(live_file)
            if not s_exists and l_stat is None:
                continue        # this drifted file isn't under this live dir
            if (s_exists and src_file.stat().st_size > _DIFF_CAP) or \
               (l_stat is not None and (l_stat.st_size or 0) > _DIFF_CAP):
                res["too_big"] = True
                return res
            vb = src_file.read_bytes() if s_exists else b""      # missing side reads empty
            lb = sess.read_file(live_file) if l_stat is not None else b""
            if _is_binary(vb) or _is_binary(lb):
                res["binary"] = True
                return res
            res["version"] = vb.decode(errors="replace")
            res["live"] = lb.decode(errors="replace")
            return res
    res["error"] = "file not found under the app"
    return res


def _is_binary(b: bytes) -> bool:
    return b"\0" in b[:8000]


# --- liveness -----------------------------------------------------------
_poller_started = False
_fail_counts: dict = {}
_next_attempt: dict = {}
_last_drift_at: dict = {}
_drift_cost: dict = {}      # pc_ip -> seconds the last drift check took (drives the interval)


def start_poller():
    """Synthesize heartbeats for agentless devices.

    Assumes a single coordinator process — pi-setup.sh runs uvicorn without --workers.
    With N workers you would get N pollers (and N divergent in-memory command queues,
    which is already true of the agent queue today).
    """
    global _poller_started
    if _poller_started:
        return
    _poller_started = True
    threading.Thread(target=_poll_loop, daemon=True).start()


def _poll_loop():
    while True:
        try:
            _poll_once()
        except Exception:
            pass
        time.sleep(max(5, config.SSH_POLL_SECONDS))


def _poll_once():
    now = time.time()
    for dev in db.list_ssh_devices():
        ip = dev["pc_ip"]
        # Back off on a device that is simply switched off: three failures in a row and
        # we drop to once a minute, so a powered-down box doesn't cost a connect timeout
        # on every cycle. It still goes offline on schedule — that's driven by last_seen.
        if now < _next_attempt.get(ip, 0):
            continue
        # An import or deploy holds this device's connection, sometimes for minutes.
        # Probing would queue behind it and stall every other device's poll, and a
        # transfer in flight is already proof the device is up.
        if ssh_transport.is_busy(ip):
            db.touch_agent(ip, folder=_folder(ip))
            continue
        try:
            with ssh_transport.session(ip, _opener(dev)) as sess:
                rc, _out, _err = sess.run("printf ok", timeout=8)
                if rc != 0:
                    raise SSHError(f"probe exited {rc}")
            _fail_counts.pop(ip, None)
            _next_attempt.pop(ip, None)
            db.touch_ssh_device(ip, ok=True)
            db.touch_agent(ip, folder=_folder(ip))
            _maybe_refresh_drift(ip)
        except Exception as e:
            fails = _fail_counts[ip] = _fail_counts.get(ip, 0) + 1
            if fails >= 3:
                _next_attempt[ip] = time.time() + 60
            db.touch_ssh_device(ip, ok=False, error=str(e) or e.__class__.__name__)
            ssh_transport.drop(ip)


def _maybe_refresh_drift(pc_ip: str):
    """Keep the Fleet clean/dirty flag current. The Windows agent recomputes this on
    every heartbeat; the SSH equivalent is a local walk plus one SFTP walk, which for a
    normal config tree is well under a second.

    The interval adapts to what the check actually costs on this device, so a small tree
    updates within a poll or two while a huge one backs off on its own rather than
    spending the coordinator's time in a loop. 0 disables it entirely.
    """
    if not config.SSH_DRIFT_SECONDS:
        return
    now = time.time()
    # Never spend more than ~10% of elapsed time drift-checking one device.
    interval = max(config.SSH_DRIFT_SECONDS, _drift_cost.get(pc_ip, 0) * 10)
    if now - _last_drift_at.get(pc_ip, 0) < interval:
        return
    _last_drift_at[pc_ip] = now
    if not git_ops.ref_sha(config.TRAINING_LIVE):
        return
    if pc_ip not in manifest.load_manifest_at(config.TRAINING_LIVE).get("pcs", {}):
        return
    started = time.time()
    try:
        drift(pc_ip)          # records the clean flag itself
    except Exception:
        return
    finally:
        _drift_cost[pc_ip] = time.time() - started
        _last_drift_at[pc_ip] = time.time()


def shutdown():
    ssh_transport.close_all()
