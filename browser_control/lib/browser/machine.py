"""machine — the census: every browser here, and the rows verbs report.

`browsers()` walks /proc once; the projections (`brief`, `_row`,
`_endpoint_details`) are what `list`, `info` and `tab list` return.
"""
from __future__ import annotations

import os

from browser_control.lib import (
    attachments as attachments_lib,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib import browser as _pkg  # pyright: ignore[reportMissingImports]
from browser_control.lib import (
    cdp,  # pyright: ignore[reportMissingImports]
    )
from browser_control.lib.coerce import (  # pyright: ignore[reportMissingImports]
    as_int,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_AMBIGUOUS_BROWSER,
    ERR_CDP_ERROR,
    ControlError,
    fail,
)
from browser_control.lib.paths import (  # pyright: ignore[reportMissingImports]
    is_managed,
    norm,
)
from browser_control.lib.proc import (  # pyright: ignore[reportMissingImports]
    cmdline_value,
    exe_path,
    main_processes,
)


def browsers() -> list[dict]:
    """Every Chromium-family browser RUNNING here, ours or the user's.

    One /proc pass over the main processes: each with the profile it runs on
    (`--user-data-dir`, else the default data directory for that executable)
    and whether a DevTools endpoint answers for it — which is what makes it
    drivable. `managed` says the profile is one this CLI owns and `attached`
    that it was attached for tab writes, so the answer is about the whole
    machine rather than our own corner of it.
    """
    rows: list[dict] = []
    attached = attachments_lib.STORE.records()
    for pid, exe, cmd in main_processes():
        flag = cmdline_value(cmd, "--user-data-dir")
        profile = flag or _pkg._default_profile(exe)
        port = cdp.port_of(profile) if profile else 0
        if not port:
            port = as_int(cmdline_value(cmd, "--remote-debugging-port"))
        reachable = cdp.answers(port)
        endpoint: dict = {"port": port, "reachable": reachable,
                          "verified": False}
        if reachable:
            # WHO holds the port: the row reports the pid and exe the kernel
            # says own the listening socket, and `verified` says whether that
            # process is this browser. Nothing here DRIVES an unverified row.
            owner = _pkg.endpoint_owner(profile, port)
            endpoint["verified"] = bool(owner["verified"])
            endpoint["listener"] = {"pid": owner["pid"],
                                    "exe": owner["exe"]}
            if owner["verified"]:
                endpoint["tabs"] = len(cdp.page_rows_at(port))
            else:
                endpoint["reason"] = str(owner["reason"])
        rows.append({"pid": pid, "exe": exe,
                     "path": exe_path(pid),
                     "profile": profile,
                     "profile_from": ("flag" if flag else
                                      ("default" if profile else "")),
                     "managed": is_managed(profile),
                     "attached": norm(profile) in attached,
                     "cdp": endpoint})
    # ours first, then the attached ones, then whatever can be driven, by pid
    return sorted(rows, key=lambda r: (not r["managed"], not r["attached"],
                                       not r["cdp"]["verified"], r["pid"]))

def list_browsers() -> dict:
    """`list`: every browser running here, drivable or not."""
    rows = _pkg.browsers()
    return {"ok": True, "count": len(rows),
            "drivable": sum(1 for r in rows if r["cdp"]["verified"]),
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

def _who(rows: list[dict]) -> str:
    """The browsers in a message, named so a caller can act on them."""
    return "; ".join(f"{r['exe']} on {r['profile']}" for r in rows)

def _writable(browser: str = "") -> list[dict]:
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
    """
    return [r for r in _drivable(browser, strict=False)
            if r["managed"] or r["attached"]]

def _readable(browser: str = "") -> list[dict]:
    """The browsers a READ may target: ours and attached first, else every
    drivable one.

    Reading a stranger's tab list is not the same act as typing into it, so a
    read reaches what a write may not — but "ours first" is what keeps an
    unqualified read from refusing `tab-ambiguous` when the user's own browser
    happens to run beside ours.
    """
    return _writable(browser) or _drivable(browser)

def _drivable(browser: str = "", strict: bool = True) -> list[dict]:
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
    """
    answering = [r for r in _pkg.browsers() if r["cdp"].get("reachable")]
    verified = [r for r in answering if r["cdp"].get("verified")]
    matches = _narrow(verified, browser)
    if browser and matches:
        rows = [r for r in matches if r["managed"] or r["attached"]] or matches
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
    if row["cdp"].get("reachable") and not row["cdp"].get("verified"):
        return [], str(row["cdp"].get("reason") or
                       "the endpoint is not this profile's browser")
    try:
        port = as_int(row["cdp"]["port"])
        return cdp.rows_to_tabs(cdp.page_rows_at(port)), ""
    except ControlError as e:
        return [], e.message

def brief(row: dict) -> dict:
    """One browser row, small enough to ride along in a tab reply."""
    return {"pid": row["pid"], "exe": row["exe"], "profile": row["profile"],
            "managed": row["managed"], "attached": row["attached"],
            "port": as_int(row["cdp"]["port"])}

def _row(row: dict) -> dict:
    """One browser row without its endpoint block (reported apart)."""
    return {key: row[key] for key in ("pid", "exe", "path", "profile",
                                      "profile_from", "managed",
                                      "attached")}

def _endpoint_details(row: dict) -> dict:
    """One browser row's endpoint, with the version it reports itself."""
    details = dict(row["cdp"])
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
    vanished would be a claim of absence.
    """
    groups: list[dict] = []
    total = 0
    for row in _drivable(browser, strict=False):
        tabs, error = _pkg._tabs_of(row)
        group = {**_pkg._brief(row), "tabs": tabs, "count": len(tabs)}
        if error:
            group["error"] = error
        total += len(tabs)
        groups.append(group)
    reply: dict = {"ok": True, "count": total, "browsers": groups}
    suspects = _narrow([r for r in _pkg.browsers()
                        if r["cdp"].get("reachable")
                        and not r["cdp"].get("verified")], browser)
    if suspects:
        reply["unverified"] = [
            {**_pkg._brief(r), "listener": r["cdp"].get("listener"),
             "reason": r["cdp"].get("reason")}
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
    live = [r for r in rows if r["managed"] or r["attached"]]
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
        return {"ok": True, "running": bool(row["cdp"]["reachable"]),
                "browser": _row(row), "cdp": _endpoint_details(row)}
    path = _pkg.binary(browser) if browser else _pkg.binary()
    profile = _pkg.instance_dir(path)
    return {"ok": True, "running": False,
            "browser": {"pid": 0, "exe": os.path.basename(path),
                        "path": path, "profile": profile,
                        "profile_from": "managed", "managed": True,
                        "attached": False},
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
            if row["managed"] or row["attached"]:
                ours.append((row, tab, index))
            else:
                foreign.append(_foreign_row(row, tab))
    return ours, foreign
