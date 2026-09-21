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
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from datetime import datetime, timezone

from browser_control.lib import attachments as attachments_lib
from browser_control.lib import browser as browser_lib
from browser_control.lib import locks, seedtree
from browser_control.lib.browser import (  # pyright: ignore[reportMissingImports]
    profiles,
    root,
    scope,
)
from browser_control.lib.coerce import as_int  # pyright: ignore[reportMissingImports]
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


# --- what is IN a profile: the login stores, read from a COPY --------------
#: A Chrome PROFILE directory (the one Chrome reads under a `--user-data-dir`)
#: is told apart from the instance root by what is inside it; these are the
#: files a login lands in, or beside.
STORE_MARKERS = ("Cookies", "Login Data", "Preferences")

#: `profile logins` bounds: hosts listed, and cookie names named per host
DEFAULT_SITES = 20
MAX_SITES = 200
COOKIE_NAMES = 12
#: how many hosts `profile seed` reads back from what it just copied
SEED_LOGINS_SITES = 10

#: what the verb's oracle IS, said once so both callers report the same thing
LOGINS_VERIFIED_BY = ("rows in the profile's own Cookies and Login Data "
                      "stores, read from a COPY of each file")
LOGINS_NOTE = ("the stores on disk are the evidence: cookie names, hosts, "
               "expiry and password COUNTS are reported, never a value; a "
               "cookie can be revoked server-side, and `snapshot: true` means "
               "a browser is running on this profile, so disk may lag it; a "
               "store with `readable: false` could not be read, so its rows "
               "are UNKNOWN, not zero")

#: Chrome's clock starts at 1601-01-01, Python's at 1970-01-01
_CHROME_EPOCH = 11_644_473_600


