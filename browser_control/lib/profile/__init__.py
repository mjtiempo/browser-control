"""profiles — seeing, seeding and resetting the profiles this CLI manages.

`profile` is a browser-level noun, the way `tab` is a page-level one, and its
three verbs are what a caller needs BEFORE `open`:

* ``profile info``  — what exists under the root, how much it weighs, whether a
  browser is on it, and whether this CLI is attached to it;
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
3. **What landed is read back.** Every file the source has (minus the skips) is
   checked for in the target, by size; a mismatch refuses `seed-not-verified`
   instead of reporting a login that is not there. `--dry` counts first, so the
   caller can see the weight before agreeing to it with `--force`.

Seeding copies between profiles OF THE SAME MACHINE AND USER, which is what
makes it work at all: Chrome encrypts cookies and passwords with a key the OS
keyring holds, so the copied files decrypt for the same human. Copying a profile
to another machine needs that key too, and this verb does not pretend to.
"""
from __future__ import annotations

import os
import shutil
import time

from browser_control.lib import attachments as attachments_lib
from browser_control.lib import browser as browser_lib
from browser_control.lib import locks, seedtree
from browser_control.lib.browser import (  # pyright: ignore[reportMissingImports]
    profiles,
    root,
    scope,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_BAD_ARGS,
    ERR_NOT_MANAGED,
    ERR_PROFILE_EXISTS,
    ERR_RESET_FAILED,
    ERR_RESET_NOT_VERIFIED,
    ERR_SEED_NOT_VERIFIED,
    fail,
)
from browser_control.lib.instance import (
    Instance,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib.paths import (  # pyright: ignore[reportMissingImports]
    LOCK_FILE,
    PID_FILE,
    expand,
    lock_path,
    pid_file,
)

# Never copied, at any depth, by NAME: the lock files that make a profile look
# in use, our own records, and the caches that are hundreds of megabytes of
# nothing a caller wants. Everything else is copied, so a Chrome version that
# adds a new login store still gets seeded.
SEED_SKIP = frozenset({
    "SingletonLock", "SingletonSocket", "SingletonCookie", "Lock File",
    "DevToolsActivePort", ".pid", ".browser-control.lock",
    "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
    "DawnCache", "DawnGraphiteCache", "GraphiteDawnCache", "Media Cache",
    "CacheStorage", "ScriptCache", "Crashpad", "Crash Reports",
    "BrowserMetrics", "BrowserMetrics-spare.pma", "component_crx_cache",
    "extensions_crx_cache", "Safe Browsing", "SSLErrorAssistant",
    "First Run", "Last Version",
})













def _tree(source: str, count_skips: bool = False) -> seedtree.TreeFacts:
    """The profile tree walk, with the seed/copy skip policy named here."""
    return seedtree.walk(source, skips=SEED_SKIP, descend_skips=count_skips)


def _copy(source: str, target: str, dry: bool) -> seedtree.TreeFacts:
    """The verified copy, with the seed/copy skip policy named here."""
    return seedtree.copy_verified(source, target, dry, skips=SEED_SKIP)


def _has_content(path: str) -> bool:
    """`seedtree.has_content`, with this CLI's own bookkeeping named."""
    return seedtree.has_content(path, ours=(LOCK_FILE, PID_FILE))


_remove = seedtree.remove


def info(profile: str = "") -> dict:
    """`profile info [--profile DIR]`: what exists under this CLI's root.

    A read. Each profile is reported with its weight, when it last changed,
    whether a browser is on it (pid, port, exe), whether this CLI is attached to
    it, and whether it is the DEFAULT instance for a browser binary — which is
    the name `open` keys it by. A scoped path outside the root is refused
    `not-managed` rather than walked: an unbounded walk of a caller-named tree
    was a review finding, and the verb is about the profiles this CLI manages.
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


def _seed_destination(src: str, target: str) -> str:
    """Where a source profile's files go inside the managed INSTANCE.

    Chrome's `--user-data-dir` (the instance, the unit `--profile DIR` names)
    holds a `Default/` subdirectory, and THAT is the profile Chrome actually
    reads. Two source shapes must therefore land in two different places:

    * a whole USER-DATA directory (`Default/` is a child, or `Local State`
      sits beside it): its contents land in the instance root and bring
      `Default/` with them;
    * a single PROFILE directory (`~/.config/google-chrome/Default`,
      `Profile 1`, a snap or Flatpak path): its contents land in
      `<instance>/Default/`. Written to the instance root they were ignored
      by Chrome — cookies and all — so a "seeded" browser still showed the
      login wall (found in use: the documented `--from .../Default` produced
      a profile Chrome never read).
    """
    if os.path.isdir(os.path.join(src, "Default")) \
            or os.path.isfile(os.path.join(src, "Local State")):
        return target
    return os.path.join(target, "Default")


def seed(source: str = "", profile: str = "", browser: str = "",
         force: bool = False, dry: bool = False) -> dict:
    """`profile seed --from DIR [--profile DIR] [--force] [--dry]`.

    `--from` is REQUIRED and never guessed: which of your profiles to read is
    your decision, and the usual answers are
    `~/.config/google-chrome/Default` (or `Profile 1`), a snap path, or a
    Flatpak one — or the whole user-data directory
    (`~/.config/google-chrome`), which also works because it brings its own
    `Default/` with it. A profile directory's contents are placed in the
    instance's `Default/`, the subdirectory Chrome reads (see
    `_seed_destination`). The TARGET is a managed profile (scoped, or the
    default instance for `--browser`), it must not be running, and an
    existing one must be overwritten on purpose (`--force`) — `--dry` first
    reports what would land, in bytes and files, without writing anything.
    """
    if not source:
        fail(ERR_BAD_ARGS,
             "profile seed: --from DIR is required — the profile to copy FROM "
             "(usually ~/.config/google-chrome/Default, or the whole "
             "~/.config/google-chrome user-data directory); this CLI will "
             "not guess which of your profiles to read")
    src = expand(source)
    if not os.path.isdir(src):
        fail(ERR_BAD_ARGS, f"profile seed: {src} is not a directory")
    target = Instance.resolve(profile, browser, verb="seed").path
    dest = _seed_destination(src, target)
    if src == dest or src.startswith(dest + os.sep) \
            or dest.startswith(src + os.sep):
        fail(ERR_BAD_ARGS,
             f"profile seed: the source and the destination are the same "
             f"tree ({src} and {dest})")
    Instance(target).refuse_live("seeding")
    # the SOURCE may be running: that is a legitimate snapshot, so it is a
    # warning and not a refusal — Chrome flushes cookies to disk lazily, so what
    # is copied can lag the live state by a few seconds
    source_pid = Instance(src).live_pid()
    held = True
    # `--dry` must not write anything: taking the target's lock CREATES the
    # target directory and its lock file, and the docstring promises "without
    # writing anything" (a review flagged it). The root lock still orders the
    # read; a dry run touches no profile.
    with locks.instance_locks(lock_path(root()), lock_path(target),
                              "profile seed",
                              skip_profile_lock=dry) as (lock, profile_lock):
        if not dry:
            # the TARGET's own lock — the one `open` and `close` hold. The
            # root lock orders the attach records; this one orders the
            # PROFILE, and without it a concurrent `open` was not excluded at
            # all: `seed` could copy into a profile a browser had just been
            # started on (a review measured it). Re-checked UNDER the lock.
            Instance(target).refuse_live("seeding")
        existing = _has_content(target)
        if existing and not force and not dry:
            fail(ERR_PROFILE_EXISTS,
                 f"{target} already holds a profile — `profile seed --force` "
                 "overwrites it (logins and all), or `profile reset --force` "
                 "clears it first; `--dry` reports what this call would copy")
        facts = _copy(src, dest, dry)
        if dry:
            held = False
        elif facts["unreadable"]:
            # a directory the walk could not read is not copied and cannot be
            # verified: a PARTIAL copy must never report as verified
            fail(ERR_SEED_NOT_VERIFIED,
                 f"{len(facts['unreadable'])} director(ies) under {src} "
                 "could not be read, so this copy is PARTIAL and nothing "
                 "was verified: " + "; ".join(facts["unreadable"][:3]))
        else:
            absent = seedtree.missing(facts["entries"], src, dest)
            if absent:
                fail(ERR_SEED_NOT_VERIFIED,
                     f"{len(absent)} file(s) did not land in {dest}: "
                     + ", ".join(absent[:4]))
    reply = {"ok": True, "from": src, "profile": target,
             "profile_dir": dest, "dry": bool(dry),
             "copied_bytes": facts["bytes"], "copied_files": facts["files"],
             "dirs": facts["dirs"],
             "skipped": sorted(set(facts["skipped"])),
             "links_skipped": facts["links"],
             "special_files": facts["special"],
             "links_planted": facts["links_planted"],
             "unreadable": facts["unreadable"],
             "verified": bool(held),
             # WHAT the check could see, said out loud: the sizes of the files
             # this copy walked. A same-size corruption is outside that oracle,
             # and a caller should not have to guess which one was used.
             "verified_by": "size of every file this copy walked",
             "note": ("same machine, same user: Chrome's cookie and password "
                      "keys live in the OS keyring, so the copy decrypts here "
                      "and only here")}
    lock.warn(reply)
    profile_lock.warn(reply)
    if source_pid:
        lag = (f"the source profile is in use (pid {source_pid}): what is on "
               "disk may lag its live state by a few seconds")
        reply["warning"] = f"{reply['warning']}; {lag}" if "warning" in reply \
            else lag
    return reply


def reset(profile: str = "", browser: str = "", force: bool = False) -> dict:
    """`profile reset [--profile DIR] [--force]`: wipe a managed profile.

    Destroying logins is the kind of act that has to be said out loud, so a
    profile that holds anything refuses `profile-exists` unless `--force` — the
    same rule `close` applies to tabs and `screenshot` to an existing file. A
    profile that is running refuses `profile-live`. The read-back is that the
    path is gone.
    """
    target = Instance.resolve(profile, browser, verb="reset").path
    Instance(target).refuse_live("resetting")
    if not os.path.isdir(target):
        return {"ok": True, "profile": target, "reset": False,
                "reason": "there was no profile to reset"}
    if os.path.islink(target):
        # unreachable: `_target` refuses a symlinked profile before this verb
        # runs. Kept as a second line of defence for a link planted between
        # that check and here.
        fail(ERR_NOT_MANAGED,
             f"{target} is a symlink — this CLI wipes a profile directory, "
             "not a link to somebody else's")
    with locks.instance_locks(lock_path(root()), lock_path(target),
                              "profile reset") as (lock, profile_lock):
        # the PROFILE's lock is the one `open` holds across its whole
        # check-then-act: without it, a reset could `rmtree` the directory a
        # browser was just being started on, and the liveness verdict above was
        # stale by construction (a review measured it)
        Instance(target).refuse_live("resetting")   # re-checked UNDER the lock
        # …and the CONTENT guard belongs under it too: checked outside, a
        # profile could gain its first file between the check and the wipe, and
        # `--force` would then destroy a login nobody agreed to lose
        facts = _tree(target, count_skips=True)
        if (facts["files"] or facts["dirs"] or facts["links"]
                or facts["special"]) and not force:
            fail(ERR_PROFILE_EXISTS,
                 f"{target} holds {facts['files']} file(s), {facts['bytes']} "
                 "bytes — `profile reset --force` wipes it, logins included; "
                 "`profile info` shows it first")
        detached = browser_lib.is_attached(target)
        if detached:
            # the root lock is already held here (instance_locks): the store's
            # unlocked drop is the one that must be used
            attachments_lib.STORE.drop_unlocked(target)
        try:
            shutil.rmtree(target)
        except OSError as e:
            fail(ERR_RESET_FAILED, f"cannot remove {target}: {e}")
        _remove(pid_file(target))
    if os.path.exists(target):
        fail(ERR_RESET_NOT_VERIFIED,
             f"{target} still exists after the wipe — something recreated it")
    reply = {"ok": True, "profile": target, "reset": True,
             "files": facts["files"], "bytes_freed": facts["bytes"],
             "detached": detached, "verified": True}
    lock.warn(reply)
    profile_lock.warn(reply)
    return reply
