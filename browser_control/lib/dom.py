"""dom — what a page READS like: `tab js`, `tab wait`, `tab find`, `tab text`.

The tier above `lib.browser`: every verb resolves ONE tab through the same
rule as the other verbs (`browser._one_tab`), evaluates one expression over
that tab's own connection (`browser._eval` → `cdp.evaluate`), and shape-checks
what came back. The DOM and `JSON.stringify` are the page's to override, so
everything crossing `Runtime.evaluate` is DATA — a malformed row is dropped,
never raised out of a verb.

What the verbs are, and what they are not:

* `js`   — the escape hatch: whatever the page answers, capped, DECLARED
            unverified (the caller owns the meaning). It can write.
* `wait` — a poll for one predicate with one wall-clock budget. It proves the
            predicate passed, never that anything followed from it.
* `find` — a human target resolved to visible elements and their boxes in
            PAGE coordinates, each with a hit-test saying whether a click at
            the centre would reach it.
* `text` — the rendered text, truncated IN THE PAGE, so the reply is bounded
            no matter how large the document is.

Two contracts worth stating because they were measured, not assumed:

* **A read never activates a tab.** A background tab has a viewport, a layout
  and a working hit-test (measured 2026-09-18), so geometry is available
  without moving the user's desktop. `find` reports `visibility` and
  `viewport` and leaves the tab alone.
* **A tab with no viewport refuses `no-viewport`.** A 0x0 viewport (a
  windowless or never-shown browser) makes every box meaningless, so it is a
  refusal, not a `rendered: false` flag to interpret.
"""
from __future__ import annotations

import json
import time
from typing import Any

# The three private helpers below are this layer's contract with lib.browser:
# the tab resolution, one evaluation on that tab, and the browser block every
# reply carries. They are private because no other module needs them.
from browser_control.lib import browser as tabs  # pyright: ignore[reportMissingImports]
from browser_control.lib import cdp  # pyright: ignore[reportMissingImports]
from browser_control.lib.errors import fail  # pyright: ignore[reportMissingImports]

TEXT_CAP = 40_000           # chars `text` returns (the PAGE truncates)
FIND_CAP = 10               # matches `find` returns
WAIT_POLL_S = 0.4           # how often a `wait` samples
WAIT_DEFAULT_S = 15.0
IDLE_DEFAULT_MS = 500
EVAL_TIMEOUT_S = 15.0

# One prelude for every verb that looks for an element: the element set, the
# label, the role, the box, the hit-test, and a SHADOW-PIERCING query. Sharing
# it is what keeps `find` and `wait --for element` from drifting apart, and it
# is why `find` sees into open shadow roots while a plain
# `document.querySelector` does not.
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
  const rendered = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return null;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' ||
        s.opacity === '0') return null;
    return r;
  };
  const box = (el, vw, vh) => {
    const r = rendered(el);
    if (!r) return null;
    const left = Math.max(r.left, 0), top = Math.max(r.top, 0);
    const right = Math.min(r.right, vw), bottom = Math.min(r.bottom, vh);
    if (right - left <= 1 || bottom - top <= 1) return null;
    return {x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height,
            cx: r.left + r.width / 2, cy: r.top + r.height / 2,
            clipped: left !== r.left || top !== r.top || right !== r.right ||
                     bottom !== r.bottom};
  };
  const hitAt = (el, cx, cy, vw, vh) => {
    const x = Math.min(Math.max(cx, 1), Math.max(1, vw - 1));
    const y = Math.min(Math.max(cy, 1), Math.max(1, vh - 1));
    const hit = document.elementFromPoint(x, y);
    const ok = !!hit && (hit === el || el.contains(hit) ||
      !!(hit.shadowRoot && hit.shadowRoot.contains(el)));
    return {ok: ok, at: [Math.round(x), Math.round(y)],
            what: hit ? role(hit) +
                        (label(hit) ? ':' + label(hit).slice(0, 40) : '')
                      : null};
  };
