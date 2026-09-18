"""dom — what a page READS and ACTS like: js, wait, find, text, click, scroll.

The tier above `lib.browser`: every verb resolves ONE tab through the same
rule as the other verbs (`browser._one_tab`), and then talks to that tab —
through `cdp.Session`, ONE connection for the whole verb, because these verbs
are sequences (a click is a move, a press, a release and a read; scrolling to
an edge is a wheel and a read per step).

The DOM and `JSON.stringify` are the page's to override, so everything
crossing `Runtime.evaluate` is DATA — a malformed row is dropped, never raised
out of a verb.

What the verbs are:

* `js`     — the escape hatch: whatever the page answers, capped, DECLARED
              unverified (the caller owns the meaning). It can write.
* `wait`   — a poll for one predicate with one wall-clock budget. It proves the
              predicate passed, never that anything followed from it.
* `find`   — a human target resolved to elements and their geometry, in PAGE
              and viewport coordinates, each with `in_viewport` and a real
              hit-test (`hit`).
* `text`   — the rendered text, truncated IN THE PAGE.
* `click`  — REAL input (`Input.dispatchMouseEvent`) at an element's viewport
              centre: the same press a finger makes, so handlers that ignore
              `element.click()` (untrusted) take it.
* `scroll` — REAL wheel input (`Input.dispatchMouseEvent` `mouseWheel`), which
              reaches nested scrollers and fires lazy loading, or
              `DOM.scrollIntoViewIfNeeded` — a CDP method, not a line of
              JavaScript — to bring one element into view.

Contracts worth stating because they were measured, not assumed:

* **A read never activates a tab.** A background tab has a viewport, a layout
  and a working hit-test (measured 2026-09-18), so geometry is available
  without moving the user's desktop. A tab with no viewport refuses
  `no-viewport`.
* **A wheel's effect is ASYNCHRONOUS** (measured: `scrollY` 0 → 600 between
  0.0 s and 0.2 s after the event), so scrolling verifies by polling, the way
  `nav` waits for the move before the load.
* **CDP input is trusted, `element.click()` is not** (measured side by side):
  that is why this tier clicks with `Input.*` and never with JavaScript.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

# The private helpers below are this layer's contract with lib.browser: the
# tab resolution, one evaluation on that tab, and the browser block every
# reply carries. They are private because no other module needs them.
from browser_control.lib import audit  # pyright: ignore[reportMissingImports]
from browser_control.lib import browser as tabs  # pyright: ignore[reportMissingImports]
from browser_control.lib import cdp  # pyright: ignore[reportMissingImports]
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ControlError,
    fail,
)

TEXT_CAP = 40_000           # chars `text` returns (the PAGE truncates)
FIND_CAP = 10               # elements `find` returns (and click/scroll scan)
WAIT_POLL_S = 0.4           # how often a `wait` samples
WAIT_DEFAULT_S = 15.0
IDLE_DEFAULT_MS = 500
EVAL_TIMEOUT_S = 15.0
SCROLL_EDGE_STEP = 2500     # one wheel notch when scrolling to an edge
SCROLL_EDGE_STEPS = 12      # and how many of them an edge is worth
SCROLL_MOVE_S = 4.0         # how long one wheel is given to move something
TYPE_PAUSE_S = 0.008        # between keystrokes: the page's handlers need air

# The keys `press` can send, and the exact (key, code, virtual key code, text)
# a CDP key event needs. A wrong triple SILENTLY does nothing in the page, so
# a free-form key string is not the honest surface: an unknown name refuses
# and names the table. Only Enter and Space carry text (a keyDown without text
# does not submit a form).
KEYS = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, ""),
    "escape": ("Escape", "Escape", 27, ""),
    "backspace": ("Backspace", "Backspace", 8, ""),
    "delete": ("Delete", "Delete", 46, ""),
    "space": (" ", "Space", 32, " "),
    "arrowup": ("ArrowUp", "ArrowUp", 38, ""),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, ""),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, ""),
    "arrowright": ("ArrowRight", "ArrowRight", 39, ""),
    "home": ("Home", "Home", 36, ""),
    "end": ("End", "End", 35, ""),
    "pageup": ("PageUp", "PageUp", 33, ""),
    "pagedown": ("PageDown", "PageDown", 34, ""),
}

# One prelude for every verb that looks at the page: the element set, the
# label, the role, the geometry, the hit-test, a SHADOW-PIERCING query, and a
# one-line description of an element. Sharing it is what keeps `find`, `click`,
# `wait --for element` and `scroll TEXT` from disagreeing about what an
# element IS.
PRELUDE = r"""
  const INTERACTIVE = 'a,button,input,textarea,select,summary,label,' +
    '[role],[contenteditable="true"],[tabindex],h1,h2,h3,h4,h5,h6';
  const roots = (root, out) => {
    out.push(root);
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) roots(el.shadowRoot, out);
    }
    return out;
  };
  const query = (selector) => {
    const out = [];
    for (const root of roots(document, [])) {
      for (const el of root.querySelectorAll(selector)) out.push(el);
    }
    return out;
  };
  const attr = (el, name) => (el.getAttribute ? el.getAttribute(name) : null);
  const textOf = (el) =>
    (el.innerText === undefined ? el.textContent : el.innerText) || '';
  const label = (el) => [
      attr(el, 'aria-label'), attr(el, 'placeholder'), attr(el, 'title'),
      attr(el, 'alt'), attr(el, 'name'), el.value, textOf(el),
    ].filter((v) => typeof v === 'string' && v.trim() !== '')
     .join(' ').replace(/\s+/g, ' ').trim();
  const role = (el) => attr(el, 'role') || el.tagName.toLowerCase();
  const describe = (el) => el
    ? role(el) + (attr(el, 'id') ? '#' + attr(el, 'id') : '')
      + (label(el) ? ' ' + label(el).slice(0, 40) : '')
    : null;
  // A STABLE identity for an element: tag, id and its place in the tree — no
  // value, no innerText, so typing into it does not change it (which is what
  // makes it usable for "is this still the same field?").
  const path = (el) => {
    if (!el) return null;
    const chain = [];
    for (let n = el; n && n.parentElement; n = n.parentElement) {
      chain.push(Array.prototype.indexOf.call(n.parentElement.children, n));
    }
    return role(el) + '#' + (attr(el, 'id') || '') + '@' + chain.join('.');
  };
  const rendered = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return null;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' ||
        s.opacity === '0') return null;
    return r;
  };
  const geometry = (el, vw, vh) => {
    const r = rendered(el);
    if (!r) return null;
    const left = Math.max(r.left, 0), top = Math.max(r.top, 0);
    const right = Math.min(r.right, vw), bottom = Math.min(r.bottom, vh);
    const inViewport = right - left > 1 && bottom - top > 1;
    return {r: r, inViewport: inViewport,
            clipped: inViewport && (left !== r.left || top !== r.top ||
                                    right !== r.right || bottom !== r.bottom)};
  };
  const hitAt = (el, cx, cy, vw, vh) => {
    const x = Math.min(Math.max(cx, 1), Math.max(1, vw - 1));
    const y = Math.min(Math.max(cy, 1), Math.max(1, vh - 1));
    const hit = document.elementFromPoint(x, y);
    const ok = !!hit && (hit === el || el.contains(hit) ||
      !!(hit.shadowRoot && hit.shadowRoot.contains(el)));
    return {ok: ok, at: [Math.round(x), Math.round(y)],
            what: hit ? describe(hit) : null};
  };
