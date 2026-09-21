"""queries — the reading verbs: js, wait, find, text, extract.

Each one resolves a tab, evaluates a page-side expression, and shapes the
answer; nothing here changes the page.
"""
from __future__ import annotations

import json
import time
from typing import Any

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
)
from browser_control.lib.dom.pagedata import (
    _ATTR_NAME,
    _FIELD_NAME,
)
from browser_control.lib.dom.scripts import (
    EXTRACT_EXPR,
    FIND_EXPR,
    TEXT_EXPR,
    WAIT_EXPRS,
    fill,
)
from browser_control.lib.errors import (
    ERR_AMBIGUOUS_ELEMENT,
    ERR_BAD_ARGS,
    ERR_CDP_ERROR,
    ERR_NO_MATCH,
    ERR_WAIT_TIMEOUT,
    fail,
)

TEXT_CAP = 40_000           # chars `text` returns (the PAGE truncates)

FIND_CAP = 10               # elements `find` returns (and click/scroll scan)

EXTRACT_CAP = 10            # records `extract` returns by default

EXTRACT_MAX_MATCHES = 500   # and the most it will return at all

EXTRACT_FIELD_CHARS = 1_000  # chars kept of ONE field (sliced IN the page)

EXTRACT_FIELD_MAX = 20_000  # and the most one field may keep

EXTRACT_TOTAL_CHARS = 20_000  # field text across the whole reply, page-side

WAIT_POLL_S = 0.4           # how often a `wait` samples

WAIT_DEFAULT_S = 15.0

IDLE_DEFAULT_MS = 500

EVAL_TIMEOUT_S = 15.0

def _query_args(text: str | None, selector: str | None,
                verb: str) -> tuple[str, str]:
    """(needle, css) for a verb that takes TEXT or `--selector CSS`."""
    needle = str(text or "").strip()
    css = str(selector or "").strip()
    if bool(needle) == bool(css):
        fail(ERR_BAD_ARGS, f"{verb}: give TEXT or --selector CSS, not both")
    return needle, css

def _matches_in(session: cdp.Session, needle: str, css: str, cap: int) -> dict:
    """The page's answer to the shared matcher (see `FIND_EXPR`)."""
    expression = fill(
        FIND_EXPR, mode=json.dumps("selector" if css else "text"),
        needle=json.dumps(needle.lower()), selector=json.dumps(css),
        cap=str(cap))
    data = session.evaluate(expression)
    if not isinstance(data, dict):
        fail(ERR_CDP_ERROR, "the page did not answer with an object")
    return data

def _pick(data: dict, needle: str, css: str, index: int | None,
          row: dict | None = None, tab_row: dict | None = None) -> dict:
    """The ONE element a click or a reveal acts on.

    Several matches is not a choice this tool makes for the caller: it refuses
    and names them with the index to pass. A refusal over a page that HAS frames
    also says so — the matcher cannot see inside them, and "nothing matches" is
    exactly the claim that must not be made loosely. The caller passes the
    resolution it already holds, and the census runs only when a refusal is
    actually being built.
    """
    rows = _pkg._well_formed(data.get("matches") or [], ("tag", "box", "point"))
    if not rows:
        offscreen = as_int(data.get("offscreen"))
        hint = (f" — {offscreen} candidate(s) are rendered but NOT in the "
                "viewport: `tab scroll TEXT` brings one into view"
                if offscreen else "")
        fail(ERR_NO_MATCH,
             f"no rendered element matches {needle or css!r} on "
             f"{str(data.get('title'))!r}{hint}"
             + _pkg._frames_note(row, tab_row))
    if index is None:
        if len(rows) > 1:
            where = "; ".join(f"[{i}] {_pkg._describe(candidate)}"
                              for i, candidate in enumerate(rows[:5]))
            fail(ERR_AMBIGUOUS_ELEMENT,
                 f"{len(rows)} elements match {needle or css!r} — pick one "
                 f"with --index N: {where}"
                 + _pkg._frames_note(row, tab_row))
        index = 0
    if not 0 <= index < len(rows):
        fail(ERR_BAD_ARGS,
             f"--index {index} is out of range: {len(rows)} element(s) match")
    return rows[index]

