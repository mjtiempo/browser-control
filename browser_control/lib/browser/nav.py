"""nav — the page verbs: nav, history, activate, reload.

Each one acts through CDP and then READS THE EFFECT BACK: the address as
observed, `performance.timeOrigin` for a new document, the page's own
`visibilityState` for activation.
"""
from __future__ import annotations

import contextlib
import time
from typing import Any

from browser_control.lib import browser as _pkg  # pyright: ignore[reportMissingImports]
from browser_control.lib import (
    cdp,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib.browser.constants import (  # pyright: ignore[reportMissingImports]
    ACTIVATE_TIMEOUT_S,
    HISTORY_TIMEOUT_S,
    NAV_MOVE_S,
    NAV_TIMEOUT_S,
    READY_EXPR,
    RELOAD_TIMEOUT_S,
)
from browser_control.lib.browser.readback import (  # pyright: ignore[reportMissingImports]
    page_eval,
    page_session,
    page_ws,
)
from browser_control.lib.coerce import (  # pyright: ignore[reportMissingImports]
    as_int,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_ACTIVATE_NOT_VERIFIED,
    ERR_BAD_ARGS,
    ERR_CDP_UNREACHABLE,
    ERR_NAV_FAILED,
    ERR_NAV_NOT_VERIFIED,
    ERR_RELOAD_NOT_VERIFIED,
    ControlError,
    fail,
)
from browser_control.lib.poll import (  # pyright: ignore[reportMissingImports]
    POLL_FAST,
    POLL_LOAD,
    POLL_SLOW,
    deadline,
    poll,
)


def _no_drive(browser: str = "", reason: str = "") -> None:
    """Refuse a drive with nothing to drive — and say WHICH thing is missing.

    "No drivable browser" is honest for an unscoped call. A scoped one asked
    about ONE instance, so the refusal names that instance and the command that
    would start it; a call narrowed by name gets the name. An endpoint that
    ANSWERED but did not verify is a different refusal (`cdp-not-local`), which
    the callers check first — this one is for nothing being there at all.
    """
    scoped = _pkg._scoped()
    asked = [part for part in ((f"on {scoped}" if scoped else ""),
                               (f"matching {browser!r}" if browser else ""))
             if part]
    where = f" {' '.join(asked)}" if asked else ""
    fix = (f"`open --profile {scoped}` starts it" if scoped
           else "run `browser-control-cli open`")
    fail(ERR_CDP_UNREACHABLE,
         f"no drivable browser{where} — {fix}"
         + (f" ({reason})" if reason else ""))

def _same_page(left: object, right: object) -> bool:
    """Do two URLs name the same page?

    The browser normalises what it was given (`https://a.com` becomes
    `https://a.com/`), so a trailing slash alone is not a different page —
    while a different query or fragment is.
    """
    return str(left or "").rstrip("/") == str(right or "").rstrip("/")

def _eval(profile: str, target_id: str, expression: str,
          timeout: float = 15.0) -> Any:
    """Evaluate one expression on that tab's own connection."""
    return page_eval(profile, target_id, expression, timeout)

def _href(profile: str, target_id: str) -> str:
    """The tab's address, or "" when it cannot be read (mid-navigation)."""
    try:
        return str(_eval(profile, target_id, "location.href", timeout=5) or "")
    except ControlError:
        return ""

def _wait_document(profile: str, target_id: str,
                   timeout: float = NAV_TIMEOUT_S) -> bool:
    """Poll until the document is complete AND parsed, or the deadline.

    Every sample is bounded by what is left of the budget: a page that stops
    answering must not spend a fresh 15s on each attempt.
    """
    end = deadline(timeout)

    def probe() -> bool:
        with contextlib.suppress(ControlError):
            return _eval(profile, target_id, READY_EXPR,
                         timeout=max(0.5, end - time.time())) == "complete+body"
        return False

    return bool(poll(probe, timeout=timeout, interval=POLL_LOAD)[1])

def _ready(profile: str, target_id: str) -> bool:
    """Is the current document complete and parsed, right now?"""
    try:
        return _eval(profile, target_id, READY_EXPR,
                     timeout=5) == "complete+body"
    except ControlError:
        return False

def _wait_move(profile: str, target_id: str, before_url: str,
               before_origin: float | None,
               timeout: float = NAV_MOVE_S) -> bool:
    """Did the tab LEAVE the document it was on?

    This is the first question, and `readyState` cannot answer it: the page
    being left is ALREADY complete, so waiting for "complete" returns before
    the navigation starts and the read-back then sees the old URL (that is how
    a redirect came back as `nav-not-verified`). A real navigation creates a
    new document (`performance.timeOrigin`), a fragment navigation changes the
    address — either one is the move.
    """
    def probe() -> bool:
        origin = _time_origin(profile, target_id)
        if origin is not None and before_origin is not None \
                and origin != before_origin:
            return True
        now = _href(profile, target_id)
        # `before_url and …`: with an unreadable before, `_same_page(now, "")`
        # is always false, so the move used to be reported as PROVEN by a
        # tautology (a review measured it). An unknown before is handled by the
        # caller, which judges the AFTER state instead.
        return bool(now and before_url and not _same_page(now, before_url))

    return bool(poll(probe, timeout=timeout, interval=POLL_SLOW)[1])

def _wait_url_change(profile: str, target_id: str, before: str,
                     timeout: float = HISTORY_TIMEOUT_S) -> bool:
    """Did the tab's address leave `before` within the deadline?

    `before` must be KNOWN (`before and …`): with an unreadable address,
    `_same_page(now, "")` is always false, so any readable address would count
    as "it changed" — the same tautology `nav`'s `moved` had (a review found it
    here after that one was fixed). Callers that cannot know the address judge
    the AFTER state instead.
    """
    def probe() -> bool:
        now = _href(profile, target_id)
        return bool(now and before and not _same_page(now, before))

    return bool(poll(probe, timeout=timeout, interval=POLL_SLOW)[1])

def _time_origin(profile: str, target_id: str) -> float | None:
    """The document's `performance.timeOrigin`, or None when unreadable.

    It changes exactly when a NEW document is created, which is the only
    honest way to tell a fast reload from a read of the old page.
    """
    try:
        return float(_eval(profile, target_id, "performance.timeOrigin",
                           timeout=5))
    except (ControlError, TypeError, ValueError):
        return None

def _wait_new_document(profile: str, target_id: str, before: float,
                       timeout: float = RELOAD_TIMEOUT_S) -> bool:
    """Is there a document whose timeOrigin differs from `before`?"""
    def probe() -> bool:
        now = _time_origin(profile, target_id)
        return now is not None and now != before

    return bool(poll(probe, timeout=timeout, interval=POLL_SLOW)[1])

def nav(url: str, tab: str = "", browser: str = "") -> dict:
    """`tab nav`: navigate ONE tab with the BROWSER's own command.

    `Page.navigate` runs no JavaScript in the page, so a tab whose renderer is
    parked (a suppressed dialog, a script that never yields) is still
    navigable — the browser replaces the document AND its renderer, which makes
    this verb the way OUT of that state (measured: the parked tab answers again
    as soon as the new document commits).

    The read-backs are unchanged: the address as OBSERVED and whether the
    document finished loading. Chromium refuses the navigation itself for a
    name that does not resolve (`nav-failed`), a tab that never left the page
    it was on refuses `nav-not-verified` (a beforeunload prompt, typically),
    and a URL the browser turns into a DOWNLOAD is named as one: no page
    navigated, so calling it a navigation would be a lie.
    """
    target = _pkg.safe_url(url)
    row, tab_row = _pkg._one_tab(tab, browser, for_write=True)
    profile, target_id = str(row["profile"]), str(tab_row["id"])
    before = _href(profile, target_id)
    before_origin = _time_origin(profile, target_id)
    with page_session(profile, target_id) as session:
        try:
            reply = session.call("Page.navigate", {"url": target})
        except ControlError as e:
            fail(ERR_NAV_FAILED,
                 f"the browser refused to navigate to {target!r}: {e.message}")
    refused = str(reply.get("errorText") or "")
    if reply.get("isDownload"):
        fail(ERR_NAV_FAILED,
             f"the browser treated {target!r} as a DOWNLOAD, so no page "
             "navigated and the tab is where it was")
    # `moved` is NULL when the address BEFORE could not be read (a parked tab is
    # exactly that case): "did it move" has no oracle then, so the read-back
    # below takes its place — and it POLLS, because the new document's renderer
    # may not answer on the first try (a review found this branch reading once
    # where every other path polls)
    moved: bool | None = None if not before else _wait_move(profile, target_id,
                                                            before,
                                                            before_origin)
    if moved is None:
        def probe() -> bool:
            url_read = _href(profile, target_id)
            return bool(url_read and _same_page(target, url_read))

        poll(probe, timeout=NAV_MOVE_S, interval=POLL_SLOW)
    loaded = _wait_document(profile, target_id) if moved else _ready(profile,
                                                                     target_id)
    url_read = _href(profile, target_id)
    if url_read.startswith("chrome-error://") or refused:
        fail(ERR_NAV_FAILED,
             f"the browser could not load {target!r}"
             f"{f' ({refused})' if refused else ''} — the tab is on its own "
             "error page (a name that does not resolve, a refused connection "
             "or a certificate problem), not the requested document")
    if moved is None and (not url_read or not _same_page(target, url_read)):
        fail(ERR_NAV_NOT_VERIFIED,
             f"this tab's address could not be read before the navigation and "
             f"reports {url_read!r} after it — the navigation did not verify "
             "(the tab may still be parked on a dialog: `tab dialog state`)")
    if moved is not None and not moved and not _same_page(target, before):
        fail(ERR_NAV_NOT_VERIFIED,
             f"the tab is still at {before!r} after navigating to {target!r} "
             "— a beforeunload prompt the browser is waiting on (`tab dialog "
             "state`), or a navigation Chromium cancelled")
    return {"ok": True, "tab": f"id:{target_id}", "url": target,
            "url_read": url_read, "loaded": loaded, "moved": moved,
            "browser": _pkg._brief(row)}

def history(direction: str, tab: str = "", browser: str = "") -> dict:
    """`tab back` / `tab forward`: move the tab's history, then read it back.

    `history.back()` with nothing to go back to is not an error the page
    reports, so the reply is the OBSERVED change: a tab still at the same
    address after the wait refuses `nav-not-verified`.

    The MOVE is the protocol's own (`Page.getNavigationHistory` +
    `Page.navigateToHistoryEntry`), not page JavaScript: a page that shadows
    `history.back` could otherwise turn a real move into a refusal, or into a
    navigation of its own choosing — and "whatever CDP can do natively, do that"
    is this project's rule (a review flagged the `history.back()` call).
    """
    if direction not in ("back", "forward"):
        fail(ERR_BAD_ARGS, f"history: {direction!r} is not back or forward")
    row, tab_row = _pkg._one_tab(tab, browser, for_write=True)
    profile, target_id = str(row["profile"]), str(tab_row["id"])
    before = _href(profile, target_id)
    tab_ws = page_ws(profile, target_id)
    listing = cdp.call(tab_ws, "Page.getNavigationHistory")
    entries = listing.get("entries") if isinstance(listing, dict) else None
    index = as_int(listing.get("currentIndex")) if isinstance(listing, dict) \
        else -1
    if not isinstance(entries, list) or not entries:
        fail(ERR_NAV_FAILED,
             f"the browser reports no history for this tab, so there is no "
             f"{direction} entry to move to")
    wanted = index - 1 if direction == "back" else index + 1
    if wanted < 0 or wanted >= len(entries):
        fail(ERR_NAV_FAILED,
             f"the tab is at history entry {index + 1} of {len(entries)} — "
             f"there is no {direction} entry to move to")
    cdp.call(tab_ws, "Page.navigateToHistoryEntry",
             {"entryId": (entries[wanted] or {}).get("id")})
    if not _wait_url_change(profile, target_id, before):
        if not before:
            # the address could not be read BEFORE, so "it changed" has no
            # oracle: what proves the move is the tab answering on a real page
            after = _href(profile, target_id)
            if not after or after.startswith("chrome-error://"):
                fail(ERR_NAV_NOT_VERIFIED,
                     f"this tab's address could not be read before {direction} "
                     f"and reports {after!r} after it — the move did not verify")
        else:
            fail(ERR_NAV_NOT_VERIFIED,
                 f"the tab is still at {before!r} after {direction} — the "
                 "browser moved to a history entry whose address did not "
                 "change (a same-document entry), or the page re-set it")
    url_read = _href(profile, target_id)
    if url_read.startswith("chrome-error://"):
        fail(ERR_NAV_FAILED,
             f"the {direction} entry did not load — the tab is on the "
             "browser's own error page")
    return {"ok": True, "direction": direction, "tab": f"id:{target_id}",
            "url_before": before, "url_read": url_read,
            "browser": _pkg._brief(row)}

def activate(tab: str = "", browser: str = "") -> dict:
    """`tab activate`: bring ONE tab to the front, verified by the page.

    `Page.bringToFront` makes the tab the active one and raises its window —
    the one thing a background tab cannot do for itself, and what a page that
    visibility-gates (or gets throttled) needs. Chromium is not asked to
    confirm anything, so the oracle is the page's own
    `document.visibilityState`: read before, read after, and a tab that still
    reports `hidden` is a refusal rather than a claim. A second window's own
    active tab is ALSO visible, which is why `--tab active` is ambiguous with
    two windows instead of picking one.
    """
    row, tab_row = _pkg._one_tab(tab, browser, for_write=True)
    profile, target_id = str(row["profile"]), str(tab_row["id"])
    with page_session(profile, target_id) as session:
        before = str(session.evaluate("document.visibilityState") or "")
        session.call("Page.bringToFront")
        def probe() -> str:
            nonlocal after
            with contextlib.suppress(ControlError):
                after = str(session.evaluate("document.visibilityState",
                                             timeout=4) or after)
            return after

        after = before
        _attempts, after = poll(probe, timeout=ACTIVATE_TIMEOUT_S,
                                interval=POLL_FAST,
                                accept=lambda value: value == "visible")
    if after != "visible":
        fail(ERR_ACTIVATE_NOT_VERIFIED,
             f"the tab still reports visibility={after or 'unreadable'!r} "
             "after `Page.bringToFront` — its window may be hidden entirely "
             "(another workspace, or iconified), and this CLI drives "
             "browsers, not the desktop")
    return {"ok": True, "activated": True, "verified": True,
            "tab": f"id:{target_id}", "visibility_before": before,
            "visibility": after, "changed": before != after,
            "url": tab_row.get("url"), "title": tab_row.get("title"),
            "note": ("Page.bringToFront activates the tab and raises its "
                     "window; the read-back is the page's own "
                     "document.visibilityState"),
            "browser": _pkg._brief(row)}

def reload_page(tab: str = "", browser: str = "") -> dict:
    """`tab reload`: reload ONE tab and prove a NEW document is there.

    `location.reload()` returns immediately and a fast page can be complete
    again before a read, so the oracle is `performance.timeOrigin` — it
    changes exactly when a document is created.
    """
    row, tab_row = _pkg._one_tab(tab, browser, for_write=True)
    profile, target_id = str(row["profile"]), str(tab_row["id"])
    before = _time_origin(profile, target_id)
    if before is None:
        fail(ERR_RELOAD_NOT_VERIFIED,
             f"tab {target_id[:10]}… did not report a document time — there "
             "is nothing to compare a reload against")
    _eval(profile, target_id, "location.reload(); 'reloading'", timeout=10)
    if not _wait_new_document(profile, target_id, before):
        fail(ERR_RELOAD_NOT_VERIFIED,
             f"no new document within {RELOAD_TIMEOUT_S:g}s — the page may "
             "block the reload, or it is still loading")
    return {"ok": True, "tab": f"id:{target_id}", "reloaded": True,
            "url_read": _href(profile, target_id), "browser": _pkg._brief(row)}
