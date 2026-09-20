"""scripts — the page-side JavaScript, as named expressions.

The page's DOM and `JSON.stringify` are the page's to override, so every
expression here treats what it reads as DATA. `PRELUDE` is one definition of
what an element IS, shared by every matcher.
"""
from __future__ import annotations

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
  return {iw: innerWidth, ih: innerHeight, dpr: devicePixelRatio,
          sw: document.documentElement.scrollWidth,
          sh: document.documentElement.scrollHeight,
          url: location.href, title: document.title,
          visibility: document.visibilityState};
})())""")

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