def _match_args(expression: str, needle: str, css: str,
                index: int | None = None, visible: bool = True,
                **extra: Any) -> str:
    """Fill the placeholders every matcher expression shares.

    `extra` carries the tokens only SOME matchers have (`__CAP__`, `__X__`/
    `__Y__`, `__VALUE__`): the strict `fill` refuses an unknown name and a
    leftover, so a caller must declare everything its expression holds.
    """
    values: dict = {"mode": json.dumps("selector" if css else "text"),
                    "needle": json.dumps(needle.lower()),
                    "selector": json.dumps(css)}
    if "__VISIBLE__" in expression:
        values["visible"] = "true" if visible else "false"
    if "__INDEX__" in expression:
        values["index"] = str(as_int(index))
    values.update(extra)
    return fill(expression, **values)

def _node_of(session: cdp.Session, expression: str) -> int:
    """The DOM node an element-returning expression resolves to.

    Without `returnByValue` the element comes back as a REMOTE OBJECT that
    lives as long as the session, and `DOM.requestNode` turns it into the
    nodeId a DOM method takes — which is how focus and reveal act without a
    line of JavaScript.
    """
    handle = session.handle(expression)
    if not handle:
        return 0
    session.call("DOM.getDocument", {"depth": 0})
    node = session.call("DOM.requestNode", {"objectId": handle})
    return as_int(node.get("nodeId"))

def js(expression: str, tab: str = "", browser: str = "") -> dict:
    """`tab js`: the page's own answer — capped, and NOT verified.

    This is the escape hatch, and it can WRITE (it is resolved like one, so it
    needs a browser this CLI manages or has attached). The reply says
    `verified: false` rather than pretending the value means something, and
    the value is the page's OWN (`raw=True`): a string that parses as JSON is
    still that string, where this CLI's internal probes stringify on purpose
    and keep the decode. Structure still arrives — `returnByValue` serializes
    an object or array expression itself, so `({a: 1})` answers an object
    while `JSON.stringify({a: 1})` answers its string (a review measured the
    ambiguity: `localStorage.getItem('k')` holding "null" answered null).
    """
    expr = str(expression or "").strip()
    if not expr:
        fail(ERR_BAD_ARGS, "tab js: an EXPRESSION is required")
    page = _pkg.Tab.open(tab, browser, for_write=True)
    row, tab_row = page.row, page.tab_row
    with page.session() as session:
        value = session.evaluate(expr, raw=True)
    return {"ok": True, "tab": f"id:{tab_row['id']}", "value": value,
            "verified": False, "note": "evaluated, not interpreted",
            "browser": browser_lib.brief(row)}

