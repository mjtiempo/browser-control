"""attachments — the one owner of `attached.json`.

A record is what opens the tab-write gate, so the file's shape, its atomic
replacement and its read-modify-write discipline live in one place. Records
are keyed by NORMALISED profile path, and a missing, unreadable or malformed
file is {}: an attachment that cannot be read is not an authorization to write
anywhere.

Every read-modify-write runs under the ROOT lock (`lib/locks.py`), so two
`attach`s at once cannot lose each other's line. A caller that already holds
the root lock (`profile reset`, inside `instance_locks`) uses the explicit
`drop_unlocked`: taking the lock again would wait on the lock it holds.
"""
from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from typing import Any

from browser_control.lib import proc as proc_lib
from browser_control.lib.coerce import as_int
from browser_control.lib.errors import (
    ERR_ATTACH_FAILED,
    fail,
)
from browser_control.lib.locks import LockState, profile_lock
from browser_control.lib.paths import lock_path, norm, root

ATTACH_FILE = "attached.json"
#: A record's KIND: local attachments (the original shape, and the default a
#: record without a `kind` keeps) and REMOTE endpoints, which live in the same
#: store but belong to the `remote` noun alone. One store, two kinds: each
#: noun reads and drops only its own.
KIND_LOCAL = "local"
KIND_REMOTE = "remote"
#: How much of the attach store is a RECORD: one row is ~200 bytes (`profile`,
#: `pid`, `port`, `exe`, `managed`, `attached_at`), so a megabyte is thousands
#: of concurrent attachments — far past any real machine, and the point is only
#: that the read is BOUNDED. A store past it reads as "no records", which is
#: the fail-closed direction this file already documents for every other
#: unusable shape (no attachment is not an authorization to write anywhere).
ATTACH_FILE_CAP = 1 << 20

__all__ = ["ATTACH_FILE", "ATTACH_FILE_CAP", "KIND_LOCAL", "KIND_REMOTE",
           "STORE", "AttachmentStore"]


def row_kind(record: dict) -> str:
    """A record's kind: `"remote"`, or `"local"` for the original shape.

    One reader, so the same missing-`kind` record cannot be local to one
    caller and remote to another: the store shipped without a `kind` field,
    and every one of those records is a LOCAL attachment.
    """
    return KIND_REMOTE if str(record.get("kind") or "") == KIND_REMOTE \
        else KIND_LOCAL