"""

FIND_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const vw = window.innerWidth, vh = window.innerHeight;
  const base = {url: location.href, title: document.title,
                ready: document.readyState,
                visibility: document.visibilityState, viewport: [vw, vh],
                active: describe(document.activeElement),
                scroll: [Math.round(scrollX), Math.round(scrollY)]};
  if (vw <= 0 || vh <= 0) {
    return Object.assign(base, {matches: [], total: 0, offscreen: 0,
                                truncated: false, degenerate: true});
  }
  const mode = __MODE__, needle = __NEEDLE__, selector = __SELECTOR__,
        cap = __CAP__;
  const all = mode === 'selector' ? query(selector)
    : query(INTERACTIVE).filter(
        (el) => label(el).toLowerCase().indexOf(needle) >= 0);
  const out = [];
  let offscreen = 0;
  for (const el of all) {
    const g = geometry(el, vw, vh);
    if (!g) continue;                       // not rendered: not a target
    if (!g.inViewport) offscreen++;
    if (out.length >= cap) continue;
    const r = g.r;
    const hit = g.inViewport ? hitAt(el, r.left + r.width / 2,
                                     r.top + r.height / 2, vw, vh)
                             : {ok: false, at: null, what: null};
    out.push({tag: el.tagName.toLowerCase(), role: role(el),
              name: attr(el, 'aria-label') || attr(el, 'name') || null,
              type: attr(el, 'type'), href: el.href || null,
              text: label(el).slice(0, 120),
              in_viewport: g.inViewport, clipped: g.clipped,
              box: [Math.round(r.left + scrollX), Math.round(r.top + scrollY),
                    Math.round(r.width), Math.round(r.height)],
              center: [Math.round(r.left + scrollX + r.width / 2),
                       Math.round(r.top + scrollY + r.height / 2)],
              viewport: [Math.round(r.left), Math.round(r.top),
                         Math.round(r.width), Math.round(r.height)],
              point: [Math.round(r.left + r.width / 2),
                      Math.round(r.top + r.height / 2)],
              hit: hit.ok, hit_element: hit.what, hit_at: hit.at});
  }
  return Object.assign(base, {matches: out, total: all.length, offscreen: offscreen,
                              truncated: all.length > out.length,
                              degenerate: false});
})())""")

TEXT_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const selector = __SELECTOR__, cap = __CAP__;
  const base = {url: location.href, title: document.title,
                ready: document.readyState,
                visibility: document.visibilityState,
                viewport: [window.innerWidth, window.innerHeight],
                selector: selector || 'body'};
  const el = selector ? query(selector)[0] : document.body;
  if (!el) {
    return Object.assign(base, {found: false, text: '', length: 0,
                                truncated: false});
  }
  const full = textOf(el);
  return Object.assign(base, {found: true, text: full.slice(0, cap),
                              length: full.length, truncated: full.length > cap});
})())""")

# The element a focus, a reveal or an upload acts on, as a REMOTE OBJECT (no
# returnByValue), so it can become a nodeId or an objectId for a DOM method —
# a CDP method, not a line of JavaScript. `__VISIBLE__` is false for upload: a
# file input is usually hidden on purpose, and hiding it is not a reason to
# refuse to fill it.
ELEMENT_EXPR = ("(() => {" + PRELUDE + r"""
  const mode = __MODE__, needle = __NEEDLE__, selector = __SELECTOR__,
        index = __INDEX__, visible = __VISIBLE__;
  const all = mode === 'selector' ? query(selector)
    : query(INTERACTIVE).filter(
        (el) => label(el).toLowerCase().indexOf(needle) >= 0);
  const live = all.filter((el) => !visible || rendered(el) !== null);
  return live[index] || null;
})()""")

# Did the DOM focus actually land on that element? (The oracle for `focus`.)
FOCUS_PROBE = ("JSON.stringify((() => {" + PRELUDE + r"""
  const mode = __MODE__, needle = __NEEDLE__, selector = __SELECTOR__,
        index = __INDEX__;
  const all = mode === 'selector' ? query(selector)
    : query(INTERACTIVE).filter(
        (el) => label(el).toLowerCase().indexOf(needle) >= 0);
  const live = all.filter((el) => rendered(el) !== null);
  const el = live[index] || null;
  return {found: !!el, focused: !!el && el === document.activeElement,
          active: describe(document.activeElement),
          tag: el ? el.tagName.toLowerCase() : null};
})())""")

# What the DOM focus is, before and after text: enough to judge whether the
# text landed, and NEVER the value itself (a password would ride home in it).
TEXT_TARGET_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const el = document.activeElement;
  const empty = !el || el === document.body || el === document.documentElement;
  const frame = !!(el && el.tagName && el.tagName.toLowerCase() === 'iframe');
  const value = el && typeof el.value === 'string' ? el.value : null;
  const editable = !!(el && el.isContentEditable);
  const text = editable ? textOf(el) : null;
  return {focused: !empty, frame: frame,
          editable: !empty && (value !== null || editable),
          secret: frame || /type\s*=\s*["']?password\b/i.test(
            (el && el.outerHTML) || ''),
          active: describe(el), target: path(el),
          length: value !== null ? value.length
                  : (text !== null ? text.length : null)};
})())""")