def wait(mode: str, selector: str | None = None, expr: str | None = None,
         timeout: float = WAIT_DEFAULT_S, idle_ms: int = IDLE_DEFAULT_MS,
         tab: str = "", browser: str = "") -> dict:
    """`tab wait`: poll ONE predicate to a wall-clock deadline.

    One budget for the whole wait (`cdp.evaluate_until` keeps ONE connection),
    samples bounded by what is left of it, and a refusal that names what it
    waited for and how long. `--for js` runs caller code, so that mode is
    resolved like a write.
    """
    name = _pkg.mode_of(mode)
    if name not in WAIT_EXPRS:
        fail(ERR_BAD_ARGS,
             f"tab wait: --for is load|idle|element|js, got {mode!r}")
    if name == "element" and not selector:
        fail(ERR_BAD_ARGS, "tab wait: --for element needs --selector CSS")
    if name == "js" and not expr:
        fail(ERR_BAD_ARGS, "tab wait: --for js needs --expr EXPRESSION")
    if name != "element" and selector:
        fail(ERR_BAD_ARGS,
             f"tab wait: --selector is only for --for element (not {name})")
    if name != "js" and expr:
        fail(ERR_BAD_ARGS, f"tab wait: --expr is only for --for js (not {name})")
    try:
        seconds = float(timeout)
    except (TypeError, ValueError):
        fail(ERR_BAD_ARGS, f"tab wait: --timeout needs a number, got {timeout!r}")
    if not 0 < seconds < 3600 or seconds != seconds:
        fail(ERR_BAD_ARGS,
             f"tab wait: --timeout must be finite and positive, got {timeout!r}")
    row, tab_row = _pkg._resolve(tab, browser, for_write=(name == "js"))
    profile, target_id = str(row["profile"]), str(tab_row["id"])
    template = WAIT_EXPRS[name]
    values: dict = {}
    if "__SELECTOR__" in template:
        values["selector"] = json.dumps(str(selector or ""))
    if "__EXPR__" in template:
        values["expr"] = str(expr or "false")
    if "__IDLE_MS__" in template:
        values["idle_ms"] = str(as_int(idle_ms, IDLE_DEFAULT_MS))
    expression = fill(template, **values)
    started = time.time()
    value, samples = cdp.evaluate_until(
        _pkg._document_ws(cdp.port_of(profile), target_id), expression, bool,
        seconds, WAIT_POLL_S)
    if value == "thenable":
        # measured: `Boolean(promise)` is TRUE the moment the promise is made,
        # so an async predicate passed before anything settled (a review found
        # it) — the expression reports the thenable instead of coercing it
        fail(ERR_BAD_ARGS,
             "tab wait --for js: the expression returned a Promise — a wait "
             "polls a BOOLEAN, and a Promise is truthy the moment it is made, "
             "so the wait would pass before anything settled (await it in the "
             "page, or set a flag and poll that)")
    if not value:
        what = selector or expr or ""
        fail(ERR_WAIT_TIMEOUT,
             f"tab wait --for {name}"
             + (f" {what!r}" if what else "")
             + f" did not pass within {seconds:g}s ({samples} samples)")
    return {"ok": True, "for": name, "waited_s": round(time.time() - started, 1),
            "samples": samples, "tab": f"id:{target_id}",
            "browser": browser_lib.brief(row)}

def _extract_field(spec: str, verb: str = "tab extract") -> tuple[str, dict]:
    """One `NAME=SELECTOR[@ATTR]` field spec, parsed and validated.

    `@ATTR` at the end names an attribute; with nothing before it (`name=@href`)
    the attribute is read from the MATCH element itself, and `:scope` as the
    selector reads the match's own text. A selector may legitimately contain
    `@` (`[href*="@"]`), so the tail only counts as an attribute when it looks
    like one.
    """
    text = str(spec or "")
    name, sep, value = text.partition("=")
    name = name.strip()
    if not sep or not name:
        fail(ERR_BAD_ARGS,
             f"{verb}: a field is NAME=SPEC (e.g. "
             "`--field text=[data-testid=tweetText]` or "
             f"`--field time=time@datetime`) — got {spec!r}")
    if not _FIELD_NAME.match(name):
        fail(ERR_BAD_ARGS,
             f"{verb}: {name!r} is not a field name (letters, digits, `_` and "
             "`-`, starting with a letter or `_`)")
    sel = value.strip()
    attr_name = ""
    head, at, tail = sel.rpartition("@")
    if at and _ATTR_NAME.match(tail.strip()):
        sel, attr_name = head.strip(), tail.strip()
    if not sel and not attr_name:
        fail(ERR_BAD_ARGS,
             f"{verb}: {spec!r} names neither a selector nor an attribute — "
             "use `name=SELECTOR`, `name=SELECTOR@attr` or `name=@attr`")
    return name, {"sel": sel, "attr": attr_name}

