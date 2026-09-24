"""stores — what is IN a profile: the login stores, read from a COPY.

`profile logins` and the read-back `profile seed` performs answer the same
question from the same evidence: the profile's own `Cookies` and `Login Data`
files, copied before they are opened (a live browser may hold them), read with
literal, BOUNDED SQL, and reported as hosts, cookie NAMES, expiry and password
COUNTS — never a value. This module owns that reading, and the bounds and
wording both callers share.

Every read is bounded because a store is UNTRUSTED: it can be a file this CLI
did not write (a `--from` profile, a store Chrome itself is rewriting), and a
crafted one — a VIEW named `cookies` over an unterminated recursive CTE —
controls its ROW COUNT independently of its file size, answering forever out of
4 KB. The unbounded read that used to be here died with `MemoryError` at 1.4 GB
peak RSS (a review measured it), so the row count is capped, every value's text
is truncated in SQL, the copy is opened read-only, and a cap that BIT is
reported as the `capped` fact rather than passing for the whole store.

The row cap and the text cap bound the RESULT, not the WORK: SQLite must still
materialise every row expression, and a row-DEPENDENT one — a view answering
`hex(zeroblob(4000000 + (c.x & 1)))` — is work constant folding cannot remove,
measured at >25 s out of one 4 KB store and scaling with the row's own size,
with no deadline anywhere on the path and SIGINT doing nothing (the statement
runs in C, where a Python signal handler is not consulted). So every read ALSO
runs under a WALL-CLOCK budget (`STORE_READ_BUDGET_S`, checked every
`STORE_READ_CHECK_OPCODES` VM instructions by a progress handler): a store that
outruns it is CUT OFF, and the reply says so — `readable: false` with the reason
in `error` and `capped: true` saying its rows are a FLOOR, never a smaller
number passed off as the store. `pragma trusted_schema=OFF` rides beside it, so
a schema this CLI did not write cannot reach a non-innocuous function.

The COPY each read is made from lives in this CLI's own 0700 tree
(`<root>/.store-reads`), not the shared temp directory: a caller who kills a
read that hangs (SIGTERM/SIGKILL) skips `TemporaryDirectory`'s cleanup, and a
leftover byte copy of `Cookies`/`Login Data` under /tmp was invisible to every
sweep this tool has. Stale copies — ours and the legacy
`browser-control-logins-*` spelling — are collected under four proofs (name,
kind, owner, age) by `_sweep_copies`.
"""
from __future__ import annotations

