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

import base64
import binascii
import contextlib
import json
import math
import os
import re
import time
from typing import Any

# The private helpers below are this layer's contract with lib.browser: the
# tab resolution, one evaluation on that tab, and the browser block every
# reply carries. They are private because no other module needs them.
from browser_control.lib import audit, cdp
from browser_control.lib import browser as tabs
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ControlError,
    fail,
)
from browser_control.lib.text import foreign  # pyright: ignore[reportMissingImports]

TEXT_CAP = 40_000           # chars `text` returns (the PAGE truncates)
FIND_CAP = 10               # elements `find` returns (and click/scroll scan)
EXTRACT_CAP = 10            # records `extract` returns by default
EXTRACT_MAX_MATCHES = 500   # and the most it will return at all
EXTRACT_FIELD_CHARS = 1_000  # chars kept of ONE field (sliced IN the page)
EXTRACT_FIELD_MAX = 20_000  # and the most one field may keep
EXTRACT_TOTAL_CHARS = 20_000  # field text across the whole reply, page-side
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_ATTR_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:.-]*$")
WAIT_POLL_S = 0.4           # how often a `wait` samples
WAIT_DEFAULT_S = 15.0
IDLE_DEFAULT_MS = 500
EVAL_TIMEOUT_S = 15.0
SCROLL_EDGE_STEP = 2500     # one wheel notch when scrolling to an edge
SCROLL_EDGE_STEPS = 12      # and how many of them an edge is worth
SCROLL_MOVE_S = 4.0         # how long one wheel is given to move something

# Which FRAME a verb is about, set once per process by the CLI from `--frame`:
# a URL substring or an index from `tab frames`. Empty means the page itself.
# It is a SCOPE, exactly like `--profile`, and the CLI clears it on every
# invocation that does not pass the flag.
FRAME: dict[str, Any] = {"wanted": "", "resolved": None}


def frame(wanted: str | None = None) -> str:
    """Set, clear or read the frame this process's verbs are about.

    `None` READS it; `""` clears it (the CLI clears a call that passes no
    `--frame`, so no verb inherits another's scope); anything else sets it.
    The read form matters: making "no argument" clear the scope as well meant
    every lookup wiped what it was looking at (which is exactly the bug this
    docstring exists to prevent).
    """
    if wanted is not None:
        FRAME["wanted"] = str(wanted).strip()
        # a resolution belongs to the call that made it: nothing may inherit
        # the last call's frame in a reply
        FRAME["resolved"] = None
    return FRAME["wanted"]


def frame_resolved() -> dict | None:
    """Which frame the last session actually attached to, or None.

    An index is the page's live iframe order, so "which document did that act
    in" is not something a caller can infer from their own argument. `_session`
    records the resolution here and the CLI puts it in the reply, in ONE place,
    so no verb has to remember to and none can forget to.
    """
    resolved = FRAME.get("resolved")
    return dict(resolved) if isinstance(resolved, dict) else None


def mode_of(mode: str | None) -> str:
    """The mode a mode-carrying subcommand will run, normalised in ONE place.

    Three calls answer differently by mode — `tab wait --for`, `tab dialog MODE`
    and `tab media MODE` — and the capability gate authorises a call BY its
    mode, so the gate and the verb that runs must read it the same way.
    Measured, and the reason this function exists: the gate used to compare the
    raw token (`--for JS` was not `js`, and `tab media PLAY` was not `play`),
    so `--allow read` authorised a call that then ran caller code. Normalising
    here — the verb normalises here too — means one spelling cannot be a read to
    the gate and a write to the browser.
    """
    return str(mode or "").strip().lower()


# The verbs `--frame` can be ABOUT: the CLI adds a `frame` note to their replies
# and nowhere else. A verb that drives the tab rather than its document —
# `tab nav`, `tab list`, `tab activate` — has no frame scope, and saying it did
# would be a lie about what that call touched. `tab dialog` is NOT here either:
# a JavaScript dialog belongs to the TAB (`Page.handleJavaScriptDialog` goes to
# the tab's own socket, and `dialog state` asks the tab, not a document), so
# claiming a frame for it was a claim the code did not keep (a review measured
# it: the reply said `frame: 1` while the page target was driven).
FRAME_VERBS = frozenset({
    "js", "wait", "find", "text", "click", "hover", "check", "select",
    "scroll", "focus", "press", "insert", "type", "upload", "media",
    "screenshot", "extract",
})
CHECK_TIMEOUT_S = 2.0       # how long a click is given to flip `checked`
DIALOG_PROBE_S = 1.5        # how long the tab is given to prove it is awake
DIALOG_CLEAR_S = 3.0        # how long the renderer is given to come back
PNG_SIG = b"\x89PNG\r\n\x1a\n"
SHOT_MAX_PX = 100_000        # a dimension no screenshot of this page can have
TYPE_PAUSE_S = 0.008        # between keystrokes: the page's handlers need air
MEDIA_TIMEOUT_S = 5.0       # how long a play/pause is given to take effect

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
      attr(el, 'alt'), attr(el, 'name'),
      // NEVER a password field's value: `describe` rides into replies and
      // refusals, so including it handed the secret the verb was told not to
      // echo straight back to the caller (a review flagged it). Other values
      // stay — `find` matches an input by them.
      (el.type === 'password' ? '' : el.value), textOf(el),
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

# Reading playback state, and calling play()/pause(), is the ONE place with no
# CDP method: the `Media` domain is experimental and event-only, and the media
# KEY is a toggle aimed at whichever session has focus (a background tab's
# audio, or nothing at all). So this verb drives the ELEMENT and then verifies
# what the page reports.
MEDIA_STATE_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const all = Array.from(document.querySelectorAll('video, audio'));
  const area = (el) => { const r = el.getBoundingClientRect();
                         return Math.round(r.width * r.height); };
  // the element that is PLAYING first, else the biggest one on the page: a
  // page can carry a preview clip beside the real player
  const playing = all.filter((el) => !el.paused && !el.ended);
  const el = playing[0] ||
    all.slice().sort((a, b) => area(b) - area(a))[0] || null;
  const base = {count: all.length, found: !!el,
                error: window.__bcMediaError || null};
  if (!el) return base;
  return Object.assign(base, {
    element: el.tagName.toLowerCase(),
    playing: !el.paused && !el.ended, paused: el.paused, ended: el.ended,
    muted: el.muted, volume: el.volume, rate: el.playbackRate,
    time: Number((el.currentTime || 0).toFixed(2)),
    duration: Number((el.duration || 0).toFixed(2)),
    ready_state: el.readyState,
    src: String(el.currentSrc || el.src || '').slice(0, 120)});
})())""")

MEDIA_ACTION_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const mode = __MODE__, index = __INDEX__;
  const all = Array.from(document.querySelectorAll('video, audio'));
  const area = (el) => { const r = el.getBoundingClientRect();
                         return Math.round(r.width * r.height); };
  const playing = all.filter((el) => !el.paused && !el.ended);
  const el = index >= 0 ? (all[index] || null)
    : (playing[0] || all.slice().sort((a, b) => area(b) - area(a))[0] || null);
  if (!el) return {count: all.length, found: false};
  // a play() the browser REJECTS is the page refusing (no supported source, or
  // the autoplay policy): the promise is not awaited here — the reason is
  // parked where the read-back can name it
  window.__bcMediaError = null;
  let thrown = null;
  try {
    if (mode === 'play') {
      const promised = el.play();
      if (promised && typeof promised.catch === 'function') {
        promised.catch((e) => { window.__bcMediaError =
          ((e && e.name) || 'Error') + ': ' + ((e && e.message) || ''); });
      }
    } else {
      el.pause();
    }
  } catch (e) {
    thrown = ((e && e.name) || 'Error') + ': ' + ((e && e.message) || '');
    window.__bcMediaError = thrown;
  }
  return {count: all.length, found: true,
          element: el.tagName.toLowerCase(), paused: el.paused,
          error: window.__bcMediaError || null};
})())""")

# Did the pointer really LAND on that element? `:hover` is matched by the
# engine from the real hover state, so it is the page's own answer — and the
# point must still hit-test into the element (a child counts, an overlay does
# not).
HOVER_PROBE = ("JSON.stringify((() => {" + PRELUDE + r"""
  const mode = __MODE__, needle = __NEEDLE__, selector = __SELECTOR__,
        index = __INDEX__;
  const all = mode === 'selector' ? query(selector)
    : query(INTERACTIVE).filter(
        (el) => label(el).toLowerCase().indexOf(needle) >= 0);
  const live = all.filter((el) => rendered(el) !== null);
  const el = live[index] || null;
  const under = document.elementFromPoint(__X__, __Y__);
  return {found: !!el,
          hovered: !!el && el.matches(':hover'),
          chain: !!el && !!under && (el === under || el.contains(under)),
          under: under ? describe(under) : null};
})())""")