def _extract_schema(each: str, fields: list[str], cap: int = EXTRACT_CAP,
                    chars: int = EXTRACT_FIELD_CHARS, visible: bool = False,
                    verb: str = "tab extract") -> dict:
    """The JSON schema one extraction runs: parsed, validated, bounded."""
    selector = str(each or "").strip()
    if not selector:
        fail(ERR_BAD_ARGS,
             f"{verb}: --each is required — the CSS selector of the repeated "
             "item (e.g. --each article)")
    if not fields:
        fail(ERR_BAD_ARGS, f"{verb}: at least one --field NAME=SPEC is required")
    parsed: dict[str, dict] = {}
    for spec in fields:
        name, field = _extract_field(spec, verb)
        parsed[name] = field
    limit = as_int(cap, EXTRACT_CAP)
    if limit < 1:
        fail(ERR_BAD_ARGS, f"{verb}: --cap must be at least 1")
    keep = as_int(chars, EXTRACT_FIELD_CHARS)
    if keep < 1:
        fail(ERR_BAD_ARGS, f"{verb}: --chars must be at least 1")
    return {"each": selector, "fields": parsed,
            "cap": min(limit, EXTRACT_MAX_MATCHES),
            "chars": min(keep, EXTRACT_FIELD_MAX),
            "visible": bool(visible), "budget": EXTRACT_TOTAL_CHARS}

def _extract_records(rows: object, names: list[str]) -> list[dict]:
    """The record list a page answered, filtered to shape.

    A row that is not an object, or a value that is not a string or null, is
    DROPPED rather than raised: the page owns every value on this path, the
    same rule `find` follows for its rows.
    """
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({name: (row.get(name)
                           if isinstance(row.get(name), str) else None)
                    for name in names})
    return out

def extract(each: str = "", fields: list[str] | None = None,
            cap: int = EXTRACT_CAP, chars: int = EXTRACT_FIELD_CHARS,
            visible: bool = False, unique: str = "",
            tab: str = "", browser: str = "") -> dict:
    """`tab extract`: the page's repeated items as records, declaratively.

    A READ: no caller code, no writes. `--each SELECTOR` names the repeated
    element; each `--field NAME=SELECTOR[@ATTR]` names one value inside it —
    innerText by default, an attribute with `@ATTR`, `@ATTR` alone for the
    match itself, `:scope` for the match's own text. Values are sliced IN THE
    PAGE (`--chars`) and the whole answer is budgeted page-side, so a document
    cannot flood the reply; `--visible` skips what the page does not render and
    `--unique FIELD` keeps the first of duplicates.

    Everything here is the PAGE's own answer, exactly like `find` and `text`:
    the selector language is CSS, the values are DOM text/attributes, and no
    claim beyond "this is what the page showed" is made. `total` counts the
    matches the selector reached; `truncated` says the cap or the budget cut
    the list.
    """
    schema = _extract_schema(each, list(fields or []), cap, chars, visible)
    names = list(schema["fields"])
    wanted = str(unique or "").strip()
    if wanted and wanted not in names:
        fail(ERR_BAD_ARGS,
             f"tab extract: --unique {wanted!r} is not one of the fields "
             f"({', '.join(names)})")
    page = _pkg.Tab.open(tab, browser, for_write=False)
    row, tab_row = page.row, page.tab_row
    with page.session() as session:
        data = session.evaluate(
            fill(EXTRACT_EXPR, schema=json.dumps(schema)))
    data = data if isinstance(data, dict) else {}
    raw = _extract_records(data.get("matches"), names)
    records = raw
    if wanted:
        seen: set = set()
        records = []
        for record in raw:
            value = record.get(wanted)
            if value in seen:
                continue
            seen.add(value)
            records.append(record)
    reply = _pkg._reply(row, tab_row, data)
    reply.update({"ok": True, "each": schema["each"], "fields": names,
                  "count": len(records), "total": as_int(data.get("total")),
                  "truncated": (bool(data.get("truncated"))
                                or len(records) < len(raw)),
                  "matches": records})
    return _pkg._with_frame(reply)

