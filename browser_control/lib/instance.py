"""instance — the managed profile instance ONE call is about.

One resolver for "which profile": the scoped or named one wins, else the
default instance for that browser; the root boundary and the symlink refusal
are applied once, here. `profile info|seed|reset` ask this object instead of
re-deriving the rules, and the liveness question ("is a browser running on
it?") is answered from the same census every other verb reads.
"""
from __future__ import annotations

import os

from browser_control.lib import (
    browser as browser_lib,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib.coerce import as_int  # pyright: ignore[reportMissingImports]
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_NOT_MANAGED,
    ERR_PROFILE_LIVE,
    fail,
)
from browser_control.lib.paths import (  # pyright: ignore[reportMissingImports]
    expand,
    is_managed,
    norm,
    profile_dir,
    root,
)

__all__ = ["Instance"]


def _outside(path: str, verb: str) -> str:
    """The refusal for a path outside this CLI's root, per verb."""
    if verb in ("info", "logins"):
        return (f"{path} is not under {root()} — `profile {verb}` reports "
                "the profiles this CLI manages; name one under the root")
    return (f"{path} is not under {root()} — this CLI only manages the "
            "profiles in its own root (BROWSER_CONTROL_ROOT); it will not "
            "reset or seed into somebody's real browser profile")


class Instance:
    """The managed profile ONE call is about, resolved once."""

    def __init__(self, path: str) -> None:
        self.path = path

    @classmethod
    def resolve(cls, profile: str = "", browser: str = "",
                verb: str = "profile") -> Instance:
        """The scoped/named profile wins; else the default for that browser.

        The default is the same instance `open` would use. A path outside the
        root is refused, and so is a symlinked directory — a tree this CLI did
        not make: the per-child guard in the copy cannot see the top level, and
        a wipe would follow the link (a review found `seed` wrote through such
        a link into a live profile).
        """
        wanted = str(profile or "").strip() or browser_lib.scope()
        if wanted:
            path = expand(wanted)
            if not is_managed(path):
                fail(ERR_NOT_MANAGED, _outside(path, verb))
        else:
            path = profile_dir(browser_lib.binary(browser))
        if os.path.islink(path):
            fail(ERR_NOT_MANAGED,
                 f"{path} is a symlink — this CLI manages real profile "
                 "directories, and a link can point a seed or a wipe at a "
                 "tree it does not own")
        return cls(path)

    @property
    def managed(self) -> bool:
        """Is this profile one of ours?"""
        return is_managed(self.path)

    @property
    def attached(self) -> bool:
        """Is this profile attached for tab writes?"""
        return browser_lib.is_attached(self.path)

    def browsers_row(self) -> dict | None:
        """The census row for this instance, or None when no browser is on it."""
        wanted = norm(self.path)
        return next((row for row in browser_lib.browsers()
                     if row["pid"] and norm(str(row["profile"])) == wanted),
                    None)

    def live_pid(self) -> int:
        """The pid of the browser running on this profile, or 0.

        Compared by NORMALISED path: a browser spells its `--user-data-dir`
        however it was launched (a trailing slash, a `..`, a symlinked root),
        and an exact string compare read a live profile as idle — which for
        `profile reset` means wiping a running browser's directory (a review
        flagged it).
        """
        if not self.path:
            return 0
        row = self.browsers_row()
        return as_int(row["pid"]) if row else 0

    def refuse_live(self, verb: str) -> None:
        """Refuse a write to this profile while a browser is on it."""
        pid = self.live_pid()
        if pid:
            fail(ERR_PROFILE_LIVE,
                 f"pid {pid} is running on {self.path} — {verb} under a live "
                 "browser corrupts the profile (Chrome's own singleton "
                 f"warning says why): `close --profile {self.path} --force` "
                 "first")
