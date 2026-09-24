"""paths — one resolver for the managed-profile root and the paths under it.

`BROWSER_CONTROL_ROOT` moves the whole tree; every managed path is derived
from it here, so two modules cannot disagree about where "ours" begins. The
private spellings `browser.py` grew up with stay as aliases there; the
implementations live here.
"""
from __future__ import annotations

import hashlib
import os

DEFAULT_ROOT = "~/.local/share/browser-control/cdp-profiles"
ROOT_ENV = "BROWSER_CONTROL_ROOT"
PID_FILE = ".pid"
LOCK_FILE = ".browser-control.lock"
LOCK_DIR = ".locks"
LOCK_NAME_LIMIT = 40        # the readable tail of a lock file name

__all__ = ["DEFAULT_ROOT", "LOCK_DIR", "LOCK_FILE", "LOCK_NAME_LIMIT",
           "MARKER", "PID_FILE", "ROOT_ENV", "ensure_root", "expand",
           "is_managed", "lock_path", "mark", "marked", "norm",
           "pid_file", "profile_dir", "root"]

MARKER = ".browser-control.profile"


def expand(path: object) -> str:
    """A path as the caller meant it: `~` expanded and absolute."""
    return os.path.abspath(os.path.expanduser(str(path or "")))


def norm(path: object) -> str:
    """One identity for a profile path: absolute, `~` expanded.

    An empty path stays empty. `abspath("")` is the CWD, and a census row for
    a browser started with no `--user-data-dir` carries an empty profile: with
    the CWD identity that row matched `--profile $(pwd)` (a review measured the
    mis-attribution, an attach record keyed to the working directory).
    """
    text = str(path or "").strip()
    return expand(text) if text else ""


def root() -> str:
    """Where the managed profiles live (`BROWSER_CONTROL_ROOT` moves it)."""
    return expand(os.environ.get(ROOT_ENV) or DEFAULT_ROOT)


def ensure_root() -> str:
    """The managed root, created 0700 when it is missing.

    Every creator under the root calls this BEFORE its own leaf, because the
    `mode` of `os.makedirs` applies to the LEAF only: a call for a path UNDER
    the root makes the root an intermediate, and it landed at the umask
    default (0755) — so the root's mode depended on which verb ran first, and
    any local user could list the profile names it holds. Here the root is the
    leaf of its own call, so it is always the intended 0700 (a review flagged
    the 0755 root).
    """
    path = root()
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        # the caller's refusal (it names the verb and the path): re-raise the
        # filesystem error unchanged rather than turning it into a different
        # exception type here
        raise
    return path


def mark(profile: object) -> None:
    """Mark a directory as one THIS CLI created (best-effort).

    `reset --force` and `seed --force` recursively delete under the managed
    root, and the root itself is caller-chosen when `BROWSER_CONTROL_ROOT` is
    set: "everything under the root is ours" is a declaration, not proof. The
    marker is the proof the wipe guard can check (a review found the
    arbitrary-delete path a caller-chosen root opens).
    """
    try:
        with open(os.path.join(str(profile), MARKER), "w",
                  encoding="utf-8") as handle:
            handle.write("browser-control\n")
    except OSError:
        pass


def marked(profile: object) -> bool:
    """Did this CLI create that directory?

    A profile under the DEFAULT root is trusted without a marker: profiles
    created before the marker existed are still this CLI's own, and the
    default root is not caller-chosen. A root moved with
    `BROWSER_CONTROL_ROOT` gets no such trust — the marker is required there.
    """
    if root() == expand(DEFAULT_ROOT):
        return True
    return os.path.isfile(os.path.join(str(profile), MARKER))


def profile_dir(binary_path: str) -> str:
    """The managed profile for a browser, keyed by the binary we run."""
    return os.path.join(root(), os.path.basename(binary_path))


def pid_file(profile: str) -> str:
    """The file a browser's pid is recorded in, inside its profile."""
    return os.path.join(profile, PID_FILE)


def _lock_name(key: str) -> str:
    """One lock file NAME per profile key — injective, readable, bounded.

    The fold this replaced was `key.replace(os.sep, "__")`, which is NOT
    injective: the profile `<root>/a/b` and a profile literally named
    `<root>/a__b` both produced `a__b.lock`, so two instances contended on one
    lock — the safe direction, but a spurious `profile-busy` naming a holder
    that was running against a different profile. An over-long path outside
    the root degraded further: `ENAMETOOLONG` made the lock unopenable and the
    caller run UNLOCKED.

    The digest of the WHOLE key is what makes the mapping injective, and 16
    hex digits of it bound the name whatever the path's length (NAME_MAX is
    255). The readable tail is only there so a name on disk still says which
    profile it belongs to.
    """
    digest = hashlib.sha256(os.fsencode(key)).hexdigest()[:16]
    return f"{key.replace(os.sep, '_')[-LOCK_NAME_LIMIT:]}.{digest}"

def lock_path(profile: str) -> str:
    """Where the `flock` for `profile` lives: `<root>/.locks/<name>.lock`.

    OUTSIDE the profile, because `profile reset` wipes the profile directory
    WHILE holding this lock: a lock file inside the tree was deleted mid-hold,
    and the next `open` recreated the path as a new inode and took it at once —
    a browser started into the directory being wiped (a review measured it).
    A file the wipe cannot reach closes that hole; lock files are never
    deleted (an unlinked lock lets a fresh inode be created and double-taken).

    The name is derived from the profile's path under the root, so two
    profiles that share a basename (`<root>/chrome` and `<root>/a/chrome`)
    never share a lock; `_lock_name` is the injective, bounded spelling of
    that derivation (the old `os.sep` fold collided `a/b` with `a__b`). The
    root keeps its own lock as `_root`, so no profile name can collide with
    it.
    """
    target = expand(profile)
    base = root()
    rel = os.path.relpath(target, base)
    if rel == os.curdir:
        key = "_root"
    elif rel == os.pardir or rel.startswith(os.pardir + os.sep):
        # a caller-named path outside the root: key it by the whole path
        key = target.strip(os.sep) or "_root"
    else:
        key = rel
    return os.path.join(base, LOCK_DIR, _lock_name(key) + ".lock")


def is_managed(profile: str) -> bool:
    """Is this profile one of ours? Prefix-safe, so a sibling root is not.

    Compared by REAL path: a symlink under the root that points somewhere else
    (the user's own Chrome profile, say) is not one of ours, and `profile seed`
    writing through it is the one thing this tool promises never to do — a
    review measured that `abspath` alone accepted it, because the lexical path
    still LOOKED managed.
    """
    text = str(profile or "")
    if not text:
        return False
    return os.path.realpath(text).startswith(os.path.realpath(root()) + os.sep)