# What a checkbox or radio IS and whether it is checked (the oracle for
# `check`: the control's own property, read before and after the click).
CHECK_READ = ("JSON.stringify((() => {" + PRELUDE + r"""
  const mode = __MODE__, needle = __NEEDLE__, selector = __SELECTOR__,
        index = __INDEX__;
  const all = mode === 'selector' ? query(selector)
    : query(INTERACTIVE).filter(
        (el) => label(el).toLowerCase().indexOf(needle) >= 0);
  const live = all.filter((el) => rendered(el) !== null);
  const el = live[index] || null;
  if (!el) return {found: false};
  const tag = el.tagName.toLowerCase();
  const type = String(el.getAttribute('type') || '').toLowerCase();
  return {found: true, tag: tag, type: type, name: describe(el),
          checkable: tag === 'input' && (type === 'checkbox' ||
                                        type === 'radio'),
          checked: !!el.checked, disabled: !!el.disabled};
})())""")

# Which `<option>` a value names, and what the control holds now. Value first,
# then the exact label: a caller that passes what the user would SEE is not
# guessing, and the reply says which of the two matched.
SELECT_PROBE = ("JSON.stringify((() => {" + PRELUDE + r"""
  const mode = __MODE__, needle = __NEEDLE__, selector = __SELECTOR__,
        index = __INDEX__, wanted = __VALUE__;
  const all = mode === 'selector' ? query(selector)
    : query(INTERACTIVE).filter(
        (el) => label(el).toLowerCase().indexOf(needle) >= 0);
  const live = all.filter((el) => rendered(el) !== null);
  const el = live[index] || null;
  if (!el) return {found: false};
  const tag = el.tagName.toLowerCase();
  if (tag !== 'select')
    return {found: true, tag: tag, is_select: false, name: describe(el)};
  const options = Array.from(el.options);
  const optionLabel = (o) => String(o.label || o.text).trim();
  const byValue = options.filter((o) => String(o.value) === wanted);
  const byLabel = options.filter((o) => optionLabel(o) === wanted);
  const chosen = byValue.length ? byValue : byLabel;
  return {found: true, is_select: true, tag: tag, name: describe(el),
          multiple: !!el.multiple, disabled: !!el.disabled,
          value: String(el.value), selected: el.selectedIndex,
          options: options.length, matched: chosen.length,
          by: byValue.length ? 'value' : (byLabel.length ? 'label' : 'none'),
          target: chosen.length === 1 ? options.indexOf(chosen[0]) : -1,
          target_value: chosen.length === 1
            ? String(chosen[0].value) : '',
          target_label: chosen.length === 1
            ? optionLabel(chosen[0]).slice(0, 60) : '',
          candidates: chosen.slice(0, 5).map(
            (o) => String(o.value).slice(0, 40)),
          labels: options.slice(0, 12).map(
            (o) => optionLabel(o).slice(0, 40))};
})())""")

# What the page says its own geometry is — the numbers a screenshot is judged
# by (the file's own header must match them).
SHOT_METRICS = ("JSON.stringify((() => {" + PRELUDE + r"""
  return {iw: innerWidth, ih: innerHeight, dpr: devicePixelRatio,
          sw: document.documentElement.scrollWidth,
          sh: document.documentElement.scrollHeight,
          url: location.href, title: document.title,
          visibility: document.visibilityState};
})())""")

