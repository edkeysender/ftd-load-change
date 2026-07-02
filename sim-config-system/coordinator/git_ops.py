"""Thin wrapper around git on the coordinator's working clone.
This module is the ONLY place that commits, merges, tags, and pushes."""
import shutil
import subprocess
import threading
from pathlib import Path
from . import config

# Serializes every git-writing operation (stage/seal/capture/promote/rollback) so
# concurrent requests — e.g. captures from .11 and .12 in one dev session — never
# race on the single working clone. This is the lock the spec relies on.
_WRITE_LOCK = threading.Lock()


def _git(*args, check=True):
    return subprocess.run(
        ["git", "-C", str(config.WORK_CLONE), *args],
        capture_output=True, text=True, check=check,
    )


def head_sha(ref="HEAD"):
    return _git("rev-parse", ref).stdout.strip()


def ref_sha(ref):
    """Resolve a ref to a sha, or None if it doesn't exist yet (e.g. training-live
    before the first seal). Never raises."""
    r = _git("rev-parse", "--verify", "--quiet", ref, check=False)
    return r.stdout.strip() or None


def show_file(ref, path):
    """Contents of a file as of a git ref (e.g. manifest.yaml at v1.0), or None if
    the ref/file doesn't exist. Lets each version carry its own manifest."""
    r = _git("show", f"{ref}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def _has_remote() -> bool:
    return bool(_git("remote", check=False).stdout.strip())


def _push(*refs):
    """Best-effort push: a no-op when no remote is configured (e.g. local
    bootstrap before Forgejo is wired). Never fails the calling operation."""
    if not _has_remote():
        return
    _git("push", "origin", *refs, check=False)


def _push_force(*refs):
    """Force-push a movable pointer (training-live). Rollback moves training-live
    backwards, which a normal push rejects as non-fast-forward — so agents would
    keep fetching the old tip. Force keeps the remote pointer == the coordinator's."""
    if not _has_remote():
        return
    _git("push", "--force", "origin", *refs, check=False)


def publish_training_live():
    """Make the remote training-live match the coordinator's (idempotent). Called
    before every deploy so agents fetch exactly what is live."""
    _push_force(config.TRAINING_LIVE)


def ensure_repo():
    """Make the working clone usable. If GIT_REMOTE is configured and reachable,
    clone it; otherwise initialise a local repo on `master` so bootstrap works
    before Forgejo exists. Idempotent."""
    if (config.WORK_CLONE / ".git").exists():
        if _has_remote():
            _git("fetch", "--all", check=False)
        _seed_root_files()
        return
    config.WORK_CLONE.parent.mkdir(parents=True, exist_ok=True)
    cloned = False
    if config.GIT_REMOTE:
        r = subprocess.run(["git", "clone", config.GIT_REMOTE, str(config.WORK_CLONE)],
                           capture_output=True, text=True)
        cloned = r.returncode == 0
    if not cloned:
        config.WORK_CLONE.mkdir(parents=True, exist_ok=True)
        _git("init")
    # Normalize an unborn HEAD to `master`, so the first commit lands on master
    # (the spec ref). Cloning an EMPTY Forgejo repo otherwise leaves HEAD on git's
    # default branch (`main`), which broke seal's `branch -f dev master`.
    if _git("rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode != 0:
        _git("symbolic-ref", "HEAD", f"refs/heads/{config.MASTER}", check=False)
    # Fallback committer identity so annotated tags / commits never hard-fail on a
    # Pi without a global git identity. Per-op -c flags still override for attribution.
    _git("config", "user.name", "sim-coordinator", check=False)
    _git("config", "user.email", "coordinator@sim.local", check=False)
    # Git LFS: configure clean/smudge filters + pre-push hook for this clone so
    # large binaries (per .gitattributes) are stored in LFS instead of bloating git.
    _git("lfs", "install", "--local", check=False)
    _seed_root_files()


def _ident(author: str):
    """-c flags that attribute a commit/tag to the acting user."""
    return ("-c", f"user.name={author}", "-c", f"user.email={author}@sim.local")


def _seed_root_files():
    """Ensure manifest.yaml + .gitignore exist at the clone root so v1.0 is
    self-describing. Copied from the bundled seed if absent (local bootstrap)."""
    dst_manifest = config.WORK_CLONE / "manifest.yaml"
    if not dst_manifest.exists() and config.SEED_MANIFEST and config.SEED_MANIFEST.exists():
        shutil.copyfile(config.SEED_MANIFEST, dst_manifest)
    dst_ignore = config.WORK_CLONE / ".gitignore"
    if not dst_ignore.exists() and config.SEED_GITIGNORE and config.SEED_GITIGNORE.exists():
        shutil.copyfile(config.SEED_GITIGNORE, dst_ignore)
    dst_attrs = config.WORK_CLONE / ".gitattributes"
    if not dst_attrs.exists() and config.SEED_GITATTRIBUTES and config.SEED_GITATTRIBUTES.exists():
        shutil.copyfile(config.SEED_GITATTRIBUTES, dst_attrs)


def clear_folder(folder: str):
    """Remove a PC's folder in the working clone (start of a fresh import)."""
    with _WRITE_LOCK:
        target = config.WORK_CLONE / folder
        if target.exists():
            shutil.rmtree(target)


def write_files(files: dict):
    """Write one import batch into the working clone. Keys are repo paths already
    prefixed with <folder> (e.g. "pc-12-display/displays/CPTInboard/x")."""
    with _WRITE_LOCK:
        for rel_path, content in files.items():
            dst = config.WORK_CLONE / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(content)


def folder_stats(folder: str):
    """(file_count, byte_count) of a staged PC folder, for the bootstrap panel."""
    base = config.WORK_CLONE / folder
    n = b = 0
    if base.exists():
        for p in base.rglob("*"):
            if p.is_file():
                n += 1
                b += p.stat().st_size
    return n, b


def commit_all(message: str, author: str):
    _git("add", "-A")
    if not _git("status", "--porcelain").stdout.strip():
        return None  # nothing changed
    _git(*_ident(author), "commit", "-m", message)
    return head_sha()


def _ref_exists(ref: str) -> bool:
    return _git("rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0


def seal_baseline(message: str, author: str):
    """First-run: commit everything on master, tag v1.0, branch dev, set training-live.

    Idempotent: if v1.0 already exists this returns its commit and changes nothing.
    Versions are immutable — once sealed, use dev -> capture -> promote to make new
    versions, not a re-seal."""
    with _WRITE_LOCK:
        if _ref_exists("refs/tags/v1.0"):
            return head_sha("v1.0^{commit}")
        sha = commit_all(message, author)
        _git(*_ident(author), "tag", "-a", "v1.0", "-m", message)
        _git("branch", "-f", config.DEV_BRANCH, config.MASTER)
        _git("branch", "-f", config.TRAINING_LIVE, "v1.0")
        _push(config.MASTER, config.DEV_BRANCH, "v1.0")
        _push_force(config.TRAINING_LIVE)
        return sha


def capture_begin():
    """Switch the working clone to dev before a (possibly batched) capture."""
    with _WRITE_LOCK:
        _git("checkout", config.DEV_BRANCH)


def capture_write(files: dict):
    """Write one capture batch onto the dev working tree. Keys are repo-root-
    relative (e.g. "pc-12-display/displays/CPTInboard/x")."""
    with _WRITE_LOCK:
        for rel_path, content in files.items():
            target = config.WORK_CLONE / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)


def capture_commit(deleted: list, message: str, author: str):
    """Apply deletions and commit the capture to dev (serialized by the lock)."""
    with _WRITE_LOCK:
        for rel_path in deleted or []:
            p = config.WORK_CLONE / rel_path
            if p.exists():
                p.unlink()
        return commit_all(message, author)


def promote(message: str, author: str, new_tag: str, from_ref: str | None = None):
    """Squash-merge a dev build into master, tag the new version, move training-live.

    `from_ref` is the source to promote (a specific dev-N test tag); defaults to the
    dev branch tip. Promoting a tag freezes exactly that tested build as the new
    customer training version, even if dev has moved on since."""
    src = from_ref or config.DEV_BRANCH
    with _WRITE_LOCK:
        _git("checkout", config.MASTER)
        _git("merge", "--squash", src)
        sha = commit_all(f"{new_tag}: {message}", author)
        _git(*_ident(author), "tag", "-a", new_tag, "-m", message)
        _git("branch", "-f", config.TRAINING_LIVE, new_tag)
        _push(config.MASTER, new_tag)
        _push_force(config.TRAINING_LIVE)
        return sha


def rollback(tag: str):
    """Point training-live at an immutable tag (deploy happens separately).
    Force-push: moving the pointer backwards is a non-fast-forward update."""
    with _WRITE_LOCK:
        _git("branch", "-f", config.TRAINING_LIVE, tag)
        _push_force(config.TRAINING_LIVE)
        return head_sha(config.TRAINING_LIVE)


def commit_manifest(yaml_text: str, author: str = "config"):
    """Persist a manifest edit. If dev exists, commit it there (the draft branch),
    so Snapshot/Promote carry it into dev-N and v1.x. Before the first seal there's
    no dev yet, so just write the working-tree file (sealed into v1.0 later)."""
    with _WRITE_LOCK:
        if _ref_exists("refs/heads/" + config.DEV_BRANCH):
            _git("checkout", config.DEV_BRANCH)
            (config.WORK_CLONE / "manifest.yaml").write_text(yaml_text)
            _git("add", "manifest.yaml")
            if _git("diff", "--cached", "--quiet", check=False).returncode != 0:
                _git(*_ident(author), "commit", "-m", "config: update manifest")
                _push(config.DEV_BRANCH)
            return ref_sha(config.DEV_BRANCH)
        (config.WORK_CLONE / "manifest.yaml").write_text(yaml_text)
        return None


def snapshot_dev(tag: str, message: str, author: str):
    """Tag the current dev tip as a named test version (dev-v<next>) so admins have
    a list of testable builds to deploy to the sim before promoting. Force (-f):
    dev builds carry no numeric suffix, so re-snapshotting the same target version
    moves the existing tag to the new tip instead of failing."""
    with _WRITE_LOCK:
        sha = head_sha(config.DEV_BRANCH)
        _git(*_ident(author), "tag", "-f", "-a", tag, config.DEV_BRANCH, "-m", message)
        _push_force(tag)
        return sha


def changed_files(base: str, head: str):
    r = _git("diff", "--name-status", f"{base}..{head}", check=False)
    if r.returncode != 0:
        return []  # a ref doesn't exist yet (e.g. dev before seal)
    return [line.split("\t", 1) for line in r.stdout.strip().splitlines() if line]


def compare_url(base: str, head: str):
    return f"{config.FORGEJO_URL}/compare/{base}...{head}"