class AttachmentStore:
    """The attach records under one root: read, replace, add, drop."""

    def __init__(self, base: str = "") -> None:
        self._base = base

    @property
    def base(self) -> str:
        """The root the records live under — read at USE, not at import."""
        return self._base or root()

    def path(self) -> str:
        return os.path.join(self.base, ATTACH_FILE)

    def records(self, kind: str = "") -> dict[str, dict]:
        """The attach records, keyed by absolute profile path.

        `kind` filters the store: "" is every record, `"local"` is every
        record that is not a remote attachment (a record without a `kind` is
        LOCAL — the shape the store shipped with), and `"remote"` only the
        remote ones. The filter is what lets the local verbs and the `remote`
        noun share one file without one ever touching the other's records.

        The file is chosen by whoever wrote the profile ROOT, so it is read the
        way every other file this process did not write is read
        (`proc.small_file`): `O_NOFOLLOW` refuses a symlink, `O_NONBLOCK` keeps
        a reader-less FIFO from blocking the OPEN — which is where a bare
        `open()` waits for ever, before any read — and the `fstat` refuses
        anything that is not a small regular file. This was the ONE reader in
        the tree without that guard, and the census asks it on every call: a
        FIFO (or a symlink to `/dev/zero`) planted at `<root>/attached.json`
        hung `info`, `list` and `tab list` for ever (a review measured it, and
        the port/pid readers next door already had the rule). A file past the
        cap reads as "no records" like every other unusable shape.
        """
        raw = proc_lib.small_file(self.path(), ATTACH_FILE_CAP)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            # a deeply nested body raises RecursionError, not ValueError: the
            # file is written by whoever owns the root, and an unreadable
            # shape is "no records" like any other
            return {}
        if not isinstance(data, list):
            return {}
        wanted = str(kind or "")
        return {norm(row["profile"]): row for row in data
                if isinstance(row, dict) and row.get("profile")
                and (not wanted or row_kind(row) == wanted)}

    def find(self, key: str = "", *, port: int = 0) -> dict:
        """One REMOTE record by internal key and/or port, or {} — the seam.

        The EndpointGuard judges a `(profile, port)` pair; this is how it asks
        whether that pair is a remote record at all. The key is compared
        NORMALISED, exactly as `records()` keys it, and a port must match too
        when one is given (a record whose key matches but whose port moved is
        not the endpoint the verdict was asked about).
        """
        wanted = norm(key) if key else ""
        number = as_int(port)
        for record in self.records(KIND_REMOTE).values():
            if wanted and str(record.get("profile")) != wanted:
                continue
            if number and as_int(record.get("port")) != number:
                continue
            return record
        return {}

    def is_attached(self, profile: str) -> bool:
        """Is this profile attached for tab writes? (local attachments only)"""
        return norm(profile) in self.records(KIND_LOCAL)

    def put(self, record: dict) -> tuple[bool, LockState]:
        """Store one record under the root lock. `(was_already_there, lock)`."""
        def update(records: dict[str, dict]) -> bool:
            already = record["profile"] in records
            records[record["profile"]] = record
            return already

        return self._locked_update(update, "attach")

    def drop(self, profile: str = "", *, pid: int = 0,
             port: int = 0) -> tuple[list[str], LockState]:
        """Remove the LOCAL records a selector names, under the root lock."""
        def update(records: dict[str, dict]) -> list[str]:
            keys = [key for key in records
                    if row_kind(records[key]) != KIND_REMOTE
                    and ((profile and key == norm(profile))
                         or (pid and as_int(records[key].get("pid"))
                             == as_int(pid))
                         or (port and as_int(records[key].get("port"))
                             == as_int(port)))]
            for key in keys:
                records.pop(key, None)
            return keys

        return self._locked_update(update, "detach")

    def drop_all(self) -> tuple[list[str], LockState]:
        """Remove every LOCAL record, under the root lock. `(removed, lock)`."""
        def update(records: dict[str, dict]) -> list[str]:
            keys = sorted(key for key, row in records.items()
                          if row_kind(row) != KIND_REMOTE)
            for key in keys:
                records.pop(key, None)
            return keys

        return self._locked_update(update, "detach")

    def drop_remote(self, url: str = "",
                    *, all_remotes: bool = False,
                    ) -> tuple[list[str], LockState]:
        """Remove the REMOTE records a `remote detach` names.

        Returns the URLS removed (what a caller sees), not the internal keys.
        `all_remotes` takes every remote record; a URL takes exactly the one
        it names (and nothing when it names none — the caller refuses on the
        empty answer).
        """
        def update(records: dict[str, dict]) -> list[str]:
            wanted = str(url or "").strip()
            gone = [key for key, row in records.items()
                    if row_kind(row) == KIND_REMOTE
                    and (all_remotes or str(row.get("url")) == wanted)]
            found = [str(records[key].get("url")) for key in gone]
            for key in gone:
                records.pop(key, None)
            return sorted(found)

        return self._locked_update(update, "detach")

    def drop_unlocked(self, profile: str) -> list[str]:
        """Drop one LOCAL record when the CALLER already holds the root lock."""
        records = self.records()
        keys = [key for key in records if key == norm(profile)
                and row_kind(records[key]) != KIND_REMOTE]
        for key in keys:
            records.pop(key, None)
        self._write(records)
        return keys

    def _locked_update(self, fn: Callable[[dict[str, dict]], Any],
                       verb: str) -> tuple[Any, LockState]:
        """One read-modify-write of the file, under ONE root lock."""
        with profile_lock(lock_path(self.base), verb) as lock:
            records = self.records()
            result = fn(records)
            self._write(records)
        return result, lock

    def _write(self, records: dict[str, dict]) -> None:
        """Replace the attach file. One scratch file and a rename, so a crash
        cannot leave a half-written list of authorizations.

        The scratch file is opened EXCLUSIVE and never through a link: the
        predictable `.new` name was a pre-creatable symlink, and `open(...,
        "w")` truncated whatever it pointed at (CWE-377, the same guard the
        screenshot temp file carries).
        """
        path = self.path()
        temp = f"{path}.new"
        try:
            os.makedirs(self.base, mode=0o700, exist_ok=True)
            handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            fail(ERR_ATTACH_FAILED,
                 f"cannot write {path}: a leftover scratch file is in the way "
                 f"({temp}) — remove it and try again")
        except OSError as e:
            fail(ERR_ATTACH_FAILED, f"cannot write {path}: {e}")
        try:
            with os.fdopen(handle, "w") as out:
                json.dump(sorted(records.values(),
                                 key=lambda r: str(r.get("profile"))),
                          out, indent=1)
            os.replace(temp, path)
        except OSError as e:
            with contextlib.suppress(OSError):
                os.remove(temp)
            fail(ERR_ATTACH_FAILED, f"cannot write {path}: {e}")


#: The process-wide store: the root is read at use, so a test that moves
#: BROWSER_CONTROL_ROOT sees the move.
STORE = AttachmentStore()
