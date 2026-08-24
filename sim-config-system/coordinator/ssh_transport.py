"""SSH/SFTP transport for agentless Linux devices.

Connections and primitives only — no fleet semantics (see ssh_ops.py for those).

A Windows PC runs the Go agent, which walks its own disk and robocopies files itself. A
Linux device managed here runs nothing: the coordinator does that work remotely over
SFTP. Everything in this module is therefore the server-side equivalent of something in
agent/ — the walker mirrors agent/fsops.go walkFollow, the listing mirrors
agent/browse.go, the mirror primitives back what agent/deploy.go does with robocopy.

Enrollment is password-based once; from then on the coordinator uses a per-device
ed25519 key it generated and installed. Host keys are pinned on first connect (TOFU) and
verified on every later one, so a changed key fails loudly instead of reconnecting to
whatever now answers on that address.
"""
import base64
import errno
import hashlib
import io
import os
import posixpath
import stat
import threading
from contextlib import contextmanager

from . import config, globmatch

# paramiko is an optional import at module load so the coordinator still starts (and every
# Windows-agent feature keeps working) on an install that predates the dependency. Every
# entry point checks _require_paramiko() and returns a readable error instead of a 500.
try:
    import paramiko
except ImportError:  # pragma: no cover - depends on deployment state
    paramiko = None


class SSHError(Exception):
    """Anything that went wrong talking to a device, with a message fit for the UI."""


def _require_paramiko():
    if paramiko is None:
        raise SSHError(
            "paramiko is not installed on the coordinator. Run deploy/pi-setup.sh "
            "(or pip install -r coordinator/requirements.txt) and restart sim-coordinator."
        )


# --- key material -------------------------------------------------------
def key_paths(pc_ip: str):
    """(private, public) key paths for a device. One keypair per device, so revoking one
    device's access never affects another."""
    safe = pc_ip.replace(":", "_").replace("/", "_")
    base = config.SSH_DIR / f"id_ed25519_{safe}"
    return base, base.with_suffix(".pub")


def generate_keypair(pc_ip: str, comment: str):
    """Create (or reuse) this device's keypair. Returns (private_path, public_line)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    priv, pub = key_paths(pc_ip)
    config.SSH_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(config.SSH_DIR, 0o700)
    if priv.exists() and pub.exists():
        return priv, pub.read_text().strip()

    key = ed25519.Ed25519PrivateKey.generate()
    # PrivateFormat.OpenSSH, not PKCS8: paramiko.Ed25519Key only reads the OpenSSH container.
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    line = f"{raw_pub} {comment}"

    # Create with 0600 from the start rather than chmod-ing afterwards, so the private key
    # is never briefly world-readable.
    fd = os.open(str(priv), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    pub.write_text(line + "\n")
    return priv, line


def delete_keypair(pc_ip: str):
    for p in key_paths(pc_ip):
        try:
            p.unlink()
        except OSError:
            pass


def fingerprint(key) -> str:
    """OpenSSH-style SHA256 fingerprint of a host key."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class _PinnedHostKeyPolicy:
    """Trust-on-first-use host key checking.

    paramiko consults this before authenticating, so a mismatch aborts the connection
    before the password or key is ever offered. `expected` is None during enrollment
    (record whatever answers) and the stored fingerprint on every connect after that.
    """

    def __init__(self, expected: str | None):
        self.expected = expected
        self.seen: str | None = None

    def missing_host_key(self, client, hostname, key):
        self.seen = fingerprint(key)
        if self.expected and self.seen != self.expected:
            raise SSHError(
                f"host key for {hostname} has changed ({self.seen}, expected {self.expected}). "
                "Either the device was rebuilt — remove and re-add it — or this is not the "
                "machine you enrolled."
            )


