"""scripts — the page-side JavaScript, as named expressions.

The page's DOM and `JSON.stringify` are the page's to override, so every
expression here treats what it reads as DATA. `PRELUDE` is one definition of
what an element IS, shared by every matcher.
"""
from __future__ import annotations

import re
from typing import Any

from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    fail,
)

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
  // WHICH document this expression reads: the page itself, or a same-process
  // FRAME's document. A same-process frame has no CDP target to attach to, so
  // there is nothing to open a session on — a read of one runs HERE, on the
  // page, rooted at that frame's `contentDocument`, which is the only way to
  // read it WITHOUT running caller code. The `typeof` guard is what makes every
  // expression containing this PRELUDE safe to evaluate with no root passed at
  // all (a verb that needs no other placeholder evaluates its constant raw): an
  // undeclared name under `typeof` is the string "undefined", not a
  // ReferenceError.
  const R = (typeof __ROOT__ === 'undefined') ? {doc: document, win: window}
                                              : __ROOT__;
  // Every top-level iframe the page shows, in the order `tab frames` prints
  // it: ONE walk, because the index a caller reads there is the index the
  // root below must resolve to.
  const frames = () => {
    const out = [];
    for (const root of roots(R.doc, [])) {
      for (const f of root.querySelectorAll('iframe')) out.push(f);
    }
    return out;
  };
  const query = (selector) => {
    const out = [];
    for (const root of roots(R.doc, [])) {
      for (const el of root.querySelectorAll(selector)) out.push(el);
    }
    return out;
  };
  const attr = (el, name) => (el.getAttribute ? el.getAttribute(name) : null);
  const textOf = (el) =>
    (el.innerText === undefined ? el.textContent : el.innerText) || '';
  // A page's own `document.title` and `location.href` are the PAGE's to make
  // huge, and both ride in every reply that calls itself page-bounded: a
  // measured 2 MB `document.title` made `tab find` print 2 000 517 characters
  // and `tab text` 2 240 308, because the caller stops passing a transport cap
  // for a read the page bounds itself (a review found it). Clip them IN THE
  // PAGE, and always to a string — the field is never null for a real page.
  const clip = (v, n) => String(v == null ? '' : v).slice(0, n);
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
    const s = R.win.getComputedStyle(el);
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

#: The root a page expression reads through when no frame scope is set: the
#: page's own document and window. `fill` supplies it for every expression that
#: carries `__ROOT__`, so a verb that wants a FRAME's document passes that
#: instead (see `FRAME_DOC`) and every other expression is unchanged.
PAGE_ROOT = "{doc: document, win: window}"

#: The root of a SAME-PROCESS frame's document, by the index `tab frames`
#: prints. A same-process frame has no CDP target of its own, so there is no
#: session to attach to — this is how a read reaches inside one without running
#: caller code: the expression still runs in the page, rooted at that frame's
#: `contentDocument`. The walk is `frames()`, the same one the census uses, so
#: the index here is the index `tab frames` showed.
FRAME_DOC = ("(() => {" + PRELUDE + r"""
  const f = frames()[__INDEX__];
  const doc = f && f.contentDocument;
  if (!doc) {
    throw new Error('no same-process document at frame index __INDEX__');
  }
  return {doc: doc, win: f.contentWindow};
})()""")

