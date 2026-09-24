"""queries — the reading verbs: js, wait, find, text, extract.

Each one resolves a tab, evaluates a page-side expression, and shapes the
answer; nothing here changes the page.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any

from browser_control.lib import (
    browser as browser_lib,
)
from browser_control.lib import (
    cdp,
)
from browser_control.lib import dom as _pkg
from browser_control.lib.browser.machine import (
    row_port,
)
from browser_control.lib.cdp.rpc import (
    UNDEFINED,
)
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
from browser_control.lib.text import (
    foreign,
)

TEXT_CAP = 40_000           # chars `text` returns (the PAGE truncates)

FIND_CAP = 10               # elements `find` returns (and click/scroll scan)

# and the most it will scan at all: each match carries its geometry, and the
# page answers with one JSON document capped at 64k by the CDP layer, so a
# larger `--cap` refused `result-too-large` telling the caller to "narrow the
# expression" they never wrote (a review flagged it). Sized to stay under
# that cap; a caller that needs more reads with `tab extract`/`tab text`.
FIND_MAX_MATCHES = 100

EXTRACT_CAP = 10            # records `extract` returns by default

EXTRACT_MAX_MATCHES = 500   # and the most it will return at all

#: The most `--field` flags ONE extraction may name. Every field is a selector
#: walk per record, and a record's JSON is `#fields × --chars`, so an unbounded
#: field list is an unbounded row — one that used to ride over the whole-reply
#: budget and blow the CDP transport cap (a review found it). 32 is far past
#: any table this verb is for.
EXTRACT_MAX_FIELDS = 32

EXTRACT_FIELD_CHARS = 1_000  # chars kept of ONE field (sliced IN the page)

EXTRACT_FIELD_MAX = 20_000  # and the most one field may keep

EXTRACT_TOTAL_CHARS = 20_000  # field text across the whole reply, page-side

#: THE TRANSPORT RULE this module's reads follow: an expression that bounds its
#: OWN answer IN THE PAGE — `--cap`, `--chars`, the extraction budget, and the
#: reply-base fields a page supplies (`title`/`url`, sliced in the page by
#: `scripts.py`) — is evaluated with `cap=None`. `None` is the one spelling of
#: "no post-transfer size refusal", and it is what the page-bounded reads need:
#: JSON escaping inflates the wire value far past the text the page produced
#: (`\uXXXX` is six wire characters for one character of text), so the finite
#: cap refused `result-too-large` over an expression the caller never wrote (a
#: review measured a 2 MB `document.title` riding out through the reply base
#: once the cap was lifted without the page-side slice).
#:
#: Every read that is NOT page-bounded keeps the transport's default 64 k cap:
#: `tab js` is the caller's OWN expression, and a probe's answer is the page's,
#: so for those the size refusal is the only thing between a page and this
#: tool's reply (`rpc.TRANSPORT_CAP`, 16 MiB, is the backstop under both).
#: The rule is applied at every call site below, and at `upload`'s candidate
#: scan in `actions.py` — one rule, not a judgement per verb.
#:
#: What the page's bound actually covers: the reply base (`title` ≤ 300, `url`
#: ≤ 2000, sliced by `scripts.py`) and every field an expression slices
#: (`--chars`, a row's text, the extraction budget). A match ROW still carries
#: the page's own attributes (`href`, `aria-label`) and `describe()`'s `#id`,
#: so a page with megabyte-long attributes can still make a large reply — the
#: 16 MiB frame cap is what refuses that, and slicing those fields belongs to
#: `scripts.py`, not here.
PAGE_BOUNDED_CAP = None

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
        # NEITHER given is a different mistake from BOTH given: one message
        # ("not both") sent the caller looking for a conflict that did not
        # exist, against the promise that every refusal names its cause (a
        # review flagged it)
        if not needle:
            fail(ERR_BAD_ARGS, f"{verb}: give TEXT or --selector CSS")
        fail(ERR_BAD_ARGS, f"{verb}: give TEXT or --selector CSS, not both")
    return needle, css

def _matches_in(session: cdp.Session, needle: str, css: str, cap: int) -> dict:
    """The page's answer to the shared matcher (see `FIND_EXPR`).

    `PAGE_BOUNDED_CAP` on the way out (the transport rule above): every caller
    of this matcher bounds the reply IN THE PAGE — `__CAP__` caps the rows, and
    the reply base's `title`/`url` are sliced there too — so the transport's
    post-transfer size refusal has nothing left to catch: it only fired when
    JSON escaping inflated a page-capped answer, refusing `result-too-large`
    over an expression the caller never wrote (a review found it).
    """
    expression = fill(
        FIND_EXPR, mode=json.dumps("selector" if css else "text"),
        needle=json.dumps(needle.lower()), selector=json.dumps(css),
        cap=str(cap))
    data = session.evaluate(expression, cap=PAGE_BOUNDED_CAP)
    if not isinstance(data, dict):
        fail(ERR_CDP_ERROR, "the page did not answer with an object")
    return data

def _pick(data: dict, needle: str, css: str, index: int | None,
          row: dict | None = None, tab_row: dict | None = None,
          rendered: bool = True) -> dict:
    """The ONE element a click or a reveal acts on.

    Several matches is not a choice this tool makes for the caller: it refuses
    and names them with the index to pass. A refusal over a page that HAS frames
    also says so — the matcher cannot see inside them, and "nothing matches" is
    exactly the claim that must not be made loosely. The caller passes the
    resolution it already holds, and the census runs only when a refusal is
    actually being built.

    `rendered` says whether the SCAN behind `data` filtered for what the page
    actually draws: the shared matcher does, `upload`'s candidate scan does not
    (a hidden file input is the normal case), so the no-match sentence must not
    claim a filter that was never applied (a review found the claim on a scan
    that has none).
    """
    rows = _pkg._well_formed(data.get("matches") or [], ("tag", "box", "point"))
    if not rows:
        offscreen = as_int(data.get("offscreen"))
        hint = (f" — {offscreen} candidate(s) are rendered but NOT in the "
                "viewport: `tab scroll TEXT` brings one into view"
                if offscreen else "")
        what = ("no rendered element matches" if rendered
                else "no element matches")
        fail(ERR_NO_MATCH,
             f"{what} {needle or css!r} on "
             f"{foreign(data.get('title'), 60)!r}{hint}"
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
        # say how many the page HAS as well as how many this scan resolved: a
        # count that contradicts what `tab find --cap` just printed is how a
        # caller was told "10 element(s) match" about a 50-match page (a review
        # flagged it), and a capped scan must not read as "there is no more".
        # The wording stays VERB-NEUTRAL — "N element(s)", never "N rendered
        # element(s)" and never a claim about the viewport: `upload` picks
        # through `CANDIDATES_EXPR`, whose scan has no rendered/visibility
        # filter at all, so a message promising one described a filter that was
        # never applied (a review found it)
        total = as_int(data.get("total"), len(rows))
        if total > len(rows):
            fail(ERR_BAD_ARGS,
                 f"--index {index} is out of range: this scan resolved "
                 f"{len(rows)} element(s) of the {total} that match "
                 f"(cap {as_int(data.get('cap'), len(rows))}) — narrow the "
                 "selector to name one")
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

    This read is NOT page-bounded — the expression is the caller's own and the
    page was never asked to bound its answer — so it keeps the transport's
    default cap (the rule is stated with `PAGE_BOUNDED_CAP` above). The page's
    `undefined` is its OWN value and is reported as such: `rpc._value_of`
    answers the `UNDEFINED` sentinel for it, and a sentinel that is not JSON
    would break the one-object reply, so it rides as `value: null` PLUS
    `value_type: "undefined"` — every other value keeps today's exact shape
    (`{"value": value}`), and `null` stays `{"value": null}` (a review found
    the two indistinguishable).
    """
    expr = str(expression or "").strip()
    if not expr:
        fail(ERR_BAD_ARGS, "tab js: an EXPRESSION is required")
    page = _pkg.Tab.open(tab, browser, for_write=True)
    row, tab_row = page.row, page.tab_row
    with page.session() as session:
        value = session.evaluate(expr, raw=True)
    reply = {"ok": True, "tab": f"id:{tab_row['id']}", "value": value,
             "verified": False, "note": "evaluated, not interpreted",
             "browser": browser_lib.brief(row)}
    if value is UNDEFINED:
        reply["value"] = None
        reply["value_type"] = "undefined"
    return reply

