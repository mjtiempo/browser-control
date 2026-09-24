"""scroll — `tab scroll`: real wheel input, or DOM.scrollIntoViewIfNeeded.

A wheel's effect is asynchronous (measured), so scrolling verifies by
polling until the document or the scroller under the point moved.
"""
from __future__ import annotations

from browser_control.lib import (
    browser as browser_lib,
)
from browser_control.lib import (
    cdp,
)
from browser_control.lib import dom as _pkg
from browser_control.lib.coerce import (
    as_int,
)
from browser_control.lib.dom.queries import (
    FIND_CAP,
)
from browser_control.lib.dom.scripts import (
    ELEMENT_EXPR,
    SCROLL_PROBE,
    fill,
)
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    ERR_NO_MATCH,
    ERR_SCROLL_NOT_VERIFIED,
    fail,
)
from browser_control.lib.poll import (
    POLL_FAST,
    poll,
)

SCROLL_EDGE_STEP = 2500     # one wheel notch when scrolling to an edge

SCROLL_EDGE_STEPS = 12      # and how many of them an edge is worth

SCROLL_MOVE_S = 4.0         # how long one wheel is given to move something

def scroll(by: int | None = None, edge: str | None = None,
           text: str | None = None, selector: str | None = None,
           index: int | None = None, at: str | None = None,
           tab: str = "", browser: str = "") -> dict:
    """`tab scroll`: move the page with REAL wheel input, or reveal one element.

    Three modes, one of them per call:

    * `--by N` — one wheel event of N pixels at `--at X,Y` (the viewport centre
      by default). A wheel moves what is UNDER the point, nested scrollers
      included; the reply says which scroller moved.
    * `--edge top|bottom` — wheel in steps until the document stops moving, and
      refuse unless the edge was actually reached.
    * `TEXT` / `--selector CSS` — `DOM.scrollIntoViewIfNeeded`, a CDP method,
      then prove the element is in the viewport.

    A wheel's scroll lands ASYNCHRONOUSLY (measured), so every mode verifies by
    polling to a deadline rather than reading once.
    """
    modes = [name for name, value in
             (("--by", by), ("--edge", edge),
              ("element", text or selector)) if value is not None]
    if len(modes) != 1:
        fail(ERR_BAD_ARGS,
             "tab scroll: name ONE of --by PIXELS, --edge top|bottom, or an "
             "element (TEXT / --selector CSS)")
    if at is not None and modes[0] != "--by":
        fail(ERR_BAD_ARGS, "tab scroll: --at goes with --by")
    # the arguments are checked BEFORE a browser is asked about anything
    if modes[0] == "--by" and not as_int(by):
        fail(ERR_BAD_ARGS, "tab scroll: --by needs a non-zero PIXELS")
    if edge is not None and str(edge) not in ("top", "bottom"):
        fail(ERR_BAD_ARGS, f"tab scroll: --edge is top|bottom, got {edge!r}")
    if at is not None:
        _at_point(at)                      # syntax now; the RANGE needs the
    page = _pkg.Tab.open(tab, browser, for_write=True)   # real viewport
    row, tab_row = page.row, page.tab_row
    target_id = str(tab_row["id"])
    with page.session() as session:
        data = _pkg._matches_in(session, "", "", 1)      # page facts, no matching
        viewport = _pkg._viewport(data, target_id)
        x, y = _point(at, viewport)
        if modes[0] == "element":
            return _reveal(session, row, tab_row, text, selector, index, data)
        return _wheel(session, row, tab_row, by=by, edge=edge, x=x, y=y,
                      data=data)

def _at_point(at: str, verb: str = "tab scroll") -> tuple[int, int]:
    """`--at X,Y` as two numbers — the SYNTAX, which needs no browser.

    `verb` is only for the refusal: three verbs take a point now (scroll,
    click, hover), and a message naming the wrong one is a message about the
    wrong verb.
    """
    parts = str(at).replace(" ", "").split(",")
    if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
        fail(ERR_BAD_ARGS, f"{verb}: --at needs X,Y numbers, got {at!r}")
    return as_int(parts[0], -1), as_int(parts[1], -1)

def _point(at: str | None, viewport: list[int],
           verb: str = "tab scroll") -> tuple[int, int]:
    """The wheel's point: `--at X,Y` inside the viewport, else its middle.

    `verb` is only for the refusal: `tab click --at` and `tab hover --at` take
    the same range check, and naming "tab scroll" for a click is a message about
    the wrong verb (a review flagged it — this message said "tab scroll" for
    all three).
    """
    if not at:
        return viewport[0] // 2, viewport[1] // 2
    x, y = _at_point(at, verb)
    if not (0 <= x < viewport[0] and 0 <= y < viewport[1]):
        fail(ERR_BAD_ARGS,
             f"{verb}: --at {at!r} is outside the viewport {viewport}")
    return x, y

def _probe(session: cdp.Session, x: int, y: int) -> dict:
    """The document's scroll position and the scroller under (x, y)."""
    data = session.evaluate(fill(SCROLL_PROBE, x=str(x), y=str(y)))
    return data if isinstance(data, dict) else {}

def _settle(session: cdp.Session, x: int, y: int, before: dict,
            timeout: float = SCROLL_MOVE_S) -> dict:
    """Poll until the document OR the scroller under the point moved."""
    def probe() -> dict:
        return _probe(session, x, y)

    _attempts, now = poll(
        probe, timeout=timeout, interval=POLL_FAST,
        accept=lambda got: (got.get("y"), got.get("x"), got.get("nested")) !=
                           (before.get("y"), before.get("x"),
                            before.get("nested")))
    return now

