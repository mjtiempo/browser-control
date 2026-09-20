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

__all__ = ["DEFAULT_ROOT", "LOCK_FILE", "PID_FILE", "ROOT_ENV", "expand",
           "is_managed", "lock_path", "norm", "pid_file", "profile_dir",
           "root"]


def expand(path: object) -> str:
    """A path as the caller meant it: `~` expanded and absolute."""
    return os.path.abspath(os.path.expanduser(str(path or "")))


def norm(path: object) -> str:
    """One identity for a profile path: absolute, `~` expanded."""
    return expand(path)


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
    """The file the profile's `flock` lives in, inside its profile."""
    return os.path.join(profile, LOCK_FILE)


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
