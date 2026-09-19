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

import contextlib
import os
import shutil
import time

from browser_control.lib import browser as browser_lib
from browser_control.lib.browser import (  # pyright: ignore[reportMissingImports]
    _is_managed,
    binary,
    profile_dir,
    profiles,
    root,
    scope,
)
from browser_control.lib.errors import fail  # pyright: ignore[reportMissingImports]

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


def _tree(source: str) -> dict:
    """Everything a naive copy would touch: bytes, files, dirs, skips, links.

    Walks by hand rather than with `copytree` for three reasons: the skips are
    by name at any depth, symlinks are COUNTED and not followed (a profile can
    contain links into the filesystem, and following one would write wherever it
    pointed), and the numbers are what the reply reports.
    """
    facts = {"bytes": 0, "files": 0, "dirs": 0, "links": 0,
             "skipped": [], "entries": []}
    stack = [source]
    while stack:
        here = stack.pop()
        try:
            children = list(os.scandir(here))
        except OSError:
            continue
        for child in children:
            if child.name in SEED_SKIP:
                facts["skipped"].append(child.name)
                continue
            if child.is_symlink():
                facts["links"] += 1
                continue
            if child.is_dir(follow_symlinks=False):
                facts["dirs"] += 1
                stack.append(child.path)
                continue
            try:
                facts["bytes"] += child.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            facts["files"] += 1
            facts["entries"].append(child.path)
    return facts


def _missing(source: str, target: str) -> list[str]:
    """Files the source has (minus skips) that the target does not, by size.

    The read-back for a seed: a file that is absent, or a different size, means
    the copy did not land — and a login that is not there must refuse rather
    than be reported as seeded.
    """
    absent: list[str] = []
    for path in _tree(source)["entries"]:
        relative = os.path.relpath(path, source)
        landed = os.path.join(target, relative)
        try:
            if os.path.getsize(landed) != os.path.getsize(path):
                absent.append(relative)
        except OSError:
            absent.append(relative)
        if len(absent) >= 8:
            break
    return absent


def _copy(source: str, target: str, dry: bool) -> dict:
    """Copy source into target, skipping SEED_SKIP names and symlinks."""
    facts = _tree(source)
    if dry:
        return facts
    stack = [(source, target)]
    while stack:
        here, there = stack.pop()
        try:
            os.makedirs(there, exist_ok=True)
        except OSError as e:
            fail("seed-failed", f"cannot create {there}: {e}")
        try:
            children = list(os.scandir(here))
        except OSError:
            continue
        for child in children:
            if child.name in SEED_SKIP or child.is_symlink():
                continue
            destination = os.path.join(there, child.name)
            if child.is_dir(follow_symlinks=False):
                stack.append((child.path, destination))
                continue
            try:
                shutil.copy2(child.path, destination)
            except OSError as e:
                fail("seed-failed", f"cannot copy {child.path}: {e}")
    return facts


def _int(value: object) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _has_content(path: str) -> bool:
    """Does that directory exist and hold anything? Never raises."""
    try:
        return bool(os.listdir(path))
    except OSError:
        return False


def _remove(path: str) -> None:
    """Remove a file if it is there. A removal that cannot happen is not a
    failure — the thing it was removing is what matters, and the caller checks
    that."""
    try:
        os.remove(path)
    except OSError:
        return


def _target(profile: str = "", browser: str = "") -> str:
    """The managed profile a `profile` verb acts on, or a refusal.

    The scoped/named profile wins; otherwise it is the default instance for
    that browser, the same one `open` would use. Anything outside the root is
    refused: this CLI manages its own root, and a wipe aimed at somebody's real
    profile is the mistake this rule exists to prevent.
    """
    wanted = str(profile or "").strip() or scope()
    if wanted:
        path = os.path.abspath(os.path.expanduser(wanted))
        if not _is_managed(path):
            fail("not-managed",
                 f"{path} is not under {root()} — this CLI only manages the "
                 "profiles in its own root (BROWSER_CONTROL_ROOT); it will not "
                 "reset or seed into somebody's real browser profile")
        return path
    return profile_dir(binary(browser))


def _live_pid(profile: str) -> int:
    """The pid of the browser running on that profile, or 0."""
    for row in browser_lib.browsers():
        if str(row["profile"]) == profile and row["pid"]:
            return _int(row["pid"])
    return 0


def _refuse_live(profile: str, verb: str) -> None:
    pid = _live_pid(profile)
    if pid:
        fail("profile-live",
             f"pid {pid} is running on {profile} — {verb} under a live browser "
             "corrupts the profile (Chrome's own singleton warning says why): "
             f"`close --profile {profile} --force` first")