# What the page thinks a file input holds (the read-back for `upload`).
FILES_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const mode = __MODE__, needle = __NEEDLE__, selector = __SELECTOR__,
        index = __INDEX__, visible = __VISIBLE__;
  const all = mode === 'selector' ? query(selector)
    : query(INTERACTIVE).filter(
        (el) => label(el).toLowerCase().indexOf(needle) >= 0);
  const live = all.filter((el) => !visible || rendered(el) !== null);
  const el = live[index] || null;
  const files = el && el.files ? el.files : null;
  return {found: !!el, files: files ? Array.from(files).map(
    (f) => ({name: f.name, size: f.size})) : null};
})())""")

# The document's scroll position, and the nearest scroller under a point —
# the thing a wheel THERE would move (a nested container, not the page).
SCROLL_PROBE = ("JSON.stringify((() => {" + PRELUDE + r"""
  const el = document.elementFromPoint(__X__, __Y__);
  let nested = null;
  for (let n = el; n; n = n.parentElement) {
    // the document scroller is reported as `document`, not as a nested one:
    // html/body carry the page's own scrollTop
    if (n !== document.documentElement && n !== document.body &&
        (n.scrollTop || n.scrollLeft)) {
      nested = [describe(n), Math.round(n.scrollTop)];
      break;
    }
  }
  const doc = document.documentElement;
  return {x: Math.round(scrollX), y: Math.round(scrollY),
          max: Math.round(Math.max(0, doc.scrollHeight - innerHeight)),
          nested: nested};
})())""")

STATE_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  return {url: location.href, title: document.title,
          active: describe(document.activeElement),
          x: Math.round(scrollX), y: Math.round(scrollY)};
})())""")

UPLOAD_DEFAULT_SELECTOR = "input[type=file]"

# The candidates a verb that IGNORES visibility acts on: a file input is
# usually hidden on purpose, and hiding it is not a reason to refuse to fill
# it. The rows carry the shape `_pick` needs (zeros for geometry), so the
# ambiguity rule is the same one every other verb uses.
CANDIDATES_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const mode = __MODE__, needle = __NEEDLE__, selector = __SELECTOR__,
        cap = __CAP__;
  const all = mode === 'selector' ? query(selector)
    : query(INTERACTIVE).filter(
        (el) => label(el).toLowerCase().indexOf(needle) >= 0);
  return {total: all.length, offscreen: 0, truncated: all.length > cap,
          matches: all.slice(0, cap).map((el) => ({
            tag: el.tagName.toLowerCase(), role: role(el), name: describe(el),
            text: label(el).slice(0, 120), in_viewport: false,
            clipped: false, box: [0, 0, 0, 0], center: [0, 0],
            viewport: [0, 0, 0, 0], point: [0, 0], hit: false,
            hit_element: null}))};
})())""")

WAIT_EXPRS = {
    "load": "Boolean(document.readyState === 'complete' && !!document.body)",
    "idle": ("(() => { if (document.readyState !== 'complete' || "
             "!document.body) return false; const now = performance.now(); "
             "return !performance.getEntriesByType('resource').some("
             "(e) => now - e.responseEnd < __IDLE_MS__); })()"),
    "element": ("(() => {" + PRELUDE + r"""
  const selector = __SELECTOR__;
  return query(selector).some((el) => rendered(el) !== null);
})()"""),
    "js": "Boolean(__EXPR__)",
}


def _int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _well_formed(rows: Any, required: tuple) -> list[dict]:
    """The dict rows a page-supplied extraction must be, filtered to shape.

    Everything on this list crossed `Runtime.evaluate`, and the page — not this
    tool — answered it: a row that is not the object the verb indexes is
    dropped, not fatal."""
    return [row for row in rows
            if isinstance(row, dict) and all(key in row for key in required)]


def _viewport(data: dict, target_id: str) -> list[int]:
    """The page's viewport, or a refusal when it has none."""
    viewport = [_int(v) for v in (data.get("viewport") or [])]
    if len(viewport) < 2 or viewport[0] <= 0 or viewport[1] <= 0:
        fail("no-viewport",
             f"tab {target_id[:10]}… reports no viewport ({viewport}) — a box "
             "in no viewport is not a target: this is a windowless or "
             "never-shown browser, so nothing can be measured in it")
    return viewport[:2]


def _resolve(tab: str, browser: str, for_write: bool) -> tuple[dict, dict]:
    """(browser row, tab row) — the resolution every other verb uses."""
    return tabs._one_tab(tab, browser, for_write)  # noqa: SLF001


def _session(row: dict, tab_row: dict) -> cdp.Session:
    """ONE connection for the whole verb (see `cdp.Session`)."""
    return cdp.Session(cdp.target_ws(cdp.port_of(str(row["profile"])),
                                     str(tab_row["id"])))


def _query_args(text: str | None, selector: str | None,
                verb: str) -> tuple[str, str]:
    """(needle, css) for a verb that takes TEXT or `--selector CSS`."""
    needle = str(text or "").strip()
    css = str(selector or "").strip()
    if bool(needle) == bool(css):
        fail("bad-args", f"{verb}: give TEXT or --selector CSS, not both")
    return needle, css