# Every frame of the page, as the DOM sees it. `contentDocument` readable means
# the frame shares this page's PROCESS — such a frame has no CDP target of its
# own, which is the difference `--frame` has to know about.
FRAME_CENSUS = ("JSON.stringify((() => {" + PRELUDE + r"""
  const all = roots(document, []);
  const frames = [];
  for (const root of all) {
    for (const f of root.querySelectorAll('iframe')) frames.push(f);
  }
  return frames.map((f, index) => {
    const r = f.getBoundingClientRect();
    let reads = false;
    try { reads = !!f.contentDocument } catch (e) { reads = false }
    return {index: index, url: String(f.src || ''), name: String(f.name || ''),
            same_process: reads,
            box: [Math.round(r.x), Math.round(r.y), Math.round(r.width),
                  Math.round(r.height)],
            visible: r.width > 20 && r.height > 20};
  });
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
    "js": ("(() => { const v = (__EXPR__); if (v && "
           "(typeof v === 'object' || typeof v === 'function') && "
           "typeof v.then === 'function') return 'thenable'; "
           "return Boolean(v); })()"),
}


def _int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _num(value: object, default: float = 0.0) -> float:
    """A number the PAGE reported, or `default` when it is not one."""
    try:
        return float(str(value).strip())
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
    viewport = _ints(data.get("viewport"))
    if len(viewport) < 2 or viewport[0] <= 0 or viewport[1] <= 0:
        fail("no-viewport",
             f"tab {target_id[:10]}… reports no viewport ({viewport}) — a box "
             "in no viewport is not a target: this is a windowless or "
             "never-shown browser, so nothing can be measured in it")
    return viewport[:2]


def _resolve(tab: str, browser: str, for_write: bool) -> tuple[dict, dict]:
    """(browser row, tab row) — the resolution every other verb uses."""
    return tabs._one_tab(tab, browser, for_write)  # noqa: SLF001


def _document_ws(port: int, page_target: str) -> str:
    """The websocket of the DOCUMENT a content verb acts on.

    The page itself, or the FRAME `--frame` names — and the scope is applied
    HERE, in one place, so a verb that drives its own connection cannot be the
    one that forgets it. That gap was real: `tab wait` opens its own connection
    for many samples, and `tab wait --frame 1` evaluated the predicate in the
    top document while its reply said `frame: 1`.
    """
    if FRAME["wanted"]:
        target = _frame_target(port, page_target, FRAME["wanted"])
        # WHICH frame that was: an index is the page's live iframe order, so the
        # reply says what was resolved, not only what was asked for
        FRAME["resolved"] = {"index": _int(target["index"]),
                             "url": str(target["url"]),
                             "target": str(target["target"])}
        return cdp.target_ws(port, str(target["target"]), "iframe")
    return cdp.target_ws(port, page_target)


def _session(row: dict, tab_row: dict) -> cdp.Session:
    """ONE connection for the whole verb — or for the FRAME it is scoped to.

    `--frame` attaches to that frame's OWN target. A cross-origin frame is a
    target with its own coordinate space, so every verb below works unchanged
    inside it — measured: a real click dispatched on that session fires the
    frame's own handler and the frame reports the new state.
    """
    port = cdp.port_of(str(row["profile"]))
    return cdp.Session(_document_ws(port, str(tab_row["id"])))


def frames_of(port: int, page_target: str,
              census: list | None = None) -> list[dict]:
    """Every TOP-LEVEL frame of THAT page: the DOM's view, plus its own target.

    The DOM knows the geometry and whether a frame shares the page's process
    (`contentDocument` readable — a same-process frame has no target of its
    own). The browser's target list knows which frames are separate targets and
    WHICH TAB each belongs to: `Target.getTargets` gives an iframe target a
    `parentId`, and matching on it is what keeps `--frame` inside the tab it was
    asked about. Matching by URL alone — what this used to do — sent input into
    another tab's frame: measured with two tabs embedding the same widget, both
    resolved to ONE target.

    Two facts a URL cannot settle are REFUSED rather than guessed:

    * two frames in this tab with the same URL (`candidates` > 0) — target ids
      are random, so pairing them by id order can bind the sibling (a review
      flagged it);
    * a browser that does not say WHO owns an iframe target: nothing is
      attributed (`attribution: "unattributable"`), and `_frame_target` refuses
      — a URL that appears once is not proof, because a cross-origin frame in
      this page's own process has no target while another tab's single target
      with the same URL looks unique (a review constructed exactly that).

    `census` lets a caller that already evaluated it (a read that wants the
    counts) skip a second evaluation. `committed` is the URL the browser
    actually committed, which can differ from the `src` attribute the page
    shows. The census PIERCES open shadow roots (a frame inside one is a frame
    the page can see, and walking only the document omitted it silently — a
    review found that after the matcher had learned to pierce them).
    """
    if census is None:
        census = cdp.evaluate(cdp.target_ws(port, page_target), FRAME_CENSUS) \
            or []
    owned = cdp.frame_targets(port)
    shared: dict[str, int] = {}
    attribution = ""
    if any(str(row.get("parent")) for row in owned):
        pool = [r for r in owned if r["parent"] == page_target]
        counted: dict[str, int] = {}
        for row in pool:
            key = str(row.get("url"))
            counted[key] = counted.get(key, 0) + 1
        shared = {url: count for url, count in counted.items() if count > 1}
    else:
        # This browser does not say which tab owns an iframe target, and a URL
        # that appears ONCE is not proof of ownership: a cross-origin frame that
        # stays in this page's PROCESS (a sandboxed one) has no target of its
        # own, while another tab's single target with the same URL looks unique
        # — so `--frame` would drive that other tab. A review constructed
        # exactly that, so nothing is attributed here and `_frame_target`
        # refuses rather than guessing.
        pool = []
        attribution = "unattributable"
    used: set[str] = set()
    rows: list[dict] = []
    for entry in census if isinstance(census, list) else []:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "")
        match = None
        if url and not entry.get("same_process") and not shared.get(url):
            match = next((r for r in pool if str(r.get("url")) == url
                          and str(r.get("id")) not in used), None)
        if match is not None:
            used.add(str(match["id"]))
        rows.append({"index": _int(entry.get("index")), "url": url,
                     "committed": str((match or {}).get("url") or ""),
                     "name": str(entry.get("name") or ""),
                     "box": _ints(entry.get("box")),
                     "visible": bool(entry.get("visible")),
                     "same_process": bool(entry.get("same_process")),
                     "target": str((match or {}).get("id") or ""),
                     "attribution": attribution,
                     "candidates": shared.get(url, 0)})
    return rows


def frames(row: dict, tab_row: dict) -> dict:
    """`tab frames`: the page's own TOP-LEVEL frames, and which can be driven."""
    port = cdp.port_of(str(row["profile"]))
    rows = frames_of(port, str(tab_row["id"]))
    unattributed = any(r.get("attribution") for r in rows)
    reply = {"ok": True, "count": len(rows), "frames": rows,
             # NULL, never 0, when the browser attributes nothing: "none of
             # them can be driven" is a claim this cannot support, and the note
             # below used to promise `--frame` while `--frame` refused
             # `frame-unattributable` (a review caught the contradiction)
             "separate": None if unattributed
             else sum(1 for r in rows if r["target"]),
             "note": ("this browser does not report which tab owns an iframe "
                      "target, so `--frame` cannot attribute one to this tab "
                      "— `tab js` reads a same-process frame, and "
                      "`tab click --at X,Y` hits one by coordinate"
                      if unattributed else
                      "a cross-origin frame is a target of its own: name it "
                      "with `--frame <url substring|index>` and every verb "
                      "that acts on a page's content works inside it")}
    if not rows:
        reply["note"] = ("this page has no iframes — nothing for `--frame` to "
                          "name")
    reply["tab"] = f"id:{tab_row['id']}"
    reply["browser"] = tabs._brief(row)          # noqa: SLF001
    return reply


def _frame_target(port: int, page_target: str, wanted: str) -> dict:
    """The frame a `--frame` value names, or a refusal.

    An integer is the index `tab frames` prints; anything else is a
    case-insensitive substring of the frame's URL. Several matches refuse
    `frame-ambiguous` and name the index to use instead — the rule every other
    selector follows. A frame that shares the page's PROCESS has no target to
    drive, and refuses `frame-not-separate` with what to do instead.
    """
    rows = frames_of(port, page_target)
    if not rows:
        fail("no-frame", "this page has no iframes — `tab frames` lists them")
    text_ = str(wanted or "").strip()
    if text_.isdigit():
        hits = [r for r in rows if _int(r["index"]) == _int(text_)]
    else:
        hits = [r for r in rows
                if text_.lower() in str(r.get("url") or "").lower()]
    if not hits:
        have = "; ".join(f"[{r['index']}] {str(r['url'])[:52] or 'srcdoc'}"
                         for r in rows[:4])
        fail("no-frame", f"no frame matches {wanted!r} (have: {have})")
    if len(hits) > 1:
        where = "; ".join(f"[{r['index']}] {str(r['url'])[:52]}" for r in hits[:4])
        fail("frame-ambiguous",
             f"{len(hits)} frames match {wanted!r} — pick one by index "
             f"(`--frame 0` … `--frame {len(rows) - 1}`): {where}")
    found = hits[0]
    if found.get("attribution"):
        fail("frame-unattributable",
             f"frame [{found['index']}] "
             f"{str(found['url'])[:60] or 'srcdoc'} — this browser does not "
             "report which tab owns an iframe target, so the CLI cannot tell "
             "this frame from another tab's with the same URL. `tab js` reads "
             "a same-process frame, `tab click --at X,Y` hits one by "
             "coordinate, or drive the tab that owns it")
    if found.get("candidates"):
        fail("frame-ambiguous",
             f"frame [{found['index']}] {str(found['url'])[:60]} matches "
             f"{found['candidates']} targets in this browser and it does not "
             "say which tab owns them — run `tab frames` in the tab you mean "
             "and name the frame by its index there")
    if not found["target"]:
        fail("frame-not-separate",
             f"frame [{found['index']}] "
             f"{str(found['url'])[:60] or 'srcdoc'} has no target of its OWN in "
             "this tab: either it shares the page's process (a same-origin or "
             "srcdoc frame), or its committed URL differs from the `src` the "
             "page shows (a redirect). `tab js` reads a same-process frame, "
             "and `tab click --at X,Y` hits either one by coordinate")
    return found


def _frame_summary(row: dict, tab_row: dict,
                   session: cdp.Session | None = None) -> dict:
    """What a read is NOT showing: the page's TOP-LEVEL frames, by kind.

    A `tab text` that silently omits everything inside an iframe is the one
    place this tool could read as "there is nothing there". The census makes it
    say so instead — and it is taken over the PAGE, never over a `--frame`
    scoped session, so a scoped read still reports the page it is a frame of.

    `separate` counts frames with a CDP target of their own (`--frame` can drive
    those), `cross_origin` counts frames whose document the page cannot read —
    two different facts that used to share one number. An UNREADABLE census is
    an error, not an empty page: `{}` means "no frames", and a page that breaks
    the census expression must not be able to look like a page without frames.

    A caller already holding a session hands it in: then a page with NO frames
    costs one evaluation on that connection, and the second connection (the
    browser target list) is only opened when there is something to attribute —
    every `find`/`text` used to pay for both, framed or not (a review measured
    it). `separate` is null, never 0, when the target list could not be read.
    """
    page = str(tab_row["id"])
    port = cdp.port_of(str(row["profile"]))
    try:
        if session is not None and not FRAME["wanted"]:
            census = session.evaluate(FRAME_CENSUS)
        else:
            census = cdp.evaluate(cdp.target_ws(port, page), FRAME_CENSUS)
    except ControlError as e:
        return {"error": f"{e.code}: {e.message}", "total": None,
                "separate": None, "cross_origin": None, "visible": None,
                "note": ("the frame census could not be read, so framed "
                         "content may exist that this read does not show — "
                         "that is NOT the same as a page without frames")}
    raw = census if isinstance(census, list) else []
    if not raw:
        return {}
    try:
        rows = frames_of(port, page, census=raw)
    except ControlError:
        rows = []
    if not rows:
        # the DOM census answered but the target list did not: report what is
        # known and leave `separate` NULL rather than claiming "no targets"
        return {"total": len(raw), "separate": None,
                "cross_origin": sum(1 for r in raw if isinstance(r, dict)
                                    and not r.get("same_process")),
                "same_process": sum(1 for r in raw if isinstance(r, dict)
                                    and r.get("same_process")),
                "visible": sum(1 for r in raw if isinstance(r, dict)
                               and r.get("visible"))}
    unattributed = any(r.get("attribution") for r in rows)
    return {"total": len(rows),
            # `separate` counts frames with a target of their own — NULL, never
            # 0, when the browser does not attribute targets to tabs: "none of
            # them can be driven" would be a claim this cannot support
            "separate": None if unattributed else sum(1 for r in rows
                                                      if r["target"]),
            "cross_origin": sum(1 for r in rows if not r["same_process"]),
            "same_process": sum(1 for r in rows if r["same_process"]),
            "visible": sum(1 for r in rows if r["visible"])}


def _frames_note(row: dict | None, tab_row: dict | None) -> str:
    """The sentence a REFUSAL carries when the page has frames.

    A `no-match` that says "nothing matches" while the page has a frame this
    read cannot see inside is the false-absence the whole tier is careful about,
    so the refusal says how many frames there are and how to reach them. Empty
    when there are none (nothing to explain), and computed only on the failure
    path — a verb that succeeds never pays for it.
    """
    if not row or not tab_row:
        return ""
    try:
        census = _frame_summary(row, tab_row)
    except ControlError:
        return ""
    if not census:
        return ""
    if census.get("error"):
        return (" — and the frame census could not be read either, so framed "
                "content may exist that nothing here can see")
    if census.get("total") in (None, 0):
        return ""
    if census.get("separate") is None:
        return (f" — this page has {census['total']} frame(s): `tab frames` "
                "lists them, and this browser does not say which can be "
                "driven directly")
    return (f" — this page has {census['total']} frame(s), "
            f"{census['separate']} of them separate: `tab frames` lists them "
            "and `--frame` reaches inside")


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
    rows = _well_formed(data.get("matches") or [], ("tag", "box", "point"))
    if not rows:
        offscreen = _int(data.get("offscreen"))
        hint = (f" — {offscreen} candidate(s) are rendered but NOT in the "
                "viewport: `tab scroll TEXT` brings one into view"
                if offscreen else "")
        fail("no-match",
             f"no rendered element matches {needle or css!r} on "
             f"{str(data.get('title'))!r}{hint}"
             + _frames_note(row, tab_row))
    if index is None:
        if len(rows) > 1:
            where = "; ".join(f"[{i}] {_describe(candidate)}"
                              for i, candidate in enumerate(rows[:5]))
            fail("ambiguous-element",
                 f"{len(rows)} elements match {needle or css!r} — pick one "
                 f"with --index N: {where}"
                 + _frames_note(row, tab_row))
        index = 0
    if not 0 <= index < len(rows):
        fail("bad-args",
             f"--index {index} is out of range: {len(rows)} element(s) match")
    return rows[index]


def _describe(row: dict) -> str:
    """One match, in a few words, for a refusal message (page text, sanitised)."""
    return (f"{foreign(row.get('tag'), 20)} "
            f"{foreign(row.get('name') or row.get('text'), 40)}").strip()


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


def _playback_verdict(mode: str, before: dict, after: dict) -> tuple[bool, str]:
    """Did the play/pause take effect? Judged ONLY from the read-back.

    `play` is verified when the CLOCK MOVED — not merely when the element
    stopped reporting `paused`: measured, a media element with no source
    reports `paused: false` the moment `play()` is called and never plays a
    frame, which is exactly the overclaim this check exists to catch. `pause`
    is verified by the state the page reports.
    """
    if mode == "play":
        if _num(after.get("time")) > _num(before.get("time")):
            return True, "the clock advanced"
        if _int(after.get("ready_state")) == 0:
            return False, ("the element has nothing to play (readyState 0: no "
                           "supported source)")
        return False, "the clock did not advance"
    if after.get("paused"):
        return True, "the element reports paused"
    return False, "the element is still playing"


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
    name = mode_of(mode)
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
        _document_ws(cdp.port_of(profile), target_id), expression, bool,
        seconds, WAIT_POLL_S)
    if value == "thenable":
        # measured: `Boolean(promise)` is TRUE the moment the promise is made,
        # so an async predicate passed before anything settled (a review found
        # it) — the expression reports the thenable instead of coercing it
        fail("bad-args",
             "tab wait --for js: the expression returned a Promise — a wait "
             "polls a BOOLEAN, and a Promise is truthy the moment it is made, "
             "so the wait would pass before anything settled (await it in the "
             "page, or set a flag and poll that)")
    if not value:
        what = selector or expr or ""
        fail("wait-timeout",
             f"tab wait --for {name}"
             + (f" {what!r}" if what else "")
             + f" did not pass within {seconds:g}s ({samples} samples)")
    return {"ok": True, "for": name, "waited_s": round(time.time() - started, 1),
            "samples": samples, "tab": f"id:{target_id}",
            "browser": tabs._brief(row)}  # noqa: SLF001