FIND_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const vw = window.innerWidth, vh = window.innerHeight;
  const cap = __CAP__;
  // `cap` rides in the reply: it is what lets a caller say whether the page
  // HAS more matches than were returned, instead of reporting a count that the
  // window it just printed contradicts (a review found `tab find --cap 50`
  // showing index 20 while `tab click --index 20` refused "10 element(s)
  // match")
  // `clip`: 300 characters of title and 2000 of URL is every real title and
  // every real address, and it is the page, not the transport, that bounds the
  // reply (see PRELUDE)
  const base = {url: clip(location.href, 2000), title: clip(document.title, 300),
                ready: document.readyState,
                visibility: document.visibilityState, cap: cap,
                viewport: [Math.round(vw), Math.round(vh)],
                active: describe(document.activeElement),
                scroll: [Math.round(scrollX), Math.round(scrollY)]};
  if (vw <= 0 || vh <= 0) {
    return Object.assign(base, {matches: [], total: 0, offscreen: 0,
                                truncated: false, degenerate: true});
  }
  const mode = __MODE__, needle = __NEEDLE__, selector = __SELECTOR__;
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
  // `title`/`url` are clipped like FIND_EXPR's: they are CONTEXT for the text,
  // and the text itself is what `--chars` bounds (see PRELUDE)
  const base = {url: clip(R.doc.location.href, 2000),
                title: clip(R.doc.title, 300),
                ready: R.doc.readyState,
                visibility: R.doc.visibilityState,
                viewport: [Math.round(R.win.innerWidth),
                           Math.round(R.win.innerHeight)],
                selector: selector || 'body'};
  const el = selector ? query(selector)[0] : R.doc.body;
  if (!el) {
    return Object.assign(base, {found: false, text: '', length: 0,
                                truncated: false});
  }
  const full = textOf(el);
  return Object.assign(base, {found: true, text: full.slice(0, cap),
                              length: full.length, truncated: full.length > cap});
})())""")

ELEMENT_EXPR = ("(() => {" + PRELUDE + r"""
  const mode = __MODE__, needle = __NEEDLE__, selector = __SELECTOR__,
        index = __INDEX__, visible = __VISIBLE__;
  const all = mode === 'selector' ? query(selector)
    : query(INTERACTIVE).filter(
        (el) => label(el).toLowerCase().indexOf(needle) >= 0);
  const live = all.filter((el) => !visible || rendered(el) !== null);
  return live[index] || null;
})()""")

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

TEXT_TARGET_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const el = document.activeElement;
  const empty = !el || el === document.body || el === document.documentElement;
  const frame = !!(el && el.tagName && el.tagName.toLowerCase() === 'iframe');
  const value = el && typeof el.value === 'string' ? el.value : null;
  const contenteditable = !!(el && el.isContentEditable);
  const text = contenteditable ? textOf(el) : null;
  // UNPROVABLE is not the same as "takes no text": a frame's active element, a
  // canvas, or any element the page drives with its own key handlers holds no
  // `value` and is not contentEditable, so this probe cannot READ what it
  // takes — and `Input.insertText` still goes to the page. Such a focus is
  // editable-but-unprovable (its `length` is null, which the verdict renders
  // `verified: false`); only an EMPTY focus (nothing, body, documentElement)
  // is `no-focus` (a review flagged the refusal this produced for a
  // frame/canvas the documentation promises an unclear verdict for).
  const unprovable = !empty && value === null && !contenteditable;
  // an EDITABLE BODY is a real focus: a document left editable as a whole
  // (`designMode`, or `<body contenteditable>`) puts `document.activeElement`
  // on the body, which the `empty` test above would otherwise call "nowhere to
  // type" — the probe measured it editable, so it says so and the verb takes
  // the unclear-or-verified path instead of refusing `no-focus` (a review
  // found the probe and the verb disagreeing about exactly this focus)
  const editableBody = !!(el && empty && el.isContentEditable);
  return {focused: !empty || editableBody, frame: frame,
          editable: editableBody || (!empty && (value !== null
                                                || contenteditable || frame
                                                || unprovable)),
          // a focus whose type cannot be read fails CLOSED, exactly like a
          // password field: the frame's document and an unreadable value are
          // both places a secret can land where this probe cannot see it
          secret: frame || unprovable || /type\s*=\s*["']?password\b/i.test(
            (el && el.outerHTML) || ''),
          active: describe(el), target: path(el),
          length: value !== null ? value.length
                  : (text !== null ? text.length : null)};
})())""")

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

SCROLL_PROBE = ("JSON.stringify((() => {" + PRELUDE + r"""
  const el = document.elementFromPoint(__X__, __Y__);
  const nested = [];
  for (let n = el; n; n = n.parentElement) {
    // EVERY scrolled ancestor on the chain, both axes: the wheel chains past
    // an exhausted inner scroller to an outer one, and a page can map it to
    // scrollLeft — reporting only the first scroller's scrollTop read both as
    // "nothing moved" (a review flagged it). Bounded so the probe stays a
    // probe; the document scroller is reported as `document` separately.
    if (n !== document.documentElement && n !== document.body &&
        (n.scrollTop || n.scrollLeft)) {
      nested.push([describe(n), Math.round(n.scrollTop),
                   Math.round(n.scrollLeft)]);
      if (nested.length >= 4) break;
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

CANDIDATES_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const mode = __MODE__, needle = __NEEDLE__, selector = __SELECTOR__,
        cap = __CAP__;
  const all = mode === 'selector' ? query(selector)
    : query(INTERACTIVE).filter(
        (el) => label(el).toLowerCase().indexOf(needle) >= 0);
  // `cap` rides along like FIND_EXPR's: an out-of-range `--index` must be able
  // to say how big the window it counted in was
  return {total: all.length, offscreen: 0, truncated: all.length > cap,
          cap: cap,
          matches: all.slice(0, cap).map((el) => ({
            tag: el.tagName.toLowerCase(), role: role(el), name: describe(el),
            text: label(el).slice(0, 120), in_viewport: false,
            clipped: false, box: [0, 0, 0, 0], center: [0, 0],
            viewport: [0, 0, 0, 0], point: [0, 0], hit: false,
            hit_element: null}))};
})())""")