def _matches_in(session: cdp.Session, needle: str, css: str, cap: int) -> dict:
    """The page's answer to the shared matcher (see `FIND_EXPR`)."""
    expression = (FIND_EXPR
                  .replace("__MODE__", json.dumps("selector" if css else "text"))
                  .replace("__NEEDLE__", json.dumps(needle.lower()))
                  .replace("__SELECTOR__", json.dumps(css))
                  .replace("__CAP__", str(cap)))
    data = session.evaluate(expression)
    if not isinstance(data, dict):
        fail("cdp-error", "the page did not answer with an object")
    return data


def _pick(data: dict, needle: str, css: str, index: int | None) -> dict:
    """The ONE element a click or a reveal acts on.

    Several matches is not a choice this tool makes for the caller: it refuses
    and names them with the index to pass.
    """
    rows = _well_formed(data.get("matches") or [], ("tag", "box", "point"))
    if not rows:
        offscreen = _int(data.get("offscreen"))
        hint = (f" — {offscreen} candidate(s) are rendered but NOT in the "
                "viewport: `tab scroll TEXT` brings one into view"
                if offscreen else "")
        fail("no-match",
             f"no rendered element matches {needle or css!r} on "
             f"{str(data.get('title'))!r}{hint}")
    if index is None:
        if len(rows) > 1:
            where = "; ".join(f"[{i}] {_describe(row)}"
                              for i, row in enumerate(rows[:5]))
            fail("ambiguous-element",
                 f"{len(rows)} elements match {needle or css!r} — pick one "
                 f"with --index N: {where}")
        index = 0
    if not 0 <= index < len(rows):
        fail("bad-args",
             f"--index {index} is out of range: {len(rows)} element(s) match")
    return rows[index]


def _describe(row: dict) -> str:
    """One match, in a few words, for a refusal message."""
    return (f"{row.get('tag')} "
            f"{str(row.get('name') or row.get('text') or '')[:40]}").strip()


def _element(row: dict) -> dict:
    """The element fields a reply carries (the geometry, not the page's)."""
    return {key: row.get(key) for key in
            ("tag", "role", "name", "type", "href", "text", "in_viewport",
             "clipped", "box", "center", "viewport", "point", "hit",
             "hit_element")}


def _match_args(expression: str, needle: str, css: str,
                index: int | None = None, visible: bool = True) -> str:
    """Fill the placeholders every matcher expression shares."""
    filled = (expression
              .replace("__MODE__", json.dumps("selector" if css else "text"))
              .replace("__NEEDLE__", json.dumps(needle.lower()))
              .replace("__SELECTOR__", json.dumps(css))
              .replace("__VISIBLE__", "true" if visible else "false"))
    if "__INDEX__" in filled:
        filled = filled.replace("__INDEX__", str(_int(index)))
    return filled


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
    return _int(node.get("nodeId"))


def _text_target(session: cdp.Session) -> dict:
    """What the DOM focus is right now (see `TEXT_TARGET_EXPR`).

    A probe that cannot answer is reported as an UNREADABLE focus, which the
    caller treats as a secret (fail closed) and as an oracle that cannot
    judge.
    """
    try:
        data = session.evaluate(TEXT_TARGET_EXPR)
    except Exception:                                          # noqa: BLE001
        return {"focused": True, "frame": True, "editable": True,
                "secret": True, "active": None, "length": None,
                "unreadable": True}
    return data if isinstance(data, dict) else {
        "focused": True, "frame": True, "editable": True, "secret": True,
        "active": None, "length": None, "unreadable": True}


def _is_secret(probe: dict) -> bool:
    """Is the focused field a password (or one this page cannot tell us about)?

    Pure, so the policy is testable without a browser: unreadable, a frame, or
    a `type=password` attribute all mean YES.
    """
    return bool(probe.get("secret") or probe.get("frame")
                or probe.get("unreadable"))


def _text_verdict(before: dict, after: dict, chars: int) -> tuple[bool | None, str]:
    """(verified, why) for a text verb.

    True  — the field is readable and its text grew: the text landed.
    False — the field is readable and did NOT change: it did not land.
    None  — the oracle cannot judge (a frame, a canvas, an unreadable value,
            or a focus that moved): an unclear oracle is not proof of absence.
    """
    if after.get("frame") or before.get("frame") \
            or after.get("length") is None:
        return None, "the focused field is not readable from this document"
    if after.get("target") != before.get("target"):
        return None, "the focus moved while the text was being written"
    grew = _int(after.get("length")) - _int(before.get("length"))
    if grew > 0:
        return True, f"the field grew by {grew} character(s) for {chars}"
    return False, "the focused field did not change"


def _reply(row: dict, tab_row: dict, data: dict) -> dict:
    """The page-level facts every DOM read carries."""
    return {"tab": f"id:{tab_row['id']}", "url": data.get("url"),
            "title": data.get("title"), "visibility": data.get("visibility"),
            "browser": tabs._brief(row)}  # noqa: SLF001


# ------------------------------------------------------------------- verbs
def js(expression: str, tab: str = "", browser: str = "") -> dict:
    """`tab js`: the page's own answer — capped, and NOT verified.

    This is the escape hatch, and it can WRITE (it is resolved like one, so it
    needs a browser this CLI manages or has attached). The reply says
    `verified: false` rather than pretending the value means something.
    """
    expr = str(expression or "").strip()
    if not expr:
        fail("bad-args", "tab js: an EXPRESSION is required")
    row, tab_row = _resolve(tab, browser, for_write=True)
    with _session(row, tab_row) as session:
        value = session.evaluate(expr)
    return {"ok": True, "tab": f"id:{tab_row['id']}", "value": value,
            "verified": False, "note": "evaluated, not interpreted",
            "browser": tabs._brief(row)}  # noqa: SLF001