EXTRACT_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const schema = __SCHEMA__;
  const scan = (root, selector) => {
    const out = [];
    const visit = (r) => {
      for (const el of r.querySelectorAll(selector)) out.push(el);
      if (r.shadowRoot) visit(r.shadowRoot);
    };
    visit(root);
    return out;
  };
  const candidates = query(schema.each)
    .filter((el) => !schema.visible || rendered(el));
  const chosen = candidates.slice(0, schema.cap);
  const records = [];
  let used = 0;
  let budget_hit = false;
  for (const el of chosen) {
    const row = {};
    for (const name of Object.keys(schema.fields)) {
      const spec = schema.fields[name];
      const target = spec.sel ? (scan(el, spec.sel)[0] || null) : el;
      let value = null;
      if (target) value = spec.attr ? attr(target, spec.attr) : textOf(target);
      if (typeof value === 'string') {
        value = value.trim().slice(0, schema.chars);
      }
      row[name] = value;
    }
    used += JSON.stringify(row).length;
    records.push(row);
    if (used >= schema.budget) { budget_hit = true; break; }
  }
  return {
    url: location.href, title: document.title,
    visibility: document.visibilityState,
    total: candidates.length, matches: records,
    truncated: budget_hit || candidates.length > records.length};
})())""")


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
        fail("bad-args",
             f"{verb}: a field is NAME=SPEC (e.g. "
             "`--field text=[data-testid=tweetText]` or "
             f"`--field time=time@datetime`) — got {spec!r}")
    if not _FIELD_NAME.match(name):
        fail("bad-args",
             f"{verb}: {name!r} is not a field name (letters, digits, `_` and "
             "`-`, starting with a letter or `_`)")
    sel = value.strip()
    attr_name = ""
    head, at, tail = sel.rpartition("@")
    if at and _ATTR_NAME.match(tail.strip()):
        sel, attr_name = head.strip(), tail.strip()
    if not sel and not attr_name:
        fail("bad-args",
             f"{verb}: {spec!r} names neither a selector nor an attribute — "
             "use `name=SELECTOR`, `name=SELECTOR@attr` or `name=@attr`")
    return name, {"sel": sel, "attr": attr_name}


def _extract_schema(each: str, fields: list[str], cap: int = EXTRACT_CAP,
                    chars: int = EXTRACT_FIELD_CHARS, visible: bool = False,
                    verb: str = "tab extract") -> dict:
    """The JSON schema one extraction runs: parsed, validated, bounded."""
    selector = str(each or "").strip()
    if not selector:
        fail("bad-args",
             f"{verb}: --each is required — the CSS selector of the repeated "
             "item (e.g. --each article)")
    if not fields:
        fail("bad-args", f"{verb}: at least one --field NAME=SPEC is required")
    parsed: dict[str, dict] = {}
    for spec in fields:
        name, field = _extract_field(spec, verb)
        parsed[name] = field
    limit = _int(cap, EXTRACT_CAP)
    if limit < 1:
        fail("bad-args", f"{verb}: --cap must be at least 1")
    keep = _int(chars, EXTRACT_FIELD_CHARS)
    if keep < 1:
        fail("bad-args", f"{verb}: --chars must be at least 1")
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
        fail("bad-args",
             f"tab extract: --unique {wanted!r} is not one of the fields "
             f"({', '.join(names)})")
    row, tab_row = _resolve(tab, browser, for_write=False)
    with _session(row, tab_row) as session:
        data = session.evaluate(
            EXTRACT_EXPR.replace("__SCHEMA__", json.dumps(schema)))
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
    reply = _reply(row, tab_row, data)
    reply.update({"ok": True, "each": schema["each"], "fields": names,
                  "count": len(records), "total": _int(data.get("total")),
                  "truncated": (bool(data.get("truncated"))
                                or len(records) < len(raw)),
                  "matches": records})
    return _with_frame(reply)


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
        # the census rides the session already open for the cheap case (a page
        # with no frames needs no second connection), and goes to the PAGE when
        # `--frame` scoped that session to a frame
        frames_here = _frame_summary(row, tab_row, session)
    viewport = _viewport(data, str(tab_row["id"]))
    matches = [dict(m, box=_ints(m["box"]),
                    center=_ints(m.get("center")),
                    viewport=_ints(m.get("viewport")),
                    point=_ints(m.get("point")))
               for m in _well_formed(data.get("matches") or [],
                                     ("tag", "box"))]
    if not matches:
        offscreen = _int(data.get("offscreen"))
        fail("no-match",
             f"no rendered element matches {needle or css!r} on "
             f"{str(data.get('title'))!r} (readyState {data.get('ready')!r}, "
             f"{_int(data.get('total'))} candidate(s)"
             + (f", {offscreen} offscreen" if offscreen else "") + ")"
             + _frames_note(row, tab_row))
    reply = _reply(row, tab_row, data)
    reply.update({"ok": True, "query": needle or css, "viewport": viewport,
                  "total": _int(data.get("total")),
                  "offscreen": _int(data.get("offscreen")),
                  "truncated": bool(data.get("truncated")),
                  "matches": matches})
    if frames_here:
        reply["frames"] = frames_here
    return _with_frame(reply)


def _under_point(session: cdp.Session, x: int, y: int) -> object:
    """What `document.elementFromPoint` reaches at that point, as a short name.

    The one probe both `--at` and the element path use, so "what is actually
    there" is measured the same way after every dispatch.
    """
    return session.evaluate(
        "(() => { const el = document.elementFromPoint("
        f"{x}, {y}); return el ? el.tagName.toLowerCase()"
        " + (el.id ? '#' + el.id : '') : null })()")


def _at_point_click(session: cdp.Session, x: int, y: int) -> dict:
    """A move, a press and a release at a viewport point, and what is there."""
    for kind, buttons in (("mouseMoved", 0), ("mousePressed", 1),
                          ("mouseReleased", 0)):
        session.call("Input.dispatchMouseEvent",
                     {"type": kind, "x": x, "y": y, "button": "left",
                      "buttons": buttons, "clickCount": 1})
    return {"under": _under_point(session, x, y)}


def _click_at(row: dict, tab_row: dict, at: str) -> dict:
    """`tab click --at X,Y`: real input at a POINT, for what a selector cannot
    name — a canvas, or a widget inside a frame that shares this page's process.

    There is no element to verify against, so this says `verified: false` and
    reports what the point actually REACHES: the input did land, and whether it
    was the right control is the caller's to judge from `changed` and
    `under`.
    """
    _at_point(at, "tab click")
    with _session(row, tab_row) as session:
        data = _matches_in(session, "", "", 1)     # page facts, no matching
        x, y = _point(at, _viewport(data, str(tab_row["id"])),
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
             "browser": tabs._brief(row)}         # noqa: SLF001
    return _with_frame(reply)


def _hover_at(row: dict, tab_row: dict, at: str) -> dict:
    """`tab hover --at X,Y`: a move to a point, verified by `:hover` on it."""
    _at_point(at, "tab hover")
    with _session(row, tab_row) as session:
        data = _matches_in(session, "", "", 1)
        x, y = _point(at, _viewport(data, str(tab_row["id"])),
                    "tab hover")
        session.call("Input.dispatchMouseEvent",
                     {"type": "mouseMoved", "x": x, "y": y,
                      "button": "none", "buttons": 0})
        probe = session.evaluate(
            "(() => { const el = document.elementFromPoint("
            f"{x}, {y}); return "
            "JSON.stringify({under: el ? el.tagName.toLowerCase() + "
            "(el.id ? '#' + el.id : '') : null, hovered: !!el && "
            "el.matches(':hover')}) })()")
    probe = probe if isinstance(probe, dict) else {}
    if not probe.get("hovered"):
        fail("hover-not-verified",
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
             "browser": tabs._brief(row)}         # noqa: SLF001
    return _with_frame(reply)


def _with_frame(reply: dict) -> dict:
    """Say which frame the verb acted in, when it was scoped to one.

    The RESOLVED frame as well as the value that was asked for: an index is the
    page's live iframe order, so a caller cannot infer from their own argument
    which document the verb actually changed.
    """
    if FRAME["wanted"]:
        reply["frame"] = FRAME["wanted"]
        if FRAME.get("resolved"):
            reply["frame_resolved"] = dict(FRAME["resolved"])
    return reply


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
    needle, css = _query_args(text, selector, "tab click") if at is None \
        else ("", "")
    if at is not None:
        if text or selector:
            fail("bad-args",
                 "tab click: --at is a POINT — give that or a TEXT/--selector, "
                 "not both")
        _at_point(at, "tab click")     # the POINT first: no browser needed
        row, tab_row = _resolve(tab, browser, for_write=True)
        return _click_at(row, tab_row, at)
    row, tab_row = _resolve(tab, browser, for_write=True)
    with _session(row, tab_row) as session:
        data = _matches_in(session, needle, css, FIND_CAP)
        element = _pick(data, needle, css, index, row=row,
                           tab_row=tab_row)
        if not element.get("in_viewport"):
            fail("no-viewport-target",
                 f"{_describe(element)} is at page {element.get('box')}, "
                 "outside the viewport — scroll it into view first: "
                 f"`tab scroll {needle or '--selector ' + css!r}`")
        if not element.get("hit"):
            fail("occluded",
                 f"{_describe(element)} is at viewport {element.get('point')} "
                 f"but that point reaches "
                 f"{foreign(element.get('hit_element'), 50) or 'nothing'} "
                 "instead — something is on top of it")
        # press at the point the HIT-TEST proved, not at the element's raw
        # centre: the probe CLAMPS into the viewport, so an element whose centre
        # is below the fold was tested inside it and pressed outside, and the
        # reply still said `clicked: true` (a review measured the mismatch)
        point = _ints(element.get("hit_at") or element.get("point"), 2)
        if len(point) < 2:
            # a page may answer anything for a value that crosses
            # Runtime.evaluate, and "[7]" is not a point: refuse rather
            # than raise ValueError out of the verb (both review lanes
            # found the unpack behind the `_ints` guard)
            fail("no-viewport-target",
                 f"{_describe(element)} reports no usable point "
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
        under = _under_point(session, x, y)
    after = after if isinstance(after, dict) else {}
    before = {"url": data.get("url"), "title": data.get("title"),
              "active": data.get("active"), "scroll": data.get("scroll")}
    changed = (after.get("url") != before["url"]
               or after.get("title") != before["title"]
               or after.get("active") != before["active"]
               or [after.get("x"), after.get("y")] != before["scroll"])
    return {"ok": True, "clicked": True, "element": _element(element),
            "point": [x, y], "changed": changed, "under": under,
            "before": before,
            "after": {"url": after.get("url"), "title": after.get("title"),
                      "active": after.get("active"),
                      "scroll": [after.get("x"), after.get("y")]},
            "note": ("real input (CDP), so handlers that ignore "
                     "element.click() take it; `changed` is whether anything "
                     "observable moved"),
            "tab": f"id:{tab_row['id']}", "browser": tabs._brief(row)}  # noqa: SLF001


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
            fail("bad-args",
                 "tab hover: --at is a POINT — give that or a TEXT/--selector, "
                 "not both")
        _at_point(at, "tab hover")     # the POINT first: no browser needed
        row, tab_row = _resolve(tab, browser, for_write=True)
        return _hover_at(row, tab_row, at)
    needle, css = _query_args(text, selector, "tab hover")
    row, tab_row = _resolve(tab, browser, for_write=True)
    with _session(row, tab_row) as session:
        data = _matches_in(session, needle, css, FIND_CAP)
        element = _pick(data, needle, css, index, row=row,
                           tab_row=tab_row)
        if not element.get("in_viewport"):
            fail("no-viewport-target",
                 f"{_describe(element)} is at page {element.get('box')}, "
                 "outside the viewport — scroll it into view first: "
                 f"`tab scroll {needle or '--selector ' + css!r}`")
        if not element.get("hit"):
            fail("occluded",
                 f"{_describe(element)} is at viewport {element.get('point')} "
                 f"but that point reaches "
                 f"{foreign(element.get('hit_element'), 50) or 'nothing'} "
                 "instead — something is on top of it")
        point = _ints(element.get("hit_at") or element.get("point"), 2)
        if len(point) < 2:
            # a page may answer anything for a value that crosses
            # Runtime.evaluate, and "[7]" is not a point: refuse rather
            # than raise ValueError out of the verb (both review lanes
            # found the unpack behind the `_ints` guard)
            fail("no-viewport-target",
                 f"{_describe(element)} reports no usable point "
                 f"({point!r}) — the page owns this value")
        x, y = point
        session.call("Input.dispatchMouseEvent",
                     {"type": "mouseMoved", "x": x, "y": y,
                      "button": "none", "buttons": 0})
        probe = session.evaluate(
            _match_args(HOVER_PROBE, needle, css, index)
            .replace("__X__", str(x)).replace("__Y__", str(y)))
    probe = probe if isinstance(probe, dict) else {}
    if not (probe.get("hovered") and probe.get("chain")):
        fail("hover-not-verified",
             f"{_describe(element)} at viewport {[x, y]} does not match "
             "`:hover` after the pointer moved there "
             f"({probe.get('under') or 'nothing'} is at that point) — the "
             "page may re-render, or the element moved between the read and "
             "the move")
    return {"ok": True, "hovered": True, "verified": True,
            "element": _element(element), "point": [x, y],
            "under": probe.get("under"),
            "note": ("real input (CDP): one mouseMoved; the read-back is the "
                     "engine's own `:hover` state"),
            "tab": f"id:{tab_row['id']}",
            "browser": tabs._brief(row)}       # noqa: SLF001


def _check_state(session: cdp.Session, needle: str, css: str,
                 index: int | None) -> dict:
    """The control's own checked/disabled facts, read from the page."""
    probe = session.evaluate(_match_args(CHECK_READ, needle, css, index))
    return probe if isinstance(probe, dict) else {}