MEDIA_STATE_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const all = Array.from(document.querySelectorAll('video, audio'));
  const area = (el) => { const r = el.getBoundingClientRect();
                         return Math.round(r.width * r.height); };
  const index = __INDEX__;
  // the SAME element the action drives: `__INDEX__` >= 0 is `all[index]`,
  // and -1 keeps the preferred rule — the element that is PLAYING first,
  // else the biggest one on the page, because a page can carry a preview clip
  // beside the real player. Picking independently of the action let an
  // unrelated already-playing element's clock "verify" a play aimed at
  // another one (a review found it), so both expressions share the pick.
  const playing = all.filter((el) => !el.paused && !el.ended);
  const el = index >= 0 ? (all[index] || null)
    : (playing[0] || all.slice().sort((a, b) => area(b) - area(a))[0] || null);
  // `index` in the reply is CONCRETE: the caller's, or the one the preferred
  // rule settled on AT READ TIME. That number — not the rule — is what the
  // verb drives and what every probe then reads. Re-evaluating `-1` on every
  // probe re-picked an element per call, so a `pause` the page really applied
  // was refused because the probe had moved to the other player, a reply
  // described `b.mp4` while `a.mp4` was the one paused, and a `play` on a dead
  // element was certified by an unrelated ad's clock (a review measured all
  // three). `at` is -1 only when nothing was found.
  const at = el ? all.indexOf(el) : -1;
  const base = {count: all.length, found: !!el, index: at,
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
  const action = __ACTION__, index = __INDEX__;
  const all = Array.from(document.querySelectorAll('video, audio'));
  const area = (el) => { const r = el.getBoundingClientRect();
                         return Math.round(r.width * r.height); };
  const playing = all.filter((el) => !el.paused && !el.ended);
  // the verb always passes the CONCRETE index the before-read settled on, so
  // this drives exactly the element that read proved; the -1 branch keeps the
  // pick shared with MEDIA_STATE_EXPR for a page whose identity read failed
  const el = index >= 0 ? (all[index] || null)
    : (playing[0] || all.slice().sort((a, b) => area(b) - area(a))[0] || null);
  if (!el) return {count: all.length, found: false};
  // a play() the browser REJECTS is the page refusing (no supported source, or
  // the autoplay policy): the promise is not awaited here — the reason is
  // parked where the read-back can name it
  window.__bcMediaError = null;
  let thrown = null;
  try {
    if (action === 'play') {
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
  // the element's identity rides back with the action's answer: the caller
  // compares it with the before-read's, so an element the page REPLACED under
  // the verb cannot have its clock certify an action aimed at the one before
  // it (a review measured a `play --index 0` certified by an arriving ad)
  return {count: all.length, found: true, index: index,
          element: el.tagName.toLowerCase(), paused: el.paused,
          src: String(el.currentSrc || el.src || '').slice(0, 120),
          error: window.__bcMediaError || null};
})())""")

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

SHOT_METRICS = ("JSON.stringify((() => {" + PRELUDE + r"""
  return {iw: Math.round(innerWidth), ih: Math.round(innerHeight),
          dpr: devicePixelRatio,
          sw: document.documentElement.scrollWidth,
          sh: document.documentElement.scrollHeight,
          url: location.href, title: document.title,
          visibility: document.visibilityState};
})())""")

FRAME_CENSUS = ("JSON.stringify((() => {" + PRELUDE + r"""
  return frames().map((f, index) => {
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
    #: `--match` is a SUBSTRING of the address, not a pattern: the wait is a
    #: boolean the page answers, and `location.href` is the one fact a
    #: client-side navigation cannot hide. A URL wait is what tells "the app
    #: took the tab over" from "the launch stub is still showing" — measured
    #: on a Slack permalink, where the stub answers `readyState complete` and
    #: `--for load` therefore passed while the address never left the stub.
    "url": "Boolean(String(location.href).indexOf(__MATCH__) >= 0)",
    "js": ("(() => { const v = (__EXPR__); if (v && "
           "(typeof v === 'object' || typeof v === 'function') && "
           "typeof v.then === 'function') return 'thenable'; "
           "return Boolean(v); })()"),
}

EXTRACT_EXPR = ("JSON.stringify((() => {" + PRELUDE + r"""
  const schema = __SCHEMA__;
  const scan = (root, selector) => {
    const out = [];
    // `:scope` means the match ITSELF, but querySelectorAll only ever returns
    // DESCENDANTS — a bare `:scope` (or `:scope.foo`) found nothing at all, so
    // the root is offered first when the selector can match it. The other
    // `:scope` forms (`:scope .x`) do not match the root and still come from
    // the walk below.
    const usesScope = selector.indexOf(':scope') >= 0;
    const visit = (r) => {
      if (r === root && usesScope && root.matches(selector)) out.push(root);
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
    // size the row BEFORE it is pushed: the budget is the WHOLE reply's, and
    // charging it after the push let one row ride over it — a row is
    // `#fields × --chars`, so a handful of fields blew the CDP transport cap
    // and `tab extract` refused `result-too-large` naming an expression the
    // caller never wrote (a review found it)
    const n = JSON.stringify(row).length;
    if (used + n > schema.budget) { budget_hit = true; break; }
    used += n;
    records.push(row);
  }
  return {
    // clipped like FIND_EXPR's: the RECORDS are what the schema's `budget`
    // bounds, and a 2 MB title must not ride over it (see PRELUDE)
    url: clip(location.href, 2000), title: clip(document.title, 300),
    visibility: document.visibilityState,
    total: candidates.length, matches: records,
    truncated: budget_hit || candidates.length > records.length};
})())""")


POINT_PROBE = ("(() => { const el = document.elementFromPoint(__X__, __Y__); "
               "return el ? el.tagName.toLowerCase()"
               " + (el.id ? '#' + el.id : '') : null })()")

POINT_HOVER_PROBE = (
    "(() => { const el = document.elementFromPoint(__X__, __Y__); return "
    "JSON.stringify({under: el ? el.tagName.toLowerCase() + "
    "(el.id ? '#' + el.id : '') : null, hovered: !!el && "
    "el.matches(':hover')}) })()")

#: The one value the dialog probes evaluate: the page answering at all is the
#: fact, and a constant keeps that from being spelled as a literal twice.
DIALOG_AWAKE = "1"


def fill(expression: str, **values: Any) -> str:
    """One expression with its placeholders filled, or a refusal.

    A missed placeholder used to ship as a runtime `js-error`; this refuses an
    unknown name (a caller that renamed one side) and a LEFTOVER placeholder (a
    template that grew a new one) at the point of the call.

    `__ROOT__` is the one placeholder a caller does NOT have to pass: every
    expression reads the PAGE unless a verb roots it at a same-process FRAME's
    document (`FRAME_DOC`). Defaulting it here means such a verb threads a root
    only when it has a different one — and the leftover check below still
    catches a root that was meant to be passed and was not.

    The scan and the substitution both run over the TEMPLATE, never over text a
    value brought with it: substituting in a loop across the growing expression
    rescanned the caller's own string, so a selector holding a token another
    value fills (`[data-k="__VISIBLE__"]`) was REWRITTEN inside the caller's
    CSS (a scroll then acted on `x[data-k="true"]`), and a legitimate uppercase
    token (`#__NEXT_DATA__`, `[id="__INITIAL_STATE__"]`) was refused as an
    unfilled placeholder (a review found both).
    """
    if "__ROOT__" in expression:
        values = {**values, "root": values.get("root") or PAGE_ROOT}
    filled: dict[str, str] = {}
    for name, value in values.items():
        token = f"__{name.upper()}__"
        if token not in expression:
            fail(ERR_BAD_ARGS,
                 f"fill: {token} is not a placeholder in this expression")
        filled[token] = str(value)
    found = {match.group(0)
             for match in re.finditer(r"__[A-Z0-9_]+__", expression)}
    leftovers = sorted(token for token in found if token not in filled)
    if leftovers:
        fail(ERR_BAD_ARGS,
             f"fill: unfilled placeholder(s) {', '.join(leftovers)}")
    # ONE pass over the template: a token that arrived inside a VALUE is text
    # the caller owns, and is never rescanned (see the docstring)
    out: list[str] = []
    end = 0
    for match in re.finditer(r"__[A-Z0-9_]+__", expression):
        out.append(expression[end:match.start()])
        out.append(filled[match.group(0)])
        end = match.end()
    out.append(expression[end:])
    return "".join(out)