def wait(mode: str, selector: str | None = None, expr: str | None = None,
         timeout: float = WAIT_DEFAULT_S, idle_ms: int = IDLE_DEFAULT_MS,
         tab: str = "", browser: str = "") -> dict:
    """`tab wait`: poll ONE predicate to a wall-clock deadline.

    One budget for the whole wait (`cdp.evaluate_until` keeps ONE connection),
    samples bounded by what is left of it, and a refusal that names what it
    waited for and how long. `--for js` runs caller code, so that mode is
    resolved like a write.
    """
    name = str(mode or "").strip().lower()
    if name not in WAIT_EXPRS:
        fail("bad-args",
             f"tab wait: --for is load|idle|element|js, got {mode!r}")
    if name == "element" and not selector:
        fail("bad-args", "tab wait: --for element needs --selector CSS")
    if name == "js" and not expr:
        fail("bad-args", "tab wait: --for js needs --expr EXPRESSION")
    if name != "element" and selector:
        fail("bad-args",
             f"tab wait: --selector is only for --for element (not {name})")
    if name != "js" and expr:
        fail("bad-args", f"tab wait: --expr is only for --for js (not {name})")
    try:
        seconds = float(timeout)
    except (TypeError, ValueError):
        fail("bad-args", f"tab wait: --timeout needs a number, got {timeout!r}")
    if not 0 < seconds < 3600 or seconds != seconds:
        fail("bad-args",
             f"tab wait: --timeout must be finite and positive, got {timeout!r}")
    row, tab_row = _resolve(tab, browser, for_write=(name == "js"))
    profile, target_id = str(row["profile"]), str(tab_row["id"])
    expression = (WAIT_EXPRS[name]
                  .replace("__SELECTOR__", json.dumps(str(selector or "")))
                  .replace("__EXPR__", str(expr or "false"))
                  .replace("__IDLE_MS__", str(_int(idle_ms, IDLE_DEFAULT_MS))))
    started = time.time()
    value, samples = cdp.evaluate_until(
        cdp.target_ws(cdp.port_of(profile), target_id), expression, bool,
        seconds, WAIT_POLL_S)
    if not value:
        what = selector or expr or ""
        fail("wait-timeout",
             f"tab wait --for {name}"
             + (f" {what!r}" if what else "")
             + f" did not pass within {seconds:g}s ({samples} samples)")
    return {"ok": True, "for": name, "waited_s": round(time.time() - started, 1),
            "samples": samples, "tab": f"id:{target_id}",
            "browser": tabs._brief(row)}  # noqa: SLF001


def find(text: str | None = None, selector: str | None = None,
         cap: int = FIND_CAP, tab: str = "", browser: str = "") -> dict:
    """`tab find`: resolve a human target to elements, with their geometry.

    Only interactive or labelled elements match; each match carries its box in
    PAGE coordinates and in the viewport's, whether it is `in_viewport`, and
    whether a click at its centre would reach it (`hit` — a real
    `elementFromPoint`). An element below the fold is RETURNED with
    `in_viewport: false`, not hidden behind `no-match`: the caller can see that
    it exists and scroll to it.
    """
    needle, css = _query_args(text, selector, "tab find")
    limit = max(1, _int(cap, FIND_CAP))
    row, tab_row = _resolve(tab, browser, for_write=False)
    with _session(row, tab_row) as session:
        data = _matches_in(session, needle, css, limit)
    viewport = _viewport(data, str(tab_row["id"]))
    matches = [dict(m, box=[_int(v) for v in m["box"]],
                    center=[_int(v) for v in (m.get("center") or [])],
                    viewport=[_int(v) for v in (m.get("viewport") or [])],
                    point=[_int(v) for v in (m.get("point") or [])])
               for m in _well_formed(data.get("matches") or [],
                                     ("tag", "box"))]
    if not matches:
        offscreen = _int(data.get("offscreen"))
        fail("no-match",
             f"no rendered element matches {needle or css!r} on "
             f"{str(data.get('title'))!r} (readyState {data.get('ready')!r}, "
             f"{_int(data.get('total'))} candidate(s)"
             + (f", {offscreen} offscreen" if offscreen else "") + ")")
    reply = _reply(row, tab_row, data)
    reply.update({"ok": True, "query": needle or css, "viewport": viewport,
                  "total": _int(data.get("total")),
                  "offscreen": _int(data.get("offscreen")),
                  "truncated": bool(data.get("truncated")),
                  "matches": matches})
    return reply