# --- connecting ---------------------------------------------------------
def _connect(host, port, user, *, password=None, key_path=None, expected_fp=None,
             accept_new=False):
    """Open an authenticated SSHClient. Returns (client, policy)."""
    _require_paramiko()
    if not (accept_new or expected_fp):
        raise SSHError("no pinned host key for this device; re-enroll it")
    client = paramiko.SSHClient()
    policy = _PinnedHostKeyPolicy(expected_fp)
    client.set_missing_host_key_policy(policy)
    pkey = None
    if key_path:
        try:
            pkey = paramiko.Ed25519Key.from_private_key_file(str(key_path))
        except Exception as e:
            raise SSHError(f"cannot read the key for this device ({e})")
    try:
        client.connect(
            hostname=host, port=int(port), username=user,
            password=password, pkey=pkey,
            # Without these paramiko silently tries the Pi's own agent and ~/.ssh keys,
            # which turns a clear "auth failed" into a confusing one.
            allow_agent=False, look_for_keys=False,
            timeout=config.SSH_CONNECT_TIMEOUT,
            banner_timeout=config.SSH_CONNECT_TIMEOUT,
            auth_timeout=config.SSH_CONNECT_TIMEOUT,
        )
    except SSHError:
        client.close()
        raise
    except Exception as e:
        client.close()
        raise SSHError(_friendly(e, user)) from e
    tr = client.get_transport()
    if tr is not None:
        tr.set_keepalive(15)
        # Applied before open_sftp() opens its channel. paramiko's defaults throttle SFTP
        # to single-digit MB/s, which is unusable for a multi-GB import.
        tr.default_window_size = 4 * 1024 * 1024
        tr.default_max_packet_size = 32768
    return client, policy


def _friendly(exc, user):
    """Turn paramiko's exceptions into something an operator can act on."""
    if paramiko is not None and isinstance(exc, paramiko.AuthenticationException):
        if user == "root":
            return ("authentication failed. Many distributions ship "
                    "PermitRootLogin prohibit-password, which blocks root password login — "
                    "check /etc/ssh/sshd_config on the device, or enroll a non-root user.")
        return "authentication failed (wrong username or password)"
    if isinstance(exc, OSError) and exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return "host unreachable"
    if isinstance(exc, OSError) and exc.errno == errno.ECONNREFUSED:
        return "connection refused — is sshd running on the device?"
    msg = str(exc) or exc.__class__.__name__
    return msg


# --- one-shot operations (enrollment) -----------------------------------
_PROBE_SCRIPT = r"""
id -u; id -un; uname -sr; uname -m
( . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}" )
command -v rsync >/dev/null && echo yes || echo no
command -v sha256sum >/dev/null && echo yes || echo no
"""


def probe(host, port, user, *, password=None, key_path=None, expected_fp=None,
          accept_new=False) -> dict:
    """Connect and report what the device is. Also the liveness check."""
    client, policy = _connect(host, port, user, password=password, key_path=key_path,
                              expected_fp=expected_fp, accept_new=accept_new)
    try:
        _, out, _ = client.exec_command(_PROBE_SCRIPT, timeout=15)
        lines = out.read().decode(errors="replace").splitlines()
        while len(lines) < 7:
            lines.append("")
        info = {
            "ok": True,
            "uid": lines[0].strip(),
            "user": lines[1].strip(),
            "kernel": lines[2].strip(),
            "arch": lines[3].strip(),
            "os_pretty": lines[4].strip(),
            "caps": {"rsync": lines[5].strip() == "yes",
                     "sha256sum": lines[6].strip() == "yes",
                     "sftp": False},
            "fingerprint": policy.seen or expected_fp,
        }
        # SFTP is not optional: without it there is no import, deploy or browse. Find out
        # now, at enrollment, rather than at the first transfer.
        try:
            client.open_sftp().close()
            info["caps"]["sftp"] = True
        except Exception as e:
            raise SSHError(
                f"the device answered SSH but has no SFTP subsystem ({e}). "
                "Enable 'Subsystem sftp' in /etc/ssh/sshd_config."
            )
        return info
    finally:
        client.close()


