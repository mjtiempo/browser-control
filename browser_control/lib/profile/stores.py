"""stores — what is IN a profile: the login stores, read from a COPY.

`profile logins` and the read-back `profile seed` performs answer the same
question from the same evidence: the profile's own `Cookies` and `Login Data`
files, copied before they are opened (a live browser may hold them), read with
literal SQL, and reported as hosts, cookie NAMES, expiry and password COUNTS —
never a value. This module owns that reading, and the bounds and wording both
callers share.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone

from browser_control.lib.coerce import as_int
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    fail,
)
from browser_control.lib.instance import (
    Instance,
)

# --- what is IN a profile: the login stores, read from a COPY --------------
#: A Chrome PROFILE directory (the one Chrome reads under a `--user-data-dir`)
#: is told apart from the instance root by what is inside it; these are the
#: files a login lands in, or beside.
STORE_MARKERS = ("Cookies", "Login Data", "Preferences")

#: `profile logins` bounds: hosts listed, and cookie names named per host
DEFAULT_SITES = 20
MAX_SITES = 200
COOKIE_NAMES = 12

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