def click(text: str | None = None, selector: str | None = None,
          index: int | None = None, tab: str = "", browser: str = "") -> dict:
    """`tab click`: press the element a spec resolves to, with REAL input.

    `Input.dispatchMouseEvent` — a move, a press, a release at the element's
    viewport centre — is the input a mouse produces, so a handler that ignores
    `element.click()` takes it. The point must hit-test to the element first
    (`occluded` names what is actually there), and the reply carries what
    changed afterwards: `changed: false` is a fact about a click that had no
    visible effect, not a failure, because the input DID land.
    """
    needle, css = _query_args(text, selector, "tab click")
    row, tab_row = _resolve(tab, browser, for_write=True)
    with _session(row, tab_row) as session:
        data = _matches_in(session, needle, css, FIND_CAP)
        element = _pick(data, needle, css, index)
        if not element.get("in_viewport"):
            fail("no-viewport-target",
                 f"{_describe(element)} is at page {element.get('box')}, "
                 "outside the viewport — scroll it into view first: "
                 f"`tab scroll {needle or '--selector ' + css!r}`")
        if not element.get("hit"):
            fail("occluded",
                 f"{_describe(element)} is at viewport {element.get('point')} "
                 f"but that point reaches "
                 f"{element.get('hit_element') or 'nothing'} instead — "
                 "something is on top of it")
        x, y = [_int(v) for v in (element.get("point") or [])][:2]
        for kind, buttons in (("mouseMoved", 0), ("mousePressed", 1),
                              ("mouseReleased", 0)):
            session.call("Input.dispatchMouseEvent",
                         {"type": kind, "x": x, "y": y, "button": "left",
                          "buttons": buttons, "clickCount": 1})
        after = session.evaluate(STATE_EXPR)
    after = after if isinstance(after, dict) else {}
    before = {"url": data.get("url"), "title": data.get("title"),
              "active": data.get("active"), "scroll": data.get("scroll")}
    changed = (after.get("url") != before["url"]
               or after.get("title") != before["title"]
               or after.get("active") != before["active"]
               or [after.get("x"), after.get("y")] != before["scroll"])
    return {"ok": True, "clicked": True, "element": _element(element),
            "point": [x, y], "changed": changed,
            "before": before,
            "after": {"url": after.get("url"), "title": after.get("title"),
                      "active": after.get("active"),
                      "scroll": [after.get("x"), after.get("y")]},
            "note": ("real input (CDP), so handlers that ignore "
                     "element.click() take it; `changed` is whether anything "
                     "observable moved"),
            "tab": f"id:{tab_row['id']}", "browser": tabs._brief(row)}  # noqa: SLF001


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
        fail("bad-args",
             "tab scroll: name ONE of --by PIXELS, --edge top|bottom, or an "
             "element (TEXT / --selector CSS)")
    if at is not None and modes[0] != "--by":
        fail("bad-args", "tab scroll: --at goes with --by")
    # the arguments are checked BEFORE a browser is asked about anything
    if modes[0] == "--by" and not _int(by):
        fail("bad-args", "tab scroll: --by needs a non-zero PIXELS")
    if edge is not None and str(edge) not in ("top", "bottom"):
        fail("bad-args", f"tab scroll: --edge is top|bottom, got {edge!r}")
    if at is not None:
        _at_point(at)                      # syntax now; the RANGE needs the
    row, tab_row = _resolve(tab, browser, for_write=True)   # real viewport
    target_id = str(tab_row["id"])
    with _session(row, tab_row) as session:
        data = _matches_in(session, "", "", 1)      # page facts, no matching
        viewport = _viewport(data, target_id)
        x, y = _point(at, viewport)
        if modes[0] == "element":
            return _reveal(session, row, tab_row, text, selector, index, data)
        return _wheel(session, row, tab_row, by=by, edge=edge, x=x, y=y,
                      data=data)


def _at_point(at: str) -> tuple[int, int]:
    """`--at X,Y` as two numbers — the SYNTAX, which needs no browser."""
    parts = str(at).replace(" ", "").split(",")
    if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
        fail("bad-args", f"tab scroll: --at needs X,Y numbers, got {at!r}")
    return _int(parts[0], -1), _int(parts[1], -1)


def _point(at: str | None, viewport: list[int]) -> tuple[int, int]:
    """The wheel's point: `--at X,Y` inside the viewport, else its middle."""
    if not at:
        return viewport[0] // 2, viewport[1] // 2
    x, y = _at_point(at)
    if not (0 <= x < viewport[0] and 0 <= y < viewport[1]):
        fail("bad-args",
             f"tab scroll: --at {at!r} is outside the viewport {viewport}")
    return x, y


def _probe(session: cdp.Session, x: int, y: int) -> dict:
    """The document's scroll position and the scroller under (x, y)."""
    data = session.evaluate(
        SCROLL_PROBE.replace("__X__", str(x)).replace("__Y__", str(y)))
    return data if isinstance(data, dict) else {}


