"""profiles — seeing, seeding and resetting the profiles this CLI manages.

`profile` is a browser-level noun, the way `tab` is a page-level one, and its
three verbs are what a caller needs BEFORE `open`:

* ``profile info``  — what exists under the root, how much it weighs, whether a
  browser is on it, and whether this CLI is attached to it;
* ``profile logins`` — what logins are IN one: the hosts its Cookies store
  names (with expiry) and how many saved logins it holds, read from a COPY of
  each store — evidence, not a session check;
* ``profile seed``  — give a managed profile the LOGINS of a source profile: a
  filtered copy of cookies, storage and preferences, never the caches, never a
  lock file, never our own records;
* ``profile reset`` — wipe it, so the next `open` starts clean.

Three rules, and they are the reason this is not one `shutil.copytree` call:

1. **A profile that is running is never written.** Copying under a live browser
   is how a profile gets corrupted, and Chrome says so itself in the singleton
   warning people ignore. Both verbs refuse `profile-live`, naming the pid.
2. **Only this CLI's own root is touchable.** `--profile /home/you/.config/…`
   is refused `not-managed`: reading a source profile is a copy, but *wiping*
   something outside the root would be destroying somebody's real browser. The
   root is the boundary, exactly as it is for writes.
3. **What landed is read back.** Every file the copy walked is checked for in
   the target against the size it was COPIED at — the walk's manifest, never a
   fresh stat of a source that may be a running browser rewriting it; a
   mismatch refuses `seed-not-verified` instead of reporting a login that is
   not there, and a source file whose size moved under the copy is the reported
   fact `changed`. A store that is a symlink, or that resolves outside this
   CLI's root, is reported rather than read. `--dry` counts first and says
   `would_refuse`, so the caller can see the weight — and what the real call
   would do — before agreeing to it with `--force`.

Seeding copies between profiles OF THE SAME MACHINE AND USER, which is what
makes it work at all: Chrome encrypts cookies and passwords with a key the OS
keyring holds, so the copied files decrypt for the same human. Copying a profile
to another machine needs that key too, and this verb does not pretend to.

The implementation is split by responsibility — `stores` (what is IN a
profile: its login stores, read from a COPY), `trees` (the one skip policy the
walk, the copy and the wipe all share), `seed`, `reset` — the way `browser/`
and `dom/` are. This module keeps `info` (a read of the root, the verb a
caller reaches for first) and re-exports the rest, so no call site changes.
"""
from __future__ import annotations

import os
import shutil
import time

from browser_control.lib import browser as browser_lib
from browser_control.lib.browser import (
    profiles,
    root,
    scope,
)
from browser_control.lib.instance import (
    Instance,
)
from browser_control.lib.profile.reset import (  # noqa: F401
    reset,
)
from browser_control.lib.profile.seed import (  # noqa: F401
    seed,
)
from browser_control.lib.profile.stores import (  # noqa: F401
    DEFAULT_SITES,
    logins,
    profile_links,
)
from browser_control.lib.profile.trees import (
    _tree,
)

__all__ = ["DEFAULT_SITES", "info", "logins", "reset", "seed"]


def info(profile: str = "") -> dict:
    """`profile info [--profile DIR]`: what exists under this CLI's root.

    A read. Each profile is reported with its weight, when it last changed,
    whether a browser is on it (pid, port, exe), whether this CLI is attached to
    it, and whether it is the DEFAULT instance for a browser binary — which is
    the name `open` keys it by. A scoped path outside the root is refused
    `not-managed` rather than walked: an unbounded walk of a caller-named tree
    was a review finding, and the verb is about the profiles this CLI manages.

    Every SYMLINK at the top of the profile rides in `links` (name, path, what
    it resolves to, the store markers behind it): the walk counts links and
    weighs nothing behind them, so a `Default` that is a link to a store-bearing
    directory reported as "0 files, 0 bytes" — a census that cannot tell a
    planted link from an empty profile. Nothing behind a link is read or named;
    the fact that it is there is the finding.
    """
    wanted = str(profile or "").strip() or scope()
    if wanted:
        wanted = Instance.resolve(profile, "", verb="info").path
    rows: list[dict] = []
    for path in profiles() if not wanted else [wanted]:
        inst = Instance(path)
        facts = _tree(path)
        live = inst.browsers_row()
        name = os.path.basename(path)
        row: dict = {
            "name": name,
            "path": path,
            "exists": os.path.isdir(path),
            "managed": inst.managed,
            "default_for": (name if name in browser_lib.BROWSER_BINS
                            and shutil.which(name) else ""),
            "attached": inst.attached,
            "bytes": facts["bytes"],
            "files": facts["files"],
            "links": profile_links(path),
            "modified": (time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(os.path.getmtime(path)))
                if os.path.isdir(path) else ""),
        }
        if live is not None:
            row["live"] = {"pid": live["pid"], "exe": live["exe"],
                           "port": live["cdp"]["port"],
                           "verified": live["cdp"]["verified"]}
        rows.append(row)
    rows.sort(key=lambda r: str(r["name"]))
    return {"ok": True, "root": root(), "count": len(rows),
            "profiles": rows,
            "scope": scope(),
            "note": ("`--profile DIR` addresses one instance on every verb; the "
                     "default instance for a browser is keyed by its binary "
                     "name, which is the `name` above")}
