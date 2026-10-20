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
#: where the internal keys of `remote` attachments live: one synthetic
#: profile-shaped NAME per endpoint, under the root but dot-namespaced so
#: `profiles()` never lists it and no verb ever treats it as a directory.
REMOTE_DIR = ".remote"
LOCK_NAME_LIMIT = 40        # the readable tail of a lock file name
#: the key the ROOT's own lock is derived from. It ends in a separator, which
#: `os.path.relpath` never produces for a path under the root, so no profile
#: path can spell it (see `lock_path`).
ROOT_KEY = "_root" + os.sep

__all__ = ["DEFAULT_ROOT", "LOCK_DIR", "LOCK_FILE", "LOCK_NAME_LIMIT",
           "MARKER", "PID_FILE", "REMOTE_DIR", "ROOT_ENV", "ROOT_KEY",
           "ensure_root", "expand", "identity", "is_managed", "is_remote_key",
           "lock_path", "mark", "marked", "norm", "pid_file",
           "profile_dir", "remote_dir", "remote_key", "root"]

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


def identity(path: object) -> str:
    """`norm` with the symlinks resolved: one identity for one directory.

    The liveness and lock guards compare paths, and `is_managed` already
    accepts a profile by its REAL path: a root reached through a symlink
    (`BROWSER_CONTROL_ROOT=~/link` -> `/data/profiles`) could therefore hold
    a profile whose two spellings are both managed but compare UNEQUAL. That
    defeated both guards at once -- each spelling took its own lock, and
    `refuse_live` read a running browser as idle, so `profile reset --force`
    could wipe the directory of the browser it could not see (a review
    measured the hole). An empty path stays empty, like `norm`.
    """
    text = norm(path)
    return os.path.realpath(text) if text else ""


def root() -> str:
    """Where the managed profiles live (`BROWSER_CONTROL_ROOT` moves it)."""
    return expand(os.environ.get(ROOT_ENV) or DEFAULT_ROOT)


def remote_dir() -> str:
    """The directory the internal REMOTE keys are shaped under, at USE."""
    return os.path.join(root(), REMOTE_DIR)


def remote_key(host: str, port: int) -> str:
    """The one internal key for a loopback endpoint: `<root>/.remote/H-P`.

    Built at USE, like every other path under the root, and injective for the
    three host spellings the URL policy admits (`127.0.0.1`, `localhost`,
    `::1` joined by `-` to a decimal port). It is a NAME, never a directory:
    nothing in the tree creates it, and `profiles()` skips dot-directories so
    a stray one would still not be listed.
    """
    return os.path.join(remote_dir(), f"{host}-{as_port(port)}")


def is_remote_key(path: object) -> bool:
    """Is this path a remote attachment's internal key (not a profile)?"""
    text = norm(path)
    if not text:
        return False
    return text.startswith(remote_dir() + os.sep)


def as_port(value: object) -> int:
    """A port number, or 0 — the one reading the key builder uses."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return number if 0 < number < 65536 else 0


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
        # ...and NARROW one that already existed: `mode` applies only to a leaf
        # `makedirs` CREATES, and a root from an image, an older version or a
        # plain `mkdir -p` kept the umask default (0755), where any local user
        # could list every managed profile's name (a review flagged it). Every
        # creator calls this first, so this is the one place the mode is owed.
        os.chmod(path, 0o700)
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
    root keeps its own lock under `ROOT_KEY` — a sentinel ending in a
    separator, which `relpath` never produces for a path under the root, so no
    profile name can spell it: a profile literally named `<root>/_root` used
    to key exactly `_root` like the root, so `instance_locks` flocked the SAME
    file twice and refused `profile-busy` after the wait naming the caller's
    own pid (a review measured it). `_lock_name`'s digest is what keeps the two
    files apart even though the readable tails look alike.

    Both sides are resolved through SYMLINKS before the key is derived
    (`identity`'s rule on the target, realpath on the root): a profile reached
    through a linked root and the same profile reached by its real path are
    one directory and must key ONE lock. Lexically they keyed two, so two
    callers took two different locks on one profile and both proceeded (a
    review measured it).
    """
    target = os.path.realpath(expand(profile))
    base = os.path.realpath(root())
    rel = os.path.relpath(target, base)
    if rel == os.curdir:
        key = ROOT_KEY
    elif rel == os.pardir or rel.startswith(os.pardir + os.sep):
        # a caller-named path outside the root: key it by the whole path
        key = target.strip(os.sep) or ROOT_KEY
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
