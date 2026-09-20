"""locks — the one profile lock, typed, and the root→profile choreography.

A check-then-act two processes can enter at once is two browsers on one
profile — the corruption Chrome's own "profile appears to be in use" warning
exists to prevent. The verbs serialize it themselves: `flock` on a file in the
profile (or in the root, for the attach records), which the kernel releases
when the holder dies, so there is no stale lock to clean up.

Contention is waited on and then refused `profile-busy` (naming the pid and
verb the holder wrote); a filesystem that cannot lock at all comes back as a
warning the caller REPORTS, never a silent no-op. The ordering invariant lives
in `instance_locks`, not in a comment: root lock, then profile lock.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import time
from dataclasses import dataclass
from typing import Any

from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_PROFILE_BUSY,
    fail,
)

LOCK_WAIT_S = 20.0          # as long as a cold launch is given

__all__ = ["LOCK_WAIT_S", "LockState", "instance_locks", "profile_lock"]


@dataclass
class LockState:
    """What one flock attempt produced: held, or a warning to report."""

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


def _hold(handle: Any, verb: str) -> None:
    """Say who holds it, and since when, so a refusal can name them."""
    with contextlib.suppress(OSError):
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\t{time.strftime('%Y-%m-%dT%H:%M:%S')}\t"
                     f"{verb}\n")
        handle.flush()


def _contention(error: OSError) -> bool:
    """Is that flock failure someone else holding the lock?

    The difference decides what happens next: contention is worth waiting for,
    while a filesystem that cannot lock at all has to be reported instead.
    """
    return error.errno in (errno.EACCES, errno.EAGAIN)


def _expired(deadline: float) -> bool:
    """Has the wait run out? A call, so a refusal handler reads as one."""
    return time.time() >= deadline


def _holder_text(handle: Any) -> str:
    """What the holder wrote, or a phrase for "nothing usable"."""
    return _lock_holder(handle) or "no details"


def _acquire(handle: Any, path: str, verb: str, wait: float) -> str:
    """Take the lock, or say why not: "" when taken, else a warning.

    Contention is waited on and then refused `profile-busy` (naming the pid
    and verb the holder wrote); a filesystem that cannot lock at all comes back
    as a warning, which the caller REPORTS rather than failing the verb — a
    guard that silently does nothing would be worse than none.
    """
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _hold(handle, verb)
            return ""
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
            return f"{path} cannot be locked ({e})"


@contextlib.contextmanager
def profile_lock(path: str, verb: str, wait: float = LOCK_WAIT_S):
    """Hold `path` while a check-then-act runs, or refuse `profile-busy`.

    Yields `LockState`: `held: False` is the filesystem-cannot-lock case,
    which the caller proceeds through and reports. Contention never reaches
    the caller as a warning — it waits, then refuses.

    `flock` rather than an `O_EXCL` file precisely for the stale case: the
    kernel drops it when the holder exits, crashes or is killed, so there is
    nothing to clean up and nothing to trust.
    """
    with contextlib.ExitStack() as stack:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            handle = stack.enter_context(
                open(path, "a+", encoding="utf-8"))
        except OSError as e:
            yield LockState(held=False,
                            warning=f"could not open a lock at {path}: {e}")
            return
        warning = _acquire(handle, path, verb, wait)
        try:
            yield LockState(held=not warning, warning=warning)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def instance_locks(root_path: str, profile_path: str, verb: str,
                   *, skip_profile_lock: bool = False):
    """The root lock, then the profile lock — the one ordering that is safe.

    The root lock orders the attach records; the profile lock orders the
    instance, and a profile verb must RE-CHECK liveness under it (the caller's
    line, because only the verb knows what "live" means for it).

    `skip_profile_lock` is for `--dry`: taking the profile lock would create
    the profile directory and its lock file, and a dry run must not write
    anything. Yields `(root_state, profile_state)`.
    """
    with profile_lock(root_path, verb) as root_state:
        if skip_profile_lock:
            yield root_state, LockState(held=True)
            return
        with profile_lock(profile_path, verb) as profile_state:
            yield root_state, profile_state