def _chrome_time(expires_utc: object) -> str:
    """A Chrome cookie expiry as `YYYY-MM-DDTHH:MM:SS` UTC, or "".

    `expires_utc` is microseconds since 1601-01-01, and a session cookie has
    none (0). A value the conversion cannot place is reported as ABSENT — an
    expiry nobody wrote must not read as a date.
    """
    micros = as_int(expires_utc)
    if micros <= 0:
        return ""
    try:
        moment = datetime.fromtimestamp(micros / 1_000_000 - _CHROME_EPOCH,
                                        timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


@contextlib.contextmanager
def _store_connection(db: str) -> Iterator[tuple[sqlite3.Connection | None, str]]:
    """An open connection to a COPY of one store, or `(None, reason)`.

    A COPY for two reasons: the live file can be mid-write (`database is
    locked`), and this verb must not touch the profile it reports on. A
    `-wal`/`-journal` beside the store travels with the copy, so a store in a
    write-ahead state still answers. A store that cannot be opened is an
    ANSWER — the reason is yielded, never raised: a profile whose `Cookies` is
    not a database is exactly what a caller needs told, and `seed` reads the
    same stores back without failing on one.
    """
    with tempfile.TemporaryDirectory(prefix="browser-control-logins-") as tmp:
        copy = os.path.join(tmp, os.path.basename(db))
        try:
            shutil.copy2(db, copy)
            for suffix in ("-wal", "-journal"):
                if os.path.exists(db + suffix):
                    shutil.copy2(db + suffix, copy + suffix)
        except OSError as e:
            yield None, f"cannot copy the store: {e}"
            return
        try:
            conn = sqlite3.connect(copy)
        except sqlite3.Error as e:
            yield None, str(e)
            return
        try:
            yield conn, ""
        finally:
            conn.close()


def _cookie_rows(db: str) -> tuple[list[tuple], str]:
    """One Cookies store: host, name and expiry of every cookie in it.

    The SQL is a LITERAL here and in `_login_rows` — it never crosses a
    variable, so there is nothing to inject into — and a schema SQLite cannot
    answer it from fails the read, naming what it could not find ("no such
    column: …"), which is the answer the caller gets.
    """
    with _store_connection(db) as (conn, error):
        if conn is None:
            return [], error
        try:
            return [tuple(row) for row in conn.execute(
                "select host_key, name, expires_utc from cookies")], ""
        except sqlite3.Error as e:
            return [], str(e)


def _login_rows(db: str) -> tuple[list[tuple], str]:
    """One Login Data store: the ORIGIN of every saved login, never a value."""
    with _store_connection(db) as (conn, error):
        if conn is None:
            return [], error
        try:
            return [tuple(row) for row in conn.execute(
                "select origin_url from logins")], ""
        except sqlite3.Error as e:
            return [], str(e)


def _has_store_marker(path: str) -> bool:
    """Does this directory look like a Chrome PROFILE directory?"""
    return any(os.path.isfile(os.path.join(path, name))
               for name in STORE_MARKERS)


def _profile_dirs(instance: str) -> list[str]:
    """The directories under an instance that Chrome reads a profile from.

    `Default` is the one `open` and `seed` use; `Profile 1`… are read too,
    because a whole user-data directory seeded with `--force` can bring them,
    and a login in one of them is a login this instance HAS. The instance root
    is read only when NO subdirectory looks like a profile — a directory that
    IS one. A root that also has a `Default/` is a `--user-data-dir` whose
    root-level stores Chrome never reads (what the old seed wrote there is a
    leftover, and counting it would double every login the real profile has),
    so those files are not read.
    """
    if not os.path.isdir(instance):
        return []
    found: list[str] = []
    try:
        names = sorted(os.listdir(instance))
    except OSError:
        return found
    for name in names:
        path = os.path.join(instance, name)
        if os.path.isdir(path) and not os.path.islink(path) \
                and _has_store_marker(path):
            found.append(path)
    if not found and _has_store_marker(instance):
        found.append(instance)
    return found


def _normalize_site(value: object) -> str:
    """A `--site` value as a bare host: scheme, path, userinfo and port out."""
    text = str(value or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    return text.split(":", 1)[0].strip(".")


def _host_matches(host: object, site: str) -> bool:
    """Does this cookie host belong to `site`? Suffix, not substring.

    `.x.com` and `www.x.com` both answer for `x.com`; `notx.com` must not —
    which a plain `in` test would have said it did.
    """
    bare = str(host or "").strip().lower().lstrip(".")
    return bare == site or bare.endswith("." + site)


def _origin_host(origin: object) -> str:
    """The host of a saved login's origin URL, for the same matching."""
    text = str(origin or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0].split(":", 1)[0]


def _host_rows(hosts: dict[str, dict], rows: list[tuple]) -> None:
    """Fold one Cookies store's rows into the per-host evidence.

    `expired` is true only when EVERY cookie on the host has a recorded expiry
    in the past: one live-or-session cookie is live state, and a session cookie
    has no expiry to compare against.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    for host, name, expires in rows:
        key = str(host or "")
        entry = hosts.setdefault(key, {"count": 0, "names": [],
                                       "expires": "", "expired": None})
        entry["count"] += 1
        entry["names"].append(str(name or ""))
        when = _chrome_time(expires)
        if when > entry["expires"]:
            entry["expires"] = when
        if not when or when >= now:
            entry["expired"] = False
        elif entry["expired"] is None:
            entry["expired"] = True


def _site_rows(hosts: dict[str, dict], cap: int) -> list[dict]:
    """The per-host evidence, busiest first, capped.

    `count` is the ROWS the host holds (one cookie name can appear twice with
    different paths), and `cookies` names each cookie ONCE — the question this
    verb answers is "is `auth_token` there", and a repeat says nothing.
    """
    rows = []
    for host, entry in hosts.items():
        names = sorted(set(entry["names"]))
        rows.append({
            "host": host,
            "count": entry["count"],
            "cookies": names[:COOKIE_NAMES],
            "more": max(0, len(names) - COOKIE_NAMES),
            "expires": entry["expires"],
            "expired": bool(entry["expired"]),
        })
    rows.sort(key=lambda row: (-row["count"], row["host"]))
    return rows[:cap]


def _logins_facts(instance: str, site: str = "",
                  cap: int = DEFAULT_SITES) -> dict:
    """The evidence `profile logins` reports, for ONE instance directory.

    Read-only by construction: every store is copied before it is opened (the
    real one is held by a running Chrome), only cookie NAMES, hosts and expiry
    plus the COUNT of saved logins are selected — never a cookie value, never a
    username, never a password — and nothing is written into the profile.
    `seed` calls this on its destination, so "what landed" is answered by the
    login stores themselves and not only by sizes.
    """
    dirs = _profile_dirs(instance)
    hosts: dict[str, dict] = {}
    origins: set[str] = set()
    cookie_rows = password_rows = 0
    cookies_present = passwords_present = False
    cookies_error = passwords_error = ""
    for directory in dirs:
        db = os.path.join(directory, "Cookies")
        if os.path.isfile(db):
            cookies_present = True
            rows, error = _cookie_rows(db)
            cookies_error = cookies_error or error
            rows = [row for row in rows
                    if not site or _host_matches(row[0], site)]
            cookie_rows += len(rows)
            _host_rows(hosts, rows)
        db = os.path.join(directory, "Login Data")
        if os.path.isfile(db):
            passwords_present = True
            rows, error = _login_rows(db)
            passwords_error = passwords_error or error
            for row in rows:
                origin = str(row[0] or "")
                if site and not _host_matches(_origin_host(origin), site):
                    continue
                password_rows += 1
                origins.add(origin)
    listed = _site_rows(hosts, cap)
    facts = {
        "profile_dirs": dirs,
        "stores": {
            "cookies": {"present": cookies_present,
                        "readable": cookies_present and not cookies_error,
                        "rows": cookie_rows, "hosts": len(hosts),
                        "error": cookies_error},
            "passwords": {"present": passwords_present,
                          "readable": passwords_present and not passwords_error,
                          "rows": password_rows, "origins": len(origins),
                          "error": passwords_error},
        },
        "sites": listed,
        "sites_total": len(hosts),
        "truncated": len(hosts) > len(listed),
        "verified_by": LOGINS_VERIFIED_BY,
        "note": LOGINS_NOTE,
    }
    if site:
        facts["site"] = site
    return facts


def logins(profile: str = "", browser: str = "", site: str = "",
           cap: int = DEFAULT_SITES) -> dict:
    """`profile logins [--profile DIR] [--site HOST] [--cap N]`.

    What is IN a managed profile: the hosts its Cookies store names (with the
    latest expiry each host's cookies carry), and how many saved logins it
    holds. This is the question `profile seed` answers with files, answered by
    the stores themselves — the "is it seeded?" check, without opening a
    browser.

    It is EVIDENCE, not a verdict (see `LOGINS_NOTE`): a missing cookie is not
    proof of a logout (a session-only cookie has no expiry on disk), and a
    present one is not proof of a live session (a token can be revoked
    server-side). `--site` narrows by HOST suffix; nothing matching is an empty
    `sites` list, not a refusal — the same rule the page verbs follow.
    """
    wanted = _normalize_site(site) if site else ""
    if site and not wanted:
        fail(ERR_BAD_ARGS,
             f"profile logins: --site needs a HOST (x.com, https://x.com/), "
             f"got {site!r}")
    if cap < 1:
        fail(ERR_BAD_ARGS,
             f"profile logins: --cap must be at least 1, got {cap}")
    target = Instance.resolve(profile, browser, verb="logins").path
    exists = os.path.isdir(target)
    reply = {"ok": True, "profile": target, "exists": exists}
    reply.update(_logins_facts(target, site=wanted, cap=min(cap, MAX_SITES)))
    row = Instance(target).browsers_row() if exists else None
    if row is not None:
        reply["live"] = {"pid": row["pid"], "exe": row["exe"],
                         "port": row["cdp"]["port"],
                         "verified": row["cdp"]["verified"]}
    reply["snapshot"] = row is not None
    if not exists:
        reply["note"] += (f"; no profile directory at {target} — `profile "
                           "seed` fills it, `open` creates it")
    return reply


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
