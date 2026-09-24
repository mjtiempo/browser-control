"""images — the screenshot file layer: PNG header, geometry, atomic write.

No CDP here: the bytes come from the caller, and this module judges them by
their own header, turns the page's CSS geometry into device pixels, validates
the output path, and writes the file (exclusively, or atomically over). The
verbs that talk to a page live in the DOM tier; the file rules are pure.
"""
from __future__ import annotations

import contextlib
import os

from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    ERR_FILE_EXISTS,
    ERR_SCREENSHOT_NOT_VERIFIED,
    ERR_WRITE_FAILED,
    fail,
)

PNG_SIG = b"\x89PNG\r\n\x1a\n"
MAX_PX = 100_000        # a dimension no screenshot of this page can have

__all__ = ["MAX_PX", "PNG_SIG", "expected_pixels", "output_path", "png_size",
           "write_atomic"]


def png_size(data: bytes) -> list[int]:
    """[width, height] from the PNG's OWN header, or [] when it is not one.

    The file is judged by its bytes, not by the answer that produced it: a
    screenshot whose header disagrees with the page's own geometry is refused
    before it is written anywhere.
    """
    if len(data) < 24 or not data.startswith(PNG_SIG) or data[12:16] != b"IHDR":
        return []
    return [int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big")]


def expected_pixels(css: int, dpr: float) -> int:
    """CSS pixels → device pixels, or a refusal.

    Both numbers come from the PAGE, so a nonsense pair must become a refusal
    rather than an exception or a silently wrong expectation: this is the value
    the PNG's own header is compared against.
    """
    try:
        value = int(round(css * dpr))
    except (OverflowError, ValueError) as e:
        fail(ERR_SCREENSHOT_NOT_VERIFIED,
             f"the page reports {css} px at devicePixelRatio {dpr:g}, which is "
             f"not a size ({e}) — nothing was written")
    if not 0 < value <= MAX_PX:
        fail(ERR_SCREENSHOT_NOT_VERIFIED,
             f"the page reports {css} px at devicePixelRatio {dpr:g}, i.e. "
             f"{value} device pixels — no screenshot of it exists — nothing "
             "was written")
    return value


def output_path(path: str) -> str:
    """The absolute path a screenshot may be written to, or a refusal."""
    expanded = os.path.expanduser(str(path or ""))
    if not os.path.isabs(expanded):
        # the help and this function's own docstring say ABSOLUTE; a relative
        # path used to be silently resolved against the CLI's cwd (a review
        # flagged the mismatch with `tab upload`, which refuses one)
        fail(ERR_BAD_ARGS,
             f"tab screenshot: {path!r} must be an absolute path — this tool "
             "writes where it was told, not where it happens to be run from")
    target = os.path.abspath(expanded)
    if os.path.isdir(target):
        fail(ERR_BAD_ARGS, f"tab screenshot: {target} is a directory")
    if not target.lower().endswith(".png"):
        fail(ERR_BAD_ARGS,
             f"tab screenshot: {path!r} must end in .png — the data IS a PNG, "
             "and a name that says otherwise is a lie about the file")
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        fail(ERR_BAD_ARGS,
             f"tab screenshot: {parent} is not a directory — create it first")
    return target


def write_atomic(target: str, data: bytes, force: bool) -> None:
    """Write the PNG; refuse to clobber unless `force`.

    The file is 0600 like every other file this tool writes: a screenshot is
    the rendered page of a profile that is often SEEDED with the user's real
    logins, so it is not a file to publish under the umask default (a review
    flagged the 0644). Without `force` the open is EXCLUSIVE, so "it did not
    exist" is the kernel's answer rather than a check that can lose a race.
    With `force` the bytes land on a temporary name and are renamed into
    place, so a failed write never replaces a good file.
    """
    if not force:
        try:
            handle = os.open(target,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fail(ERR_FILE_EXISTS,
                 f"tab screenshot: {target} already exists — pass --force to "
                 "replace it")
        except OSError as e:
            fail(ERR_WRITE_FAILED, f"tab screenshot: {target}: {e}")
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
            fail(ERR_WRITE_FAILED, f"tab screenshot: {target}: {e}")
        return
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
             f"tab screenshot: a leftover temporary file is in the way "
             f"({temp}) — remove it and try again")
    except OSError as e:
        fail(ERR_WRITE_FAILED, f"tab screenshot: {target}: {e}")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temp, target)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.remove(temp)
        fail(ERR_WRITE_FAILED, f"tab screenshot: {target}: {e}")