def find(text: str | None = None, selector: str | None = None,
         cap: int = FIND_CAP, tab: str = "", browser: str = "") -> dict:
    """`tab find`: resolve a human target to elements, with their geometry.

    Only interactive or labelled elements match; each match carries its box in
    PAGE coordinates and in the viewport's, whether it is `in_viewport`, and
    whether a click at its centre would reach it (`hit` — a real
    the element-at-point probe). An element below the fold is RETURNED
    `in_viewport: false`, not hidden behind `no-match`: the caller can see that
    it exists and scroll to it.
    """
    needle, css = _query_args(text, selector, "tab find")
    limit = max(1, as_int(cap, FIND_CAP))
    page = _pkg.Tab.open(tab, browser, for_write=False)
    row, tab_row = page.row, page.tab_row
    with page.session() as session:
        data = _pkg._matches_in(session, needle, css, limit)
        # the census rides the session already open for the cheap case (a page
        # with no frames needs no second connection), and goes to the PAGE when
        # `--frame` scoped that session to a frame
        frames_here = _pkg._frame_summary(row, tab_row, session)
    viewport = _pkg._viewport(data, str(tab_row["id"]))
    matches = [dict(m, box=as_ints(m["box"]),
                    center=as_ints(m.get("center")),
                    viewport=as_ints(m.get("viewport")),
                    point=as_ints(m.get("point")))
               for m in _pkg._well_formed(data.get("matches") or [],
                                     ("tag", "box"))]
    if not matches:
        offscreen = as_int(data.get("offscreen"))
        fail(ERR_NO_MATCH,
             f"no rendered element matches {needle or css!r} on "
             f"{str(data.get('title'))!r} (readyState {data.get('ready')!r}, "
             f"{as_int(data.get('total'))} candidate(s)"
             + (f", {offscreen} offscreen" if offscreen else "") + ")"
             + _pkg._frames_note(row, tab_row))
    reply = _pkg._reply(row, tab_row, data)
    reply.update({"ok": True, "query": needle or css, "viewport": viewport,
                  "total": as_int(data.get("total")),
                  "offscreen": as_int(data.get("offscreen")),
                  "truncated": bool(data.get("truncated")),
                  "matches": matches})
    if frames_here:
        reply["frames"] = frames_here
    return _pkg._with_frame(reply)

def text(selector: str | None = None, chars: int = TEXT_CAP, tab: str = "",
         browser: str = "") -> dict:
    """`tab text`: the rendered text, truncated IN THE PAGE.

    The cap is applied inside the page, so the reply stays bounded however
    large the document is (and `length` still reports the full size, which is
    how a caller sees what it did not get).
    """
    css = str(selector or "").strip()
    limit = max(1, min(TEXT_CAP, as_int(chars, TEXT_CAP)))
    page = _pkg.Tab.open(tab, browser, for_write=False)
    row, tab_row = page.row, page.tab_row
    with page.session() as session:
        data = session.evaluate(
            fill(TEXT_EXPR, selector=json.dumps(css), cap=str(limit)))
        frames_here = _pkg._frame_summary(row, tab_row, session)
    if not isinstance(data, dict):
        fail(ERR_CDP_ERROR, "tab text: the page did not answer with an object")
    if not data.get("found"):
        fail(ERR_NO_MATCH, f"tab text: no element matches {css!r}"
             + _pkg._frames_note(row, tab_row))
    reply = _pkg._reply(row, tab_row, data)
    reply.update({"ok": True, "selector": data.get("selector"),
                  "viewport": as_ints(data.get("viewport")),
                  "text": str(data.get("text") or ""),
                  "length": as_int(data.get("length")),
                  "truncated": bool(data.get("truncated"))})
    if frames_here:
        reply["frames"] = frames_here
    return _pkg._with_frame(reply)