def install_key(host, port, user, password, pubkey_line, *, expected_fp=None,
                accept_new=True) -> dict:
    """Append our public key to the device's authorized_keys, idempotently.

    One shell round trip rather than an SFTP read-modify-write: no race, and the file and
    directory permissions are fixed in the same shot (sshd silently ignores authorized_keys
    if either is group/world-writable).
    """
    if "\n" in pubkey_line or "'" in pubkey_line:
        raise SSHError("generated key line is malformed")
    script = f"""
set -e
umask 077
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
if [ ! -f "$HOME/.ssh/authorized_keys" ]; then
  touch "$HOME/.ssh/authorized_keys"
  echo CREATED_AK
fi
chmod 600 "$HOME/.ssh/authorized_keys"
if grep -qxF '{pubkey_line}' "$HOME/.ssh/authorized_keys"; then
  echo ALREADY
else
  printf '%s\\n' '{pubkey_line}' >> "$HOME/.ssh/authorized_keys"
  echo ADDED
fi
"""
    client, policy = _connect(host, port, user, password=password,
                              expected_fp=expected_fp, accept_new=accept_new)
    try:
        _, out, err = client.exec_command(script, timeout=20)
        text = out.read().decode(errors="replace")
        rc = out.channel.recv_exit_status()
        if rc != 0:
            detail = err.read().decode(errors="replace").strip() or f"exit {rc}"
            # A missing home directory is a real misconfiguration; don't paper over it by
            # creating one, just say so.
            raise SSHError(f"could not install the key: {detail}")
        return {"created_ssh_dir": "CREATED_AK" in text,
                "already_present": "ALREADY" in text,
                "fingerprint": policy.seen or expected_fp}
    finally:
        client.close()


def verify_key_auth(host, port, user, key_path, expected_fp) -> dict:
    """Prove key auth works on a fresh connection before we throw the password away."""
    return probe(host, port, user, key_path=key_path, expected_fp=expected_fp)


# --- pooled sessions ----------------------------------------------------
_pool: dict = {}                      # pc_ip -> Session
_locks: dict = {}                     # pc_ip -> threading.Lock
_stale: set = set()                   # pc_ip whose pooled session must not be reused
_pool_guard = threading.Lock()


def _lock_for(pc_ip):
    with _pool_guard:
        lk = _locks.get(pc_ip)
        if lk is None:
            lk = _locks[pc_ip] = threading.Lock()
        return lk


@contextmanager
def session(pc_ip: str, opener):
    """Borrow this device's connection, opening it if needed.

    Holding the device's lock for the whole block means one device can never occupy more
    than one worker thread — the dashboard fans /drift out to every PC at once, and an
    import can run for minutes.

    `opener()` must return a live Session; ssh_ops supplies it so this module stays
    unaware of the database.
    """
    lock = _lock_for(pc_ip)
    lock.acquire()
    try:
        with _pool_guard:
            sess = _pool.pop(pc_ip, None) if pc_ip in _stale else _pool.get(pc_ip)
            _stale.discard(pc_ip)
        if sess is not None and not sess.alive():
            sess.close()
            sess = None
        if sess is None:
            sess = opener()
            with _pool_guard:
                _pool[pc_ip] = sess
        try:
            yield sess
        except Exception:
            # A half-dead connection poisons every later call; drop it so the next
            # caller reconnects cleanly.
            sess.close()
            with _pool_guard:
                if _pool.get(pc_ip) is sess:
                    _pool.pop(pc_ip, None)
            raise
    finally:
        lock.release()


