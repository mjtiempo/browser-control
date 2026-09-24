"""machine — the census: every browser here, and the rows verbs report.

`browsers()` walks /proc once — the memo below is what makes that true for the
whole census rather than only for its first row; the projections (`brief`,
`_row`, `_endpoint_details`) are what `list`, `info` and `tab list` return.
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from dataclasses import dataclass

from browser_control.lib import (
    attachments as attachments_lib,
)
from browser_control.lib import browser as _pkg
from browser_control.lib import (
    cdp,
    )
from browser_control.lib.coerce import (
    as_int,
)
from browser_control.lib.errors import (
    ERR_AMBIGUOUS_BROWSER,
    ERR_CDP_ERROR,
    ControlError,
    fail,
)
from browser_control.lib.paths import (
    is_managed,
    norm,
)
from browser_control.lib.proc import (
    cmdline_value,
    exe_path,
    is_headless_cmd,
    main_processes,
    pid_on_marker,
)

# The process list ONE census is reading, or None outside a census. The memo
# is the answer to a measured cost: every reachable row verified itself
# through `find_pid`, which walked /proc AGAIN (3 walks for 2 browsers), and
# `list`/`tab list` census more than once. It is opened and closed by
# `census_processes`, so the list never outlives the census that read it: a
# caller that censuses twice sees /proc twice, which is what keeps "the
# browser exited mid-verb" a REPORTED fact rather than a hidden one.
_PROCS: list[tuple[int, str, str]] | None = None


@contextlib.contextmanager
def census_processes(procs: list[tuple[int, str, str]] | None = None,
                     ) -> Iterator[list[tuple[int, str, str]]]:
    """Hold ONE /proc process list open for the duration of one census.

    Re-entrant, and a nested window REUSES the outer list unless it is handed
    one: `list_tabs` opens the window for its whole reply so its two censuses
    share one walk, while a bare `browsers()` opens its own. The window is
    closed on the way out — even on a refusal — so nothing downstream reads a
    process list from a census that has already answered.
    """
    global _PROCS
    saved = _PROCS
    if procs is None and saved is None:
        procs = main_processes()
    if procs is not None:
        _PROCS = procs
    try:
        yield _PROCS if _PROCS is not None else []
    finally:
        _PROCS = saved


def profile_pid(profile: str) -> int:
    """The pid running ON this profile, from the census' own process list.

    `proc.find_pid` is the same rule over a fresh walk; this one answers from
    the list the census in progress already read, so verifying N rows costs
    ONE walk. Outside a census it walks, exactly like `find_pid`.
    """
    if not profile:
        return 0
    procs = _PROCS if _PROCS is not None else main_processes()
    for pid, _exe, cmd in procs:
        if pid_on_marker(pid, cmd, profile):
            return pid
    return 0


def row_port(row: dict) -> int:
    """The CDP port the endpoint of a browser ROW answers on.

    The row is the only place the file-vs-cmdline question is already
    resolved (`browsers()`), so a verb that holds a row never re-reads the
    port file: the file is a SECOND oracle, and for a browser started with an
    explicit `--remote-debugging-port=N` it is the wrong one — Chrome writes
    `DevToolsActivePort` only when the port is 0. Resolving it ONCE per verb is
    also what keeps a later re-read from aiming a write at a port that changed
    in between (the verify-then-use gap `owners.verify_ws_owner` closes at the
    connection itself).
    """
    return as_int(endpoint_of(row).get("port"))


def driver_port(profile: str) -> int:
    """The port to drive `profile` on: the port file when it ANSWERS AND
    VERIFIES, else the port the process itself names, else the file's.

    The ONE resolver for a caller that has a profile but no row. A browser
    this CLI started is launched with `--remote-debugging-port=0`, so the OS
    picks the port and the browser writes `DevToolsActivePort` — the file
    answers and, when its holder is really this profile's browser, it is used.
    A browser started by hand with an explicit `--remote-debugging-port=N`
    writes NO file, so `cdp.port_of` answers 0 for it: the process's own
    cmdline is the oracle then, which is exactly what `browsers()` reads into
    the row.

    ANSWERING is not OWNING. The file survives a SIGKILL and its port can be
    REUSED, so a file whose port answers can be a stranger's listener; this
    used to return that port on the strength of the answer alone, and every
    caller behind it then aimed at the stranger while the browser's own
    `--remote-debugging-port=N` was never even probed (a review measured a
    file naming 9333 held by a python process while the browser's 9222
    answered: `driver_port` returned 9333 and `attach` refused `no-browser`
    about the browser that was right there). The file's port is returned only
    when the kernel says its holder IS this profile's browser; otherwise the
    census answers with the port the PROCESS names (`browsers`, which resolves
    it the same way and in the same order), and when nothing answers the
    file's port is still returned — the honest `cdp-not-local` /
    `cdp-unreachable` refusal that follows is the one callers already handled.
    """
    file_port = as_int(cdp.port_of(profile))
    if file_port and cdp.answers(file_port) \
            and _pkg.endpoint_owner(profile, file_port).get("verified"):
        return file_port
    want = norm(str(profile or ""))
    if want:
        for row in _pkg.browsers():
            if norm(str(row.get("profile") or "")) != want:
                continue
            port = row_port(row)
            if port:
                return port
    return file_port


def browsers() -> list[dict]:
    """Every Chromium-family browser RUNNING here, ours or the user's.

    One /proc pass over the main processes — held open for the whole census
    (`census_processes`), so the identity check of every row reads the list
    this walk produced instead of walking again: each with the profile it runs
    on (`--user-data-dir`, else the default data directory for that
    executable) and whether a DevTools endpoint answers for it — which is what
    makes it drivable. `managed` says the profile is one this CLI owns and
    `attached` that it was attached for tab writes, so the answer is about the
    whole machine rather than our own corner of it; `headless` says it runs
    with no window, read from the process's own cmdline
    (`proc.is_headless_cmd`) — the mode is a fact about the process, not about
    this CLI's flags.

    The PORT is resolved the same way `driver_port` resolves it, and in that
    order: the profile's port file when it ANSWERS, else the port the process
    itself names on its own command line. A file that does not answer is not
    evidence of anything (it can be a leftover of a dead run), and preferring
    it made `list` report a live, answering browser as unreachable and
    `attach --port N` refuse `no-browser` about a browser that was right
    there. The row reports the port that ANSWERED — never one that was merely
    written down. A process whose ports all stay silent is still a row (with
    `reachable: false`): a census reports the machine, it does not drop what
    it cannot reach.

    ANSWERING is not OWNING, so when the file's port answers and the kernel
    says its holder is NOT this profile's browser, the port the process names
    on its own `--remote-debugging-port=N` is the next oracle — the browser
    may have written a stale file (it survives a SIGKILL, and its port can be
    reused) while its OWN endpoint answers all along. The row then reports
    that port, judged on its own merits, and `port_from` says WHICH oracle
    answered (`"file"` or `"cmdline"`), so a reader can tell a browser
    verified through its port file from one verified through its own argv.
    Without the second oracle the file's stranger WAS the row: `_drivable`
    refused `cdp-not-local`, `tab list` filed the live browser under
    `unverified`, `driver_port` answered the stranger's port and `attach
    --port N` refused `no-browser` about the browser that was right there (a
    review measured all of it). It is one probe per candidate: the file's
    port once, and the named port once, and only when the file both answered
    and failed to verify.
    """
    rows: list[dict] = []
    attached = attachments_lib.STORE.records()
    # ONE walk, held open for every row below: `endpoint_owner` resolves the
    # profile's pid through `profile_pid`, which reads THIS list
    with census_processes() as procs:
        for pid, exe, cmd in procs:
            flag = cmdline_value(cmd, "--user-data-dir")
            profile = flag or _pkg._default_profile(exe)
            file_port = as_int(cdp.port_of(profile)) if profile else 0
            named_port = as_int(cmdline_value(cmd, "--remote-debugging-port"))
            port = file_port
            port_from = "file" if file_port else ""
            reachable = bool(file_port) and cdp.answers(file_port)
            owner: dict = {}
            if reachable:
                # WHO holds the file's port: an answer is not an identity, so
                # the verdict decides whether it is this browser's endpoint
                owner = _pkg.endpoint_owner(profile, port)
                if not owner["verified"] and named_port \
                        and named_port != file_port and cdp.answers(named_port):
                    port, port_from = named_port, "cmdline"
                    owner = _pkg.endpoint_owner(profile, port)
            if not reachable and named_port and named_port != file_port:
                # the file did not answer: the process's OWN flag is the next
                # oracle, and the port that answers is the one reported
                port, port_from = named_port, "cmdline"
                reachable = cdp.answers(named_port)
            endpoint: dict = {"port": port, "reachable": reachable,
                              "verified": False, "port_from": port_from}
            if reachable:
                # the row reports the pid and exe the kernel says own the
                # listening socket, and `verified` says whether that process is
                # this browser. Nothing here DRIVES an unverified row.
                if not owner:
                    owner = _pkg.endpoint_owner(profile, port)
                endpoint["verified"] = bool(owner["verified"])
                endpoint["listener"] = {"pid": owner["pid"],
                                        "exe": owner["exe"]}
                if owner["verified"]:
                    try:
                        endpoint["tabs"] = len(cdp.page_rows_at(port))
                    except ControlError as e:
                        # a browser that died, or stalled, between the probe
                        # and this second GET: a census row is a REPORT, not a
                        # gate — one vanished browser must not take `list` and
                        # every shared resolver down with it (see `tabs_of`,
                        # which reports the same way). None is "no count",
                        # never "no tabs"
                        endpoint["tabs"] = None
                        endpoint["reason"] = e.message
                else:
                    endpoint["reason"] = str(owner["reason"])
            rows.append({"pid": pid, "exe": exe,
                         "path": exe_path(pid),
                         "profile": profile,
                         "profile_from": ("flag" if flag else
                                          ("default" if profile else "")),
                         "managed": is_managed(profile),
                         "attached": norm(profile) in attached,
                         "headless": is_headless_cmd(cmd, exe),
                         "cdp": endpoint})
    # ours first, then the attached ones, then whatever can be driven, by pid
    return sorted(rows, key=lambda r: (not r["managed"], not r["attached"],
                                       not endpoint_of(r)["verified"], r["pid"]))

def list_browsers() -> dict:
    """`list`: every browser running here, drivable or not.

    ONE census, and one /proc walk with it: the window is opened here so every
    `browsers()` behind this reply reads the same process list (a nested
    window reuses the outer one).
    """
    with census_processes():
        rows = _pkg.browsers()
    return {"ok": True, "count": len(rows),
            "drivable": sum(1 for r in rows if endpoint_of(r)["verified"]),
            "browsers": rows}

def is_attached(profile: str) -> bool:
    """Is this profile attached for tab writes?"""
    return attachments_lib.STORE.is_attached(profile)

def _narrow(rows: list[dict], browser: str) -> list[dict]:
    """The rows a name and/or the SCOPED profile select.

    `--browser NAME` matches the executable or the profile's basename (the name
    `open --browser NAME` keys a default profile by); `--profile DIR` (the
    process scope) matches the profile PATH and nothing else, so it can pick
    between two instances of the same browser.
    """
    wanted = os.path.basename(str(browser).strip())
    scoped = _pkg._scoped()
    out: list[dict] = []
    for row in rows:
        if wanted and wanted not in (row["exe"],
                                     os.path.basename(str(row["profile"]))):
            continue
        if scoped and norm(str(row["profile"])) != scoped:
            continue
        out.append(row)
    return out

def endpoint_of(row: dict) -> dict:
    """The endpoint block of a browser row — the ONE place that names it.

    The row stays a dict (it IS the wire format); reading it through one
    helper is what stops thirty call sites from indexing the nested
    endpoint key and spelling the shape differently.
    """
    return row.get("cdp") or {}


def may_write(row: dict) -> bool:
    """May this CLI WRITE into that browser? Managed, or attached to it.

    The rule the README promises, named once: the fallback to "every drivable
    browser" that used to be spelled inline here sent `tab press` and
    `tab about:blank` into a stranger's session.
    """
    return bool(row.get("managed") or row.get("attached"))


@dataclass(frozen=True)
class Endpoint:
    """A browser's CDP endpoint, as one row reports it."""

    port: int = 0
    reachable: bool = False
    verified: bool = False
    listener: dict | None = None
    tabs: int | None = None
    reason: str = ""
    port_from: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Endpoint:
        return cls(port=as_int(data.get("port")),
                   reachable=bool(data.get("reachable")),
                   verified=bool(data.get("verified")),
                   listener=(dict(data["listener"])
                             if isinstance(data.get("listener"), dict)
                             else None),
                   tabs=(as_int(data["tabs"])
                         if data.get("tabs") is not None else None),
                   reason=str(data.get("reason") or ""),
                   port_from=str(data.get("port_from") or ""))


@dataclass(frozen=True)
class BrowserRow:
    """One browser on this machine: identity, profile, mode, its endpoint."""

    pid: int = 0
    exe: str = ""
    path: str = ""
    profile: str = ""
    profile_from: str = ""
    managed: bool = False
    attached: bool = False
    headless: bool = False
    endpoint: Endpoint = Endpoint()

    @classmethod
    def from_dict(cls, row: dict) -> BrowserRow:
        return cls(pid=as_int(row.get("pid")), exe=str(row.get("exe") or ""),
                   path=str(row.get("path") or ""),
                   profile=str(row.get("profile") or ""),
                   profile_from=str(row.get("profile_from") or ""),
                   managed=bool(row.get("managed")),
                   attached=bool(row.get("attached")),
                   headless=bool(row.get("headless")),
                   endpoint=Endpoint.from_dict(endpoint_of(row)))

    def as_brief(self) -> dict:
        """The row small enough to ride along in a tab reply."""
        return {"pid": self.pid, "exe": self.exe, "profile": self.profile,
                "managed": self.managed, "attached": self.attached,
                "headless": self.headless, "port": self.endpoint.port}

    def as_row(self) -> dict:
        """The row without its endpoint block (reported apart)."""
        return {"pid": self.pid, "exe": self.exe, "path": self.path,
                "profile": self.profile, "profile_from": self.profile_from,
                "managed": self.managed, "attached": self.attached,
                "headless": self.headless}


def _who(rows: list[dict]) -> str:
    """The browsers in a message, named so a caller can act on them."""
    return "; ".join(f"{r['exe']} on {r['profile']}" for r in rows)

def _writable(browser: str = "", rows: list[dict] | None = None) -> list[dict]:
    """The browsers a WRITE may target: the ones this CLI manages, plus the
    attached ones.

    `_drivable` decides what "drivable" means (the endpoint must be the browser
    it claims to be), and this narrows that to the browsers a write may touch.
    A browser nobody handed over is NOT one of them, however drivable it is:
    the README promises writes go to a managed or attached browser, and the
    fallback to "every drivable browser" that used to be here sent `tab press`
    and `tab about:blank` into a stranger's session — measured against a
    throwaway Chrome whose only sin was publishing a debugging port, which
    then gained a tab it never asked for. `_one_tab` and `_writable_profile`
    name the stranger and refuse instead of picking it.

    `rows` is a census the CALLER already made, so one verb censuses once.
    """
    return [r for r in _drivable(browser, strict=False, rows=rows)
            if may_write(r)]

def _readable(browser: str = "", rows: list[dict] | None = None) -> list[dict]:
    """The browsers a READ may target: ours and attached first, else every
    drivable one.

    Reading a stranger's tab list is not the same act as typing into it, so a
    read reaches what a write may not — but "ours first" is what keeps an
    unqualified read from refusing `tab-ambiguous` when the user's own browser
    happens to run beside ours. `rows` is a census the caller already made.
    """
    return _writable(browser, rows) or _drivable(browser, rows=rows)

def _drivable(browser: str = "", strict: bool = True,
              rows: list[dict] | None = None) -> list[dict]:
    """The running browsers that answer CDP AND are the browsers they claim.

    `browser` narrows to the one named — its executable, or the basename of
    its profile, which is the name `open --browser NAME` keys a profile by.
    One name can match two browsers (the same browser on two profiles, as a
    tool that manages its own copy makes likely): the MANAGED one wins, since
    that is the browser this CLI would drive and the only one a write may
    touch. An empty result is []: whether that is a refusal is the caller's
    question.

    `strict` makes an endpoint that answered but did NOT verify a refusal
    (`cdp-not-local`) rather than a silent omission — `list` wants the row, a
    drive wants the refusal. Nothing is ever driven from an unverified row.

    `rows` is a census the caller ALREADY made — `tab list` answers both of its
    halves from one (`census_processes` keeps the /proc walk single); without
    one this censuses for itself.
    """
    answering = [r for r in (rows if rows is not None else _pkg.browsers())
                 if endpoint_of(r).get("reachable")]
    verified = [r for r in answering if endpoint_of(r).get("verified")]
    matches = _narrow(verified, browser)
    if browser and matches:
        rows = [r for r in matches if may_write(r)] or matches
    else:
        rows = matches
    if strict and not rows:
        _pkg._drive_refusal(browser, answering)
    return sorted(rows, key=lambda r: (r["exe"], str(r["profile"])))

def _tabs_or_fail(row: dict) -> list[dict]:
    """The tabs of one browser, or a REFUSAL when it did not answer.

    `_tabs_of` returns (tabs, error) and seven callers took `[0]`, dropping the
    error: a list that could not be read then became a claim of ABSENCE — "no
    tab matches … have: none" for a browser that simply did not answer, which is
    exactly what the error value exists to prevent (a review flagged it).
    """
    tabs, error = _pkg._tabs_of(row)
    if error:
        fail(ERR_CDP_ERROR, f"{row['exe']} on {row['profile']}: {error}")
    return tabs

def tabs_of(row: dict) -> tuple[list[dict], str]:
    """(page tabs, error) for one browser row.

    A browser that stopped answering between the probe and the read is
    REPORTED: an empty list is "no tabs", an error is "no answer". A row whose
    endpoint did not VERIFY is reported the same way — the tab list of a
    stranger is not this browser's tab list, and no count is claimed.
    """
    if endpoint_of(row).get("reachable") and not endpoint_of(row).get("verified"):
        return [], str(endpoint_of(row).get("reason") or
                       "the endpoint is not this profile's browser")
    try:
        port = as_int(endpoint_of(row)["port"])
        return cdp.rows_to_tabs(cdp.page_rows_at(port)), ""
    except ControlError as e:
        return [], e.message

def brief(row: dict) -> dict:
    """One browser row, small enough to ride along in a tab reply."""
    return BrowserRow.from_dict(row).as_brief()

def _row(row: dict) -> dict:
    """One browser row without its endpoint block (reported apart)."""
    return BrowserRow.from_dict(row).as_row()

def _endpoint_details(row: dict) -> dict:
    """One browser row's endpoint, with the version it reports itself."""
    details = dict(endpoint_of(row))
    if details.get("reachable"):
        version = cdp.version_at(as_int(details.get("port")))
        details["version"] = str(version.get("Browser") or "")
        details["protocol"] = str(version.get("Protocol-Version") or "")
        details["user_agent"] = str(version.get("User-Agent") or "")
    return details

def list_tabs(browser: str = "") -> dict:
    """`tab list`: the page tabs of every DRIVABLE browser, by browser.

    Sorted by browser, which is the order `list` uses and the reason a tab
    handle only means something next to the browser it came from: two tabs
    with the same title in two browsers are two rows, not one. `browser`
    narrows the answer to one of them.

    A browser whose endpoint did not VERIFY is not listed under `browsers`
    (nothing of it is driven or counted) — it appears under `unverified`, with
    the pid and exe that actually hold the port, because a row that silently
    vanished would be a claim of absence. The whole reply is ONE census
    (the window opened here), so the `unverified` half does not walk /proc a
    second time either.
    """
    with census_processes():
        rows = _pkg.browsers()
        groups: list[dict] = []
        total = 0
        for row in _drivable(browser, strict=False, rows=rows):
            tabs, error = _pkg._tabs_of(row)
            group = {**_pkg._brief(row), "tabs": tabs, "count": len(tabs)}
            if error:
                group["error"] = error
            total += len(tabs)
            groups.append(group)
        suspects = _narrow([r for r in rows
                            if endpoint_of(r).get("reachable")
                            and not endpoint_of(r).get("verified")], browser)
    reply: dict = {"ok": True, "count": total, "browsers": groups}
    if suspects:
        reply["unverified"] = [
            {**_pkg._brief(r), "listener": endpoint_of(r).get("listener"),
             "reason": endpoint_of(r).get("reason")}
            for r in suspects]
    return reply

def browser_info(browser: str = "") -> dict:
    """`info`: the browser this CLI would drive — or the one named — and its
    endpoint.

    "Would drive" includes an ATTACHED browser: that is the one a `tab` write
    goes to. `running: false` is an ANSWER, not a refusal — which browser
    `open` would start, and where its profile lives, is knowable without one
    running. `--browser NAME` and `--profile DIR` both narrow it, so a scoped
    call reports the instance it is about.
    """
    rows = _narrow(_pkg.browsers(), browser)
    # ours first, then an attached one: those are the browsers a tab write
    # could reach, and the one `info` is really about
    live = [r for r in rows if may_write(r)]
    if len(live) > 1:
        names = ", ".join(os.path.basename(str(r["profile"]))
                          + (" (attached)" if r["attached"] else "")
                          for r in live)
        fail(ERR_AMBIGUOUS_BROWSER,
             f"{len(live)} writable browsers are up ({names}) — name one with "
             "--profile DIR (the instance) or --browser NAME")
    if live:
        return {"ok": True, "running": True, "browser": _row(live[0]),
                "cdp": _endpoint_details(live[0])}
    if rows and (browser or _pkg._scoped()):
        # a NAMED browser this CLI cannot (or may not) drive: report it rather
        # than pretend. With no name and no scope, an unmanaged and unreachable
        # browser is not an answer at all — say `running: false` instead.
        row = rows[0]
        return {"ok": True, "running": bool(endpoint_of(row)["reachable"]),
                "browser": _row(row), "cdp": _endpoint_details(row)}
    path = _pkg.binary(browser) if browser else _pkg.binary()
    profile = _pkg.instance_dir(path)
    return {"ok": True, "running": False,
            "browser": {"pid": 0, "exe": os.path.basename(path),
                        "path": path, "profile": profile,
                        "profile_from": "managed", "managed": True,
                        "attached": False, "headless": False},
            "cdp": {"port": 0, "reachable": False}}

def _foreign_row(row: dict, tab: dict) -> dict:
    """One match this CLI may not close, small enough to report whole."""
    return {"id": tab["id"], "title": tab["title"], "url": tab["url"],
            "pid": row["pid"], "exe": row["exe"],
            "profile": row["profile"]}

def _closeable(browser: str) -> list[dict]:
    """The drivable browser rows, or a refusal naming what to do about it."""
    rows = _drivable(browser)
    if not rows:
        _pkg._no_drive(browser)
    return rows

def _split(rows: list[dict]) -> tuple[list[tuple[dict, dict, int]],
                                      list[dict]]:
    """(tabs this CLI may close, matches in browsers it may not).

    A match this CLI does not drive is REPORTED, never closed: a bulk close
    over the whole machine that hits the user's own browsing must say so
    rather than act on it, fail, or pretend it did not see it.
    """
    ours: list[tuple[dict, dict, int]] = []
    foreign: list[dict] = []
    for row in rows:
        for index, tab in enumerate(_tabs_or_fail(row)):
            if may_write(row):
                ours.append((row, tab, index))
            else:
                foreign.append(_foreign_row(row, tab))
    return ours, foreign