def _idle_window(value: object) -> int:
    """`--idle-ms` as a window of whole milliseconds, judged as the CALLER
    spelled it.

    The window is validated BEFORE `as_int`, which truncates toward zero:
    `-0.5` and `-1e-9` became 0, and the idle predicate is `now - responseEnd <
    __IDLE_MS__` — a negative window is never true, so `--for idle` passed on
    its FIRST sample over a page that was still loading (a review found it);
    a non-numeric value quietly became the 500 ms default instead of a
    refusal. `0` is a real window ("no quiet period at all") and stays valid.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        fail(ERR_BAD_ARGS, _idle_offence(value))
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        fail(ERR_BAD_ARGS, _idle_offence(value))
    if not math.isfinite(number) or number < 0:
        fail(ERR_BAD_ARGS, _idle_offence(value))
    return int(number)

def _idle_offence(value: object) -> str:
    """The one refusal `_idle_window` makes, naming the caller's own value."""
    return ("tab wait: --idle-ms is a window in milliseconds, so it must be a "
            f"number 0 or more, got {foreign(value, 60)!r}")

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
    # a NEGATIVE idle window INVERTS the predicate (`now - ended < -N` is never
    # true, so `--for idle` passed on its first sample), which is the opposite
    # of what the caller asked for — a refusal, never a silent meaning
    idle = _idle_window(idle_ms)
    row, tab_row = _pkg._resolve(tab, browser, for_write=(name == "js"))
    profile, target_id = str(row["profile"]), str(tab_row["id"])
    template = WAIT_EXPRS[name]
    values: dict = {}
    if "__SELECTOR__" in template:
        values["selector"] = json.dumps(str(selector or ""))
    if "__EXPR__" in template:
        values["expr"] = str(expr or "false")
    if "__IDLE_MS__" in template:
        values["idle_ms"] = str(idle)
    expression = fill(template, **values)
    # MONOTONIC, like the deadline it is measured against: a wall-clock step
    # mid-wait made `waited_s` disagree with the budget that was enforced
    # (`lib/poll.py` documents why the deadline is monotonic — a review found
    # this reply still reading `time.time()`)
    started = time.monotonic()
    value, samples = cdp.evaluate_until(
        _pkg._document_ws(row_port(row) or as_int(cdp.port_of(profile)),
                          target_id), expression, bool,
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
    return {"ok": True, "for": name,
            "waited_s": round(time.monotonic() - started, 1),
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
    if len(fields) > EXTRACT_MAX_FIELDS:
        fail(ERR_BAD_ARGS,
             f"{verb}: {len(fields)} --field flags is more than this verb "
             f"takes (max {EXTRACT_MAX_FIELDS}) — one record carries every "
             "field, so the row is `fields × --chars`")
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
    page = _pkg.Tab.open(tab, browser, for_write=False, same_process=True)
    row, tab_row = page.row, page.tab_row
    with page.session() as session:
        # `PAGE_BOUNDED_CAP` (the transport rule above): the whole reply is
        # budgeted IN THE PAGE (`--chars`, the schema's `budget`, and the
        # reply base's `url`/`title`, sliced by `scripts.py`), so the
        # transport's size refusal has nothing to add — it only fired when
        # JSON escaping inflated a page-bounded answer (a review found it)
        data = session.evaluate(
            fill(EXTRACT_EXPR, schema=json.dumps(schema), root=page.root()),
            cap=PAGE_BOUNDED_CAP)
        # the frame census rides the session already open, exactly as find and
        # text attach it: without it a framed page answered `count: 0` with no
        # hint, the one shape that reads as "there is nothing there" (a review
        # found extract omitted the helper its sibling reads carry)
        frames_here = _pkg._frame_summary(row, tab_row, session)
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
    if frames_here:
        reply["frames"] = frames_here
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
    limit = as_int(cap, FIND_CAP)
    if limit < 1:
        fail(ERR_BAD_ARGS, "tab find: --cap must be at least 1")
    limit = min(limit, FIND_MAX_MATCHES)
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
        # the page's own words ride into the refusal, so they are flattened and
        # capped like every sibling: a 2 MB `document.title` made a 2 000 000
        # character stderr line (a review found it)
        fail(ERR_NO_MATCH,
             f"no rendered element matches {needle or css!r} on "
             f"{foreign(data.get('title'), 60)!r} (readyState "
             f"{foreign(data.get('ready'), 20)!r}, "
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
    limit = as_int(chars, TEXT_CAP)
    if limit < 1:
        fail(ERR_BAD_ARGS, "tab text: --chars must be at least 1")
    limit = min(limit, TEXT_CAP)
    page = _pkg.Tab.open(tab, browser, for_write=False, same_process=True)
    row, tab_row = page.row, page.tab_row
    with page.session() as session:
        # `PAGE_BOUNDED_CAP` (the transport rule above): `--chars` truncates IN
        # THE PAGE and so does the reply base's `title`, so the reply is
        # already bounded by the caller's own number — the transport cap only
        # refused pages whose text is escape-dense (a review found it)
        data = session.evaluate(
            fill(TEXT_EXPR, selector=json.dumps(css), cap=str(limit),
                 root=page.root()), cap=PAGE_BOUNDED_CAP)
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