"""

FIND_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const vw = window.innerWidth, vh = window.innerHeight;
  const base = {url: location.href, title: document.title,
                ready: document.readyState,
                visibility: document.visibilityState, viewport: [vw, vh]};
  if (vw <= 0 || vh <= 0) {
    return Object.assign(base, {matches: [], total: 0, truncated: false,
                                degenerate: true});
  }
  const mode = __MODE__, needle = __NEEDLE__, selector = __SELECTOR__,
        cap = __CAP__;
  const all = mode === 'selector' ? query(selector)
    : query(INTERACTIVE).filter(
        (el) => label(el).toLowerCase().indexOf(needle) >= 0);
  const out = [];
  for (const el of all) {
    const b = box(el, vw, vh);
    if (!b) continue;
    const hit = hitAt(el, b.cx, b.cy, vw, vh);
    out.push({tag: el.tagName.toLowerCase(), role: role(el),
              name: attr(el, 'aria-label') || attr(el, 'name') || null,
              type: attr(el, 'type'), href: el.href || null,
              text: label(el).slice(0, 120), clipped: b.clipped,
              box: [Math.round(b.x), Math.round(b.y), Math.round(b.w),
                    Math.round(b.h)],
              center: [Math.round(b.x + b.w / 2), Math.round(b.y + b.h / 2)],
              hit: hit.ok, hit_element: hit.what, hit_at: hit.at});
    if (out.length >= cap) break;
  }
  return Object.assign(base, {matches: out, total: all.length,
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
    dropped, not fatal. An empty result then refuses through the verb's own
    no-match path."""
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


def _eval(row: dict, tab_row: dict, expression: str,
          timeout: float = EVAL_TIMEOUT_S) -> Any:
    """One evaluation on that tab's own connection."""
    return tabs._eval(str(row["profile"]), str(tab_row["id"]),  # noqa: SLF001
                      expression, timeout=timeout)


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
    `verified: false` rather than pretending the value means something: `tab
    text`, `tab find` and `tab info` are the verbs that reason about a page.
    """
    expr = str(expression or "").strip()
    if not expr:
        fail("bad-args", "tab js: an EXPRESSION is required")
    row, tab_row = _resolve(tab, browser, for_write=True)
    value = _eval(row, tab_row, expr)
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
    """`tab find`: resolve a human target to visible elements, in PAGE coords.

    Only interactive or labelled elements match; each match says whether a
    click at its centre would reach it (`hit`), whether it is partially
    scrolled out (`clipped`), and where it is in the page's own coordinates.
    No screen point: this tool owns no window.
    """
    needle = str(text or "").strip()
    css = str(selector or "").strip()
    if bool(needle) == bool(css):
        fail("bad-args", "tab find: give TEXT or --selector CSS, not both")
    limit = max(1, _int(cap, FIND_CAP))
    row, tab_row = _resolve(tab, browser, for_write=False)
    expression = (FIND_EXPR
                  .replace("__MODE__", json.dumps("selector" if css else "text"))
                  .replace("__NEEDLE__", json.dumps(needle.lower()))
                  .replace("__SELECTOR__", json.dumps(css))
                  .replace("__CAP__", str(limit)))
    data = _eval(row, tab_row, expression)
    if not isinstance(data, dict):
        fail("cdp-error", "tab find: the page did not answer with an object")
    viewport = _viewport(data, str(tab_row["id"]))
    matches = [dict(m, box=[_int(v) for v in m["box"]],
                    center=[_int(v) for v in (m.get("center") or [])])
               for m in _well_formed(data.get("matches") or [], ("tag", "box"))]
    if not matches:
        fail("no-match",
             f"no visible element matches {needle or css!r} on "
             f"{str(data.get('title'))!r} (readyState {data.get('ready')!r}, "
             f"{_int(data.get('total'))} candidate(s))")
    reply = _reply(row, tab_row, data)
    reply.update({"ok": True, "query": needle or css, "viewport": viewport,
                  "total": _int(data.get("total")),
                  "truncated": bool(data.get("truncated")),
                  "matches": matches})
    return reply


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
    expression = (TEXT_EXPR.replace("__SELECTOR__", json.dumps(css))
                            .replace("__CAP__", str(limit)))
    data = _eval(row, tab_row, expression)
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
