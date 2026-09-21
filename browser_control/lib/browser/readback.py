"""readback — the bounded waiters shared by lifecycle and the verbs.

Every one of these reads its own effect back before a verb reports: rows
after a start, a URL after a navigation, ids after a close.
"""
from __future__ import annotations

from typing import Any

from browser_control.lib import browser as _pkg  # pyright: ignore[reportMissingImports]
from browser_control.lib import (
    cdp,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib.browser.constants import (  # pyright: ignore[reportMissingImports]
    LAUNCH_WAIT_S,
    PORT_WAIT_S,
    TAB_WAIT_S,
)
from browser_control.lib.browser.owners import (  # pyright: ignore[reportMissingImports]
    GUARD,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_CLOSE_TAB_NOT_VERIFIED,
    ControlError,
    fail,
)
from browser_control.lib.poll import (  # pyright: ignore[reportMissingImports]
    POLL_NORMAL,
    POLL_SLOW,
    poll,
)


def rows(profile: str) -> list[dict]:
    """The page tabs, as every verb reports them."""
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
    """
    def probe() -> bool:
        port = cdp.port_of(profile)
        if not port:
            return False
        GUARD.forget(profile, port)
        return bool(_pkg.endpoint_owner(profile, port).get("verified"))

    return bool(poll(probe, timeout=timeout, interval=POLL_SLOW)[1])

def _wait_rows(profile: str, timeout: float = TAB_WAIT_S) -> list[dict]:
    return poll(lambda: _pkg._rows(profile), timeout=timeout, interval=POLL_SLOW,
                accept=bool, on_error=lambda _e: [])[1]

def _wait_tabs(profile: str, ids: list[str],
               timeout: float = TAB_WAIT_S) -> tuple[dict, list[str]]:
    """(rows by id, ids still missing) after ONE bounded poll."""
    def probe() -> tuple[dict, list[str]]:
        try:
            rows = _pkg._rows(profile)
        except ControlError:
            rows = []                  # mid-startup, or the browser is gone
        seen = {r["id"]: r for r in rows if r["id"] in ids}
        return seen, [i for i in ids if i not in seen]

    _attempts, (seen, missing) = poll(probe, timeout=timeout,
                                      interval=POLL_SLOW,
                                      accept=lambda pair: not pair[1])
    return seen, missing

def _wait_url(profile: str, url: str,
              timeout: float = TAB_WAIT_S) -> dict | None:
    """The tab a just-started browser opened for `url`, once it shows it.

    Matched by the requested URL, tolerant of the trailing slash the browser
    adds: a startup page has no id we were told, so the URL is what names it.
    The match stops at a path boundary — a bare prefix let a restored tab at
    `example.com/page2` answer for `example.com/page` (a review measured the
    mis-attribution).
    """
    want = url.rstrip("/")

    def probe() -> dict | None:
        for row in _pkg._rows(profile):
            got = str(row["url"]).rstrip("/")
            if got == want or got.startswith((want + "/", want + "?")):
                return row
        return None

    return poll(probe, timeout=timeout, interval=POLL_SLOW, accept=bool)[1]

def _require_tab_list_readable(profile: str, error: ControlError) -> None:
    """Refuse to read "the list could not be read" as "the ids are gone".

    Only an unreachable endpoint with no port file at all is the browser
    having exited. A timeout, a malformed `/json` or an oversized body refuses
    instead of claiming absence — the same class of bug `_tabs_or_fail` was
    fixed for (a review flagged it).
    """
    if error.code != "cdp-unreachable" or cdp.port_of(profile):
        fail(ERR_CLOSE_TAB_NOT_VERIFIED,
             f"the tab list could not be read back after the close: "
             f"{error.message}")

def _wait_ids_gone(profile: str, ids: list[str],
                   timeout: float = PORT_WAIT_S) -> list[str]:
    """The ids STILL in that browser's tab list after a bounded wait.

    `Target.closeTarget` answers before the tab is gone, and a page with a
    beforeunload handler can keep it open: the list is the only honest proof,
    and its survivors ARE the report.
    """
    def probe() -> list[str]:
        try:
            open_ids = {r["id"] for r in _pkg._rows(profile)}
        except ControlError as e:
            _require_tab_list_readable(profile, e)
            open_ids = set()          # the browser is gone: so are its tabs
        return [i for i in ids if i in open_ids]

    return poll(probe, timeout=timeout, interval=POLL_NORMAL,
                accept=lambda left: not left)[1]


def page_ws(profile: str, target_id: str) -> str:
    """The websocket URL of ONE tab's page target — the one place that spells it."""
    port = cdp.port_of(profile)
    return cdp.target_ws(port, target_id)


def page_session(profile: str, target_id: str, *,
                  page_domain: bool = True) -> cdp.Session:
    """ONE session on that tab's page target.

    `page_domain=False` is the parked-dialog case: a dialog already up cannot be
    announced, and enabling the domain is what blocks on a parked tab.
    """
    return cdp.Session(page_ws(profile, target_id), page_domain=page_domain)


def page_eval(profile: str, target_id: str, expression: str,
              timeout: float = 15.0) -> Any:
    """One expression evaluated on that tab's page target."""
    return cdp.evaluate(page_ws(profile, target_id), expression, timeout=timeout)
