"""paths — one resolver for the managed-profile root and the paths under it.

`BROWSER_CONTROL_ROOT` moves the whole tree; every managed path is derived
from it here, so two modules cannot disagree about where "ours" begins. The
private spellings `browser.py` grew up with stay as aliases there; the
implementations live here.
"""
from __future__ import annotations

import os

DEFAULT_ROOT = "~/.local/share/browser-control/cdp-profiles"
ROOT_ENV = "BROWSER_CONTROL_ROOT"
PID_FILE = ".pid"
LOCK_FILE = ".browser-control.lock"
LOCK_DIR = ".locks"

__all__ = ["DEFAULT_ROOT", "LOCK_DIR", "LOCK_FILE", "PID_FILE", "ROOT_ENV",
           "expand", "is_managed", "lock_path", "norm", "pid_file",
           "profile_dir", "root"]


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


def profile_dir(binary_path: str) -> str:
    """The managed profile for a browser, keyed by the binary we run."""
    return os.path.join(root(), os.path.basename(binary_path))


def pid_file(profile: str) -> str:
    """The file a browser's pid is recorded in, inside its profile."""
    return os.path.join(profile, PID_FILE)


def lock_path(profile: str) -> str:
    """Where the `flock` for `profile` lives: `<root>/.locks/<name>.lock`.

    OUTSIDE the profile, because `profile reset` wipes the profile directory
    WHILE holding this lock: a lock file inside the tree was deleted mid-hold,
    and the next `open` recreated the path as a new inode and took it at once —
    a browser started into the directory being wiped (a review measured it).
    A file the wipe cannot reach closes that hole; lock files are never
    deleted (an unlinked lock lets a fresh inode be created and double-taken).

    The name is the profile's path under the root with separators folded, so
    two profiles that share a basename (`<root>/chrome` and `<root>/a/chrome`)
    never share a lock. The root keeps its own lock as `_root`, so no profile
    name can collide with it.
    """
    target = expand(profile)
    base = root()
    rel = os.path.relpath(target, base)
    if rel == os.curdir:
        name = "_root"
    elif rel == os.pardir or rel.startswith(os.pardir + os.sep):
        # a caller-named path outside the root: key it by the whole path
        name = target.strip(os.sep) or "_root"
    else:
        name = rel
    return os.path.join(base, LOCK_DIR, name.replace(os.sep, "__") + ".lock")


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