def _settle(session: cdp.Session, x: int, y: int, before: dict,
            timeout: float = SCROLL_MOVE_S) -> dict:
    """Poll until the document OR the scroller under the point moved."""
    deadline = time.time() + timeout
    now = before
    while time.time() < deadline:
        now = _probe(session, x, y)
        if (now.get("y"), now.get("x"), now.get("nested")) != \
                (before.get("y"), before.get("x"), before.get("nested")):
            return now
        time.sleep(0.15)
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
        delta = _int(by)
        session.call("Input.dispatchMouseEvent",
                     {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0,
                      "deltaY": delta, "button": "none", "buttons": 0})
        after = _settle(session, x, y, before)
        steps = 1
        moved = (_int(after.get("y")) != _int(before.get("y"))
                 or after.get("nested") != before.get("nested"))
        if not moved:
            # a delta was asked for and nothing took it: already at that end
            # of the document is the one reason that is not a failure
            at_edge = ((_int(before.get("y")) == 0 and delta < 0)
                       or (abs(_int(before.get("y"))
                               - _int(before.get("max"))) <= 2 and delta > 0))
            if not at_edge:
                fail("scroll-not-verified",
                     f"nothing moved: the document is still at y="
                     f"{_int(after.get('y'))} (max {_int(after.get('max'))}) "
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
        want = 0 if direction < 0 else _int(after.get("max"))
        if abs(_int(after.get("y")) - want) > 2:
            fail("scroll-not-verified",
                 f"the document stopped at y={_int(after.get('y'))} of "
                 f"max {_int(after.get('max'))} after {steps} wheel step(s) — "
                 "the bottom/top was not reached (a sticky scroller, or a "
                 "point that is over something that does not scroll)")
    moved_document = _int(after.get("y")) != _int(before.get("y"))
    moved_nested = after.get("nested") != before.get("nested")
    return {"ok": True, "moved": moved_document or moved_nested,
            "document": {"before": _int(before.get("y")),
                         "after": _int(after.get("y")),
                         "max": _int(after.get("max"))},
            "nested": {"before": before.get("nested"),
                       "after": after.get("nested")},
            "point": [x, y], "steps": steps,
            "tab": f"id:{tab_row['id']}", "browser": tabs._brief(row)}  # noqa: SLF001


def _reveal(session: cdp.Session, row: dict, tab_row: dict,
            text: str | None, selector: str | None, index: int | None,
            data: dict) -> dict:
    """`DOM.scrollIntoViewIfNeeded` for one element, then prove it is visible."""
    needle, css = _query_args(text, selector, "tab scroll")
    node_id = _node_of(session, _match_args(ELEMENT_EXPR, needle, css, index))
    if not node_id:
        fail("no-match",
             f"no rendered element matches {needle or css!r}"
             + (f" (--index {index} is past the end)" if index else ""))
    session.call("DOM.scrollIntoViewIfNeeded", {"nodeId": node_id})
    # prove it: the SAME matcher now finds it inside the viewport
    deadline = time.time() + SCROLL_MOVE_S
    found: dict = {}
    while time.time() < deadline:
        found = _matches_in(session, needle, css, FIND_CAP)
        rows = _well_formed(found.get("matches") or [], ("tag", "box"))
        now = [row for row in rows if row.get("in_viewport")]
        if now:
            break
        time.sleep(0.15)
    rows = _well_formed(found.get("matches") or [], ("tag", "box"))
    inside = [row for row in rows if row.get("in_viewport")]
    if not inside:
        fail("scroll-not-verified",
             f"{needle or css!r} is still outside the viewport after "
             "DOM.scrollIntoViewIfNeeded — the element may be inside a "
             "container that cannot scroll it into view")
    return {"ok": True, "revealed": True, "element": _element(inside[0]),
            "scroll": found.get("scroll"), "tab": f"id:{tab_row['id']}",
            "browser": tabs._brief(row)}  # noqa: SLF001


def text(selector: str | None = None, chars: int = TEXT_CAP, tab: str = "",
         browser: str = "") -> dict:
    """`tab text`: the rendered text, truncated IN THE PAGE.

    The cap is applied inside the page, so the reply stays bounded however
    large the document is (and `length` still reports the full size, which is
    how a caller sees what it did not get).
    """
    css = str(selector or "").strip()
    limit = max(1, min(TEXT_CAP, _int(chars, TEXT_CAP)))
    row, tab_row = _resolve(tab, browser, for_write=False)
    with _session(row, tab_row) as session:
        data = session.evaluate(TEXT_EXPR.replace("__SELECTOR__",
                                                  json.dumps(css))
                                .replace("__CAP__", str(limit)))
    if not isinstance(data, dict):
        fail("cdp-error", "tab text: the page did not answer with an object")
    if not data.get("found"):
        fail("no-match", f"tab text: no element matches {css!r}")
    reply = _reply(row, tab_row, data)
    reply.update({"ok": True, "selector": data.get("selector"),
                  "viewport": [_int(v) for v in (data.get("viewport") or [])],
                  "text": str(data.get("text") or ""),
                  "length": _int(data.get("length")),
                  "truncated": bool(data.get("truncated"))})
    return reply


def focus(text: str | None = None, selector: str | None = None,
          index: int | None = None, tab: str = "", browser: str = "") -> dict:
    """`tab focus`: put the DOM focus (the caret) on one element.

    This is the CARET, not the tab's frontmost position — that is `tab
    activate`. Focusing needs no coordinates, no window focus and no hit-test,
    so it works on a background tab and while a layer surface owns the pointer;
    it does scroll the element into view, which is why an element outside the
    viewport is a perfectly good target. `DOM.focus` is the CDP method, and the
    read-back is `document.activeElement`.
    """
    needle, css = _query_args(text, selector, "tab focus")
    row, tab_row = _resolve(tab, browser, for_write=True)
    with _session(row, tab_row) as session:
        data = _matches_in(session, needle, css, FIND_CAP)
        element = _pick(data, needle, css, index)
        node_id = _node_of(session,
                           _match_args(ELEMENT_EXPR, needle, css, index))
        if not node_id:
            fail("no-match",
                 f"{_describe(element)} left the document before the focus "
                 "could be set")
        try:
            session.call("DOM.focus", {"nodeId": node_id})
        except ControlError as e:
            # the protocol says WHY (a disabled control, an element that
            # cannot be focused): that is the verb's own verdict, not a
            # generic CDP failure
            fail("focus-not-verified",
                 f"{_describe(element)} cannot take the DOM focus: {e.message}")
        probe = session.evaluate(_match_args(FOCUS_PROBE, needle, css, index))
    probe = probe if isinstance(probe, dict) else {}
    if not probe.get("focused"):
        fail("focus-not-verified",
             f"{_describe(element)} did not take the DOM focus — "
             f"{probe.get('active') or 'nothing'} has it instead (a disabled "
             "control, or an element that cannot be focused)")
    return {"ok": True, "focused": True, "element": _element(element),
            "active": probe.get("active"), "tab": f"id:{tab_row['id']}",
            "browser": tabs._brief(row)}  # noqa: SLF001


def press(key: str, tab: str = "", browser: str = "") -> dict:
    """`tab press`: one key event at the DOM focus (CDP `Input`).

    Enter submits a focused form, Tab moves on, Escape closes a widget — the
    page's own handlers run, exactly as if a finger pressed it. The DISPATCH is
    verified (an error envelope refuses); the effect belongs to the page, so the
    reply says `verified: false` and the caller reads the outcome with `tab
    text`, `tab info` or `tab js`.
    """
    name = str(key or "").strip().lower()
    if name not in KEYS:
        fail("bad-args", f"tab press: unknown key {key!r} "
                         f"(have: {', '.join(sorted(KEYS))})")
    key_name, code, vk, text = KEYS[name]
    row, tab_row = _resolve(tab, browser, for_write=True)
    with _session(row, tab_row) as session:
        down: dict = {"type": "keyDown" if text else "rawKeyDown",
                      "key": key_name, "code": code,
                      "windowsVirtualKeyCode": vk,
                      "nativeVirtualKeyCode": vk}
        if text:
            down["text"] = text
            down["unmodifiedText"] = text
        session.call("Input.dispatchKeyEvent", down)
        session.call("Input.dispatchKeyEvent",
                     {"type": "keyUp", "key": key_name, "code": code,
                      "windowsVirtualKeyCode": vk,
                      "nativeVirtualKeyCode": vk})
    return {"ok": True, "key": key_name, "target": "page",
            "verified": False,
            "note": ("the key event was dispatched; read the effect with "
                     "`tab text`, `tab info` or `tab js`"),
            "tab": f"id:{tab_row['id']}", "browser": tabs._brief(row)}  # noqa: SLF001


def _preflight(session: cdp.Session, text: str, verb: str) -> dict:
    """Refuse a text write with nowhere to go; mark a PROVEN secret.

    `Input.insertText` into no focus is a silent no-op, and a click target that
    takes no text is the same trap one step further — both are refusals that
    name the fix rather than writes nobody can see.
    """
    before = _text_target(session)
    if not before.get("focused"):
        fail("no-focus",
             f"{verb}: nothing is focused, so there is nowhere to put the text "
             "— run `tab focus TEXT` first")
    if not before.get("editable"):
        fail("no-focus",
             f"{verb}: the focus is on {before.get('active')!r}, which takes "
             "no text — run `tab focus TEXT` first")
    if _is_secret(before):
        audit.LOG.mark_secret(text)   # fail closed: a secret, or unreadable
    return before


def _text_reply(row: dict, tab_row: dict, before: dict, after: dict,
                text: str, rung: str) -> dict:
    """The reply `insert` and `type` share, from the read-back verdict."""
    verified, why = _text_verdict(before, after, len(text))
    if verified is not None and not verified:
        fail(f"{rung}-not-verified",
             f"{why}: {before.get('active')!r} did not take the "
             f"{len(text)} character(s)")
    reply = {"ok": True, "rung": rung, "chars": len(text),
             "active": after.get("active") or before.get("active"),
             "verified": bool(verified),
             "length_before": before.get("length"),
             "length_after": after.get("length"),
             "tab": f"id:{tab_row['id']}",
             "browser": tabs._brief(row)}  # noqa: SLF001
    if verified is None:
        reply["note"] = why
    return reply


def insert(text: str, tab: str = "", browser: str = "") -> dict:
    """`tab insert`: insert TEXT at the DOM focus — ONE atomic input event.

    The durable rung: `Input.insertText` puts the whole string in in one call,
    the way an IME does, and the read-back judges whether the focused field
    grew. The text itself is NEVER echoed — a password's value would ride home
    in the reply — and a password (or an unreadable focus) marks the action log
    so the secret is written as a length.
    """
    value = str(text or "")
    if not value:
        fail("bad-args", "tab insert: TEXT is required")
    row, tab_row = _resolve(tab, browser, for_write=True)
    with _session(row, tab_row) as session:
        before = _preflight(session, value, "tab insert")
        session.call("Input.insertText", {"text": value})
        after = _text_target(session)
    return _text_reply(row, tab_row, before, after, value, "insert")


def type_text(text: str, tab: str = "", browser: str = "") -> dict:
    """`tab type`: type TEXT as REAL per-character key events.

    Three events per character (keyDown, char, keyUp) on ONE connection, for a
    page whose handlers listen per key (autocomplete, validation, masked
    inputs). `tab insert` is the rung to try first — this one is slower and
    exists for the pages where it is the only thing that works. A newline in
    TEXT becomes an Enter key press; a character outside the Latin-1 range
    still rides the `char` event even if its virtual key code means nothing.
    """
    value = str(text or "")
    if not value:
        fail("bad-args", "tab type: TEXT is required")
    row, tab_row = _resolve(tab, browser, for_write=True)
    with _session(row, tab_row) as session:
        before = _preflight(session, value, "tab type")
        for char in value:
            if char == "\n":
                session.call("Input.dispatchKeyEvent",
                             {"type": "keyDown", "key": "Enter",
                              "code": "Enter", "windowsVirtualKeyCode": 13,
                              "nativeVirtualKeyCode": 13})
                session.call("Input.dispatchKeyEvent",
                             {"type": "keyUp", "key": "Enter",
                              "code": "Enter", "windowsVirtualKeyCode": 13,
                              "nativeVirtualKeyCode": 13})
            else:
                vk = ord(char)
                base = {"key": char, "code": char,
                        "windowsVirtualKeyCode": vk,
                        "nativeVirtualKeyCode": vk}
                session.call("Input.dispatchKeyEvent",
                             {"type": "keyDown", **base})
                session.call("Input.dispatchKeyEvent",
                             {"type": "char", "text": char, **base})
                session.call("Input.dispatchKeyEvent",
                             {"type": "keyUp", **base})
            time.sleep(TYPE_PAUSE_S)      # the page's handlers need air
        after = _text_target(session)
    return _text_reply(row, tab_row, before, after, value, "type")


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
        fail("bad-args",
             f"tab upload: FILE must be an absolute path, got {file_path!r}")
    if not os.path.isfile(file_path):
        fail("no-file", f"tab upload: no such file: {file_path}")
    size = os.path.getsize(file_path)
    css = str(selector or "").strip() or UPLOAD_DEFAULT_SELECTOR
    row, tab_row = _resolve(tab, browser, for_write=True)
    with _session(row, tab_row) as session:
        data = session.evaluate(
            _match_args(CANDIDATES_EXPR, "", css).replace("__CAP__",
                                                          str(FIND_CAP)))
        element = _pick(data if isinstance(data, dict) else {},
                        "", css, index)
        handle = session.handle(_match_args(ELEMENT_EXPR, "", css, index,
                                            visible=False))
        if not handle:
            fail("no-match",
                 f"tab upload: {element.get('tag')} left the document before "
                 "the file could be set")
        session.call("DOM.setFileInputFiles",
                     {"files": [file_path], "objectId": handle})
        got = session.evaluate(_match_args(FILES_EXPR, "", css, index,
                                           visible=False))
    got = got if isinstance(got, dict) else {}
    files = got.get("files")
    files = files if isinstance(files, list) else []
    first = files[0] if files and isinstance(files[0], dict) else {}
    name = os.path.basename(file_path)
    if len(files) != 1 or first.get("name") != name \
            or _int(first.get("size"), -1) != size:
        fail("upload-not-verified",
             f"tab upload: the page holds {files!r}, not one file named "
             f"{name!r} of {size} bytes")
    return {"ok": True, "file": file_path,
            "input": {"selector": css, "files": files},
            "tab": f"id:{tab_row['id']}",
            "browser": tabs._brief(row)}  # noqa: SLF001