def _checkable(probe: dict, element: dict) -> None:
    """Refuse what a click cannot make checked, naming what it is."""
    if not probe.get("checkable"):
        kind = str(probe.get("tag") or element.get("tag") or "element")
        type_ = str(probe.get("type") or "")
        label = f"{kind}[{type_}]" if type_ else kind
        fail("not-checkable",
             f"{_describe(element)} is {label} — `tab check` drives a "
             "checkbox or a radio button")
    if probe.get("disabled"):
        fail("not-checkable",
             f"{_describe(element)} is disabled — a user cannot change it, "
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
    needle, css = _query_args(text, selector, "tab check")
    row, tab_row = _resolve(tab, browser, for_write=True)
    want = not uncheck
    with _session(row, tab_row) as session:
        data = _matches_in(session, needle, css, FIND_CAP)
        element = _pick(data, needle, css, index, row=row,
                           tab_row=tab_row)
        before = _check_state(session, needle, css, index)
        _checkable(before, element)
        if bool(before.get("checked")) == want:
            return {"ok": True, "checked": want, "changed": False,
                    "verified": True, "element": _element(element),
                    "note": ("already in that state — no click was sent, "
                             "because a click would toggle it"),
                    "tab": f"id:{tab_row['id']}",
                    "browser": tabs._brief(row)}       # noqa: SLF001
        if not element.get("in_viewport"):
            fail("no-viewport-target",
                 f"{_describe(element)} is at page {element.get('box')}, "
                 "outside the viewport — scroll it into view first: "
                 f"`tab scroll {needle or '--selector ' + css!r}`")
        if not element.get("hit"):
            fail("occluded",
                 f"{_describe(element)} is at viewport {element.get('point')} "
                 f"but that point reaches "
                 f"{foreign(element.get('hit_element'), 50) or 'nothing'} "
                 "instead — something is on top of it")
        point = _ints(element.get("hit_at") or element.get("point"), 2)
        if len(point) < 2:
            # a page may answer anything for a value that crosses
            # Runtime.evaluate, and "[7]" is not a point: refuse rather
            # than raise ValueError out of the verb (both review lanes
            # found the unpack behind the `_ints` guard)
            fail("no-viewport-target",
                 f"{_describe(element)} reports no usable point "
                 f"({point!r}) — the page owns this value")
        x, y = point
        for kind, buttons in (("mouseMoved", 0), ("mousePressed", 1),
                              ("mouseReleased", 0)):
            session.call("Input.dispatchMouseEvent",
                         {"type": kind, "x": x, "y": y, "button": "left",
                          "buttons": buttons, "clickCount": 1})
        after = _check_state(session, needle, css, index)
        deadline = time.time() + CHECK_TIMEOUT_S
        while bool(after.get("checked")) != want and time.time() < deadline:
            time.sleep(0.15)
            after = _check_state(session, needle, css, index)
    if bool(after.get("checked")) != want:
        fail("check-not-verified",
             f"{_describe(element)} reports checked={after.get('checked')} "
             f"after the click at viewport {[x, y]} — the page may have "
             "re-set it, or the control is not the one that reacted")
    return {"ok": True, "checked": want, "changed": True, "verified": True,
            "element": _element(element), "point": [x, y],
            "checked_before": bool(before.get("checked")),
            "note": "real input (CDP); the read-back is the control's own `checked`",
            "tab": f"id:{tab_row['id']}",
            "browser": tabs._brief(row)}           # noqa: SLF001


def _select_probe(session: cdp.Session, needle: str, css: str,
                  index: int | None, value: str) -> dict:
    """Which option a value names, and what the control holds (see the SQL)."""
    expression = (_match_args(SELECT_PROBE, needle, css, index)
                  .replace("__VALUE__", json.dumps(str(value))))
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
    needle, css = _query_args(text, selector, "tab select")
    wanted = str(value or "")
    if not wanted:
        fail("bad-args",
             "tab select: --value is required — the option's value, or its "
             "exact label")
    row, tab_row = _resolve(tab, browser, for_write=True)
    with _session(row, tab_row) as session:
        data = _matches_in(session, needle, css, FIND_CAP)
        element = _pick(data, needle, css, index, row=row,
                           tab_row=tab_row)
        probe = _select_probe(session, needle, css, index, wanted)
        if not probe.get("is_select"):
            kind = str(probe.get("tag") or element.get("tag") or "?")
            fail("not-a-select",
                 f"{_describe(element)} is a {kind}, not a <select> — this "
                 "verb picks one option, and only a <select> has options")
        if probe.get("disabled"):
            fail("not-a-select",
                 f"{_describe(element)} is disabled — a user cannot choose in "
                 "it, and neither will this")
        if probe.get("multiple"):
            fail("not-a-select",
                 f"{_describe(element)} is a multiple select — it holds a "
                 "SET of options, and this verb sets one (use `tab js`)")
        matched = _int(probe.get("matched"))
        if not matched:
            names = [foreign(name, 30)
                     for name in _list(probe.get("labels"))[:12]]
            labels = ", ".join(names)
            fail("no-match",
                 f"no <option> in {_describe(element)} has value or label "
                 f"{wanted!r} (have: {labels or 'none'})")
        if matched > 1:
            candidates = ", ".join(foreign(name, 30) for name in
                                   _list(probe.get("candidates"))[:5])
            fail("ambiguous-option",
                 f"{matched} options in {_describe(element)} match "
                 f"{wanted!r}: {candidates} — their VALUES are what tell them "
                 "apart")
        target = _int(probe.get("target"), -1)
        selected = _int(probe.get("selected"), -1)
        delta = target - selected
        if not delta:
            return {"ok": True, "selected": True, "changed": False,
                    "verified": True, "trusted": True, "keys": 0,
                    "element": _element(element),
                    "value": probe.get("value"),
                    "label": probe.get("target_label"),
                    "option_index": target, "by": probe.get("by"),
                    "note": "already selected — no key was sent",
                    "tab": f"id:{tab_row['id']}",
                    "browser": tabs._brief(row)}   # noqa: SLF001
        node_id = _node_of(session,
                           _match_args(ELEMENT_EXPR, needle, css, index))
        if not node_id:
            fail("no-match",
                 f"{_describe(element)} left the document before the choice "
                 "could be made")
        try:
            session.call("DOM.focus", {"nodeId": node_id})
        except ControlError as e:
            fail("select-not-verified",
                 f"{_describe(element)} cannot take the DOM focus, so the "
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
        after = _select_probe(session, needle, css, index, wanted)
        deadline = time.time() + CHECK_TIMEOUT_S
        while (_int(after.get("selected"), -1) != target
               and time.time() < deadline):
            time.sleep(0.15)
            after = _select_probe(session, needle, css, index, wanted)
    if _int(after.get("selected"), -1) != target \
            or str(after.get("value")) != str(probe.get("target_value")):
        fail("select-not-verified",
             f"{_describe(element)} reports "
             f"value={after.get('value')!r} at index "
             f"{after.get('selected')!r} after {abs(delta)} arrow key(s) — "
             f"the wanted option is index {target} "
             f"(value {probe.get('target_value')!r}); the page may ignore the "
             "key events, or re-set the control")
    return {"ok": True, "selected": True, "changed": True, "verified": True,
            "trusted": True, "element": _element(element),
            "value": after.get("value"), "label": probe.get("target_label"),
            "option_index": target, "options": probe.get("options"),
            "by": probe.get("by"), "keys": abs(delta),
            "value_before": probe.get("value"),
            "note": ("REAL key events (CDP) on the focused control, so the "
                     "page's `change` handler sees isTrusted: true"),
            "tab": f"id:{tab_row['id']}",
            "browser": tabs._brief(row)}           # noqa: SLF001


NO_DIALOG_NOTE = (
    "the browser is not showing a JavaScript dialog for this tab. Two ways "
    "that happens: there is none, or the dialog was SUPPRESSED because no "
    "client had the Page domain enabled when it opened — there is nothing "
    "left to answer then, and the renderer stays parked: `tab nav URL` "
    "replaces the document (and its renderer), `tab close` ends the tab")

# Chromium's own words for "there is no dialog": the one error that makes
# accept/dismiss definitive rather than a guess.
NO_DIALOG_MARK = "No dialog is showing"


def _no_dialog(error: ControlError) -> bool:
    """Did the browser refuse because it is showing no dialog of ours?"""
    return NO_DIALOG_MARK in str(error.message)


def dialog(mode: str = "state", text: str | None = None, tab: str = "",
           browser: str = "") -> dict:
    """`tab dialog`: read whether a JavaScript dialog is up, or answer it.

    Measured (Chrome 152, headed), and the reason this verb exists:

    * a page's own dialog PARKS its renderer — every read on that tab times
      out until the dialog is answered;
    * Chrome SUPPRESSES dialogs for a target whose Page domain was never
      enabled: the browser shows nothing, `handleJavaScriptDialog` reports
      "No dialog is showing", and the tab never comes back;
    * with the domain enabled first, the dialog is announced, answered, and
      the renderer returns — which is why every session this CLI opens enables
      it (`cdp.Session`).

    So `state` reports only what can be PROVEN: the tab answering proves no
    dialog is blocking it (`open: false`), and a tab that cannot answer is
    reported as `open: null, verified: false` rather than as a claim of
    absence. `accept`/`dismiss` is the definitive answer — the browser errors
    for a dialog it is not showing — and its read-back is the renderer
    answering again.
    """
    name = mode_of(mode) or "state"
    if name not in ("state", "accept", "dismiss"):
        fail("bad-args",
             f"tab dialog: MODE is state, accept or dismiss, got {mode!r}")
    if name == "state":
        row, tab_row = _resolve(tab, browser, for_write=False)
        opened: object = None
        verified = False
        note = ""
        try:
            with _session(row, tab_row) as session:
                try:
                    session.evaluate("1", timeout=DIALOG_PROBE_S)
                    opened, verified = False, True
                    note = ("the tab answers, so no dialog is blocking it "
                            "(a dialog parks the renderer)")
                except ControlError as e:
                    if e.code != "eval-timeout":
                        raise
                    note = ("the tab does not answer: a JavaScript dialog it "
                            "opened, or a script that does not yield — "
                            "`tab dialog accept|dismiss` tells them apart, "
                            "because the browser errors for a dialog it is "
                            "not showing")
        except ControlError as e:
            if e.code != "blocked":
                raise
            note = ("the tab was already blocked before the Page domain "
                    "could be enabled — if a dialog did it, Chromium "
                    "suppressed it and there is nothing left to answer: "
                    "`tab dialog accept|dismiss` says so for certain, and "
                    "`tab close` ends it")
        return {"ok": True, "open": opened, "verified": verified,
                "blocked": True if opened is None else None, "note": note,
                "tab": f"id:{tab_row['id']}",
                "browser": tabs._brief(row)}       # noqa: SLF001
    # accept | dismiss: NO Page.enable first — a dialog that is already up
    # cannot be announced any more, and enabling is exactly what blocks on a
    # parked tab, while the handling command answers regardless
    params: dict = {"accept": name == "accept"}
    if text is not None:
        params["promptText"] = str(text)
    row, tab_row = _resolve(tab, browser, for_write=True)
    with cdp.Session(cdp.target_ws(cdp.port_of(str(row["profile"])),
                                   str(tab_row["id"])),
                     page_domain=False) as session:
        try:
            session.call("Page.handleJavaScriptDialog", params)
        except ControlError as e:
            if _no_dialog(e):
                fail("no-dialog", NO_DIALOG_NOTE)
            raise
        answered = False
        deadline = time.time() + DIALOG_CLEAR_S
        while time.time() < deadline:
            try:
                session.evaluate("1", timeout=0.8)
                answered = True
                break
            except ControlError as e:
                if e.code not in ("eval-timeout", "cdp-error", "blocked"):
                    raise
                time.sleep(0.2)
    if not answered:
        fail("dialog-not-verified",
             f"the {name} was sent, but the tab still does not answer "
             f"within {DIALOG_CLEAR_S:g}s — a page can open another dialog "
             "immediately (see `tab dialog state`), or a script is spinning")
    return {"ok": True, "handled": True, "accepted": name == "accept",
            "verified": True, "prompt_text": text if text is not None else None,
            "note": ("the browser answered the dialog and the tab answers "
                     "again; the dialog's own text is not in this reply — it "
                     "opened before this connection, and Chromium announces a "
                     "dialog only once"),
            "tab": f"id:{tab_row['id']}",
            "browser": tabs._brief(row)}           # noqa: SLF001


def _png_size(data: bytes) -> list[int]:
    """[width, height] from the PNG's OWN header, or [] when it is not one.

    The file is judged by its bytes, not by the answer that produced it: a
    screenshot whose header disagrees with the page's own geometry is refused
    before it is written anywhere.
    """
    if len(data) < 24 or not data.startswith(PNG_SIG) or data[12:16] != b"IHDR":
        return []
    return [int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big")]


def _pixels(css: int, dpr: float) -> int:
    """CSS pixels → device pixels, or a refusal.

    Both numbers come from the PAGE, so a nonsense pair must become a refusal
    rather than an exception or a silently wrong expectation: this is the value
    the PNG's own header is compared against.
    """
    try:
        value = int(round(css * dpr))
    except (OverflowError, ValueError) as e:
        fail("screenshot-not-verified",
             f"the page reports {css} px at devicePixelRatio {dpr:g}, which is "
             f"not a size ({e}) — nothing was written")
    if not 0 < value <= SHOT_MAX_PX:
        fail("screenshot-not-verified",
             f"the page reports {css} px at devicePixelRatio {dpr:g}, i.e. "
             f"{value} device pixels — no screenshot of it exists — nothing "
             "was written")
    return value


def _shot_target(path: str) -> str:
    """The absolute path a screenshot may be written to, or a refusal."""
    expanded = os.path.expanduser(str(path or ""))
    if not os.path.isabs(expanded):
        # the help and this function's own docstring say ABSOLUTE; a relative
        # path used to be silently resolved against the CLI's cwd (a review
        # flagged the mismatch with `tab upload`, which refuses one)
        fail("bad-args",
             f"tab screenshot: {path!r} must be an absolute path — this tool "
             "writes where it was told, not where it happens to be run from")
    target = os.path.abspath(expanded)
    if os.path.isdir(target):
        fail("bad-args", f"tab screenshot: {target} is a directory")
    if not target.lower().endswith(".png"):
        fail("bad-args",
             f"tab screenshot: {path!r} must end in .png — the data IS a PNG, "
             "and a name that says otherwise is a lie about the file")
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        fail("bad-args",
             f"tab screenshot: {parent} is not a directory — create it first")
    return target


def _write_shot(target: str, data: bytes, force: bool) -> None:
    """Write the PNG; refuse to clobber unless `force`.

    Without `force` the open is EXCLUSIVE, so "it did not exist" is the
    kernel's answer rather than a check that can lose a race. With `force` the
    bytes land on a temporary name and are renamed into place, so a failed
    write never replaces a good file.
    """
    if not force:
        try:
            handle = os.open(target,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            fail("file-exists",
                 f"tab screenshot: {target} already exists — pass --force to "
                 "replace it")
        except OSError as e:
            fail("write-failed", f"tab screenshot: {target}: {e}")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(data)
        except OSError as e:
            # the exclusive open succeeded, but the WRITE failed (ENOSPC,
            # EFBIG…): without this the OSError escaped as ERR[internal] and
            # left a truncated file at a path the caller was told was not
            # written (a review flagged it)
            with contextlib.suppress(OSError):
                os.remove(target)
            fail("write-failed", f"tab screenshot: {target}: {e}")
        return
    temp = f"{target}.bc-{os.getpid()}.part"
    try:
        # EXCLUSIVE and never through a link: the predictable temp name was a
        # pre-creatable symlink, and `open(..., "wb")` truncated whatever it
        # pointed at (a review flagged CWE-377). A leftover temp is refused,
        # not silently adopted.
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        fail("write-failed",
             f"tab screenshot: a leftover temporary file is in the way "
             f"({temp}) — remove it and try again")
    except OSError as e:
        fail("write-failed", f"tab screenshot: {target}: {e}")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temp, target)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.remove(temp)
        fail("write-failed", f"tab screenshot: {target}: {e}")


def screenshot(path: str, full: bool = False, force: bool = False,
               tab: str = "", browser: str = "") -> dict:
    """`tab screenshot`: the page as a PNG the file's OWN header vouches for.

    A READ: nothing about the page changes, so it needs no `attach` — but it
    writes a file, which is why the path must be absolute, end in `.png`, and
    not already exist without `--force`.

    `Page.captureScreenshot` is the CDP method, and the verification is
    arithmetic rather than faith: the PNG's IHDR must agree with the page's
    own `innerWidth/innerHeight` (or `scrollWidth/scrollHeight` with `--full`)
    times `devicePixelRatio` — measured, those are EXACT on this browser. A
    file that would not match is not written at all.
    """
    target = _shot_target(path)
    row, tab_row = _resolve(tab, browser, for_write=False)
    with _session(row, tab_row) as session:
        metrics = session.evaluate(SHOT_METRICS)
        if not isinstance(metrics, dict):
            fail("cdp-error", "the page did not report its geometry")
        shot = session.call("Page.captureScreenshot",
                            {"format": "png",
                             "captureBeyondViewport": bool(full)})
        encoded = str(shot.get("data") or "")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as e:
            fail("cdp-error",
                 f"Page.captureScreenshot: the data is not base64 ({e})")
    dpr = _num(metrics.get("dpr"), 1.0)
    if not math.isfinite(dpr) or dpr <= 0:
        fail("screenshot-not-verified",
             f"the page reports devicePixelRatio {metrics.get('dpr')!r}, which "
             "no size can be checked against — nothing was written")
    want = ([_int(metrics.get("sw")), _int(metrics.get("sh"))] if full
            else [_int(metrics.get("iw")), _int(metrics.get("ih"))])
    expected = [_pixels(css, dpr) for css in want]
    size = _png_size(data)
    if not size:
        fail("screenshot-not-verified",
             "the bytes are not a PNG (no signature, no IHDR) — nothing was "
             "written")
    if size != expected:
        fail("screenshot-not-verified",
             f"the PNG is {size[0]}x{size[1]} but this page says the "
             f"{'document' if full else 'viewport'} is {want[0]}x{want[1]} CSS "
             f"px at devicePixelRatio {dpr:g} ({expected[0]}x{expected[1]} "
             "pixels) — nothing was written")
    _write_shot(target, data, force)
    return {"ok": True, "path": target, "bytes": len(data),
            "width": size[0], "height": size[1], "full": bool(full),
            "device_pixel_ratio": dpr, "css_size": want, "verified": True,
            "url": metrics.get("url"), "title": metrics.get("title"),
            "visibility": metrics.get("visibility"),
            "note": ("the file's IHDR matches the page's own geometry; a "
                     "read, so no attach is needed"),
            "tab": f"id:{tab_row['id']}",
            "browser": tabs._brief(row)}           # noqa: SLF001


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


def _at_point(at: str, verb: str = "tab scroll") -> tuple[int, int]:
    """`--at X,Y` as two numbers — the SYNTAX, which needs no browser.

    `verb` is only for the refusal: three verbs take a point now (scroll,
    click, hover), and a message naming the wrong one is a message about the
    wrong verb.
    """
    parts = str(at).replace(" ", "").split(",")
    if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
        fail("bad-args", f"{verb}: --at needs X,Y numbers, got {at!r}")
    return _int(parts[0], -1), _int(parts[1], -1)


def _ints(value: object, count: int = 0) -> list[int]:
    """A numeric list from the page, or [] — never a TypeError out of a verb.

    `_well_formed` checks that KEYS exist, and its callers then unpack the
    values (`box`, `center`, `point`, `viewport`); a page that answers
    `{"point": 7}` raised `TypeError` out of the verb instead of refusing, and
    the page owns every value on that path (a review flagged it). A value that
    is not a list at all is []: iterating a dict would have produced its KEYS as
    numbers, which is worse than nothing.
    """
    if not isinstance(value, (list, tuple)):
        return []
    out = [_int(item) for item in value]
    return out[:count] if count else out


def _list(value: object) -> list:
    """A page-supplied list, or [] — never a TypeError out of a verb.

    The sibling of `_ints` for sequences of NAMES: `tab select` sliced
    `probe["labels"]` and `probe["candidates"]` unguarded, so a page that
    answered a number or an object raised `TypeError` instead of the intended
    `no-match`/`ambiguous-option` refusal (a review flagged it). A dict is not
    a list here: its keys would be read as labels.
    """
    return list(value) if isinstance(value, (list, tuple)) else []


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
        fail("bad-args",
             f"{verb}: --at {at!r} is outside the viewport {viewport}")
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
             + (f" (--index {index} is past the end)" if index else "")
             + _frames_note(row, tab_row))
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
        frames_here = _frame_summary(row, tab_row, session)
    if not isinstance(data, dict):
        fail("cdp-error", "tab text: the page did not answer with an object")
    if not data.get("found"):
        fail("no-match", f"tab text: no element matches {css!r}"
             + _frames_note(row, tab_row))
    reply = _reply(row, tab_row, data)
    reply.update({"ok": True, "selector": data.get("selector"),
                  "viewport": _ints(data.get("viewport")),
                  "text": str(data.get("text") or ""),
                  "length": _int(data.get("length")),
                  "truncated": bool(data.get("truncated"))})
    if frames_here:
        reply["frames"] = frames_here
    return _with_frame(reply)


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
        element = _pick(data, needle, css, index, row=row,
                           tab_row=tab_row)
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
    reply = {"ok": True, "verb": rung, "chars": len(text),
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
                        "", css, index, row=row, tab_row=tab_row)
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


def _media_reply(row: dict, tab_row: dict, state: dict, mode: str,
                 before: dict | None = None) -> dict:
    """The media reply: what the PAGE reports, plus the clock as evidence."""
    reply = {"ok": True, "mode": mode,
             "element": state.get("element"),
             "count": _int(state.get("count")),
             "playing": bool(state.get("playing")),
             "paused": bool(state.get("paused")),
             "ended": bool(state.get("ended")),
             "muted": bool(state.get("muted")),
             "volume": _num(state.get("volume")),
             "rate": _num(state.get("rate")),
             "time": _num(state.get("time")),
             "duration": _num(state.get("duration")),
             "ready_state": _int(state.get("ready_state")),
             "src": str(state.get("src") or ""),
             "tab": f"id:{tab_row['id']}",
             "browser": tabs._brief(row)}  # noqa: SLF001
    if before is not None:
        reply["time_before"] = _num(before.get("time"))
        reply["advanced"] = _num(state.get("time")) > _num(before.get("time"))
    return reply


def _poll_media(session: cdp.Session, mode: str, before: dict,
                timeout: float = MEDIA_TIMEOUT_S) -> dict:
    """Poll the page's own playback state until it followed, or the deadline.

    A play() the browser REJECTED stops the poll at once: the reason (the
    autoplay policy, no supported source) is the answer, and waiting would
    only make the caller wait for it.
    """
    deadline = time.time() + timeout
    state = before
    while True:
        got = session.evaluate(MEDIA_STATE_EXPR)
        state = got if isinstance(got, dict) else {}
        if not state.get("found"):
            return state
        verified, _why = _playback_verdict(mode, before, state)
        if verified or state.get("error"):
            return state
        if time.time() >= deadline:
            return state
        time.sleep(0.2)


def media(mode: str, index: int | None = None, tab: str = "",
          browser: str = "") -> dict:
    """`tab media state|play|pause`: read, start or stop the page's media.

    There is no CDP method for playback — the `Media` domain is experimental
    and event-only, and a media KEY is a toggle aimed at whichever session has
    focus (a background tab's audio, or nothing) — so this verb drives the
    ELEMENT and then VERIFIES what the page reports: a play that leaves
    `paused: true` refuses `media-not-verified`, a `play()` the browser rejects
    (the autoplay policy, no supported source) refuses `media-blocked` with the
    reason, and the state read is `tab media state`. `--index` picks among
    several players; without it the playing one — else the largest — is used,
    and `count` says how many the page has.
    """
    name = mode_of(mode)
    if name not in ("state", "play", "pause"):
        fail("bad-args", f"tab media: MODE is state|play|pause, got {mode!r}")
    if index is not None and name == "state":
        fail("bad-args", "tab media state: --index is for play/pause")
    row, tab_row = _resolve(tab, browser, for_write=(name != "state"))
    with _session(row, tab_row) as session:
        before = session.evaluate(MEDIA_STATE_EXPR)
        before = before if isinstance(before, dict) else {}
        if not before.get("found"):
            fail("no-media",
                 f"tab media: the page has no video or audio element "
                 f"({_int(before.get('count'))} found)")
        if name == "state":
            return _media_reply(row, tab_row, before, name)
        # `__MODE__` here is play|pause, NOT the matcher's text|selector, so
        # this expression is filled directly (and -1 means "the preferred one")
        session.evaluate(
            MEDIA_ACTION_EXPR.replace("__MODE__", json.dumps(name))
            .replace("__INDEX__",
                     str(-1 if index is None else _int(index))))
        after = _poll_media(session, name, before)
    if not after.get("found"):
        fail("no-media", f"tab media {name}: the element left the page")
    verified, why = _playback_verdict(name, before, after)
    if not verified:
        if after.get("error"):
            fail("media-blocked",
                 f"tab media {name}: the page refused: {after['error']}" + (
                     " — if that is the autoplay policy, a real gesture is "
                     "needed first (`tab click` the player)"
                     if "interact" in str(after.get("error")) else ""))
        fail("media-not-verified", f"tab media {name}: {why}")
    return _media_reply(row, tab_row, after, name, before)