def _wheel(session: cdp.Session, row: dict, tab_row: dict, by: int | None,
           edge: str | None, x: int, y: int, data: dict) -> dict:
    """The wheel modes: one delta, or repeated steps to an edge.

    `scroll` has already validated the arguments; what is left here is the
    input and the read-back.
    """
    before = _probe(session, x, y)
    steps = 0
    if edge is None:
        delta = as_int(by)
        session.call("Input.dispatchMouseEvent",
                     {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0,
                      "deltaY": delta, "button": "none", "buttons": 0})
        after = _settle(session, x, y, before)
        steps = 1
        moved = (as_int(after.get("y")) != as_int(before.get("y"))
                 or after.get("nested") != before.get("nested"))
        if not moved:
            # a delta was asked for and nothing took it: already at that end
            # of the document is the one reason that is not a failure
            at_edge = ((as_int(before.get("y")) == 0 and delta < 0)
                       or (abs(as_int(before.get("y"))
                               - as_int(before.get("max"))) <= 2 and delta > 0))
            if not at_edge:
                fail(ERR_SCROLL_NOT_VERIFIED,
                     f"nothing moved: the document is still at y="
                     f"{as_int(after.get('y'))} (max {as_int(after.get('max'))}) "
                     f"and no scroller under ({x}, {y}) moved — the wheel may "
                     "have landed on something that does not scroll (aim it "
                     "with --at X,Y)")
    else:
        direction = -1 if str(edge) == "top" else 1
        after = before
        for step in range(SCROLL_EDGE_STEPS):
            session.call("Input.dispatchMouseEvent",
                         {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0,
                          "deltaY": direction * SCROLL_EDGE_STEP,
                          "button": "none", "buttons": 0})
            steps = step + 1
            moved = _settle(session, x, y, after)
            if (moved.get("y"), moved.get("nested")) == (after.get("y"),
                                                         after.get("nested")):
                after = moved
                break                     # nothing moved: an edge, or a wall
            after = moved
        want = 0 if direction < 0 else as_int(after.get("max"))
        if abs(as_int(after.get("y")) - want) > 2:
            fail(ERR_SCROLL_NOT_VERIFIED,
                 f"the document stopped at y={as_int(after.get('y'))} of "
                 f"max {as_int(after.get('max'))} after {steps} wheel step(s) — "
                 "the bottom/top was not reached (a sticky scroller, or a "
                 "point that is over something that does not scroll)")
    moved_document = as_int(after.get("y")) != as_int(before.get("y"))
    moved_nested = after.get("nested") != before.get("nested")
    return {"ok": True, "moved": moved_document or moved_nested,
            "document": {"before": as_int(before.get("y")),
                         "after": as_int(after.get("y")),
                         "max": as_int(after.get("max"))},
            "nested": {"before": before.get("nested"),
                       "after": after.get("nested")},
            "point": [x, y], "steps": steps,
            "tab": f"id:{tab_row['id']}", "browser": browser_lib.brief(row)}

def _reveal(session: cdp.Session, row: dict, tab_row: dict,
            text: str | None, selector: str | None, index: int | None,
            data: dict) -> dict:
    """`DOM.scrollIntoViewIfNeeded` for one element, then prove it is visible."""
    needle, css = _pkg._query_args(text, selector, "tab scroll")
    node_id = _pkg._node_of(session, _pkg._match_args(ELEMENT_EXPR, needle, css, index))
    if not node_id:
        fail(ERR_NO_MATCH,
             f"no rendered element matches {needle or css!r}"
             + (f" (--index {index} is past the end)" if index else "")
             + _pkg._frames_note(row, tab_row))
    session.call("DOM.scrollIntoViewIfNeeded", {"nodeId": node_id})
    # prove it: the SAME index (not "any match") is the one that must be
    # inside the viewport — accepting any in-viewport match reported an
    # already-visible earlier element as the one revealed (a review found the
    # false proof, the family's one job being that the proof is true)
    def indexed_in_viewport(data: dict) -> bool:
        rows = _pkg._well_formed(data.get("matches") or [], ("tag", "box"))
        i = as_int(index) if index is not None else 0
        return 0 <= i < len(rows) and bool(rows[i].get("in_viewport"))

    _attempts, found = poll(
        lambda: _pkg._matches_in(session, needle, css, FIND_CAP),
        timeout=SCROLL_MOVE_S, interval=POLL_FAST,
        accept=indexed_in_viewport)
    rows = _pkg._well_formed(found.get("matches") or [], ("tag", "box"))
    i = as_int(index) if index is not None else 0
    if not (0 <= i < len(rows)) or not rows[i].get("in_viewport"):
        fail(ERR_SCROLL_NOT_VERIFIED,
             f"{needle or css!r}"
             + (f" at --index {index}" if index is not None else "")
             + " is still outside the viewport after "
             "DOM.scrollIntoViewIfNeeded — the element may be inside a "
             "container that cannot scroll it into view")
    return {"ok": True, "revealed": True, "element": _pkg._element(rows[i]),
            "scroll": found.get("scroll"), "tab": f"id:{tab_row['id']}",
            "browser": browser_lib.brief(row)}
