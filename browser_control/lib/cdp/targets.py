"""targets — the browser's `/json` rows, and host-checked websocket URLs.

A row is shaped here and a target id is resolved to a websocket URL here; the
host check is part of the URL construction, so no caller can forget it.
"""
from __future__ import annotations

from typing import Any

from browser_control.lib.cdp.endpoint import (
    _get_port,
    get_json,
)
from browser_control.lib.cdp.rpc import (
    _checked_ws,
)
from browser_control.lib.cdp.session import (
    call,
)
from browser_control.lib.errors import (
    ERR_CDP_ERROR,
    ERR_NO_FRAME,
    ERR_NO_PAGE_TAB,
    ControlError,
    fail,
)


def page_rows(profile: str) -> list[dict]:
    """The page targets of the browser on this profile."""
    return _pages(get_json(profile, "/json"))

def page_rows_at(port: int) -> list[dict]:
    """The page targets of the browser answering on this loopback port."""
    return _pages(_get_port(port, "/json"))

def frame_targets(port: int) -> list[dict]:
    """The IFRAME targets of this browser, EACH WITH THE TAB THAT OWNS IT.

    `/json` lists iframe targets without their parent, so a frame matched by
    URL alone can belong to ANOTHER tab — measured: two tabs embedding the same
    widget both resolved to ONE target, and `--frame` then typed into the tab
    nobody asked about. The browser endpoint's `Target.getTargets` carries
    `parentId` for an iframe: the owning tab's target id, which is the proof
    this returns. `[]` means the browser did not say (or would not answer), and
    the caller then trusts only a URL that is unique in the whole browser.
    """
    version = _get_port(port, "/json/version")
    url = str((version or {}).get("webSocketDebuggerUrl") or "") \
        if isinstance(version, dict) else ""
    if not url:
        return []
    try:
        result = call(_checked_ws(url), "Target.getTargets")
    except ControlError:
        return []
    infos = result.get("targetInfos") if isinstance(result, dict) else None
    if not isinstance(infos, list):
        return []
    out: list[dict] = []
    for info in infos:
        if not isinstance(info, dict) or str(info.get("type")) != "iframe":
            continue
        out.append({"id": str(info.get("targetId") or ""),
                    "url": str(info.get("url") or ""),
                    "title": str(info.get("title") or ""),
                    "parent": str(info.get("parentId") or ""),
                    "parent_frame": str(info.get("parentFrameId") or "")})
    return sorted(out, key=lambda row: row["id"])

def _of_kind(rows: Any, kind: str) -> list[dict]:
    """A `/json` reply -> the targets of one kind, id-sorted.

    Sorted by target id locally: CDP does not document `/json` order, so "the
    tabs" would otherwise follow an external array's order.
    """
    if not isinstance(rows, list):
        fail(ERR_CDP_ERROR, "/json answered a shape this tool cannot read")
    found = [r for r in rows
             if isinstance(r, dict) and r.get("type") == kind and r.get("id")]
    return sorted(found, key=lambda r: str(r["id"]))

def _pages(rows: Any) -> list[dict]:
    """A `/json` reply -> its PAGE targets, id-sorted."""
    return _of_kind(rows, "page")

def target_ws(port: int, target_id: str, kind: str = "page") -> str:
    """The websocket of ONE target on `port`, host-checked.

    `kind="page"` is a tab; `kind="iframe"` is a cross-origin frame, which is
    a target of its own. Read from `/json` — whose rows carry it — so the
    connection is that target's own, not a re-resolution of a spec that may
    have moved since.
    """
    rows = _of_kind(_get_port(port, "/json"), kind)
    row = next((r for r in rows if str(r.get("id")) == str(target_id)), None)
    what = "tab" if kind == "page" else "frame"
    code = ERR_NO_PAGE_TAB if kind == "page" else ERR_NO_FRAME
    if row is None:
        fail(code, f"{what} {str(target_id)[:10]}… left the {what} list")
    ws = str(row.get("webSocketDebuggerUrl") or "")
    if not ws:
        fail(code, f"{what} {str(target_id)[:10]}… has no websocket endpoint")
    return _checked_ws(ws, what)

def rows_to_tabs(rows: list[dict]) -> list[dict]:
    """One page row, as every verb reports it."""
    return [{"id": str(r.get("id") or ""),
             "title": str(r.get("title") or ""),
             "url": str(r.get("url") or "")} for r in rows]


def browser_ws(profile: str) -> str:
    """The browser endpoint's websocket URL, shape- and host-checked."""
    info = get_json(profile, "/json/version")
    if not isinstance(info, dict):
        fail(ERR_CDP_ERROR, "/json/version answered a shape this tool cannot read")
    ws = str(info.get("webSocketDebuggerUrl") or "")
    if not ws:
        fail(ERR_CDP_ERROR, "/json/version names no browser websocket endpoint")
    return _checked_ws(ws)

def browser_call(profile: str, method: str, params: dict) -> dict:
    """One CDP call on the BROWSER endpoint (`Target.*`)."""
    return call(browser_ws(profile), method, params)