def info(profile: str = "") -> dict:
    """`profile info [--profile DIR]`: what exists under this CLI's root.

    A read. Each profile is reported with its weight, when it last changed,
    whether a browser is on it (pid, port, exe), whether this CLI is attached to
    it, and whether it is the DEFAULT instance for a browser binary — which is
    the name `open` keys it by.
    """
    wanted = str(profile or "").strip() or scope()
    rows: list[dict] = []
    for path in profiles() if not wanted else [
            os.path.abspath(os.path.expanduser(wanted))]:
        facts = _tree(path)
        live = next((r for r in browser_lib.browsers()
                     if str(r["profile"]) == path), None)
        name = os.path.basename(path)
        row: dict = {
            "name": name,
            "path": path,
            "exists": os.path.isdir(path),
            "managed": _is_managed(path),
            "default_for": (name if name in browser_lib.BROWSER_BINS
                            and shutil.which(name) else ""),
            "attached": browser_lib.is_attached(path),
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


def seed(source: str = "", profile: str = "", browser: str = "",
         force: bool = False, dry: bool = False) -> dict:
    """`profile seed --from DIR [--profile DIR] [--force] [--dry]`.

    `--from` is REQUIRED and never guessed: which of your profiles to read is
    your decision, and the usual answers are
    `~/.config/google-chrome/Default` (or `Profile 1`), a snap path, or a
    Flatpak one. The TARGET is a managed profile (scoped, or the default
    instance for `--browser`), it must not be running, and an existing one must
    be overwritten on purpose (`--force`) — `--dry` first reports what would
    land, in bytes and files, without writing anything.
    """
    if not source:
        fail("bad-args",
             "profile seed: --from DIR is required — the profile to copy FROM "
             "(usually ~/.config/google-chrome/Default); this CLI will not "
             "guess which of your profiles to read")
    src = os.path.abspath(os.path.expanduser(str(source)))
    if not os.path.isdir(src):
        fail("bad-args", f"profile seed: {src} is not a directory")
    target = _target(profile, browser)
    if src == target or src.startswith(target + os.sep) \
            or target.startswith(src + os.sep):
        fail("bad-args",
             f"profile seed: the source and the target are the same tree "
             f"({src})")
    _refuse_live(target, "seeding")
    held = True
    with browser_lib._lock(browser_lib._lock_path(root()),   # noqa: SLF001
                           "profile seed") as lock:
        existing = _has_content(target)
        if existing and not force and not dry:
            fail("profile-exists",
                 f"{target} already holds a profile — `profile seed --force` "
                 "overwrites it (logins and all), or `profile reset --force` "
                 "clears it first; `--dry` reports what this call would copy")
        facts = _copy(src, target, dry)
        if dry:
            held = False
        else:
            absent = _missing(src, target)
            if absent:
                fail("seed-not-verified",
                     f"{len(absent)} file(s) did not land in {target}: "
                     + ", ".join(absent[:4]))
    reply = {"ok": True, "from": src, "profile": target, "dry": bool(dry),
             "copied_bytes": facts["bytes"], "copied_files": facts["files"],
             "dirs": facts["dirs"],
             "skipped": sorted(set(facts["skipped"])),
             "links_skipped": facts["links"],
             "verified": bool(held),
             "note": ("same machine, same user: Chrome's cookie and password "
                      "keys live in the OS keyring, so the copy decrypts here "
                      "and only here")}
    if lock["warning"]:
        reply["warning"] = lock["warning"]
    return reply


def reset(profile: str = "", browser: str = "", force: bool = False) -> dict:
    """`profile reset [--profile DIR] [--force]`: wipe a managed profile.

    Destroying logins is the kind of act that has to be said out loud, so a
    profile that holds anything refuses `profile-exists` unless `--force` — the
    same rule `close` applies to tabs and `screenshot` to an existing file. A
    profile that is running refuses `profile-live`. The read-back is that the
    path is gone.
    """
    target = _target(profile, browser)
    _refuse_live(target, "resetting")
    if not os.path.isdir(target):
        return {"ok": True, "profile": target, "reset": False,
                "reason": "there was no profile to reset"}
    facts = _tree(target)
    if facts["files"] and not force:
        fail("profile-exists",
             f"{target} holds {facts['files']} file(s), {facts['bytes']} "
             "bytes — `profile reset --force` wipes it, logins included; "
             "`profile info` shows it first")
    with browser_lib._lock(browser_lib._lock_path(root()),   # noqa: SLF001
                           "profile reset") as lock:
        detached = browser_lib.is_attached(target)
        if detached:
            records = browser_lib._attached()                # noqa: SLF001
            records.pop(browser_lib._norm(target), None)     # noqa: SLF001
            browser_lib._write_attached(records)             # noqa: SLF001
        try:
            shutil.rmtree(target)
        except OSError as e:
            fail("reset-failed", f"cannot remove {target}: {e}")
        _remove(browser_lib._pid_file(target))               # noqa: SLF001
    if os.path.exists(target):
        fail("reset-not-verified",
             f"{target} still exists after the wipe — something recreated it")
    reply = {"ok": True, "profile": target, "reset": True,
             "files": facts["files"], "bytes_freed": facts["bytes"],
             "detached": detached, "verified": True}
    if lock["warning"]:
        reply["warning"] = lock["warning"]
    return reply
