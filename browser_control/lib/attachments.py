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

import json
import os
from collections.abc import Callable
from typing import Any

from browser_control.lib.coerce import as_int  # pyright: ignore[reportMissingImports]
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_ATTACH_FAILED,
    fail,
)
from browser_control.lib.locks import LockState, profile_lock
from browser_control.lib.paths import lock_path, norm, root

ATTACH_FILE = "attached.json"

__all__ = ["ATTACH_FILE", "STORE", "AttachmentStore"]


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

    def records(self) -> dict[str, dict]:
        """The attach records, keyed by absolute profile path."""
        try:
            with open(self.path()) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, list):
            return {}
        return {norm(row["profile"]): row for row in data
                if isinstance(row, dict) and row.get("profile")}

    def get(self, profile: str) -> dict | None:
        """The record for one profile, or None."""
        return self.records().get(norm(profile))

    def is_attached(self, profile: str) -> bool:
        """Is this profile attached for tab writes?"""
        return norm(profile) in self.records()

    def put(self, record: dict) -> tuple[bool, LockState]:
        """Store one record under the root lock. `(was_already_there, lock)`."""
        def update(records: dict[str, dict]) -> bool:
            already = record["profile"] in records
            records[record["profile"]] = record
            return already

        return self._locked_update(update, "attach")

    def drop(self, profile: str = "", *, pid: int = 0,
             port: int = 0) -> tuple[list[str], LockState]:
        """Remove the records a selector names, under the root lock."""
        def update(records: dict[str, dict]) -> list[str]:
            keys = [key for key in records
                    if (profile and key == norm(profile))
                    or (pid and as_int(records[key].get("pid")) == as_int(pid))
                    or (port and as_int(records[key].get("port"))
                        == as_int(port))]
            for key in keys:
                records.pop(key, None)
            return keys

        return self._locked_update(update, "detach")

    def drop_all(self) -> tuple[list[str], LockState]:
        """Remove every record, under the root lock. `(removed, lock)`."""
        def update(records: dict[str, dict]) -> list[str]:
            keys = sorted(records)
            records.clear()
            return keys

        return self._locked_update(update, "detach")

    def drop_unlocked(self, profile: str) -> list[str]:
        """Drop one record when the CALLER already holds the root lock."""
        records = self.records()
        keys = [key for key in records if key == norm(profile)]
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
        cannot leave a half-written list of authorizations."""
        path = self.path()
        temp = f"{path}.new"
        try:
            os.makedirs(self.base, exist_ok=True)
            with open(temp, "w") as handle:
                json.dump(sorted(records.values(),
                                 key=lambda r: str(r.get("profile"))),
                          handle, indent=1)
            os.replace(temp, path)
        except OSError as e:
            fail(ERR_ATTACH_FAILED, f"cannot write {path}: {e}")


#: The process-wide store: the root is read at use, so a test that moves
#: BROWSER_CONTROL_ROOT sees the move.
STORE = AttachmentStore()
