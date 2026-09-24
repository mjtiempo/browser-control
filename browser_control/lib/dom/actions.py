"""actions — the pointer verbs: click, hover, check, select, focus, upload.

Real CDP input at a proven point, then a read-back of the control's own
state — a page that ignores the event refuses rather than claiming success.
"""
from __future__ import annotations

import contextlib
import json
import os

from browser_control.lib import (
    browser as browser_lib,
)
from browser_control.lib import (
    cdp,
)
from browser_control.lib import dom as _pkg
from browser_control.lib.coerce import (
    as_int,
    as_ints,
    as_list,
)
from browser_control.lib.dom.keys import (
    key_event,
)
from browser_control.lib.dom.queries import (
    FIND_MAX_MATCHES,
    PAGE_BOUNDED_CAP,
)
from browser_control.lib.dom.result import (
    PageState,
)
from browser_control.lib.dom.scripts import (
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
from browser_control.lib.errors import (
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
from browser_control.lib.poll import (
    POLL_FAST,
    poll,
)
from browser_control.lib.text import (
    foreign,
)

CHECK_TIMEOUT_S = 2.0       # how long a click is given to flip `checked`


@contextlib.contextmanager
def _target(needle: str, css: str, index: int | None,  # noqa: ANN202
            tab: str, browser: str):
    """The prelude every element verb runs: the write-authorized tab, its
    session, the matched spec and the picked element.

    One place, because the ORDER is the contract — the tab is resolved for
    writes before a session opens, and the pick is what refuses `no-match` and
    `ambiguous-element` with the tab's own row — and five verbs spelled it out
    identically. Yields `(session, data, element, row, tab_row)`.
    """
    page = _pkg.Tab.open(tab, browser, for_write=True)
    row, tab_row = page.row, page.tab_row
    with page.session() as session:
        # FIND_MAX_MATCHES, not the 10 `find` prints by default: `tab find
        # --cap 50` can show index 20, and an action that scanned only 10
        # refused it with a count the same tool had just contradicted (a review
        # found it). The page still bounds the reply; this is its window.
        data = _pkg._matches_in(session, needle, css, FIND_MAX_MATCHES)
        element = _pkg._pick(data, needle, css, index, row=row,
                             tab_row=tab_row)
        yield session, data, element, row, tab_row


def _under_point(session: cdp.Session, x: int, y: int) -> object:
    """What the element-at-point probe reaches there, as a short name.

    The one probe both `--at` and the element path use, so "what is actually
    there" is measured the same way after every dispatch.
    """
    return session.evaluate(fill(POINT_PROBE, x=str(x), y=str(y)))

def click_point(session: cdp.Session, x: int, y: int) -> None:
    """A move, a press and a release at a viewport point, with REAL input.

    The ONE dispatch every pointer verb uses: the caller has already PROVEN the
    point (`aim` below), or is deliberately pressing a raw coordinate (`--at`).
    """
    for kind, buttons in (("mouseMoved", 0), ("mousePressed", 1),
                          ("mouseReleased", 0)):
        session.call("Input.dispatchMouseEvent",
                     {"type": kind, "x": x, "y": y, "button": "left",
                      "buttons": buttons, "clickCount": 1})


def aim(element: dict, needle: str, css: str) -> list:
    """The viewport point a pointer verb may press, or a refusal.

    The three guards every element verb shares: in the viewport, hit-tested to the
    element, and a usable point (the page owns every value that crossed
    `Runtime.evaluate`, so "[7]" is not a point).
    """
    if not element.get("in_viewport"):
        fail(ERR_NO_VIEWPORT_TARGET,
             f"{_pkg._describe(element)} is at page {element.get('box')}, "
             "outside the viewport — scroll it into view first: "
             f"`tab scroll {needle or '--selector ' + css!r}`")
    if not element.get("hit"):
        fail(ERR_OCCLUDED,
             f"{_pkg._describe(element)} is at viewport "
             f"{element.get('point')} but that point reaches "
             f"{foreign(element.get('hit_element'), 50) or 'nothing'} "
             "instead — something is on top of it")
    point = as_ints(element.get("hit_at") or element.get("point"), 2)
    if len(point) < 2:
        fail(ERR_NO_VIEWPORT_TARGET,
             f"{_pkg._describe(element)} reports no usable point "
             f"({point!r}) — the page owns this value")
    return point


def _at_point_click(session: cdp.Session, x: int, y: int) -> dict:
    """A move, a press and a release at a viewport point, and what is there."""
    click_point(session, x, y)
    return {"under": _pkg._under_point(session, x, y)}

def _head(described: object) -> str:
    """The identity inside ONE `describe()`-style string: its `role#id` head.

    `describe()` (scripts.py) spells an element `role#id label`, and the label
    after the first space carries a control's VALUE, its option text and its
    state — exactly what an action is meant to change — so identity stops at
    the first space (a `<select>`'s label IS its selected option). A probe that
    sends a `path` (tag, id and the element's position in the tree) is stronger
    and `_identity` prefers it.
    """
    return str(described or "").strip().split(" ", 1)[0]

def _identity(probe: dict) -> str:
    """The identity ONE probe reply carries for the element it re-resolved.

    `path` first when the reply has one, else the describe-style field a probe
    reports (`name`/`active`/`under`); "" when it reports none at all.
    """
    for key in ("path", "name", "active", "under"):
        head = _head(probe.get(key))
        if head:
            return head
    return ""

def _same_control(first: dict, second: dict) -> bool:
    """Does a later probe report the SAME element an earlier one did?

    The read-back re-resolves the spec by INDEX, so a page that re-renders
    between the action and the sample hands the verdict a different control's
    state — a review scripted exactly that: the check probe picked `input#a`,
    the poll's next sample answered `input#b` checked, and the reply certified
    `verified: true` about `input#a`. False only when both reports carry an
    identity and they DIFFER: a probe that reports none leaves nothing to
    compare, and the verb's own state check stands as before.
    """
    first_id, second_id = _identity(first), _identity(second)
    if not first_id or not second_id:
        return True
    return first_id == second_id

def _still_the_pick(described: object, element: dict) -> bool:
    """Do a describe-style string and a match ROW name the same element?

    What the two share: the role (`role(el)`, which a row carries as `role`, or
    as its `tag` when the page set no ARIA role) and the label (`describe()`
    slices it to 40, a row to 120). The ID is the one identity field a row does
    not carry, so a same-role, same-label sibling is as far as this can tell —
    a role or label that DIFFERS is another control, and the read-back must
    never certify one of those. A page that rewrites the field's own label under
    the verb (an `onfocus` default) is refused too: the read-back cannot tell
    that apart from a swap, and this verb refuses over a claim it cannot support.
    """
    text = str(described or "").strip()
    if not text:
        return True                     # nothing reported: no evidence either way
    head, _, label = text.partition(" ")
    role, _, _id = head.partition("#")
    want_role = str(element.get("role") or element.get("tag") or "").lower()
    return bool(role) and role.lower() == want_role \
        and label == str(element.get("text") or "")[:40]

def _reads_as_the_pick(described: object, element: dict) -> bool:
    """Does a probe's describe name the element the action was AIMED at?

    Strongest reading first: when the pick's own describe is known — the
    matcher measured `hit_element` at the picked element's centre, and that
    string's role and label are the row's own — the probe must name the same
    `role#id`, which is what catches a page that swapped `input#a` for a
    same-looking `input#b` at the same index. Otherwise (the point reached a
    child, or the element was outside the viewport when it was picked) the
    roles and labels are compared, all a row and a `describe()` share.
    """
    text = str(described or "").strip()
    if not text:
        return True                     # nothing reported: no evidence either way
    aimed = str(element.get("hit_element") or "").strip()
    if _head(aimed) and _still_the_pick(aimed, element):
        return _head(text) == _head(aimed)
    return _still_the_pick(text, element)

def _changed_under(element: dict, read_back: object, aimed: object) -> str:
    """What every element verb says when the read-back speaks of another
    element: both sides, because "it changed" alone cannot be acted on.

    The identity fields are the page's own, so they are flattened and capped
    on the way into the refusal; each verb keeps its OWN code (the code is what
    a caller branches on).
    """
    return (f"{_pkg._describe(element)} changed under the verb: the action "
            f"aimed at {foreign(aimed, 60)!r} but the read-back reports "
            f"{foreign(read_back, 60)!r} — a state that belongs to another "
            "element cannot certify this one")

def _click_at(row: dict, tab_row: dict, at: str) -> dict:
    """`tab click --at X,Y`: real input at a POINT, for what a selector cannot
    name — a canvas, or a widget inside a frame that shares this page's process.

    There is no element to verify against, so this says `verified: false` and
    reports what the point actually REACHES: the input did land, and whether it
    was the right control is the caller's to judge from `changed` and
    `under`.
    """
    _pkg._at_point(at, "tab click")
    with _pkg.Tab(row, tab_row).session() as session:
        data = _pkg._matches_in(session, "", "", 1)     # page facts, no matching
        x, y = _pkg._point(at, _pkg._viewport(data, str(tab_row["id"])),
                    "tab click")
        before = session.evaluate(fill(STATE_EXPR))
        probe = _at_point_click(session, x, y)
        after = session.evaluate(fill(STATE_EXPR))
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    changed = PageState.from_dict(after).changed(PageState.from_dict(before))
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
    with _pkg.Tab(row, tab_row).session() as session:
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
             f"moved there ({foreign(probe.get('under'), 60) or 'nothing'} "
             "is at that point)")
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
    with _target(needle, css, index, tab, browser) as (session, data, element,
                                                       row, tab_row):
        x, y = aim(element, needle, css)
        click_point(session, x, y)
        after = session.evaluate(fill(STATE_EXPR))
        # what the point reaches AFTERWARDS: a click legitimately changes the
        # document, so this is information rather than a verdict — but a reply
        # that says `clicked: true` should also say what it can still see there
        under = _pkg._under_point(session, x, y)
    after = after if isinstance(after, dict) else {}
    before = {"url": data.get("url"), "title": data.get("title"),
              "active": data.get("active"), "scroll": data.get("scroll")}
    changed = PageState.from_dict(after).changed(PageState.from_dict(before))
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
    with _target(needle, css, index, tab, browser) as (session, data, element,
                                                       row, tab_row):
        x, y = aim(element, needle, css)
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
             f"({foreign(probe.get('under'), 60) or 'nothing'} is at that "
             "point) — the "
             "page may re-render, or the element moved between the read and "
             "the move")
    # the read-back re-resolves the spec by INDEX and proves `:hover` about
    # whatever it got: without this the verdict certified a re-rendered element
    # as the one that was picked. Both sides here are the SAME measurement
    # (`describe()` of `document.elementFromPoint` at the aim point), taken
    # before the move by the matcher (`hit_element`) and after it by the probe
    # (`under`), so their `role#id` heads are compared directly
    if _head(probe.get("under")) and _head(element.get("hit_element")) \
            and _head(probe["under"]) != _head(element["hit_element"]):
        fail(ERR_HOVER_NOT_VERIFIED,
             _changed_under(element, probe.get("under"),
                            element.get("hit_element")))
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
    probe = session.evaluate(_pkg._match_args(CHECK_READ, needle, css, index),
                             timeout=CHECK_TIMEOUT_S)
    return probe if isinstance(probe, dict) else {}

