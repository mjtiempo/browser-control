"""readback — the bounded waiters shared by lifecycle and the verbs.

Every one of these reads its own effect back before a verb reports: rows
after a start, a URL after a navigation, ids after a close.

The endpoint is the CALLER's: each helper takes the `port` the verb already
resolved (a row's own endpoint) and falls back to the one resolver a profile
without a row has (`driver_port`) — never to the port FILE alone, which a
browser started with an explicit `--remote-debugging-port=N` never writes.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import (
    urlsplit,
)

from browser_control.lib import browser as _pkg
from browser_control.lib import (
    cdp,
)
from browser_control.lib.browser.constants import (
    LAUNCH_WAIT_S,
    PORT_WAIT_S,
    TAB_WAIT_S,
)
from browser_control.lib.browser.machine import (
    driver_port,
)
from browser_control.lib.browser.owners import (
    GUARD,
    verify_ws_owner,
)
from browser_control.lib.coerce import (
    as_int,
)
from browser_control.lib.errors import (
    ERR_CDP_ERROR,
    ERR_CLOSE_TAB_NOT_VERIFIED,
    ControlError,
    fail,
)
from browser_control.lib.poll import (
    POLL_NORMAL,
    POLL_SLOW,
    poll,
)


def rows(profile: str, port: int = 0) -> list[dict]:
    """The page tabs, as every verb reports them.

    The port is the one the caller resolved (a row's own endpoint), or
    `driver_port`'s answer for a caller that has no row: the file when it
    ANSWERS, else the port the process itself names. The file is never the
    only oracle — a browser started with an explicit
    `--remote-debugging-port=N` writes none, and reading it would answer 0 for
    a browser that is right there. With no port known at all,
    `cdp.page_rows(profile)` answers, which is the refusal that names the
    missing DevTools port.
    """
    endpoint = as_int(port) or driver_port(profile)
    if endpoint:
        return cdp.rows_to_tabs(cdp.page_rows_at(endpoint))
    return cdp.rows_to_tabs(cdp.page_rows(profile))

def _wait_own_port(profile: str, timeout: float = LAUNCH_WAIT_S) -> bool:
    """The endpoint THIS call started must ANSWER and VERIFY.

    "Something answers" is not enough, and the port file it reads can be the
    stale one the caller just decided to ignore: `open` then reported
    `started: true` with a stranger's port and the stranger's tabs (a review
    flagged it). The owner is re-judged on the CURRENT port file, with the
    memo evicted through `GUARD.forget`, until it verifies — the browser this
    call spawned names the profile in its own command line, so its endpoint is
    the one that can pass.

    The FILE is the right oracle HERE and only here: this waits for a browser
    launched with `--remote-debugging-port=0`, which is the one case Chrome
    writes `DevToolsActivePort` for.
    """
    def probe() -> bool:
        port = cdp.port_of(profile)
        if not port:
            return False
        GUARD.forget(profile, port)
        return bool(_pkg.endpoint_owner(profile, port).get("verified"))

    return bool(poll(probe, timeout=timeout, interval=POLL_SLOW)[1])

def _wait_rows(profile: str, timeout: float = TAB_WAIT_S,
               port: int = 0) -> list[dict]:
    return poll(lambda: _pkg._rows(profile, port), timeout=timeout,
                interval=POLL_SLOW, accept=bool, on_error=lambda _e: [])[1]

def _wait_tabs(profile: str, ids: list[str], timeout: float = TAB_WAIT_S,
               port: int = 0) -> tuple[dict, list[str]]:
    """(rows by id, ids still missing) after ONE bounded poll."""
    def probe() -> tuple[dict, list[str]]:
        try:
            rows = _pkg._rows(profile, port)
        except ControlError:
            rows = []                  # mid-startup, or the browser is gone
        seen = {r["id"]: r for r in rows if r["id"] in ids}
        return seen, [i for i in ids if i not in seen]

    _attempts, (seen, missing) = poll(probe, timeout=timeout,
                                      interval=POLL_SLOW,
                                      accept=lambda pair: not pair[1])
    return seen, missing

def url_landed(want: str, got: str) -> bool:
    """Did the browser LAND on the address that was asked for?

    The browser normalises what it is given (`https://a.com` becomes
    `https://a.com/`), so a trailing slash alone is not a different page while
    a different path, query or host is. The match stops at a path boundary — a
    bare prefix let a restored tab at `example.com/page2` answer for
    `example.com/page` (a review measured the mis-attribution).

    This is tier 1 of `_wait_url`'s ladder and the test `open` reports as
    `matched`.
    """
    left = str(want or "").rstrip("/")
    right = str(got or "").rstrip("/")
    return bool(left) and (right == left
                           or right.startswith((left + "/", left + "?")))

def _host_of(url: str) -> str:
    """The host a URL names, canonicalised for a same-site comparison.

    Case-folded and without a leading `www.`: both are the browser's own
    canonicalisation of an address nobody else changed.
    """
    try:
        host = urlsplit(str(url or "")).hostname or ""
    except ValueError:                 # a malformed URL is not a host
        return ""
    return host.lower().removeprefix("www.")

def _same_site(want: str, got: str) -> bool:
    """Tier 2: did the REQUESTED HOST answer?

    A startup navigation that ends on another address of the same host is the
    browser's own doing, not a failure: an `http://` request HSTS-upgrades to
    `https://`, a host is lower-cased or gains a `www.`, a default port
    appears, a path is canonicalised. Matching only the requested URL (tier 1)
    left every one of those reported as "the startup page never appeared" —
    and `open` then SIGTERMed the healthy browser it had just started (a
    review proved the matcher on all three spellings).
    """
    left, right = _host_of(want), _host_of(got)
    return bool(left) and left == right

def _fallback_row(rows_: list[dict], url: str) -> dict | None:
    """Tier 3: the page row to report when the requested address is nowhere.

    The first non-blank row, in the order the browser listed it, preferring a
    same-host row and then any real page over an `about:`/`chrome://`
    placeholder — a browser that is up with a page open HAS a tab, and the
    caller is told which address it is on rather than that nothing appeared.
    `None` only when there is no page row at all, which is the one case that
    is genuinely "no page tab".
    """
    usable = [r for r in rows_ if str(r.get("url") or "").strip()]
    if not usable:
        return rows_[0] if rows_ else None
    for row in usable:
        if _same_site(url, str(row.get("url") or "")):
            return row
    for row in usable:
        got = str(row.get("url") or "")
        if not got.startswith(("about:", "chrome://")):
            return row
    return usable[0]

def _wait_url(profile: str, url: str,
              timeout: float = TAB_WAIT_S) -> dict | None:
    """The tab a just-started browser opened for `url`, by an explicit LADDER.

    1. the requested URL itself, tolerant of the trailing slash the browser
       adds and stopping at a path boundary (`url_landed`);
    2. failing that, the address the navigation ACTUALLY landed on when it is
       the requested HOST answering (`_same_site`): a redirect, an HSTS
       http→https upgrade, a canonicalised path or a `www.` the browser added;
    3. failing both, and ONLY when the tab list is not empty, the first
       non-blank page row (`_fallback_row`) — a real page in preference to an
       `about:`/`chrome://` placeholder.

    Tiers 1 and 2 are polled for the whole budget, so a page that appears late
    is matched by what it IS rather than answered by a placeholder seen first;
    the caller reports which tier answered (`url_landed`) and names both
    addresses when it was not the requested one.

    The row comes back with the TIER that produced it under `tier` (a private
    key, never part of a tab row's wire shape): a tier-1 or tier-2 row is a
    page the requested address, or at least the requested HOST, answered for,
    while a tier-3 row is by definition one the call did NOT match — in a
    restored session, somebody else's tab. `lifecycle._opened_entry` is the
    reader: it reports a tier-3 row as an OBSERVATION instead of handing the
    caller a handle to a page this call never opened (a review measured `open`
    answering `tab: id:…` about a restored tab while the requested page was
    never opened at all).

    `None` means the tab list never held a page ROW — the only honest "the
    startup page never appeared". A navigation that merely ended somewhere
    else is a fact to report, never a reason to stop the healthy browser that
    produced it: the exact-match form this replaced made `open` SIGTERM the
    browser it had just started over a URL the browser had canonicalised.
    """
    best: dict | None = None
    tier = 0

    def probe() -> dict | None:
        nonlocal best, tier
        found = _pkg._rows(profile)
        for row in found:
            if url_landed(url, str(row.get("url") or "")):
                best, tier = row, 1
                return row
        same_site = next((row for row in found
                          if _same_site(url, str(row.get("url") or ""))), None)
        if same_site is not None:
            best, tier = same_site, 2
        else:
            # no row of the requested HOST: `_fallback_row` prefers one but
            # cannot find what is not there, so whatever it names is tier 3
            fallback = _fallback_row(found, url)
            if fallback:
                best, tier = fallback, 3
        return None

    row = poll(probe, timeout=timeout, interval=POLL_SLOW, accept=bool)[1] \
        or best
    if row is None:
        return None
    return {**row, "tier": tier}

def _require_tab_list_readable(profile: str, error: ControlError,
                               port: int = 0) -> None:
    """Refuse to read "the list could not be read" as "the ids are gone".

    Only an unreachable endpoint whose port has NO holder is the browser
    having exited. The port FILE is not proof of life: measured on this
    host's Chrome, `DevToolsActivePort` survives a graceful exit AND a
    SIGKILL, so gating this on the file's existence made the "browser
    exited" branch unreachable — `tab close` on the ONLY tab refused
    `close-tab-not-verified` after its own success (a review found it). The
    kernel's listener table is the honest answer. A timeout, a malformed
    `/json`, an oversized body, or a port still held by something else
    refuses instead of claiming absence.

    `port` is the endpoint the caller resolved; without one the census
    resolves it (`driver_port`), because the file alone answers 0 for a
    browser that was started with an explicit `--remote-debugging-port`.
    """
    endpoint = as_int(port) or driver_port(profile)
    if error.code != "cdp-unreachable" \
            or (endpoint and cdp.listener_of(endpoint)):
        fail(ERR_CLOSE_TAB_NOT_VERIFIED,
             f"the tab list could not be read back after the close: "
             f"{error.message}")

def _wait_ids_gone(profile: str, ids: list[str], timeout: float = PORT_WAIT_S,
                   port: int = 0) -> list[str]:
    """The ids STILL in that browser's tab list after a bounded wait.

    `Target.closeTarget` answers before the tab is gone, and a page with a
    beforeunload handler can keep it open: the list is the only honest proof,
    and its survivors ARE the report.

    The read goes through `_rows`, whose own resolver is the census-aware one
    (`driver_port`), so this works for a browser that wrote no port file as
    well as for one this CLI started; `port` — when the caller has it — is the
    endpoint the readability check judges, which is the port the read used.
    """
    endpoint = as_int(port) or driver_port(profile)

    def probe() -> list[str]:
        try:
            open_ids = {r["id"] for r in _pkg._rows(profile)}
        except ControlError as e:
            _require_tab_list_readable(profile, e, endpoint)
            open_ids = set()          # the browser is gone: so are its tabs
        return [i for i in ids if i in open_ids]

    return poll(probe, timeout=timeout, interval=POLL_NORMAL,
                accept=lambda left: not left)[1]


def browser_ws_at(port: int) -> str:
    """The BROWSER endpoint's websocket on a port the caller resolved.

    `cdp.browser_ws` resolves the port from the profile's FILE, which a
    browser started with an explicit `--remote-debugging-port=N` never writes;
    this is the same two steps (`/json/version`, then the host check) on an
    explicit port, so a verb that holds a row drives the endpoint THAT row
    reports.
    """
    info = cdp._get_port(port, "/json/version")
    if not isinstance(info, dict):
        fail(ERR_CDP_ERROR,
             "/json/version answered a shape this tool cannot read")
    ws = str(info.get("webSocketDebuggerUrl") or "")
    if not ws:
        fail(ERR_CDP_ERROR, "/json/version names no browser websocket endpoint")
    return cdp._checked_ws(ws)


def browser_call_at(profile: str, port: int, method: str,
                    params: dict) -> dict:
    """One CDP call on the BROWSER endpoint of a port the caller resolved.

    The port's holder is judged IMMEDIATELY before the connection
    (`verify_ws_owner`): a `Target.*` call is a write, the verdict that
    authorised this verb judged a listener a moment earlier, and the port can
    be rebound in between.
    """
    verify_ws_owner(profile, port)
    return cdp.call(browser_ws_at(port), method, params)


def page_ws(profile: str, target_id: str, port: int = 0) -> str:
    """The websocket URL of ONE tab's page target — the one place that spells it.

    `port` is the endpoint the caller resolved (a verified row's own port);
    without one the census resolves it (`driver_port`), never the port file
    alone. The holder of that port is re-judged RIGHT HERE
    (`verify_ws_owner`), because this is where page traffic is aimed: a
    verdict from a moment ago is not proof that the listener is still this
    profile's browser, and keystrokes and caller JS must not go to a process
    that took the port over.
    """
    endpoint = as_int(port) or driver_port(profile)
    verify_ws_owner(profile, endpoint)
    return cdp.target_ws(endpoint, target_id)


def page_session(profile: str, target_id: str, *,
                  page_domain: bool = True, port: int = 0) -> cdp.Session:
    """ONE session on that tab's page target.

    `page_domain=False` is the parked-dialog case: a dialog already up cannot be
    announced, and enabling the domain is what blocks on a parked tab. `port`
    is the endpoint the caller resolved — see `page_ws`.
    """
    return cdp.Session(page_ws(profile, target_id, port),
                       page_domain=page_domain)


def page_eval(profile: str, target_id: str, expression: str,
              timeout: float = 15.0, port: int = 0) -> Any:
    """One expression evaluated on that tab's page target."""
    return cdp.evaluate(page_ws(profile, target_id, port), expression,
                        timeout=timeout)
