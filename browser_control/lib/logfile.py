"""logfile — the action log's file and permission policy.

The lines carry the argv of a call (`tab js <program>`, a file path), so the
file is opened 0600, an existing wider file is narrowed with `fchmod`, and a
short write is NOT success: a full filesystem must not truncate the record
while the writer reports it landed. When the configured path cannot be
written — or a wider file cannot be narrowed — the line goes to a private
scratch directory (`mkdtemp`, unique and 0700). A record is lost only when
neither can be written, never silently.

`lib/audit.py` owns the RECORD (what a line says, and what a proven secret
becomes); this module owns where it goes.
"""
from __future__ import annotations

import os
import stat
import tempfile
import time

SCRATCH_PREFIX = "browser-control"

__all__ = ["SCRATCH_PREFIX", "FileSink"]


class FileSink:
    """The file side of the action log: one line, one path, one scratch dir."""

    def __init__(self) -> None:
        self._scratch = ""

    def scratch_dir(self) -> str:
        """A PRIVATE scratch directory for this process, created on first use.

        `tempfile.mkdtemp`: the name is unique, the mode is 0700, and the
        directory is ours. The timestamped name this used to build could be
        pre-created by another local user — or be a symlink — and
        `exist_ok=True` accepted it, so the fallback log could land in a
        directory they owned, or fail against all eight names and drop the
        record (a review flagged it). Two runs in the same second also shared
        one directory before, which the docstring claimed they never would. ""
        when even /tmp cannot be written, which every caller has to read as
        "no scratch".
        """
        if self._scratch:
            return self._scratch
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            self._scratch = tempfile.mkdtemp(prefix=f"{SCRATCH_PREFIX}-{stamp}-")
        except OSError:
            return ""
        return self._scratch

    def fallback(self, path: str) -> str:
        """Where the record goes when the configured file cannot be written."""
        scratch = self.scratch_dir()
        if not scratch or os.path.dirname(os.path.abspath(path)) == scratch:
            return ""
        return os.path.join(scratch, "actions.jsonl")

    def write(self, path: str, line: str) -> bool:
        """One line to one file, making its directory first. False on failure.

        Opened 0600, and an existing wider file is narrowed with `fchmod`: the
        lines carry the argv, so a world-readable action log is the second half
        of the promise that only a PROVEN secret is redacted (a review flagged
        the mode).

        Opened `O_NOFOLLOW` and only if it is a REGULAR file: a symlink at the
        path had every line appended to whatever it pointed at, and the target
        `fchmod`ed 0600 — arbitrary append into a file this CLI did not choose
        (CWE-377, the guard the pid-file writer already carries). A refused
        file falls back to the private scratch log like any other failure.
        """
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                             | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(handle)
                if not stat.S_ISREG(info.st_mode):
                    return False
                if stat.S_IMODE(info.st_mode) & 0o077:
                    try:
                        os.fchmod(handle, 0o600)
                    except OSError:
                        # a file we cannot narrow may be readable by others, and
                        # this line carries the argv: refuse THIS file and let
                        # the caller fall back to the private scratch log rather
                        # than write it where it cannot be protected (a review
                        # flagged that the chmod could lose the record)
                        return False
                payload = line.encode("utf-8")
                offset = 0
                while offset < len(payload):
                    # `os.write` may write fewer bytes than asked (a full
                    # filesystem, RLIMIT_FSIZE); the return value was dropped,
                    # so a short write silently truncated the line while the
                    # writer still said it succeeded — no scratch fallback, no
                    # record (a review flagged it).
                    written = os.write(handle, payload[offset:])
                    if written <= 0:
                        return False
                    offset += written
            finally:
                os.close(handle)
            return True
        except OSError:
            return False
