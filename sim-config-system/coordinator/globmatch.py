"""Path labelling and exclude-glob matching for coordinator-side (SSH) transfers.

A faithful port of the Go agent's agent/fsops.go, so a device managed over SSH lands in
the monorepo with exactly the same layout a Windows agent would produce for the same
manifest entry. The repo-relative path a file gets here is also what excludes are matched
against, which is what lets per-app globs like "ProSim-AR/Navdata/**" work as written.

Deliberate divergence from the Go original: matching is CASE-SENSITIVE by default.
fsops.go hard-codes (?i) because Windows paths are case-insensitive; on Linux "Cache/"
and "cache/" are genuinely different directories. manifest.yaml's defaults already spell
out both variants, so nothing regresses.
"""
import hashlib
import os
import posixpath
import re
import stat

# glob pattern (+ case flag) -> compiled regex. Patterns come from the manifest, so the
# set is small and bounded.
_CACHE: dict[tuple[str, bool], re.Pattern] = {}

# Regex metacharacters escaped verbatim by fsops.go:107. '-' is absent on purpose: it is
# only special inside a character class, and we never emit the pattern's chars into one.
_SPECIAL = set(".+()|^${}[]\\")


def _slash(path: str) -> str:
    return path.replace("\\", "/")


def _base(path: str) -> str:
    """Trailing-slash-tolerant basename on a forward-slash path — filepath.Base semantics."""
    p = _slash(path).rstrip("/")
    return posixpath.basename(p) if p else "/"


def glob_to_regex(glob: str) -> str:
    """Convert a /-separated glob (**, *, ?) to an anchored regex. Port of
    fsops.go:84-116, including the '**/' -> '(?:.*/)?' rule that makes "a/**/b" and
    "a/**" both behave."""
    g = _slash(glob)
    out = ["^"]
    i = 0
    n = len(g)
    while i < n:
        c = g[i]
        if c == "*":
            if i + 1 < n and g[i + 1] == "*":
                i += 1
                if i + 1 < n and g[i + 1] == "/":
                    i += 1
                    out.append("(?:.*/)?")   # ** matches across separators
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")          # * stays within a segment
        elif c == "?":
            out.append("[^/]")
        elif c in _SPECIAL:
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    out.append("$")
    return "".join(out)


def _compiled(pattern: str, ignore_case: bool) -> re.Pattern:
    key = (pattern, ignore_case)
    rx = _CACHE.get(key)
    if rx is None:
        rx = re.compile(glob_to_regex(pattern), re.IGNORECASE if ignore_case else 0)
        _CACHE[key] = rx
    return rx


def excluded(rel_path: str, patterns, ignore_case: bool = False) -> bool:
    """True if a repo-relative path matches any denylist glob."""
    rel = _slash(rel_path)
    for p in patterns or ():
        if _compiled(p, ignore_case).match(rel):
            return True
    return False


def live_labels(live) -> dict[str, str]:
    """Map each of an app's `live` dirs to the subfolder it occupies inside the app's repo
    folder. Port of fsops.go:26-45.

      - 1 live dir  -> ""  (its contents land directly under app.repo)
      - N live dirs -> the dir's basename, so they don't collide; basenames that DO
        collide (e.g. P3D's two "Lockheed Martin" dirs) get a short path-hash suffix.
    """
    live = list(live or [])
    if len(live) == 1:
        return {live[0]: ""}
    seen: dict[str, int] = {}
    for d in live:
        k = _base(d).lower()
        seen[k] = seen.get(k, 0) + 1
    labels = {}
    for d in live:
        base = _base(d)
        if seen[base.lower()] > 1:
            h = hashlib.sha1(_slash(d).encode()).hexdigest()[:6]
            base = f"{base}__{h}"
        labels[d] = base
    return labels


def repo_rel(label: str, live_dir: str, full_path: str) -> str:
    """Forward-slash repo-relative path of a file under a live dir. Port of fsops.go:49-59."""
    base = _slash(live_dir).rstrip("/")
    full = _slash(full_path)
    try:
        rel = posixpath.relpath(full, base) if base else full
    except ValueError:
        rel = posixpath.basename(full)
    return f"{label}/{rel}" if label else rel


def walk_local(root, label: str, excludes, ignore_case: bool = False):
    """Walk an exported repo subtree, yielding (rel, full_path, size, mtime, mode) for
    each included regular file — the same shape the SFTP walker yields, so the local and
    remote indexes can be compared key for key.

    Symlinks are NOT followed (an export from git contains none) and are skipped.
    """
    root = str(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            rel = repo_rel(label, root, full)
            if excluded(rel, excludes, ignore_case):
                continue
            yield rel, full, st.st_size, st.st_mtime, st.st_mode
