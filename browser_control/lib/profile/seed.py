"""seed — `profile seed`: give a managed profile the LOGINS of a source one.

A filtered, verified copy between profiles of the same machine and user: the
login stores and preferences, never the caches, never a lock file, never this
CLI's own records (`trees.SEED_SKIP`), with the destination's lock held across
the whole copy — and across the `--force` wipe that precedes it, because
`--force` REPLACES the target rather than merging into it (the wipe is
`reset`'s, so what is left is the source's content and never a mix of the
two). What landed is read back twice — by file size, and by the login stores
themselves (`stores._logins_facts`).
"""
from __future__ import annotations

import os
import shutil

from browser_control.lib import (
    attachments as attachments_lib,
    browser as browser_lib,
    locks,
    seedtree,
)
from browser_control.lib.browser import root
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    ERR_PROFILE_EXISTS,
    ERR_RESET_FAILED,
    ERR_RESET_NOT_VERIFIED,
    ERR_SEED_NOT_VERIFIED,
    fail,
)
from browser_control.lib.instance import (
    Instance,
)
from browser_control.lib.paths import (
    expand,
    lock_path,
    pid_file,
)
from browser_control.lib.profile.stores import (
    _logins_facts,
)
from browser_control.lib.profile.trees import (
    _copy,
    _has_content,
    _remove,
    _tree,
)

#: how many hosts `profile seed` reads back from what it just copied
SEED_LOGINS_SITES = 10


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
    default instance for `--browser`) and it must not be running. An existing
    one must be overwritten on purpose (`--force`), and `--force` WIPES it
    first: the copy alone writes only what the SOURCE holds, so files unique to
    the target used to survive it and the profile became a mix of two (a review
    flagged the promise the refusal makes). `--dry` first reports what would
    land AND what `--force` would destroy, in bytes and files, without writing
    anything.
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
        # `--force` OVERWRITES, which is what the refusal above promises: the
        # copy alone writes only what the SOURCE holds, so files unique to the
        # target survived it and the result was a MIX of the two profiles (a
        # review flagged the promise). This is `reset`'s wipe, under the same
        # lock and for the same reason — the profile the caller agreed to
        # replace goes away before anything new lands — and what it destroys is
        # counted FIRST, because the reply has to say what was LOST, not only
        # what arrived.
        wipe = _tree(target, count_skips=True) if existing and force else None
        detached = False
        if wipe is not None and not dry:
            detached = browser_lib.is_attached(target)
            if detached:
                # the root lock is already held here (instance_locks): the
                # store's unlocked drop is the one that must be used
                attachments_lib.STORE.drop_unlocked(target)
            try:
                shutil.rmtree(target)
            except OSError as e:
                fail(ERR_RESET_FAILED,
                     f"profile seed --force cannot remove {target}: {e}")
            _remove(pid_file(target))
            if os.path.exists(target):
                fail(ERR_RESET_NOT_VERIFIED,
                     f"{target} still exists after the wipe — something "
                     "recreated it")
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
    wiped = wipe if not dry else None    # the facts of a wipe that HAPPENED
    reply = {"ok": True, "from": src, "profile": target,
             "profile_dir": dest, "dry": bool(dry),
             "copied_bytes": facts["bytes"], "copied_files": facts["files"],
             "dirs": facts["dirs"],
             "skipped": sorted(set(facts["skipped"])),
             "links_skipped": facts["links"],
             "special_files": facts["special"],
             "links_planted": facts["links_planted"],
             "unreadable": facts["unreadable"],
             # what `--force` DESTROYED, not only what arrived: the wipe runs
             # under the same lock as the copy, so the two counts describe one
             # consistent transition
             "wiped": wiped is not None,
             "wiped_files": wiped["files"] if wiped is not None else 0,
             "wiped_bytes": wiped["bytes"] if wiped is not None else 0,
             "detached": detached,
             "verified": bool(held),
             # WHAT the check could see, said out loud: the sizes of the files
             # this copy walked. A same-size corruption is outside that oracle,
             # and a caller should not have to guess which one was used.
             "verified_by": "size of every file this copy walked",
             "note": ("same machine, same user: Chrome's cookie and password "
                      "keys live in the OS keyring, so the copy decrypts here "
                      "and only here")}
    if dry and wipe is not None:
        # a dry run writes nothing, so the wipe it would cause is reported
        # under its own name — a dry reply must never claim a wipe that did
        # not happen
        reply["would_wipe"] = {"files": wipe["files"], "bytes": wipe["bytes"]}
    if not dry:
        # WHAT landed, read back from the login stores themselves: the size
        # check above proves the bytes moved, and this answers the question
        # the caller actually asked — is a login IN it?
        reply["logins"] = _logins_facts(target, cap=SEED_LOGINS_SITES)
    lock.warn(reply)
    profile_lock.warn(reply)
    if source_pid:
        lag = (f"the source profile is in use (pid {source_pid}): what is on "
               "disk may lag its live state by a few seconds")
        reply["warning"] = f"{reply['warning']}; {lag}" if "warning" in reply \
            else lag
    return reply
