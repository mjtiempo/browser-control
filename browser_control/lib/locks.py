"""locks — the one profile lock, typed, and the root→profile choreography.

A check-then-act two processes can enter at once is two browsers on one
profile — the corruption Chrome's own "profile appears to be in use" warning
exists to prevent. The verbs serialize it themselves: `flock` on a file that
lives BESIDE the profiles (`<root>/.locks/`), which the kernel releases
when the holder dies, so there is no stale lock to clean up. Beside, not
inside: `profile reset` wipes the profile while holding its lock, and a lock
file inside the wiped tree was deleted mid-hold and re-created by the next
`open` as a fresh inode — mutual exclusion was believed, not held.

Contention is waited on and then refused `profile-busy` (naming the pid and
verb the holder wrote). A lock that cannot be TAKEN at all — the path cannot be
opened (a directory or a symlink planted there, a permission the caller does
not have), or the filesystem cannot `flock` (ENOLCK) — is refused
`profile-unusable`, naming the path and the OS error. It is never a warning the
caller runs through: a guard whose failure mode is "proceed anyway" is no guard,
and two `open`s on one profile is the corruption this module exists to prevent
(a review measured the fail-open: a lock path pre-created as a directory, or a
filesystem without `flock`, let both callers through). The ordering invariant
lives in `instance_locks`, not in a comment: root lock, then profile lock.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import time
from dataclasses import dataclass
from typing import Any

from browser_control.lib.errors import (
    ERR_PROFILE_BUSY,
    ERR_PROFILE_UNUSABLE,
    fail,
)
from browser_control.lib.paths import (
    ensure_root,
)

LOCK_WAIT_S = 20.0          # as long as a cold launch is given

__all__ = ["LOCK_WAIT_S", "LockState", "instance_locks", "profile_lock"]


@dataclass
class LockState:
    """What one flock produced: held, with at most a warning to report.

    Every state this module YIELDS is `held: True`: a lock that cannot be taken
    refuses (see `profile_lock`) instead of coming back `held: False`, so no
    caller can read "not held" as "proceed". `warning` stays for the one
    held-but-imperfect case — a holder line that could not be written, so a
    later contention refusal has to say "no details" — and for the callers that
    already read it.
    """

    held: bool
    warning: str = ""

    def warn(self, reply: dict) -> dict:
        """Carry a cannot-lock warning into a reply, appending to one there."""
        if self.warning:
            reply["warning"] = (f"{reply['warning']}; {self.warning}"
                                if "warning" in reply else self.warning)
        return reply


def _lock_holder(handle: Any) -> str:
    """What the holder wrote: "pid 123 since 12:34:56 (open)", or ""."""
    try:
        handle.seek(0)
        parts = handle.read().strip().split("\t")
    except OSError:
        return ""
    if len(parts) < 2:
        return ""
    stamp = parts[1][11:19] or parts[1]
    verb = f" ({parts[2]})" if len(parts) > 2 and parts[2] else ""
    return f"pid {parts[0]} since {stamp}{verb}"


def _hold(handle: Any, path: str, verb: str) -> str:
    """Say who holds it, and since when, so a refusal can name them.

    Returns "" when the holder line is written, or a warning when it is not.
    The lock IS held either way — mutual exclusion does not depend on the
    file's contents — so a write that fails here is reported, never raised; the
    cost is that a later contention refusal can only say "no details", which is
    what the caller is told now.
    """
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\t{time.strftime('%Y-%m-%dT%H:%M:%S')}\t"
                     f"{verb}\n")
        handle.flush()
    except OSError as e:
        return (f"{path} is held by this call, but its holder line could not "
                f"be written ({e})")
    return ""


def _contention(error: OSError) -> bool:
    """Is that flock failure someone else holding the lock?

    The difference decides what happens next: contention is worth waiting for,
    while a filesystem that cannot lock at all is refused `profile-unusable`.
    """
    return error.errno in (errno.EACCES, errno.EAGAIN)


def _expired(deadline: float) -> bool:
    """Has the wait run out? A call, so a refusal handler reads as one."""
    return time.monotonic() >= deadline


def _holder_text(handle: Any) -> str:
    """What the holder wrote, or a phrase for "nothing usable"."""
    return _lock_holder(handle) or "no details"


def _acquire(handle: Any, path: str, verb: str, wait: float) -> str:
    """Take the lock, or say why not: "" when taken, else a warning.

    Contention is waited on and then refused `profile-busy` (naming the pid
    and verb the holder wrote). Anything else is a lock the filesystem will not
    GIVE — `flock` on a filesystem without it answers ENOLCK/ENOTSUP — and that
    refuses `profile-unusable`: it used to come back as a warning the caller
    reported while it ran UNLOCKED, which is the one outcome a lock exists to
    prevent. The warning returned here is `_hold`'s: held, holder line missing.
    """
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if _contention(e):
                if _expired(deadline):
                    fail(ERR_PROFILE_BUSY,
                         f"another browser-control call holds {path} "
                         f"[{_holder_text(handle)}] and has been for "
                         f"{wait:g}s — nothing was started or stopped here. "
                         "Wait for that call, then run this again")
                time.sleep(0.15)
                continue
            fail(ERR_PROFILE_UNUSABLE,
                 f"{path} cannot be locked ({e}) — this filesystem will not "
                 "hold the profile lock, so nothing was opened, closed, "
                 "seeded or wiped here")
        return _hold(handle, path, verb)


@contextlib.contextmanager
def profile_lock(path: str, verb: str, wait: float = LOCK_WAIT_S):  # noqa: ANN201
    """Hold `path` while a check-then-act runs, or REFUSE — never fail open.

    Yields `LockState(held=True)`, carrying a warning only when the holder line
    could not be written. Everything else refuses: contention after the wait is
    `profile-busy` (naming the holder), and a lock that cannot be opened or
    taken at all is `profile-unusable`, naming this path and the OS error and
    saying the profile cannot be locked. `held: False` is never yielded: the
    caller that proceeded through it ran its check-then-act unlocked, so a lock
    path pre-created as a directory or symlink, or a filesystem without
    `flock`, put two browsers on one profile (a review measured it).

    `flock` rather than an `O_EXCL` file precisely for the stale case: the
    kernel drops it when the holder exits, crashes or is killed, so there is
    nothing to clean up and nothing to trust.
    """
    with contextlib.ExitStack() as stack:
        fd = -1
        try:
            ensure_root()
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            # O_NOFOLLOW: a symlink planted at the lock path is an error the
            # caller reports, not a file we truncate through (`_hold` cuts
            # the file down to the holder line — the screenshot temp file's
            # CWE-377 guard, applied to a path an attacker can pre-create)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            handle = stack.enter_context(os.fdopen(fd, "r+", encoding="utf-8"))
        except OSError as e:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            fail(ERR_PROFILE_UNUSABLE,
                 f"cannot open a lock at {path} ({e}) — the profile cannot be "
                 "locked, so this call refuses instead of running without a "
                 "lock; remove whatever is in the way (a directory or a symlink "
                 "at that path), or use a filesystem that can hold a lock")
        warning = _acquire(handle, path, verb, wait)
        try:
            yield LockState(held=True, warning=warning)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def instance_locks(root_path: str, profile_path: str, verb: str,  # noqa: ANN201
                   *, skip_profile_lock: bool = False):
    """The root lock, then the profile lock — the one ordering that is safe.

    The root lock orders the attach records; the profile lock orders the
    instance, and a profile verb must RE-CHECK liveness under it (the caller's
    line, because only the verb knows what "live" means for it).

    `skip_profile_lock` is for `--dry`: taking the profile lock would create
    the lock DIRECTORY (and the profile lock), and a dry run must not write
    anything. Yields `(root_state, profile_state)`.
    """
    with profile_lock(root_path, verb) as root_state:
        if skip_profile_lock:
            yield root_state, LockState(held=True)
            return
        with profile_lock(profile_path, verb) as profile_state:
            yield root_state, profile_state
