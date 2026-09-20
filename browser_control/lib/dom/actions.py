"""actions — the pointer verbs: click, hover, check, select, focus, upload.

Real CDP input at a proven point, then a read-back of the control's own
state — a page that ignores the event refuses rather than claiming success.
"""
from __future__ import annotations

import json
import os

from browser_control.lib import (
    browser as browser_lib,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib import (
    cdp,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib import dom as _pkg  # pyright: ignore[reportMissingImports]
from browser_control.lib.coerce import (  # pyright: ignore[reportMissingImports]
    as_int,
    as_ints,
    as_list,
)
from browser_control.lib.dom.keys import (  # pyright: ignore[reportMissingImports]
    KEYS,
)
from browser_control.lib.dom.queries import (  # pyright: ignore[reportMissingImports]
    FIND_CAP,
)
from browser_control.lib.dom.scripts import (  # pyright: ignore[reportMissingImports]
    CANDIDATES_EXPR,
    CHECK_READ,
    ELEMENT_EXPR,
    FILES_EXPR,
    FOCUS_PROBE,
    HOVER_PROBE,
    POINT_HOVER_PROBE,
    POINT_PROBE,
    SELECT_PROBE,
    STATE_EXPR,
    UPLOAD_DEFAULT_SELECTOR,
    fill,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_AMBIGUOUS_OPTION,
    ERR_BAD_ARGS,
    ERR_CHECK_NOT_VERIFIED,
    ERR_FOCUS_NOT_VERIFIED,
    ERR_HOVER_NOT_VERIFIED,
    ERR_NO_FILE,
    ERR_NO_MATCH,
    ERR_NO_VIEWPORT_TARGET,
    ERR_NOT_A_SELECT,
    ERR_NOT_CHECKABLE,
    ERR_OCCLUDED,
    ERR_SELECT_NOT_VERIFIED,
    ERR_UPLOAD_NOT_VERIFIED,
    ControlError,
    fail,
)
from browser_control.lib.poll import (  # pyright: ignore[reportMissingImports]
    POLL_FAST,
    poll,
)
from browser_control.lib.text import (  # pyright: ignore[reportMissingImports]
    foreign,
)

CHECK_TIMEOUT_S = 2.0       # how long a click is given to flip `checked`

def _under_point(session: cdp.Session, x: int, y: int) -> object:
    """What the element-at-point probe reaches there, as a short name.

    The one probe both `--at` and the element path use, so "what is actually
    there" is measured the same way after every dispatch.
    """
    return session.evaluate(fill(POINT_PROBE, x=str(x), y=str(y)))

def _at_point_click(session: cdp.Session, x: int, y: int) -> dict:
    """A move, a press and a release at a viewport point, and what is there."""
    for kind, buttons in (("mouseMoved", 0), ("mousePressed", 1),
                          ("mouseReleased", 0)):
        session.call("Input.dispatchMouseEvent",
                     {"type": kind, "x": x, "y": y, "button": "left",
                      "buttons": buttons, "clickCount": 1})
    return {"under": _pkg._under_point(session, x, y)}

def _click_at(row: dict, tab_row: dict, at: str) -> dict:
    """`tab click --at X,Y`: real input at a POINT, for what a selector cannot
    name — a canvas, or a widget inside a frame that shares this page's process.

    There is no element to verify against, so this says `verified: false` and
    reports what the point actually REACHES: the input did land, and whether it
    was the right control is the caller's to judge from `changed` and
    `under`.
    """
    _pkg._at_point(at, "tab click")
    with _pkg._session(row, tab_row) as session:
        data = _pkg._matches_in(session, "", "", 1)     # page facts, no matching
        x, y = _pkg._point(at, _pkg._viewport(data, str(tab_row["id"])),
                    "tab click")
        before = session.evaluate(STATE_EXPR)
        probe = _at_point_click(session, x, y)
        after = session.evaluate(STATE_EXPR)
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    changed = (after.get("url") != before.get("url")
               or after.get("title") != before.get("title")
               or after.get("active") != before.get("active")
               or [after.get("x"), after.get("y")]
               != [before.get("x"), before.get("y")])
    reply = {"ok": True, "clicked": True, "point": [x, y],
             "under": probe.get("under"), "changed": changed,
             "verified": False,
             "note": ("real input (CDP) at a point: the hit-test can only say "
                      "what the point reaches, so read `changed` and the "
                      "page yourself. A point is also a MOMENT: read it, and a "
                      "page that reflows can move the control out from under "
                      "the click (so `under` says what is there afterwards)"),
             "tab": f"id:{tab_row['id']}",
             "browser": browser_lib.brief(row)}
    return _pkg._with_frame(reply)

def _hover_at(row: dict, tab_row: dict, at: str) -> dict:
    """`tab hover --at X,Y`: a move to a point, verified by `:hover` on it."""
    _pkg._at_point(at, "tab hover")
    with _pkg._session(row, tab_row) as session:
        data = _pkg._matches_in(session, "", "", 1)
        x, y = _pkg._point(at, _pkg._viewport(data, str(tab_row["id"])),
                    "tab hover")
        session.call("Input.dispatchMouseEvent",
                     {"type": "mouseMoved", "x": x, "y": y,
                      "button": "none", "buttons": 0})
        probe = session.evaluate(
            fill(POINT_HOVER_PROBE, x=str(x), y=str(y)))
    probe = probe if isinstance(probe, dict) else {}
    if not probe.get("hovered"):
        fail(ERR_HOVER_NOT_VERIFIED,
             f"nothing at viewport {[x, y]} matches `:hover` after the pointer "
             f"moved there ({probe.get('under') or 'nothing'} is at that point)")
    reply = {"ok": True, "hovered": True, "verified": False,
             "point": [x, y], "under": probe.get("under"),
             "note": ("real input (CDP): one mouseMoved at a point; the "
                      "read-back is the engine's own `:hover` there. A POINT "
                      "is not a selector: `under` is whatever the point "
                      "reaches (an overlay qualifies), so this reports what "
                      "happened rather than claiming it was the element "
                      "intended — `tab hover TEXT|--selector CSS` is the "
                      "verified form"),
             "tab": f"id:{tab_row['id']}",
             "browser": browser_lib.brief(row)}
    return _pkg._with_frame(reply)

def click(text: str | None = None, selector: str | None = None,
          index: int | None = None, tab: str = "", browser: str = "",
          at: str | None = None) -> dict:
    """`tab click`: press the element a spec resolves to, with REAL input.

    `Input.dispatchMouseEvent` — a move, a press, a release at the element's
    viewport centre — is the input a mouse produces, so a handler that ignores
    `element.click()` takes it. The point must hit-test to the element first
    (`occluded` names what is actually there), and the reply carries what
    changed afterwards: `changed: false` is a fact about a click that had no
    visible effect, not a failure, because the input DID land.
    """
    needle, css = _pkg._query_args(text, selector, "tab click") if at is None \
        else ("", "")
    if at is not None:
        if text or selector:
            fail(ERR_BAD_ARGS,
                 "tab click: --at is a POINT — give that or a TEXT/--selector, "
                 "not both")
        _pkg._at_point(at, "tab click")     # the POINT first: no browser needed
        row, tab_row = _pkg._resolve(tab, browser, for_write=True)
        return _click_at(row, tab_row, at)
    row, tab_row = _pkg._resolve(tab, browser, for_write=True)
    with _pkg._session(row, tab_row) as session:
        data = _pkg._matches_in(session, needle, css, FIND_CAP)
        element = _pkg._pick(data, needle, css, index, row=row,
                           tab_row=tab_row)
        if not element.get("in_viewport"):
            fail(ERR_NO_VIEWPORT_TARGET,
                 f"{_pkg._describe(element)} is at page {element.get('box')}, "
                 "outside the viewport — scroll it into view first: "
                 f"`tab scroll {needle or '--selector ' + css!r}`")
        if not element.get("hit"):
            fail(ERR_OCCLUDED,
                 f"{_pkg._describe(element)} is at viewport {element.get('point')} "
                 f"but that point reaches "
                 f"{foreign(element.get('hit_element'), 50) or 'nothing'} "
                 "instead — something is on top of it")
        # press at the point the HIT-TEST proved, not at the element's raw
        # centre: the probe CLAMPS into the viewport, so an element whose centre
        # is below the fold was tested inside it and pressed outside, and the
        # reply still said `clicked: true` (a review measured the mismatch)
        point = as_ints(element.get("hit_at") or element.get("point"), 2)
        if len(point) < 2:
            # a page may answer anything for a value that crosses
            # Runtime.evaluate, and "[7]" is not a point: refuse rather
            # than raise ValueError out of the verb (both review lanes
            # found the unpack behind the `_ints` guard)
            fail(ERR_NO_VIEWPORT_TARGET,
                 f"{_pkg._describe(element)} reports no usable point "
                 f"({point!r}) — the page owns this value")
        x, y = point
        for kind, buttons in (("mouseMoved", 0), ("mousePressed", 1),
                              ("mouseReleased", 0)):
            session.call("Input.dispatchMouseEvent",
                         {"type": kind, "x": x, "y": y, "button": "left",
                          "buttons": buttons, "clickCount": 1})
        after = session.evaluate(STATE_EXPR)
        # what the point reaches AFTERWARDS: a click legitimately changes the
        # document, so this is information rather than a verdict — but a reply
        # that says `clicked: true` should also say what it can still see there
        under = _pkg._under_point(session, x, y)
    after = after if isinstance(after, dict) else {}
    before = {"url": data.get("url"), "title": data.get("title"),
              "active": data.get("active"), "scroll": data.get("scroll")}
    changed = (after.get("url") != before["url"]
               or after.get("title") != before["title"]
               or after.get("active") != before["active"]
               or [after.get("x"), after.get("y")] != before["scroll"])
    return {"ok": True, "clicked": True, "element": _pkg._element(element),
            "point": [x, y], "changed": changed, "under": under,
            "before": before,
            "after": {"url": after.get("url"), "title": after.get("title"),
                      "active": after.get("active"),
                      "scroll": [after.get("x"), after.get("y")]},
            "note": ("real input (CDP), so handlers that ignore "
                     "element.click() take it; `changed` is whether anything "
                     "observable moved"),
            "tab": f"id:{tab_row['id']}", "browser": browser_lib.brief(row)}

def hover(text: str | None = None, selector: str | None = None,
          index: int | None = None, tab: str = "", browser: str = "",
          at: str | None = None) -> dict:
    """`tab hover`: put the pointer ON one element, verified by `:hover`.

    Menus, tooltips and CSS-only UI open on a MOVE, not a click, and
    `Input.dispatchMouseEvent` of type `mouseMoved` is that move. Nothing in
    the DOM has to change for a hover to have happened, so the oracle is the
    engine's own hover state: the element (or something inside it) matches
    `:hover` AND the point still hit-tests into it. A point that reaches
    another element refuses `occluded` BEFORE any event is sent, exactly like
    `click` — and the pointer stays where it was put, so a caller that needs
    another position asks for it.
    """
    if at is not None:
        if text or selector:
            fail(ERR_BAD_ARGS,
                 "tab hover: --at is a POINT — give that or a TEXT/--selector, "
                 "not both")
        _pkg._at_point(at, "tab hover")     # the POINT first: no browser needed
        row, tab_row = _pkg._resolve(tab, browser, for_write=True)
        return _hover_at(row, tab_row, at)
    needle, css = _pkg._query_args(text, selector, "tab hover")
    row, tab_row = _pkg._resolve(tab, browser, for_write=True)
    with _pkg._session(row, tab_row) as session:
        data = _pkg._matches_in(session, needle, css, FIND_CAP)
        element = _pkg._pick(data, needle, css, index, row=row,
                           tab_row=tab_row)
        if not element.get("in_viewport"):
            fail(ERR_NO_VIEWPORT_TARGET,
                 f"{_pkg._describe(element)} is at page {element.get('box')}, "
                 "outside the viewport — scroll it into view first: "
                 f"`tab scroll {needle or '--selector ' + css!r}`")
        if not element.get("hit"):
            fail(ERR_OCCLUDED,
                 f"{_pkg._describe(element)} is at viewport {element.get('point')} "
                 f"but that point reaches "
                 f"{foreign(element.get('hit_element'), 50) or 'nothing'} "
                 "instead — something is on top of it")
        point = as_ints(element.get("hit_at") or element.get("point"), 2)
        if len(point) < 2:
            # a page may answer anything for a value that crosses
            # Runtime.evaluate, and "[7]" is not a point: refuse rather
            # than raise ValueError out of the verb (both review lanes
            # found the unpack behind the `_ints` guard)
            fail(ERR_NO_VIEWPORT_TARGET,
                 f"{_pkg._describe(element)} reports no usable point "
                 f"({point!r}) — the page owns this value")
        x, y = point
        session.call("Input.dispatchMouseEvent",
                     {"type": "mouseMoved", "x": x, "y": y,
                      "button": "none", "buttons": 0})
        probe = session.evaluate(
            _pkg._match_args(HOVER_PROBE, needle, css, index,
                             x=str(x), y=str(y)))
    probe = probe if isinstance(probe, dict) else {}
    if not (probe.get("hovered") and probe.get("chain")):
        fail(ERR_HOVER_NOT_VERIFIED,
             f"{_pkg._describe(element)} at viewport {[x, y]} does not match "
             "`:hover` after the pointer moved there "
             f"({probe.get('under') or 'nothing'} is at that point) — the "
             "page may re-render, or the element moved between the read and "
             "the move")
    return {"ok": True, "hovered": True, "verified": True,
            "element": _pkg._element(element), "point": [x, y],
            "under": probe.get("under"),
            "note": ("real input (CDP): one mouseMoved; the read-back is the "
                     "engine's own `:hover` state"),
            "tab": f"id:{tab_row['id']}",
            "browser": browser_lib.brief(row)}

def _check_state(session: cdp.Session, needle: str, css: str,
                 index: int | None) -> dict:
    """The control's own checked/disabled facts, read from the page."""
    probe = session.evaluate(_pkg._match_args(CHECK_READ, needle, css, index))
    return probe if isinstance(probe, dict) else {}

def _checkable(probe: dict, element: dict) -> None:
    """Refuse what a click cannot make checked, naming what it is."""
    if not probe.get("checkable"):
        kind = str(probe.get("tag") or element.get("tag") or "element")
        type_ = str(probe.get("type") or "")
        label = f"{kind}[{type_}]" if type_ else kind
        fail(ERR_NOT_CHECKABLE,
             f"{_pkg._describe(element)} is {label} — `tab check` drives a "
             "checkbox or a radio button")
    if probe.get("disabled"):
        fail(ERR_NOT_CHECKABLE,
             f"{_pkg._describe(element)} is disabled — a user cannot change it, "
             "and neither will this")

def check(text: str | None = None, selector: str | None = None,
          index: int | None = None, uncheck: bool = False, tab: str = "",
          browser: str = "") -> dict:
    """`tab check`: make a checkbox (or radio) checked, or not, and read it.

    Real input at the element's centre — the click a user makes — and the
    oracle is the control's own `checked`. Already in the wanted state means
    NO click: a click would toggle it away, so the reply is `changed: false`
    and the read-back still stands behind it.
    """
    needle, css = _pkg._query_args(text, selector, "tab check")
    row, tab_row = _pkg._resolve(tab, browser, for_write=True)
    want = not uncheck
    with _pkg._session(row, tab_row) as session:
        data = _pkg._matches_in(session, needle, css, FIND_CAP)
        element = _pkg._pick(data, needle, css, index, row=row,
                           tab_row=tab_row)
        before = _check_state(session, needle, css, index)
        _checkable(before, element)
        if bool(before.get("checked")) == want:
            return {"ok": True, "checked": want, "changed": False,
                    "verified": True, "element": _pkg._element(element),
                    "note": ("already in that state — no click was sent, "
                             "because a click would toggle it"),
                    "tab": f"id:{tab_row['id']}",
                    "browser": browser_lib.brief(row)}
        if not element.get("in_viewport"):
            fail(ERR_NO_VIEWPORT_TARGET,
                 f"{_pkg._describe(element)} is at page {element.get('box')}, "
                 "outside the viewport — scroll it into view first: "
                 f"`tab scroll {needle or '--selector ' + css!r}`")
        if not element.get("hit"):
            fail(ERR_OCCLUDED,
                 f"{_pkg._describe(element)} is at viewport {element.get('point')} "
                 f"but that point reaches "
                 f"{foreign(element.get('hit_element'), 50) or 'nothing'} "
                 "instead — something is on top of it")
        point = as_ints(element.get("hit_at") or element.get("point"), 2)
        if len(point) < 2:
            # a page may answer anything for a value that crosses
            # Runtime.evaluate, and "[7]" is not a point: refuse rather
            # than raise ValueError out of the verb (both review lanes
            # found the unpack behind the `_ints` guard)
            fail(ERR_NO_VIEWPORT_TARGET,
                 f"{_pkg._describe(element)} reports no usable point "
                 f"({point!r}) — the page owns this value")
        x, y = point
        for kind, buttons in (("mouseMoved", 0), ("mousePressed", 1),
                              ("mouseReleased", 0)):
            session.call("Input.dispatchMouseEvent",
                         {"type": kind, "x": x, "y": y, "button": "left",
                          "buttons": buttons, "clickCount": 1})
        _attempts, after = poll(
            lambda: _check_state(session, needle, css, index),
            timeout=CHECK_TIMEOUT_S, interval=POLL_FAST,
            accept=lambda state: bool(state.get("checked")) == want)
    if bool(after.get("checked")) != want:
        fail(ERR_CHECK_NOT_VERIFIED,
             f"{_pkg._describe(element)} reports checked={after.get('checked')} "
             f"after the click at viewport {[x, y]} — the page may have "
             "re-set it, or the control is not the one that reacted")
    return {"ok": True, "checked": want, "changed": True, "verified": True,
            "element": _pkg._element(element), "point": [x, y],
            "checked_before": bool(before.get("checked")),
            "note": "real input (CDP); the read-back is the control's own `checked`",
            "tab": f"id:{tab_row['id']}",
            "browser": browser_lib.brief(row)}

def _select_probe(session: cdp.Session, needle: str, css: str,
                  index: int | None, value: str) -> dict:
    """Which option a value names, and what the control holds (see the SQL)."""
    expression = _pkg._match_args(SELECT_PROBE, needle, css, index,
                                  value=json.dumps(str(value)))
    probe = session.evaluate(expression)
    return probe if isinstance(probe, dict) else {}

def select(text: str | None = None, selector: str | None = None,
           value: str = "", index: int | None = None, tab: str = "",
           browser: str = "") -> dict:
    """`tab select`: choose one `<option>` with REAL key events.

    A `<select>` popup is the browser's own widget and no CDP method opens it —
    but it does not need opening: focusing the control and pressing ArrowDown /
    ArrowUp moves the selection, and measured, that fires the page's own
    `change` handler with `isTrusted: true`, exactly as a user's choice does.
    `--value` names an option by its value first, then by its exact label; the
    read-back is the control's own value and selectedIndex, so a page that
    ignores or re-sets the choice refuses instead of claiming success.
    """
    needle, css = _pkg._query_args(text, selector, "tab select")
    wanted = str(value or "")
    if not wanted:
        fail(ERR_BAD_ARGS,
             "tab select: --value is required — the option's value, or its "
             "exact label")
    row, tab_row = _pkg._resolve(tab, browser, for_write=True)
    with _pkg._session(row, tab_row) as session:
        data = _pkg._matches_in(session, needle, css, FIND_CAP)
        element = _pkg._pick(data, needle, css, index, row=row,
                           tab_row=tab_row)
        probe = _select_probe(session, needle, css, index, wanted)
        if not probe.get("is_select"):
            kind = str(probe.get("tag") or element.get("tag") or "?")
            fail(ERR_NOT_A_SELECT,
                 f"{_pkg._describe(element)} is a {kind}, not a <select> — this "
                 "verb picks one option, and only a <select> has options")
        if probe.get("disabled"):
            fail(ERR_NOT_A_SELECT,
                 f"{_pkg._describe(element)} is disabled — a user cannot choose in "
                 "it, and neither will this")
        if probe.get("multiple"):
            fail(ERR_NOT_A_SELECT,
                 f"{_pkg._describe(element)} is a multiple select — it holds a "
                 "SET of options, and this verb sets one (use `tab js`)")
        matched = as_int(probe.get("matched"))
        if not matched:
            names = [foreign(name, 30)
                     for name in as_list(probe.get("labels"))[:12]]
            labels = ", ".join(names)
            fail(ERR_NO_MATCH,
                 f"no <option> in {_pkg._describe(element)} has value or label "
                 f"{wanted!r} (have: {labels or 'none'})")
        if matched > 1:
            candidates = ", ".join(foreign(name, 30) for name in
                                   as_list(probe.get("candidates"))[:5])
            fail(ERR_AMBIGUOUS_OPTION,
                 f"{matched} options in {_pkg._describe(element)} match "
                 f"{wanted!r}: {candidates} — their VALUES are what tell them "
                 "apart")
        target = as_int(probe.get("target"), -1)
        selected = as_int(probe.get("selected"), -1)
        delta = target - selected
        if not delta:
            return {"ok": True, "selected": True, "changed": False,
                    "verified": True, "trusted": True, "keys": 0,
                    "element": _pkg._element(element),
                    "value": probe.get("value"),
                    "label": probe.get("target_label"),
                    "option_index": target, "by": probe.get("by"),
                    "note": "already selected — no key was sent",
                    "tab": f"id:{tab_row['id']}",
                    "browser": browser_lib.brief(row)}
        node_id = _pkg._node_of(session,
                           _pkg._match_args(ELEMENT_EXPR, needle, css, index))
        if not node_id:
            fail(ERR_NO_MATCH,
                 f"{_pkg._describe(element)} left the document before the choice "
                 "could be made")
        try:
            session.call("DOM.focus", {"nodeId": node_id})
        except ControlError as e:
            fail(ERR_SELECT_NOT_VERIFIED,
                 f"{_pkg._describe(element)} cannot take the DOM focus, so the "
                 f"arrow keys would go elsewhere: {e.message}")
        key = KEYS["arrowdown" if delta > 0 else "arrowup"]
        key_name, code, vk, _text = key
        for _step in range(abs(delta)):
            session.call("Input.dispatchKeyEvent",
                         {"type": "rawKeyDown", "key": key_name,
                          "code": code, "windowsVirtualKeyCode": vk,
                          "nativeVirtualKeyCode": vk})
            session.call("Input.dispatchKeyEvent",
                         {"type": "keyUp", "key": key_name, "code": code,
                          "windowsVirtualKeyCode": vk,
                          "nativeVirtualKeyCode": vk})
        _attempts, after = poll(
            lambda: _select_probe(session, needle, css, index, wanted),
            timeout=CHECK_TIMEOUT_S, interval=POLL_FAST,
            accept=lambda got: as_int(got.get("selected"), -1) == target)
    if as_int(after.get("selected"), -1) != target \
            or str(after.get("value")) != str(probe.get("target_value")):
        fail(ERR_SELECT_NOT_VERIFIED,
             f"{_pkg._describe(element)} reports "
             f"value={after.get('value')!r} at index "
             f"{after.get('selected')!r} after {abs(delta)} arrow key(s) — "
             f"the wanted option is index {target} "
             f"(value {probe.get('target_value')!r}); the page may ignore the "
             "key events, or re-set the control")
    return {"ok": True, "selected": True, "changed": True, "verified": True,
            "trusted": True, "element": _pkg._element(element),
            "value": after.get("value"), "label": probe.get("target_label"),
            "option_index": target, "options": probe.get("options"),
            "by": probe.get("by"), "keys": abs(delta),
            "value_before": probe.get("value"),
            "note": ("REAL key events (CDP) on the focused control, so the "
                     "page's `change` handler sees isTrusted: true"),
            "tab": f"id:{tab_row['id']}",
            "browser": browser_lib.brief(row)}

def focus(text: str | None = None, selector: str | None = None,
          index: int | None = None, tab: str = "", browser: str = "") -> dict:
    """`tab focus`: put the DOM focus (the caret) on one element.

    This is the CARET, not the tab's frontmost position — that is `tab
    activate`. Focusing needs no coordinates, no window focus and no hit-test,
    so it works on a background tab and while a layer surface owns the pointer;
    it does scroll the element into view, which is why an element outside the
    viewport is a perfectly good target. `DOM.focus` is the CDP method, and the
    read-back is the page's own active element.
    """
    needle, css = _pkg._query_args(text, selector, "tab focus")
    row, tab_row = _pkg._resolve(tab, browser, for_write=True)
    with _pkg._session(row, tab_row) as session:
        data = _pkg._matches_in(session, needle, css, FIND_CAP)
        element = _pkg._pick(data, needle, css, index, row=row,
                           tab_row=tab_row)
        node_id = _pkg._node_of(session,
                           _pkg._match_args(ELEMENT_EXPR, needle, css, index))
        if not node_id:
            fail(ERR_NO_MATCH,
                 f"{_pkg._describe(element)} left the document before the focus "
                 "could be set")
        try:
            session.call("DOM.focus", {"nodeId": node_id})
        except ControlError as e:
            # the protocol says WHY (a disabled control, an element that
            # cannot be focused): that is the verb's own verdict, not a
            # generic CDP failure
            fail(ERR_FOCUS_NOT_VERIFIED,
                 f"{_pkg._describe(element)} cannot take the DOM focus: {e.message}")
        probe = session.evaluate(_pkg._match_args(FOCUS_PROBE, needle, css, index))
    probe = probe if isinstance(probe, dict) else {}
    if not probe.get("focused"):
        fail(ERR_FOCUS_NOT_VERIFIED,
             f"{_pkg._describe(element)} did not take the DOM focus — "
             f"{probe.get('active') or 'nothing'} has it instead (a disabled "
             "control, or an element that cannot be focused)")
    return {"ok": True, "focused": True, "element": _pkg._element(element),
            "active": probe.get("active"), "tab": f"id:{tab_row['id']}",
            "browser": browser_lib.brief(row)}

def upload(path: str, selector: str | None = None, index: int | None = None,
           tab: str = "", browser: str = "") -> dict:
    """`tab upload`: attach a local file to an `<input type=file>`.

    The one form control page JavaScript cannot fill: `input.files` is
    read-only and a hidden input cannot be clicked. The route is the CDP method
    `DOM.setFileInputFiles`, which takes the element as an OBJECT id — so it
    works through a shadow root too — and the input is usually hidden on
    purpose, which is why this verb (alone) does not require visibility. The
    file is a path on THIS machine, checked before the browser is asked
    anything, and the read-back is what the PAGE thinks it holds: one file,
    the same name and the same size.
    """
    file_path = str(path or "")
    if not os.path.isabs(file_path):
        fail(ERR_BAD_ARGS,
             f"tab upload: FILE must be an absolute path, got {file_path!r}")
    if not os.path.isfile(file_path):
        fail(ERR_NO_FILE, f"tab upload: no such file: {file_path}")
    size = os.path.getsize(file_path)
    css = str(selector or "").strip() or UPLOAD_DEFAULT_SELECTOR
    row, tab_row = _pkg._resolve(tab, browser, for_write=True)
    with _pkg._session(row, tab_row) as session:
        data = session.evaluate(
            _pkg._match_args(CANDIDATES_EXPR, "", css, cap=str(FIND_CAP)))
        element = _pkg._pick(data if isinstance(data, dict) else {},
                        "", css, index, row=row, tab_row=tab_row)
        handle = session.handle(_pkg._match_args(ELEMENT_EXPR, "", css, index,
                                            visible=False))
        if not handle:
            fail(ERR_NO_MATCH,
                 f"tab upload: {element.get('tag')} left the document before "
                 "the file could be set")
        session.call("DOM.setFileInputFiles",
                     {"files": [file_path], "objectId": handle})
        got = session.evaluate(_pkg._match_args(FILES_EXPR, "", css, index,
                                           visible=False))
    got = got if isinstance(got, dict) else {}
    files = got.get("files")
    files = files if isinstance(files, list) else []
    first = files[0] if files and isinstance(files[0], dict) else {}
    name = os.path.basename(file_path)
    if len(files) != 1 or first.get("name") != name \
            or as_int(first.get("size"), -1) != size:
        fail(ERR_UPLOAD_NOT_VERIFIED,
             f"tab upload: the page holds {files!r}, not one file named "
             f"{name!r} of {size} bytes")
    return {"ok": True, "file": file_path,
            "input": {"selector": css, "files": files},
            "tab": f"id:{tab_row['id']}",
            "browser": browser_lib.brief(row)}
