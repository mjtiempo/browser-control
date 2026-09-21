"""reset — `profile reset`: wipe a managed profile, or say why not.

The one verb here that DESTROYS, so its guards are the point: a running profile
is refused, a symlinked target is refused, and a profile that holds content is
refused unless `--force`. It holds the profile's lock for the WHOLE wipe, and
that lock file lives outside the tree it deletes (`paths.lock_path`) — inside
it, the `rmtree` deleted the very file the block held.
"""
from __future__ import annotations

import os
import shutil

from browser_control.lib import (
    attachments as attachments_lib,
    browser as browser_lib,
)
from browser_control.lib import locks
from browser_control.lib.browser import root
from browser_control.lib.errors import (
    ERR_NOT_MANAGED,
    ERR_PROFILE_EXISTS,
    ERR_RESET_FAILED,
    ERR_RESET_NOT_VERIFIED,
    fail,
)
from browser_control.lib.instance import (
    Instance,
)
from browser_control.lib.paths import (
    lock_path,
    pid_file,
)
from browser_control.lib.profile.trees import (
    _remove,
    _tree,
)


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
        # stale by construction (a review measured it). The lock FILE lives in
        # `<root>/.locks/` (paths.lock_path), outside the tree wiped below —
        # inside it, the rmtree deleted the very file this block holds, and the
        # next `open` re-created the path as a fresh inode and took it at once.
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