import contextlib
import errno
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import tempfile
import time
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
from browser_control.lib.paths import (
    ensure_root,
    root,
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

#: How many rows this CLI will read out of ONE store. A hostile store answers
#: forever (see the module docstring), so the read is capped; a real profile
#: holds thousands of cookie rows, far below this. `capped: true` in the reply
#: says the cap BIT, so the counts are read as a FLOOR, never as the store.
MAX_STORE_ROWS = 20_000

#: The longest text ONE store value may put in the reply: a 64 MB cookie `name`
#: used to ride into it verbatim. Truncation happens in SQL, so the huge value
#: never crosses into Python.
MAX_STORE_TEXT = 256

#: How long ONE store read may run before this CLI cuts it off. The row/text
#: caps bound the RESULT; this bounds the WORK, which a hostile store controls
#: with a row-DEPENDENT expression (`hex(zeroblob(4000000 + (c.x & 1)))`) that
#: SQLite must materialise row by row — measured at >25 s out of a 4 KB file.
#: The budget is per STORE (the copy of the file is not charged to it), seconds
#: rather than milliseconds because a real profile holds thousands of rows of
#: small values and reads in a few milliseconds.
STORE_READ_BUDGET_S = 5.0

#: How many SQLite VM instructions run between two deadline checks. Deliberately
#: small: the progress handler sees whole instructions, so the abort lands
#: within this many instructions' worth of WORK — and ONE instruction can be a
#: multi-megabyte allocation — while checking this often costs milliseconds over
#: a full `MAX_STORE_ROWS` read.
STORE_READ_CHECK_OPCODES = 100

#: Where a store COPY is made: a private subdirectory of this CLI's OWN root
#: (`paths.root()`), not the shared temp directory. A caller kills a read that
#: hangs, and a kill skips the cleanup — so the leftover must at least be inside
#: the tree the caller already trusts, named so `_sweep_copies` can prove it is
#: ours. Dot-prefixed, because `profiles()` and `_profile_locations` both skip
#: this CLI's own bookkeeping (`.locks` is the other one).
COPY_DIR_NAME = ".store-reads"

#: A copy directory's name: the pid that made it, then `mkdtemp`'s random tail.
#: The pid is what lets a sweep leave a READ IN PROGRESS alone.
COPY_NAME_RE = re.compile(r"^([0-9]+)-[A-Za-z0-9_]{8}$")

#: The copy spelling the read used BEFORE it moved under our own root: a bare
#: `mkdtemp(prefix="browser-control-logins-")` under /tmp. No pid was recorded,
#: so only the owner and the age can vouch for one — and they are exactly what
#: `_sweep_copies` checks. New fallback copies carry their pid in the same name.
LEGACY_COPY_PREFIX = "browser-control-logins-"
LEGACY_COPY_NAME_RE = re.compile(
    r"^" + LEGACY_COPY_PREFIX + r"(?:([0-9]+)-)?[A-Za-z0-9_]{8}$")

#: A copy directory older than this is STALE and may be swept: an hour is far
#: longer than any read this CLI runs (`STORE_READ_BUDGET_S` twice over), so a
#: live read is never inside the window.
STALE_COPY_AGE_S = 3600.0

#: The two reads, spelled ONCE as module-level LITERALS: every bound rides as a
#: `?` placeholder (`limit` included — SQLite takes one there), so no value
#: ever crosses into the SQL text. The `limit` asks for ONE ROW PAST the cap —
#: that extra row is the `capped` fact — and the `substr` caps every value,
#: `expires_utc` included, because a hostile schema can answer a column with a
#: megabyte-sized blob. `cast(… as text)` makes the cap exact: a BLOB reached
#: Python as bytes, whose `repr` is four characters per byte. A parameterized
#: `limit` still stops an unterminated recursive CTE at the row asked for
#: (measured: 20 001 rows in 8 ms out of a 4 KB file). That bounds the ROWS; the
#: WORK one row may cost is bounded separately, by the read budget above, because
#: a row expression SQLite must materialise is not something a `limit` avoids.
COOKIE_ROWS_SQL = ("select cast(substr(host_key, 1, ?) as text), "
                   "cast(substr(name, 1, ?) as text), "
                   "cast(substr(expires_utc, 1, ?) as text) "
                   "from cookies limit ?")
COOKIE_ROWS_ARGS = (MAX_STORE_TEXT, MAX_STORE_TEXT, 32, MAX_STORE_ROWS + 1)
LOGIN_ROWS_SQL = ("select cast(substr(origin_url, 1, ?) as text) "
                  "from logins limit ?")
LOGIN_ROWS_ARGS = (MAX_STORE_TEXT, MAX_STORE_ROWS + 1)

#: what the verb's oracle IS, said once so both callers report the same thing
LOGINS_VERIFIED_BY = ("rows in the profile's own Cookies and Login Data "
                      "stores, read from a COPY of each file")
LOGINS_NOTE = ("the stores on disk are the evidence: cookie names, hosts, "
               "expiry and password COUNTS are reported, never a value; a "
               "cookie can be revoked server-side, and `snapshot: true` means "
               "a browser is running on this profile, so disk may lag it; a "
               "store with `readable: false` could not be read, so its rows "
               "are UNKNOWN, not zero; a store with `capped: true` either holds "
               f"more rows than this CLI reads ({MAX_STORE_ROWS}) or was cut "
               f"off at its per-store read budget ({STORE_READ_BUDGET_S:g}s), "
               "so its `rows` and `hosts` are a FLOOR, not the whole store")

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


def _copy_store(source: str, copy: str) -> None:
    """Copy ONE store file — never through a symlink, in bounded memory.

    `O_NOFOLLOW` is the atomic guard and the `islink` check names the reason:
    `shutil.copy2` FOLLOWS a link, so a symlink planted at
    `<profile>/Default/Cookies` was copied out of the managed root and censused
    as this profile's own store (a review measured a host from OUTSIDE the root
    in the reply). An `ELOOP` from here becomes the store's `error`, and the
    store is never read.

    The bytes are streamed, so a store that is large on disk costs a buffer,
    not its size in RAM; the read that follows is bounded separately
    (`COOKIE_ROWS_SQL`). `O_NONBLOCK` rides on the open for the same reason the
    read has a deadline: a non-regular file at the store path (a FIFO planted
    between the census and the copy) must fail as an `OSError` the caller
    reports, never block the copy for ever.
    """
    if os.path.islink(source):
        raise OSError(errno.ELOOP,
                      "is a symlink — this CLI reads the store FILE, never "
                      "through a link", source)
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        with open(copy, "wb") as writer:
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                writer.write(chunk)
    finally:
        os.close(fd)


class _ReadBudget:
    """The WALL-CLOCK budget ONE store read runs under.

    SQLite calls `check` every `STORE_READ_CHECK_OPCODES` VM instructions, and a
    non-zero return aborts the running statement with
    `OperationalError("interrupted")` — the only way a read that never finishes
    can be cut off, because the statement runs in C where a Python signal
    handler is not consulted (a review measured SIGINT doing nothing to the
    crafted store). `start` is called just before the statement runs, so the
    COPY of the file is not charged to the read.
    """

    def __init__(self) -> None:
        self.deadline = 0.0
        self.tripped = False

    def start(self) -> None:
        """Begin the budget: the deadline is NOW plus `STORE_READ_BUDGET_S`."""
        self.deadline = time.monotonic() + STORE_READ_BUDGET_S

    def check(self) -> int:
        """The progress handler: non-zero aborts the read once time is up.

        A budget nobody armed is armed by the FIRST instruction that runs, so a
        caller that forgets `start` still gets a bounded read instead of an
        unbounded one: the deadline is then measured from the statement rather
        than from the copy before it, which is the safe direction.
        """
        if not self.deadline:
            self.start()
            return 0
        if time.monotonic() >= self.deadline:
            self.tripped = True
            return 1
        return 0


def _cut_off_reason() -> str:
    """Why a store's rows are a FLOOR: the read hit the wall-clock budget.

    The message says the read was CUT OFF rather than letting an interrupted
    store read as a small one: `readable: false` alone would leave a caller to
    guess whether the store is empty or was abandoned.
    """
    return (f"interrupted after {STORE_READ_BUDGET_S:g}s — this store took "
            "longer than this CLI's per-store read budget, so the read was CUT "
            "OFF and its rows are a FLOOR, not the whole store")


def _pid_alive(pid: int) -> bool:
    """Is that pid a process this user can see? An unknowable pid is ALIVE.

    Used by `_sweep_copies` to leave a read that is still running alone. Only
    `ESRCH` — no such process — proves the pid is gone; `EPERM` means it exists
    under another user, and any other failure means this CLI cannot tell, and a
    directory it cannot prove is dead is not one it deletes.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _sweep_copies(parent: str, pattern: re.Pattern[str]) -> None:
    """Remove STALE store copies this CLI left in `parent`; never raises.

    A read that is killed — what a caller does to one that hangs — leaves its
    copy behind, because the context manager's cleanup never runs, and nothing
    used to collect it. Four proofs before anything is deleted, because `parent`
    can be the shared temp directory as well as our own: the NAME must match
    this CLI's own spelling (`pattern`), the entry must be a real DIRECTORY
    (checked with `lstat`, so a symlink cannot aim the delete elsewhere), it
    must be OWNED by this user, and it must be older than `STALE_COPY_AGE_S`. A
    name that records a pid is kept while that process is alive, and this
    process's own copy is never touched.
    """
    try:
        names = os.listdir(parent)
    except OSError:
        return
    now = time.time()
    mine = str(os.getpid())
    for name in names:
        match = pattern.match(name)
        if match is None or match.group(1) == mine:
            continue
        path = os.path.join(parent, name)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            continue
        if now - info.st_mtime < STALE_COPY_AGE_S:
            continue
        pid = match.group(1)
        if pid is not None and _pid_alive(int(pid)):
            continue
        shutil.rmtree(path, ignore_errors=True)


def _copy_root() -> str:
    """This CLI's OWN 0700 directory for store copies, "" when it cannot be made.

    `<root>/.store-reads`, beside the `.locks` this CLI already keeps there: the
    copy is a byte copy of `Cookies`/`Login Data`, and the shared temp directory
    is not where one should be left behind (see `_sweep_copies`). `ensure_root`
    first, so the root itself is never an intermediate at the umask default. ""
    means the root could not be made private — an ANSWER for the caller, which
    then uses `mkdtemp` under /tmp exactly as the read used to.
    """
    try:
        ensure_root()
        path = os.path.join(root(), COPY_DIR_NAME)
        os.makedirs(path, mode=0o700, exist_ok=True)
        # narrow a directory that was already there: `mode` applies to the LEAF
        # of `makedirs` only, and this one holds copies of login stores
        os.chmod(path, 0o700)
    except OSError:
        return ""
    return path


def _store_workspace() -> str:
    """A private directory for ONE store copy; the CALLER removes it.

    In this CLI's own root when it can be made (`_copy_root`), and swept of
    stale siblings first; under /tmp only as the fallback. A kill still skips
    the caller's cleanup, which is why both spellings carry what a later sweep
    needs to prove they are ours. The LEGACY /tmp spelling is swept on every
    read, even though this CLI no longer makes one: those are the leftovers an
    older version left behind when it was killed, and nothing else would collect
    them.

    An `OSError` here is the CALLER's to report as the store's `error`: a
    directory that cannot be made is a store that cannot be copied, which is an
    answer, not a crash.
    """
    legacy = tempfile.gettempdir()
    _sweep_copies(legacy, LEGACY_COPY_NAME_RE)
    parent = _copy_root()
    if parent:
        _sweep_copies(parent, COPY_NAME_RE)
        prefix = f"{os.getpid()}-"
    else:
        parent = legacy
        prefix = f"{LEGACY_COPY_PREFIX}{os.getpid()}-"
    return tempfile.mkdtemp(prefix=prefix, dir=parent)


@contextlib.contextmanager
def _store_connection(db: str) -> Iterator[tuple[sqlite3.Connection | None,
                                                  str, _ReadBudget]]:
    """An open READ-ONLY connection to a COPY of one store, or `(None, reason)`.

    A COPY for two reasons: the live file can be mid-write (`database is
    locked`), and this verb must not touch the profile it reports on. A
    `-wal`/`-journal` beside the store travels with the copy, so a store in a
    write-ahead state still answers. The copy is opened `mode=ro`, so a hostile
    schema cannot write through this CLI even in the private directory the copy
    lives in (`_store_workspace`).

    `immutable=1` is deliberately NOT used beside it, and the reason is
    measured, not stylistic: SQLite IGNORES the `-wal` of an immutable
    database, so the committed rows a live Chrome keeps there — the freshest
    logins of all — silently vanish from the census and from `seed`'s
    read-back. `mode=ro` reads them (it builds the `-shm` it needs in the copy
    directory, which is ours).

    The returned connection carries the read's DEADLINE (`_ReadBudget`, armed by
    the caller with `budget.start()` before its statement) and
    `pragma trusted_schema=OFF`, so a schema this CLI did not write can neither
    outrun the budget nor reach a non-innocuous function. The pragma is
    best-effort: an SQLite too old to know it must not turn a readable store
    into an unreadable one.

    A store that cannot be opened is an ANSWER — the reason is yielded, never
    raised: a profile whose `Cookies` is not a database is exactly what a
    caller needs told, `seed` reads the same stores back without failing on one,
    and a copy directory that cannot even be made is reported the same way. The
    copy is removed when the read is done — with `ignore_errors`, because a read
    that ANSWERS must not be turned into a failure by its own cleanup: a copy
    left behind is collected later by `_sweep_copies`.
    """
    budget = _ReadBudget()
    try:
        tmp = _store_workspace()
    except OSError as e:
        yield None, f"cannot copy the store: {e}", budget
        return
    try:
        # the workspace stays for the WHOLE read: it holds the copy the
        # connection reads from, and leaving it before `connect` deleted the
        # file out from under the read ("unable to open database file")
        copy = os.path.join(tmp, os.path.basename(db))
        try:
            _copy_store(db, copy)
            for suffix in ("-wal", "-journal"):
                if os.path.lexists(db + suffix):
                    _copy_store(db + suffix, copy + suffix)
        except OSError as e:
            yield None, f"cannot copy the store: {e}", budget
            return
        try:
            # `as_uri` percent-encodes a temp path holding a space or a '?':
            # a raw f-string URI would have opened a DIFFERENT file
            conn = sqlite3.connect(pathlib.Path(copy).as_uri() + "?mode=ro",
                                   uri=True)
        except sqlite3.Error as e:
            yield None, str(e), budget
            return
        try:
            with contextlib.suppress(sqlite3.Error):
                # defence in depth: the schema is the untrusted input, and a
                # view reaching a non-innocuous function is not something this
                # CLI reads stores for. Best-effort — see the docstring.
                conn.execute("pragma trusted_schema = off")
            conn.set_progress_handler(budget.check, STORE_READ_CHECK_OPCODES)
            yield conn, "", budget
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _cookie_rows(db: str) -> tuple[list[tuple], str, bool]:
    """One Cookies store: host, name and expiry of up to `MAX_STORE_ROWS` rows.

    The SQL is the module literal `COOKIE_ROWS_SQL` — it never crosses a
    variable, so there is nothing to inject into — and it is BOUNDED: the
    `limit` caps the rows and the `substr` caps every value's text. One row
    past the cap is requested on purpose: that row is the `capped` fact. A
    schema SQLite cannot answer it from fails the read, naming what it could
    not find ("no such column: …"), which is the answer the caller gets.

    The read also runs under `STORE_READ_BUDGET_S`: the two caps above bound
    the RESULT, and a row expression heavy enough to defeat constant folding
    (see the module docstring) is bounded HERE, by the progress handler the
    connection carries. A read that the deadline CUT OFF is reported as
    unreadable with that reason and `capped: true` — never as a store with
    fewer rows, which is what an interrupted `fetchall` would otherwise look
    like.
    """
    with _store_connection(db) as (conn, error, budget):
        if conn is None:
            return [], error, False
        budget.start()
        try:
            rows = conn.execute(COOKIE_ROWS_SQL, COOKIE_ROWS_ARGS).fetchall()
        except sqlite3.Error as e:
            if budget.tripped:
                return [], _cut_off_reason(), True
            return [], str(e), False
        capped = len(rows) > MAX_STORE_ROWS
        return [tuple(row) for row in rows[:MAX_STORE_ROWS]], "", capped


def _login_rows(db: str) -> tuple[list[tuple], str, bool]:
    """One Login Data store: the ORIGIN of up to `MAX_STORE_ROWS` logins.

    Bounded exactly as `_cookie_rows` is: the oracle is evidence, so a store
    that answers forever is capped and the cap is REPORTED, never silently
    trimmed — and a store that outruns the wall-clock budget is reported as CUT
    OFF, which is a different fact from a full one.
    """
    with _store_connection(db) as (conn, error, budget):
        if conn is None:
            return [], error, False
        budget.start()
        try:
            rows = conn.execute(LOGIN_ROWS_SQL, LOGIN_ROWS_ARGS).fetchall()
        except sqlite3.Error as e:
            if budget.tripped:
                return [], _cut_off_reason(), True
            return [], str(e), False
        capped = len(rows) > MAX_STORE_ROWS
        return [tuple(row) for row in rows[:MAX_STORE_ROWS]], "", capped


def _store_here(path: str) -> bool:
    """Is there something AT this store path — a link counting as one?

    `os.path.isfile` follows links, so it answered "no store" for a DANGLING
    one and "a store" for a link pointing anywhere. A link is something at the
    path: the census must say `present: true, readable: false` with the reason,
    never `present: false`, which would hide the plant.
    """
    return os.path.islink(path) or os.path.isfile(path)


def _unreadable_store(db: str) -> str:
    """Why this store must NOT be read, or "" when it may be.

    Two escapes, both closed before a single byte is copied: a SYMLINK (never
    followed — `_copy_store` refuses it again with `O_NOFOLLOW`, so a link
    planted between this check and the copy is still not read), and a path
    whose REAL path leaves the managed root. The reason is the store's `error`
    in the census, so the caller is told which of the two happened.
    """
    if os.path.islink(db):
        return (f"{db} is a symlink — this CLI reads the store FILE, never "
                "through a link (a planted link would census somebody else's "
                "profile, or another login, as this one)")
    real = os.path.realpath(db)
    managed = os.path.realpath(root())
    if not real.startswith(managed + os.sep):
        return (f"{db} resolves to {real}, outside {managed} — this CLI reports "
                "the stores under its own root only")
    return ""


def _store_markers(path: str) -> list[str]:
    """Which store markers exist AT this directory (a link followed, never read).

    The one-level-down spelling of `_store_here`: it answers "does something
    profile-shaped sit here" without opening a byte, so a SYMLINKED `Default`
    can be reported as a location — with the stores it holds named, still
    without reading through it.
    """
    return [name for name in STORE_MARKERS
            if _store_here(os.path.join(path, name))]


def _has_store_marker(path: str) -> bool:
    """Does this directory look like a Chrome PROFILE directory?"""
    return bool(_store_markers(path))


def _symlinked_dir_reason(path: str) -> str:
    """Why a SYMLINKED profile-shaped subdirectory is a location, not nothing.

    Same rule as `_store_here`, one level UP: something IS there, it is just not
    something this CLI reads — `_profile_locations` never descends into a link,
    `_copy_store` refuses one with `O_NOFOLLOW`, and the whole point of the rule
    is that a planted link must not census somebody else's profile, or another
    login, as this one. The LINK's target is deliberately not named: the reply
    says what this CLI saw and refused, and the outside path it points at is not
    this profile's business.
    """
    return (f"{path} is a symlink — this CLI reads the store FILE, never "
            "through a link (a planted link would census somebody else's "
            "profile, or another login, as this one), so no store under it was "
            "read")


def _link_entry(name: str, path: str) -> dict:
    """One symlink the census SAW: named, classified, never followed for reads.

    `kind` is what the link resolves to now ("directory", "file", "other" for a
    device or a FIFO, "none" for a dangling one), `stores` names the store
    markers behind it — computed with `stat` only — and `reason` is the refusal
    when it is profile-shaped. Nothing here names the target: a link is reported
    as a fact about this profile, and the path it points at may be somebody
    else's.
    """
    try:
        # ONE `stat` through the link, for the KIND alone: nothing is opened,
        # read or listed behind it
        info = os.stat(path)
    except OSError:
        info = None                     # a dangling link resolves to nothing
    if info is None:
        kind = "none"
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        kind = "other"
    markers = _store_markers(path) if kind == "directory" else []
    return {"name": name, "path": path, "kind": kind, "stores": markers,
            "reason": _symlinked_dir_reason(path) if markers else ""}


def _profile_locations(instance: str) -> tuple[list[str], list[dict]]:
    """Where an instance's stores are READ from, and the links it PLANTED.

    `Default` is the one `open` and `seed` use; `Profile 1`… are read too,
    because a whole user-data directory seeded with `--force` can bring them,
    and a login in one of them is a login this instance HAS. The instance root
    is read only when NO subdirectory looks like a profile — a directory that
    IS one. A root that also has a `Default/` is a `--user-data-dir` whose
    root-level stores Chrome never reads (what the old seed wrote there is a
    leftover, and counting it would double every login the real profile has),
    so those files are not read.

    A SYMLINKED child is never read from, and is never silently DROPPED either:
    a `Default` that is a link to a store-bearing directory used to leave the
    census with no directories at all, so the whole instance answered "no stores"
    — `present: false, error: ""` about a profile that is right there. It comes
    back in the second half of the pair instead, as a LOCATION with its reason
    (the answer `_store_here` already gives for the store FILE one level down).
    Dot-directories are skipped: `.locks` and `.store-reads` are this CLI's own
    bookkeeping, exactly as `profiles()` says at the root.
    """
    if not os.path.isdir(instance):
        return [], []
    found: list[str] = []
    links: list[dict] = []
    try:
        names = sorted(os.listdir(instance))
    except OSError:
        return found, links
    for name in names:
        if name.startswith("."):
            continue
        path = os.path.join(instance, name)
        if os.path.islink(path):
            links.append(_link_entry(name, path))
            continue
        if os.path.isdir(path) and _has_store_marker(path):
            found.append(path)
    if not found and _has_store_marker(instance):
        found.append(instance)
    return found, links


def profile_links(path: str) -> list[dict]:
    """The symlinks the census SAW at the top of one profile, named.

    `profile info` reports what it found, and a symlinked profile-shaped
    subdirectory is the one finding a size census cannot express: the walk
    counts it as a link and weighs nothing behind it, so the row said "0 files,
    0 bytes" about a directory full of stores. This is that fact, said out loud
    (see `_link_entry`); the LINK is never followed for a read and its target is
    never named.
    """
    return _profile_locations(path)[1]


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
    Bounded and non-following too: a store that is a symlink, or that resolves
    outside this CLI's root, is reported with a reason instead of read, the row
    count is capped (`capped`) and every value's text is truncated in SQL.
    `seed` calls this on its destination, so "what landed" is answered by the
    login stores themselves and not only by sizes.

    A symlinked profile-shaped SUBDIRECTORY (a `Default` that is a link) is the
    same refusal one level up: the stores behind it are reported PRESENT with
    that reason and read by nobody, and the links themselves ride in `links`, so
    the census says what it saw instead of reporting no stores at all.
    """
    dirs, links = _profile_locations(instance)
    hosts: dict[str, dict] = {}
    origins: set[str] = set()
    cookie_rows = password_rows = 0
    cookies_present = passwords_present = False
    cookies_capped = passwords_capped = False
    cookies_error = passwords_error = ""
    for directory in dirs:
        db = os.path.join(directory, "Cookies")
        if _store_here(db):
            cookies_present = True
            # a store this CLI must not read (a symlink, a path resolving out
            # of the root) is REPORTED with its reason: skipping it silently
            # would census somebody else's login as this profile's
            reason = _unreadable_store(db)
            if reason:
                cookies_error = cookies_error or reason
            else:
                rows, error, capped = _cookie_rows(db)
                cookies_capped = cookies_capped or capped
                cookies_error = cookies_error or error
                rows = [row for row in rows
                        if not site or _host_matches(row[0], site)]
                cookie_rows += len(rows)
                _host_rows(hosts, rows)
        db = os.path.join(directory, "Login Data")
        if _store_here(db):
            passwords_present = True
            reason = _unreadable_store(db)
            if reason:
                passwords_error = passwords_error or reason
            else:
                rows, error, capped = _login_rows(db)
                passwords_capped = passwords_capped or capped
                passwords_error = passwords_error or error
                for row in rows:
                    origin = str(row[0] or "")
                    if site and not _host_matches(_origin_host(origin), site):
                        continue
                    password_rows += 1
                    origins.add(origin)
    for entry in links:
        # a symlinked profile-shaped subdirectory holds stores this census must
        # not read, and must not call ABSENT either: `present: true` with the
        # reason is the honest row, and `readable: false` keeps its counts out
        # of the reply
        reason = str(entry["reason"])
        if "Cookies" in entry["stores"]:
            cookies_present = True
            cookies_error = cookies_error or reason
        if "Login Data" in entry["stores"]:
            passwords_present = True
            passwords_error = passwords_error or reason
    listed = _site_rows(hosts, cap)
    facts = {
        "profile_dirs": dirs,
        # every symlink at the top of the instance, profile-shaped or not: the
        # census reports what it saw, and never follows one
        "links": links,
        "stores": {
            "cookies": {"present": cookies_present,
                        "readable": cookies_present and not cookies_error,
                        "rows": cookie_rows, "hosts": len(hosts),
                        # the read was BOUNDED: `capped` says the bound bit, so
                        # `rows`/`hosts` are a floor and `rows_limit` is the cap
                        # they were read under (a crafted store answers forever,
                        # and a store cut off at the budget is a floor too)
                        "capped": cookies_capped,
                        "rows_limit": MAX_STORE_ROWS,
                        "error": cookies_error},
            "passwords": {"present": passwords_present,
                          "readable": passwords_present and not passwords_error,
                          "rows": password_rows, "origins": len(origins),
                          "capped": passwords_capped,
                          "rows_limit": MAX_STORE_ROWS,
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