def is_busy(pc_ip: str) -> bool:
    """True if an operation currently holds this device's connection.

    The liveness poller uses this to skip a device mid-import or mid-deploy: it would
    otherwise block on the lock for the whole transfer, stalling the poll of every other
    device behind it. A transfer in progress is proof of life anyway.
    """
    lock = _lock_for(pc_ip)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def drop(pc_ip: str):
    """Stop reusing this device's connection — after a failure, or when its credentials
    changed.

    Only closes the session if nobody is using it. An import holds its session for
    minutes, and yanking it out from under that thread would fail the transfer; marking
    it stale instead means the holder finishes normally and the next caller reconnects.
    """
    lock = _lock_for(pc_ip)
    if lock.acquire(blocking=False):
        try:
            with _pool_guard:
                sess = _pool.pop(pc_ip, None)
                _stale.discard(pc_ip)
            if sess is not None:
                sess.close()
        finally:
            lock.release()
    else:
        with _pool_guard:
            _stale.add(pc_ip)


def close_all():
    with _pool_guard:
        sessions = list(_pool.values())
        _pool.clear()
        _stale.clear()
    for s in sessions:
        s.close()


class Session:
    """A live SSH + SFTP connection to one device."""

    def __init__(self, client):
        self.client = client
        self.sftp = client.open_sftp()

    def alive(self) -> bool:
        tr = self.client.get_transport()
        return bool(tr and tr.is_active())

    def close(self):
        for closer in (getattr(self, "sftp", None), self.client):
            try:
                closer.close()
            except Exception:
                pass

    # -- shell ----------------------------------------------------------
    def run(self, script: str, timeout: int = 20):
        _, out, err = self.client.exec_command(script, timeout=timeout)
        data = out.read().decode(errors="replace")
        rc = out.channel.recv_exit_status()
        return rc, data, err.read().decode(errors="replace")

    # -- listing --------------------------------------------------------
    def listdir(self, path: str, cap: int = 5000):
        """One directory level, in the shape the dashboard's file tree expects.

        Symlinks need care: listdir_attr returns lstat results, so a symlink pointing at
        a directory looks like a plain file and the tree refuses to expand it. This is the
        same trap agent/fsops.go documents for junctions. Resolve each link with stat();
        a broken link degrades to a zero-byte file, never a directory.
        """
        path = _norm(path)
        entries, truncated = [], 0
        attrs = self.sftp.listdir_attr(path)
        if len(attrs) > cap:
            truncated = len(attrs)
            attrs = attrs[:cap]
        for a in attrs:
            full = posixpath.join(path, a.filename)
            mode = a.st_mode or 0
            is_link = stat.S_ISLNK(mode)
            size = a.st_size or 0
            if is_link:
                try:
                    t = self.sftp.stat(full)
                    mode, size = t.st_mode or 0, t.st_size or 0
                except IOError:
                    mode = stat.S_IFREG          # broken link: show it, don't expand it
                    size = 0
            entries.append({"name": a.filename, "path": full,
                            "is_dir": stat.S_ISDIR(mode), "is_link": is_link,
                            "size": size, "mode": mode,
                            "mtime": a.st_mtime or 0})
        entries.sort(key=lambda e: (not e["is_dir"], e["name"]))
        return entries, truncated

    # -- walking --------------------------------------------------------
    def walk(self, root: str, excludes, label: str = ""):
        """Yield (rel, full, size, mtime, mode) for every included regular file under
        `root`. The SFTP counterpart of agent/fsops.go walkFollow: follows directory
        symlinks (config often lives behind one), skips unreadable directories rather
        than aborting the whole import, and keeps a visited set of resolved paths so a
        cyclic link terminates.
        """
        root = _norm(root)
        visited = set()
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                real = self.sftp.normalize(current)
            except IOError:
                real = current
            if real in visited:
                continue
            visited.add(real)
            try:
                attrs = self.sftp.listdir_attr(current)
            except IOError:
                continue          # unreadable dir — skip, don't fail the run
            for a in attrs:
                full = posixpath.join(current, a.filename)
                mode = a.st_mode or 0
                size = a.st_size or 0
                mtime = a.st_mtime or 0
                if stat.S_ISLNK(mode):
                    try:
                        t = self.sftp.stat(full)
                        mode, size, mtime = t.st_mode or 0, t.st_size or 0, t.st_mtime or mtime
                    except IOError:
                        continue  # broken link
                if stat.S_ISDIR(mode):
                    stack.append(full)
                    continue
                if not stat.S_ISREG(mode):
                    continue      # fifo/socket/device — walking /dev would never finish
                rel = globmatch.repo_rel(label, root, full)
                if globmatch.excluded(rel, excludes):
                    continue
                yield rel, full, size, mtime, mode

    def isdir(self, path: str) -> bool:
        try:
            return stat.S_ISDIR(self.sftp.stat(_norm(path)).st_mode or 0)
        except IOError:
            return False

    def stat(self, path: str):
        try:
            return self.sftp.stat(_norm(path))
        except IOError:
            return None

    # -- transfer -------------------------------------------------------
    def read_file(self, path: str, cap: int | None = None) -> bytes:
        try:
            with self.sftp.open(_norm(path), "rb") as f:
                size = cap if cap is not None else (f.stat().st_size or 0)
                if size:
                    f.prefetch(size)   # without this paramiko reads are painfully slow
                return f.read(size) if cap is not None else f.read()
        except IOError as e:
            raise SSHError(f"reading {path}: {e}") from e

    def mkdirs(self, path: str):
        path = _norm(path)
        missing = []
        probe_path = path
        while probe_path not in ("/", "") and not self.isdir(probe_path):
            missing.append(probe_path)
            probe_path = posixpath.dirname(probe_path)
        for p in reversed(missing):
            try:
                self.sftp.mkdir(p)
            except IOError as e:
                if not self.isdir(p):     # lost a race, or genuinely can't create it
                    raise SSHError(f"creating directory {p}: {e}") from e

    def write_file(self, path: str, data: bytes, mode: int = 0o644, mtime=None):
        """Write atomically, then restore the file's mode and timestamp.

        Both of the trailing steps matter. The exec bit is the only permission git
        records, so a script deployed without it is broken on arrival. And without
        restoring mtime, every deploy leaves the tree looking modified, so the very next
        drift check reports the whole load as dirty.
        """
        path = _norm(path)
        self.mkdirs(posixpath.dirname(path))
        tmp = path + ".simtmp"
        try:
            self.sftp.putfo(io.BytesIO(data), tmp, len(data), confirm=True)
            try:
                self.sftp.posix_rename(tmp, path)
            except (IOError, AttributeError):
                # posix-rename@openssh.com missing: plain rename can't clobber, so unlink first.
                try:
                    self.sftp.remove(path)
                except IOError:
                    pass
                self.sftp.rename(tmp, path)
            self.sftp.chmod(path, mode)
            if mtime:
                self.sftp.utime(path, (mtime, mtime))
        except IOError as e:
            # Bare "[Errno 13] Permission denied" says nothing about which file, so name it.
            try:
                self.sftp.remove(tmp)          # don't leave a .simtmp behind
            except IOError:
                pass
            raise SSHError(f"writing {path}: {e}") from e

    def check_writable(self, path: str):
        """Can we actually write here? Returns None if yes, else the reason.

        Used before a mirror starts, so a permissions problem is reported up front
        instead of after some of the files have already been replaced.
        """
        probe = posixpath.join(_norm(path), ".sim-write-probe")
        try:
            with self.sftp.open(probe, "wb") as f:
                f.write(b"")
        except IOError as e:
            return str(e) or e.__class__.__name__
        try:
            self.sftp.remove(probe)
        except IOError:
            pass
        return None

    def remove(self, path: str):
        """Unlink a file or symlink. Never follows the link."""
        self.sftp.remove(_norm(path))

    def rmdir(self, path: str):
        try:
            self.sftp.rmdir(_norm(path))
            return True
        except IOError:
            return False          # not empty, or not ours to remove

    def normalize(self, path: str) -> str:
        try:
            return self.sftp.normalize(_norm(path))
        except IOError:
            return _norm(path)


def _norm(path: str) -> str:
    p = (path or "/").replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return posixpath.normpath(p)
