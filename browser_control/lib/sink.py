"""sink — the one place a verb writes a file the caller names.

Two verbs write to a path the caller chooses — `tab screenshot` (a PNG) and
`tab js --out` (the page's own value, as JSON) — and the rules are the same for
both: an ABSOLUTE path (this tool writes where it was told, not where it
happens to be run from), a parent directory that exists, no clobbering without
`--force`, a 0600 file (the payload is a page rendered in a profile that is
often SEEDED with the user's real logins, so it is not a file to publish under
the umask default), an EXCLUSIVE open so "it did not exist" is the kernel's
answer rather than a check that can lose a race, and — with `force` — a
temporary name renamed into place, so a failed write never replaces a good
file. Those rules lived in `lib/images.py` until the second writer needed them;
they live here now, parameterised by the verb that names them in its refusals.

`write_atomic` answers the bytes ON DISK, not the bytes handed in: the one
caller that must not merely assume its payload landed (`tab js --out`, whose
whole point is a value too big to cross the reply) compares that size against
what it wrote and refuses when they disagree.
"""
from __future__ import annotations

import contextlib
import os

from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    ERR_FILE_EXISTS,
    ERR_WRITE_FAILED,
    fail,
)

__all__ = ["output_path", "write_atomic"]


def output_path(verb: str, path: str, *, suffix: str = "",
                suffix_why: str = "") -> str:
    """The absolute path `verb` may write to, or a refusal.

    `suffix`/`suffix_why` name a format a verb's payload IS (`tab screenshot`
    writes a PNG, and a name that says otherwise is a lie about the file);
    a verb whose payload is plain text passes neither.
    """
    expanded = os.path.expanduser(str(path or ""))
    if not expanded:
        fail(ERR_BAD_ARGS, f"{verb}: --out needs a PATH")
    if not os.path.isabs(expanded):
        fail(ERR_BAD_ARGS,
             f"{verb}: {path!r} must be an absolute path — this tool writes "
             "where it was told, not where it happens to be run from")
    target = os.path.abspath(expanded)
    if os.path.isdir(target):
        fail(ERR_BAD_ARGS, f"{verb}: {target} is a directory")
    if suffix and not target.lower().endswith(suffix):
        fail(ERR_BAD_ARGS,
             f"{verb}: {path!r} must end in {suffix}"
             + (f" — {suffix_why}" if suffix_why else ""))
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        fail(ERR_BAD_ARGS,
             f"{verb}: {parent} is not a directory — create it first")
    return target


def write_atomic(verb: str, target: str, data: bytes, force: bool) -> int:
    """Write the bytes 0600; refuse to clobber unless `force`; answer the
    bytes ON DISK.

    Without `force` the open is exclusive, and a failed WRITE removes what it
    created instead of leaving a truncated file at a path the caller was told
    was not written. With `force` the bytes land on a temporary name and are
    renamed into place, so a failed write never replaces a good file.
    """
    if not force:
        try:
            handle = os.open(target,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fail(ERR_FILE_EXISTS,
                 f"{verb}: {target} already exists — pass --force to "
                 "replace it")
        except OSError as e:
            fail(ERR_WRITE_FAILED, f"{verb}: {target}: {e}")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(data)
        except OSError as e:
            # the exclusive open succeeded, but the WRITE failed (ENOSPC,
            # EFBIG…): without this the OSError escaped as ERR[internal] and
            # left a truncated file at a path the caller was told was not
            # written (a review flagged it)
            with contextlib.suppress(OSError):
                os.remove(target)
            fail(ERR_WRITE_FAILED, f"{verb}: {target}: {e}")
        return _size(verb, target)
    temp = f"{target}.bc-{os.getpid()}.part"
    try:
        # EXCLUSIVE and never through a link: the predictable temp name was a
        # pre-creatable symlink, and `open(..., "wb")` truncated whatever it
        # pointed at (a review flagged CWE-377). A leftover temp is refused,
        # not silently adopted.
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fail(ERR_WRITE_FAILED,
             f"{verb}: a leftover temporary file is in the way ({temp}) — "
             "remove it and try again")
    except OSError as e:
        fail(ERR_WRITE_FAILED, f"{verb}: {target}: {e}")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temp, target)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.remove(temp)
        fail(ERR_WRITE_FAILED, f"{verb}: {target}: {e}")
    return _size(verb, target)


def _size(verb: str, target: str) -> int:
    """The file's own byte count — the read-back a writer is judged by."""
    try:
        return os.stat(target).st_size
    except OSError as e:
        fail(ERR_WRITE_FAILED, f"{verb}: {target}: {e}")