def _checkable(probe: dict, element: dict) -> None:
    """Refuse what a click cannot make checked, naming what it is.

    A control that is GONE from the document is not "a thing that cannot be
    checked": the probe answers `found: false` for it, and the refusal says so
    in the words the sibling verbs use (a review found `select` reporting a
    vanished element as "is a select, not a `<select>`" — the check analogue
    lost the control's own type the same way). `not-checkable` stays for a real
    control of the wrong kind or a disabled one.
    """
    if probe.get("found") is False:
        fail(ERR_NO_MATCH,
             f"{_pkg._describe(element)} left the document before it could be "
             "checked")
    if not probe.get("checkable"):
        kind = foreign(probe.get("tag") or element.get("tag") or "element", 20)
        type_ = foreign(probe.get("type"), 20)
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

    The probe re-resolves the spec by index, so the state it reports is only
    this control's while it still IS this control: the read-back must name the
    element that was picked (and, after the click, the same element the
    pre-click probe named), or the verb refuses instead of certifying another
    control's `checked` (a review scripted `input#a` picked and `input#b`
    answering the poll).
    """
    needle, css = _pkg._query_args(text, selector, "tab check")
    want = not uncheck
    with _target(needle, css, index, tab, browser) as (session, data, element,
                                                       row, tab_row):
        before = _check_state(session, needle, css, index)
        _checkable(before, element)
        if not _reads_as_the_pick(before.get("name"), element):
            fail(ERR_CHECK_NOT_VERIFIED,
                 _changed_under(element, before.get("name"),
                                element.get("hit_element")))
        if bool(before.get("checked")) == want:
            return {"ok": True, "checked": want, "changed": False,
                    "verified": True, "element": _pkg._element(element),
                    "note": ("already in that state — no click was sent, "
                             "because a click would toggle it"),
                    "tab": f"id:{tab_row['id']}",
                    "browser": browser_lib.brief(row)}
        x, y = aim(element, needle, css)
        click_point(session, x, y)
        _attempts, after = poll(
            lambda: _check_state(session, needle, css, index),
            timeout=CHECK_TIMEOUT_S, interval=POLL_FAST,
            accept=lambda state: _same_control(before, state)
            and bool(state.get("checked")) == want)
    if not _same_control(before, after):
        fail(ERR_CHECK_NOT_VERIFIED,
             _changed_under(element, _identity(after), _identity(before)))
    if bool(after.get("checked")) != want:
        fail(ERR_CHECK_NOT_VERIFIED,
             f"{_pkg._describe(element)} reports "
             f"checked={foreign(after.get('checked'), 20)} "
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
    probe = session.evaluate(expression, timeout=CHECK_TIMEOUT_S)
    return probe if isinstance(probe, dict) else {}

def _focus_node(session: cdp.Session, needle: str, css: str, index: int | None,
                element: dict, *, code: str, gone: str, what: str = "") -> None:
    """Put the DOM focus on the node a spec resolves to, or refuse.

    ONE path, because `select` focuses the control so its arrow keys land in
    it and `focus` IS the focus: the node-id lookup, the `DOM.focus` call and
    the two refusals are the same code, and the verbs differ only in WHOSE
    verdict they are — `code` is the verb's own, `gone` what the element did
    when it left the document, and `what` why the focus mattered (empty for
    the verb whose whole point is it). Keeping a copy per verb is how a fix
    to one silently changes the other.

    A `DOM.focus` refusal is the verb's verdict rather than a generic CDP
    failure: the protocol says WHY (a disabled control, an element that
    cannot be focused).
    """
    node_id = _pkg._node_of(session,
                            _pkg._match_args(ELEMENT_EXPR, needle, css, index))
    if not node_id:
        fail(ERR_NO_MATCH, f"{_pkg._describe(element)} {gone}")
    try:
        session.call("DOM.focus", {"nodeId": node_id})
    except ControlError as e:
        why = f", {what}" if what else ""
        fail(code, f"{_pkg._describe(element)} cannot take the DOM focus"
                   f"{why}: {e.message}")

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
    ignores or re-sets the choice refuses instead of claiming success. The
    probe re-resolves the spec by index, so the read-back must also still BE
    the control that was picked: a `<select>` that re-rendered into another one
    holding the wanted option is not this verb's success (the pre-key probe and
    every poll sample must name the same `role#id`).
    """
    needle, css = _pkg._query_args(text, selector, "tab select")
    wanted = str(value or "")
    if not wanted:
        fail(ERR_BAD_ARGS,
             "tab select: --value is required — the option's value, or its "
             "exact label")
    with _target(needle, css, index, tab, browser) as (session, data, element,
                                                       row, tab_row):
        probe = _select_probe(session, needle, css, index, wanted)
        if probe.get("found") is False:
            # a VANISHED control is not "a thing that is not a <select>": the
            # old message told the caller "select#s is a select, not a
            # `<select>`" (a review found it), and the sibling verbs already
            # say what happened in words
            fail(ERR_NO_MATCH,
                 f"{_pkg._describe(element)} left the document before the "
                 "choice could be made")
        if not probe.get("is_select"):
            kind = foreign(probe.get("tag") or element.get("tag") or "?", 20)
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
        if not _reads_as_the_pick(probe.get("name"), element):
            fail(ERR_SELECT_NOT_VERIFIED,
                 _changed_under(element, probe.get("name"),
                                element.get("hit_element")))
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
        _focus_node(session, needle, css, index, element,
                    code=ERR_SELECT_NOT_VERIFIED,
                    gone="left the document before the choice could be made",
                    what="so the arrow keys would go elsewhere")
        key_name = "arrowdown" if delta > 0 else "arrowup"
        for _step in range(abs(delta)):
            session.call("Input.dispatchKeyEvent",
                         key_event(key_name, "rawKeyDown", with_text=False))
            session.call("Input.dispatchKeyEvent",
                         key_event(key_name, "keyUp", with_text=False))
        _attempts, after = poll(
            lambda: _select_probe(session, needle, css, index, wanted),
            timeout=CHECK_TIMEOUT_S, interval=POLL_FAST,
            accept=lambda got: _same_control(probe, got)
            and as_int(got.get("selected"), -1) == target)
    if not _same_control(probe, after):
        fail(ERR_SELECT_NOT_VERIFIED,
             _changed_under(element, _identity(after), _identity(probe)))
    if as_int(after.get("selected"), -1) != target \
            or str(after.get("value")) != str(probe.get("target_value")):
        fail(ERR_SELECT_NOT_VERIFIED,
             f"{_pkg._describe(element)} reports "
             f"value={foreign(after.get('value'), 60)!r} at index "
             f"{foreign(after.get('selected'), 20)} after {abs(delta)} arrow "
             f"key(s) — the wanted option is index {target} "
             f"(value {foreign(probe.get('target_value'), 60)!r}); the page may "
             "ignore the key events, or re-set the control")
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
    read-back is the page's own active element — which must still be the
    element that was picked: `DOM.focus` and the probe each re-resolve the spec
    by index, so the `active` describe is checked against the pick before it
    certifies (a re-rendered control that grabbed the caret is not this verb's
    focus).
    """
    needle, css = _pkg._query_args(text, selector, "tab focus")
    with _target(needle, css, index, tab, browser) as (session, data, element,
                                                       row, tab_row):
        _focus_node(session, needle, css, index, element,
                    code=ERR_FOCUS_NOT_VERIFIED,
                    gone="left the document before the focus could be set")
        probe = session.evaluate(_pkg._match_args(FOCUS_PROBE, needle, css, index))
    probe = probe if isinstance(probe, dict) else {}
    if not probe.get("focused"):
        fail(ERR_FOCUS_NOT_VERIFIED,
             f"{_pkg._describe(element)} did not take the DOM focus — "
             f"{foreign(probe.get('active'), 60) or 'nothing'} has it instead "
             "(a disabled "
             "control, or an element that cannot be focused)")
    if str(probe.get("tag") or "").lower() != str(element.get("tag") or "").lower() \
            or not _reads_as_the_pick(probe.get("active"), element):
        fail(ERR_FOCUS_NOT_VERIFIED,
             _changed_under(element, probe.get("active"),
                            element.get("hit_element") or _pkg._describe(element)))
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
    page = _pkg.Tab.open(tab, browser, for_write=True)
    row, tab_row = page.row, page.tab_row
    with page.session() as session:
        # `PAGE_BOUNDED_CAP`: `CANDIDATES_EXPR` bounds the reply IN THE PAGE —
        # `__CAP__` rows, each with `describe()`/label text already sliced — so
        # this is a page-bounded read and takes the same transport rule as
        # `find`/`text`/`extract` (see `queries.PAGE_BOUNDED_CAP`), rather than
        # the default 64 k cap that a page with a huge `id` would trip
        data = session.evaluate(
            _pkg._match_args(CANDIDATES_EXPR, "", css,
                             cap=str(FIND_MAX_MATCHES)),
            cap=PAGE_BOUNDED_CAP)
        element = _pkg._pick(data if isinstance(data, dict) else {},
                        "", css, index, row=row, tab_row=tab_row,
                        rendered=False)
        handle = session.handle(_pkg._match_args(ELEMENT_EXPR, "", css, index,
                                            visible=False))
        if not handle:
            fail(ERR_NO_MATCH,
                 f"tab upload: {foreign(element.get('tag'), 20)} left the "
                 "document before the file could be set")
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
             f"tab upload: the page holds {foreign(files, 120)!r}, not one "
             f"file named {name!r} of {size} bytes")
    return {"ok": True, "file": file_path,
            "input": {"selector": css, "files": files},
            "tab": f"id:{tab_row['id']}",
            "browser": browser_lib.brief(row)}
