# Lane 3: browser_control/lib/dom.py (2778 lines, the DOM tier)

## Summary
- The slice is one module holding 18 `tab` verbs (`js`, `wait`, `find`, `text`, `extract`, `click`, `hover`, `scroll`, `focus`, `press`, `insert`, `type_text`, `upload`, `check`, `select`, `dialog`, `screenshot`, `media`) plus frame resolution, 18 page-side JS expressions, PNG/file writing, key tables, and 40+ private helpers — the largest module in the package (dom.py 2778 lines vs browser.py 2455, cdp.py 888, profile.py 522, capabilities.py 302, audit.py 225, plugins.py 196).
- Biggest structural problems, ranked: (1) no object owns "resolve a tab → open one connection → read the page → shape the reply", so that prelude is retyped 21 times; (2) the aim/hit-test/dispatch block and the six poll loops are hand-copied per verb; (3) the reply envelope and its verdict convention have three shapes and are hand-built by 13 verbs; (4) page-side JS lives as 18 `PRELUDE`-concatenated constants plus 4 inline literals, filled by 20 ad-hoc `.replace("__X__", …)` sites, with `__MODE__` meaning two different things; (5) the `--frame` scope is a process-global dict written from inside `_document_ws` and read from `_frame_summary`/`_with_frame`.
- One module-level mutable global is written at runtime: `FRAME` (dom.py:86); `KEYS` (dom.py:159) is a mutable dict treated as a constant. Two functions reach into `browser` privates (`tabs._one_tab` at 648, `tabs._brief` at 22 sites), and `cli/main.py:705` reaches into `dom._resolve`.
- What is already fine: `cdp.Session` genuinely owns one connection per verb (cdp.py:667-888); the single `PRELUDE` (dom.py:181-254) is one definition of what an element IS, shared by all 18 expressions; the pure oracles (`_is_secret` 1070, `_text_verdict` 1101, `_playback_verdict` 1080, `_png_size` 2019, `_pixels` 2032) are testable without a browser, and the tests prove it; every refusal goes through `fail(code, msg)`; frame scoping is applied in exactly one place (`_document_ws`, 651-668) so no verb can forget it.
- Verdict: **OK with notes** — no correctness defect, contract violation, or dead code found in this pass; every finding below is structural-only and contract-preserving. Because this is a whole-file structural review (no diff), findings are about shape, not about changes made by any commit.

## Findings

### F-3.1 dom.py is one module for five separable subsystems
- category: package-split
- locations: browser_control/lib/dom.py:1 — module docstring; browser_control/lib/dom.py:141 — `FRAME_VERBS`; browser_control/lib/dom.py:683 — `frames_of`; browser_control/lib/dom.py:936 — `_query_args`; browser_control/lib/dom.py:1532 — `click`; browser_control/lib/dom.py:2019 — `_png_size`; browser_control/lib/dom.py:2731 — `media`
- evidence:
  - The module docstring fronts six verbs — `"""dom — what a page READS and ACTS like: js, wait, find, text, click, scroll.` (dom.py:1) — while `FRAME_VERBS` (141-145) lists 17 and `cli/main.py` dispatches 18 dom verbs (`TAB_SUBCOMMANDS`, main.py:938-966: `"hover"`, `"check"`, `"select"`, `"dialog"`, `"screenshot"`, `"extract"`, `"media"`, …).
  - Contiguous subsystems by line range: frame scope + census 683-935 (`frames_of`, `frames`, `_frame_target`, `_frame_summary`, `_frames_note`); matching/rows 936-1035 (`_query_args`, `_matches_in`, `_pick`, `_safe`, `_describe`, `_element`, `_match_args`, `_node_of`); text-write oracle 1052-1119 (`_text_target`, `_is_secret`, `_playback_verdict`, `_text_verdict`); pointer verbs 1424-1901 (`_at_point_click`, `_click_at`, `_hover_at`, `click`, `hover`, `check`, `select`); dialog 1902-2018; PNG/file writing 2019-2130; scroll 2191-2416; text-write verbs 2417-2683; media 2684-2778.
  - Page-value coercion helpers are scattered in four places — `_int` 610, `_num` 617, `_well_formed` 625, `_viewport` 635, `_safe` 995, `_ints` 2251, `_list` 2267 — and are used by every cluster above.
  - `EXTRACT_EXPR` is defined at 1206, i.e. ~700 lines below the other 17 page-side expressions (256-607) and after two verbs have been defined; `_query_args`/`_match_args` (936, 1023) sit between the frame subsystem and the verbs that use them.
- proposal: turn the module into a package `browser_control/lib/dom/` whose `__init__.py` re-exports the frozen surface (18 verbs, `frame`, `frame_resolved`, `mode_of`, `FRAME_VERBS`, `TEXT_CAP`/`FIND_CAP`/`EXTRACT_CAP`/`EXTRACT_FIELD_CHARS`/`WAIT_DEFAULT_S`/`IDLE_DEFAULT_MS`/`WAIT_EXPRS`), and move the subsystems into `dom/tab.py`, `dom/scripts.py`, `dom/pagedata.py`, `dom/queries.py`, `dom/actions.py`, `dom/scroll.py`, `dom/text_input.py`, `dom/media.py`, `dom/dialog.py`, `dom/screenshot.py`, `dom/frames.py`, `dom/js_wait.py`, plus the dependency-free `lib/images.py`. Callers that must not change: `cli/main.py:579, 582, 602, 626, 641, 663, 677, 698, 705, 722, 734, 751, 777, 780, 799, 819, 835, 855, 862, 878, 895` and `cli/main.py:1231, 1251, 1271, 1277` (`dom.frame`, `dom.FRAME_VERBS`, `dom.frame_resolved`).
- dependencies: F-3.2, F-3.3, F-3.4, F-3.5, F-3.6, F-3.7 all land *inside* this split; do F-3.1 first or the others move twice.
- risk: medium — re-exporting keeps `dom.<verb>` and `dom._resolve` resolvable, but 12 tests monkeypatch `dom.<name>` attributes and assert positional call shapes (see F-3.12), so the re-export list is load-bearing.
- acceptance:
  - `python3 tests/test_unit.py` green (no test edits) and `browser-control-cli selftest` exits 0.
  - no file under `browser_control/lib/dom/` exceeds ~400 lines.
  - `grep -n "^def \|^class " browser_control/lib/dom/*.py` shows one responsibility per file, and `lib/dom/__init__.py` contains only imports/`__all__`.
  - import direction is `cli -> lib.dom.* -> lib.browser -> lib.cdp`: `grep -rn "lib import dom\|lib\.dom" browser_control/lib/*.py` is empty and nothing in `lib/` imports `lib/dom`.

### F-3.2 The per-verb prelude (resolve → connect → read → pick) is retyped 21 times
- category: class-candidate
- locations: browser_control/lib/dom.py:646 — `_resolve`; browser_control/lib/dom.py:671 — `_session`; browser_control/lib/dom.py:1120 — `_reply`; browser_control/lib/dom.py:1532 — `click`; browser_control/lib/dom.py:2731 — `media`
- evidence:
  - `row, tab_row = _resolve(tab, browser, for_write=…)` appears 21 times in dom.py (1138, 1176, 1353, 1391, 1552, 1554, 1633, 1636, 1718, 1805, 1946, 1983, 2146, 2226, 2427, 2461, 2506, 2580, 2601, 2651, 2750) plus once in `cli/main.py:705`.
  - `with _session(row, tab_row) as session:` is repeated for each of those, and the five-line read/pick prelude is byte-identical in six verbs: `_matches_in(session, needle, css, FIND_CAP)` then `_pick(data, needle, css, index, row=row, tab_row=tab_row)` at 1556-1558 (click), 1638-1640 (hover), 1720-1723 (check), 1807-1809 (select), 2463-2465 (focus), and via `CANDIDATES_EXPR` at 2653-2657 (upload).
  - "Page facts, no matching" is obtained by calling the *matcher* with an empty needle — `_matches_in(session, "", "", 1)  # page facts, no matching` at 1457, 1487, 2229 — so three verbs depend on `FIND_EXPR` for something that is not a match.
  - The envelope is half-shared: `_reply(row, tab_row, data)` (1120) is used by 3 verbs (1369, 1413, 2438), while 22 sites hand-write `"browser": tabs._brief(row)` and 21 hand-write `"tab": f"id:{tab_row['id']}"`.
  - The verb signatures are load-bearing: `tests/test_unit.py:884` fakes `dom.js/find/text/wait` and asserts the recorded positional args verbatim (test_unit.py:902-908: `("find", "Save", None, dom.FIND_CAP, "", "")`), same for `click`/`scroll` at test_unit.py:986-993.
- proposal: `browser_control/lib/dom/tab.py :: class Tab` — constructed INSIDE each verb from `(tab, browser, for_write)` so the public verb signatures (and the fakes in test_unit.py) do not change. It owns: the two rows, the frame scope, ONE `cdp.Session` (`with Tab.open(...) as tab:` replacing `_resolve`+`_session`), `Tab.facts()` (replacing `_matches_in(session,"","",1)`), `Tab.target(needle, css, index=None)` (replacing `_matches_in`+`_pick`), `Tab.node(expression)` (replacing `_node_of` 1036), and `Tab.reply(**fields)` (replacing `_reply` + the 22 `tabs._brief` sites).
- dependencies: F-3.1 (file), F-3.5 (reply shape), F-3.6 (frame scope moves onto the object).
- risk: medium — 21 call sites plus 6 test fakes; a mechanical change, but the frame/session side effects must land identically.
- acceptance:
  - `grep -c "with _session(row, tab_row)" browser_control/lib/dom/**` == 0 and `grep -c "tabs\._brief(row)" browser_control/lib/dom/**` == 0.
  - `grep -rn "cdp\.Session(\|cdp\.target_ws(" browser_control/lib/dom/` non-empty only in `tab.py`/`frames.py`.
  - `python3 tests/test_unit.py` green with no change to the recorded call tuples at test_unit.py:902-908 and 986-993.

### F-3.3 The "aim at an element then dispatch" block is copy-pasted in click/hover/check
- category: duplication
- locations: browser_control/lib/dom.py:1574 — `point = _ints(...)` (click); browser_control/lib/dom.py:1652 — `point = _ints(...)` (hover); browser_control/lib/dom.py:1744 — `point = _ints(...)` (check); browser_control/lib/dom.py:1436 — `_at_point_click`
- evidence:
  - The same four guards and the same dispatch are repeated: `if not element.get("in_viewport"): fail("no-viewport-target", …"scroll it into view first"…)` at 1560-1564, 1642-1646, 1734-1738; `if not element.get("hit"): fail("occluded", …)` at 1565-1570, 1647-1650, 1739-1742; `point = _ints(element.get("hit_at") or element.get("point"), 2)` + the "reports no usable point" refusal at 1574-1582, 1652-1660, 1744-1752 (the comment "both review lanes found the unpack behind the `_ints` guard" is copied verbatim three times).
  - The three-event left click `for kind, buttons in (("mouseMoved", 0), ("mousePressed", 1), ("mouseReleased", 0)): session.call("Input.dispatchMouseEvent", {…"clickCount": 1})` exists three times: `_at_point_click` 1440-1442, click 1585-1590, check 1755-1760.
  - `_hover_at` (1483) reimplements the same `mouseMoved` (1490) and then reads a *second, inline* hover oracle at 1493-1498.
- proposal: `browser_control/lib/dom/actions.py :: Tab.aim(element, needle, css) -> tuple[int, int]` (the three guards, one refusal each) and `:: click_point(session, x, y)` (the dispatch triple); `_at_point_click`, `click`, `check` call them. Migrate call sites dom.py:1440, 1574-1590, 1652-1665, 1744-1760.
- dependencies: F-3.2 (needs `Tab`), F-3.8 (the hover oracle is the same idea).
- risk: low — the refusals must keep their exact codes/messages (`no-viewport-target`, `occluded`) and the pressed point must stay `hit_at or point`.
- acceptance:
  - `grep -rn "scroll it into view first" browser_control/lib/dom/` matches exactly once.
  - `grep -rn "clickCount" browser_control/lib/dom/` matches exactly once (plus the `--at` helper if kept separate).
  - `python3 tests/test_unit.py` green; the live battery (supervisor must run) still reports `occluded` and `no-viewport-target` checks passing.

### F-3.4 Page-side JS is 18 PRELUDE-concatenated constants, 4 inline literals, and 20 `.replace()` sites
- category: lib-module
- locations: browser_control/lib/dom.py:181 — `PRELUDE`; browser_control/lib/dom.py:256 — `FIND_EXPR`; browser_control/lib/dom.py:1206 — `EXTRACT_EXPR`; browser_control/lib/dom.py:1023 — `_match_args`; browser_control/lib/dom.py:1431 — inline `elementFromPoint` literal; browser_control/lib/dom.py:1494 — inline hover literal
- evidence:
  - `PRELUDE` (181-254) is concatenated into 18 expressions (256, 303, 325, 336, 351, 368, 383, 401, 413, 433, 455, 492, 509, 529, 564, 575, 599, 1206) — good sharing — but four JS bodies bypass it entirely as Python literals: `_under_point` (1430-1433), `_hover_at` (1493-1498), the `"1"` probes (1953, 1997), and `WAIT_EXPRS["js"]` (604-607).
  - Placeholder filling is done by three different mechanisms and 20 `.replace("__X__", …)` calls: `_match_args` (1023-1034), `.replace` chains at 949-952, 1667, 2300, 2763-2765, 1179-1181, 1356, 1781, 2430-2431, 2654-2655.
  - `__MODE__` is overloaded: `_match_args` fills it with `"selector"|"text"`, while `MEDIA_ACTION_EXPR` (455) expects `play|pause` and is therefore filled by hand at 2763 with the explicit comment that it "is filled directly (and NOT the matcher's text|selector)".
  - `FIND_EXPR` is asserted structurally by name in tests (`dom.FIND_EXPR.count("__SELECTOR__") == 1`, test_unit.py:946) and `WAIT_EXPRS` is iterated by `cli/main.py:1078`.
- proposal: `browser_control/lib/dom/scripts.py` holds `PRELUDE`, every named expression (including the four inline literals, promoted to `POINT_PROBE`/`POINT_HOVER_PROBE`/`DIALOG_AWAKE`/`JS_BOOL`), `WAIT_EXPRS`, and ONE `fill(expression: str, **values) -> str` that raises on an unknown/leftover placeholder. Verbs call `fill()`; `_match_args` becomes `fill(FIND_EXPR, mode=…, needle=…, …)`.
- dependencies: F-3.1, F-3.3 (hover probe), F-3.8.
- risk: medium — a missed placeholder currently ships as a runtime `js-error` (progress.md §5.13 records exactly that class of bug), so `fill()` must refuse leftovers rather than silently leaving `__X__` in the page string.
- acceptance:
  - `grep -rn '\.replace("__' browser_control/lib/dom/` matches only inside `scripts.py`.
  - `fill()` refuses a leftover placeholder (a hermetic test asserts it), and `dom.FIND_EXPR.count("__SELECTOR__") == 1` still holds (test_unit.py:946).
  - no Python string literal containing `document.` or `elementFromPoint` outside `scripts.py`.

### F-3.5 No Result/outcome type: the reply envelope and its verdict convention have three shapes
- category: class-candidate
- locations: browser_control/lib/dom.py:1120 — `_reply`; browser_control/lib/dom.py:2548 — `_text_reply`; browser_control/lib/dom.py:2684 — `_media_reply`; browser_control/lib/dom.py:1080 — `_playback_verdict`; browser_control/lib/dom.py:1101 — `_text_verdict`; browser_control/lib/dom.py:1881 — inline select verdict
- evidence:
  - Three "verified" conventions coexist: `_playback_verdict(mode, before, after) -> tuple[bool, str]` (1080), `_text_verdict(before, after, chars) -> tuple[bool | None, str]` (1101, the tri-state is deliberate), and an inline comparison in `select` (1881-1887: `if _int(after.get("selected"), -1) != target or str(after.get("value")) != str(probe.get("target_value"))`), plus inline `"verified": False` + a prose `note` in `js` (1143), `press` (2524), `_click_at` (1479), `_hover_at` (1514).
  - `check` (1726-1772) and `select` (1806-1890) each re-implement "act, then poll the control's own property to a deadline" with their own before/after dicts.
  - Two different notions of a page snapshot: `changed = (… [after.get("x"), after.get("y")] != [before.get("x"), before.get("y")])` at 1465-1471 (`_click_at`) versus `before = {"url":…, "title":…, "active":…, "scroll":…}` and `[after.get("x"), after.get("y")] != before["scroll"]` at 1594-1599 (`click`) — the same rule, two shapes, one of which will silently compare `None` to a list if `STATE_EXPR` changes.
  - The precedent for a shared reply already exists in three places (`_reply`, `_text_reply`, `_media_reply`), so the abstraction is proven, just not applied to the other 13 verbs.
- proposal: `browser_control/lib/dom/result.py :: class PageState` (dict-from-`STATE_EXPR` with `changed(other) -> bool`) and `:: class Verdict(verified: bool | None, why: str)`; `Tab.reply(**fields)` builds `{ok, tab, browser, url, title, visibility, **fields}` so no verb can omit `tab`/`browser`. Verdict producers (`_playback_verdict`, `_text_verdict`, select's rule) return `Verdict`; refusals still go through `fail()` in the verb.
- dependencies: F-3.2 (Tab.reply), F-3.8 (`changed`), F-3.9 (the poll loops consume verdicts).
- risk: medium — the JSON keys (`verified`, `changed`, `note`, `length_before/after`) are frozen, so the type must be a thin wrapper, not a re-encoder.
- acceptance:
  - `grep -rc '"browser": tabs._brief(row)' browser_control/lib/dom/` == 0 and `grep -rn '"tab": f"id:{tab_row' browser_control/lib/dom/` == 0.
  - `grep -rn "changed = (" browser_control/lib/dom/` matches only in `result.py`.
  - `python3 tests/test_unit.py` green (it asserts `set(_playback_verdict(...))`-style tuple unpacking at test_unit.py:1296-1310 — those call sites either stay tuple-shaped or the test is updated as part of the change).

### F-3.6 `FRAME` is process-global state written from inside the connection helper
- category: hidden-state
- locations: browser_control/lib/dom.py:86 — `FRAME: dict[str, Any]`; browser_control/lib/dom.py:664 — write; browser_control/lib/dom.py:866 — read; browser_control/lib/dom.py:1525 — read
- evidence:
  - `FRAME: dict[str, Any] = {"wanted": "", "resolved": None}` (86) is the only module-level mutable global written at runtime; it is written inside `_document_ws` (660-668: `if FRAME["wanted"]: … FRAME["resolved"] = {"index":…, "url":…, "target":…}`) as a side effect of *connecting*, not of resolving.
  - `_frame_summary` (842) reads the ambient scope to choose its connection: `if session is not None and not FRAME["wanted"]: census = session.evaluate(FRAME_CENSUS)` (866-869) — a helper whose result depends on process state rather than on its arguments.
  - `_with_frame` (1518) reads it twice to decorate replies (1525-1528), and `frames()`/`_frame_target` reach `tabs._brief(row)` (785) and `cdp.port_of` directly.
  - Tests poke the global directly, including its aliasing rule: `dom.FRAME["resolved"] = {...}` then `assert dom.frame_resolved() is not dom.FRAME["resolved"]` (test_unit.py:2043-2047).
  - The same pattern exists next door for `--profile` (`browser.SCOPE`, browser.py:121), so this is a package-wide idiom, not a dom-only wart.
- proposal: `browser_control/lib/dom/frames.py :: class Frames(row, tab_row)` owning `port`, `page_target`, the census cache, and the resolved scope (`wanted`, `resolved`); the CLI keeps calling `dom.frame(value)` / `dom.frame_resolved()` as thin process-scope shims in `dom/__init__.py` (or a shared `lib/scope.py` if browser.py is refactored in the same campaign), and `Tab` carries a `Frames` instance instead of reading a global.
- dependencies: F-3.1, F-3.2.
- risk: medium — the scope must stay cleared per invocation (`cli/main.py:1231` calls `dom.frame(flags["frame"] or "")` every run) and `frame_resolved()` must keep returning a copy.
- acceptance:
  - `grep -rn "^FRAME\b\|FRAME\[" browser_control/lib/dom/` matches only inside `frames.py`'s shim (or `__init__.py`).
  - no module-level mutable dict other than declared constants in `browser_control/lib/dom/**`.
  - test_unit.py's frame checks (`t_frames_and_points`, `t_frames_bind_to_their_tab`) pass unchanged.

### F-3.7 The screenshot PNG/file layer has zero CDP dependency and belongs in a lib module
- category: lib-module
- locations: browser_control/lib/dom.py:2019 — `_png_size`; browser_control/lib/dom.py:2032 — `_pixels`; browser_control/lib/dom.py:2053 — `_shot_target`; browser_control/lib/dom.py:2077 — `_write_shot`
- evidence:
  - `_png_size(data: bytes) -> list[int]` (2019) reads the IHDR; `_pixels(css, dpr)` (2032) is arithmetic + refusal; `_shot_target(path)` (2053) validates an absolute `.png` path; `_write_shot(target, data, force)` (2077) does exclusive-open/atomic-replace with `O_CREAT|O_EXCL|O_NOFOLLOW` — 112 lines using only `os`, `math`, `contextlib` and `fail`, never `cdp`, `session`, or a browser row.
  - The tests already exercise them as pure functions with no session (`dom._png_size`, `dom._pixels`, `dom._shot_target` — test_unit.py:348-366).
  - Only `screenshot()` (2131) mixes the two concerns: `session.evaluate(SHOT_METRICS)` (2148) and `session.call("Page.captureScreenshot", …)` (2151) then the file layer.
- proposal: `browser_control/lib/images.py` (new lib module, imports only `math`, `os`, `contextlib`, `browser_control.lib.errors`) carrying `png_size`, `expected_pixels`, `output_path`, `write_atomic`; `dom/screenshot.py` keeps `screenshot()` and the geometry comparison, and imports `images`. `lib/images.py` must not import `cdp`, `browser`, or `dom`.
- dependencies: F-3.1.
- risk: low — pure move; the refusal codes (`bad-args`, `file-exists`, `write-failed`, `screenshot-not-verified`) travel with the functions.
- acceptance:
  - `python3 -c "import browser_control.lib.images"` succeeds with `browser_control.lib.cdp` absent from `sys.modules`.
  - `grep -rn "PNG_SIG\|O_NOFOLLOW" browser_control/lib/dom/` is empty.
  - test_unit.py's `t_screenshot_rules_and_symlinks` assertions pass with the new import path (test edit allowed for the import only).

### F-3.8 Two implementations of "did the hover land", two of "did the page change"
- category: duplication
- locations: browser_control/lib/dom.py:492 — `HOVER_PROBE`; browser_control/lib/dom.py:1493 — inline hover literal; browser_control/lib/dom.py:1465 — `changed` in `_click_at`; browser_control/lib/dom.py:1597 — `changed` in `click`
- evidence:
  - `HOVER_PROBE` (492-507) already asks `document.elementFromPoint(__X__, __Y__)` for `{found, hovered, chain, under}`, while `_hover_at` (1483) evaluates a *different inline* script at 1493-1498 that returns `{under, hovered}` with no `chain` — so the `--at` path verifies a strictly weaker claim than the element path from a second, unshared string.
  - `_under_point` (1424) is a third `elementFromPoint` script (1430-1433) used by `_at_point_click` (1443) and `click` (1593).
  - The page-change rule exists twice with incompatible shapes: 1465-1471 compares two `STATE_EXPR` dicts field-by-field including `[after["x"], after["y"]]`, while 1597-1599 rebuilds `before` from the matcher's payload (`{"url","title","active","scroll"}`) and compares `before["scroll"]` — the same rule, two dict layouts, and the second is where a missing field degrades to a silent inequality.
- proposal: put `POINT_PROBE`/`POINT_HOVER_PROBE` into `scripts.py` (F-3.4) and have both hover paths go through `Tab.hovered(x, y) -> Verdict`; replace both `changed` computations with `PageState.from_state(STATE_EXPR).changed(other)` (F-3.5).
- dependencies: F-3.4, F-3.5.
- risk: low-medium — `_hover_at` currently tolerates a missing `chain`, so unifying must keep `--at` refusing on `hovered == False` only (dom.py:1500-1503), not on `chain`.
- acceptance:
  - `grep -rn "elementFromPoint" browser_control/lib/dom/*.py` matches only `scripts.py`.
  - exactly one `changed` comparison expression in the package.
  - test_unit.py green; live battery's `--at` hover check unchanged (supervisor to run).

### F-3.9 Six hand-rolled poll loops, each with its own deadline bookkeeping
- category: duplication
- locations: browser_control/lib/dom.py:1760 — check loop; browser_control/lib/dom.py:1876 — select loop; browser_control/lib/dom.py:1994 — dialog loop; browser_control/lib/dom.py:2307 — `_settle`; browser_control/lib/dom.py:2396 — `_reveal`; browser_control/lib/dom.py:2716 — `_poll_media`
- evidence:
  - Each builds `deadline = time.time() + <budget>` (1760 `CHECK_TIMEOUT_S`, 1876 `CHECK_TIMEOUT_S`, 1994 `DIALOG_CLEAR_S`, 2307 the `timeout` arg, 2396 `SCROLL_MOVE_S`, 2716 `MEDIA_TIMEOUT_S`), then re-reads a page value and `time.sleep(0.15|0.2)` until the predicate or the deadline.
  - The pause constants differ without a stated reason (0.15 in 1762/1878/2309/2400, 0.2 in 1999/2714) while the budgets live in five different module constants (146-152, 74-80).
  - `cdp.evaluate_until` exists for exactly this shape (`wait`, dom.py:1183) but none of these six use it because each needs a typed read-back, not a boolean.
- proposal: `browser_control/lib/dom/poll.py :: until(read: Callable[[], Any], done: Callable[[Any], bool], timeout: float, pause: float = 0.15) -> Any` (returning the last read), used by the six sites; the per-verb budgets stay where they are.
- dependencies: F-3.5 (loops return verdicts), F-3.1 (destinations).
- risk: low — semantics are identical; only the copy-paste goes away.
- acceptance:
  - `grep -rc "deadline = time.time()" browser_control/lib/dom/` == 1 (in `poll.py`).
  - `grep -c "time.sleep(0" browser_control/lib/dom/` == 1, and no `time.sleep(0.008)` outside `text_input.py` (that one is a keystroke pause, not a poll).
  - `python3 tests/test_unit.py` green.

### F-3.10 Key triples have two sources of truth (`KEYS` vs a hardcoded Enter)
- category: duplication
- locations: browser_control/lib/dom.py:159 — `KEYS`; browser_control/lib/dom.py:2606 — hardcoded Enter in `type_text`; browser_control/lib/dom.py:1864 — `KEYS["arrowdown"…]`
- evidence:
  - `KEYS` (159-178) holds `(key, code, virtual key code, text)` and its docstring states the rule: "A wrong triple SILENTLY does nothing in the page" and "Only Enter and Space carry text".
  - `type_text` (2588) rebuilds the Enter triple by hand at 2606-2612 — `{"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}` — with no `text`/`unmodifiedText`, so it is a *third* variant of the same key that `press` sends from `KEYS["enter"]` (2505-2521, which does add `text: "\r"`).
  - `select` (1864) uses `KEYS["arrowdown" if delta > 0 else "arrowup"]` and discards the text slot with `_text`, a fourth reading of the same table.
  - `KEYS` is asserted by test (`for name, triple in dom.KEYS.items()`; `dom.KEYS["enter"][3] == "\r"` at test_unit.py:1149-1155).
- proposal: `browser_control/lib/dom/keys.py` with `KEYS` plus `key_event(name, kind) -> dict` (`rawKeyDown`/`keyDown`/`char`/`keyUp`, text attached only where `KEYS` declares it), and each verb expressing its intent as a call (`key_event("enter", "keyDown")` for the newline in `type`, `key_event("arrowdown", "rawKeyDown")` in `select`). Any behaviour difference between `press Enter` and `type "\n"` must be preserved deliberately, not by accident.
- dependencies: F-3.1.
- risk: medium — the two Enter spellings currently differ in whether `text` is set; the refactor must keep the measured behaviour (a `keyDown` without text does not submit a form, dom.py:156-158).
- acceptance:
  - `grep -rn "windowsVirtualKeyCode" browser_control/lib/dom/` matches only `keys.py`.
  - test_unit.py's `KEYS` assertions and `dom.KEYS["enter"][3] == "\r"` pass.
  - the live battery's press/type checks are unchanged (supervisor to run).

### F-3.11 The tier's public surface is implicit: the CLI and tests reach into privates
- category: naming
- locations: browser_control/lib/dom.py:648 — `tabs._one_tab`; browser_control/lib/dom.py:785 — `tabs._brief` (22 sites); browser_control/cli/main.py:705 — `dom._resolve`; browser_control/lib/dom.py:1120 — `_reply`
- evidence:
  - `# noqa: SLF001` appears 23 times in dom.py: once for `tabs._one_tab` (648) and 22 times for `tabs._brief` (785, 1124, 1143, 1203, 1479, 1514, 1610, 1682, 1732, 1774, 1851, 1899, 1976, 2016, 2188, 2380, 2414, 2489, 2524, 2562, 2681, 2701).
  - `cli/main.py:705` calls `dom._resolve(spec, browser, for_write=False)  # noqa: SLF001` and then `dom.frames(row, tab_row)` — a public verb whose two arguments are the private resolution's return value, so `frames` cannot be called without the private helper.
  - `cli/main.py` also reads implementation constants/wrappers across the boundary: `dom.EXTRACT_CAP`/`dom.EXTRACT_FIELD_CHARS` (680, 682), `dom.FIND_CAP` (724), `dom.TEXT_CAP` (736), `dom.WAIT_DEFAULT_S`/`dom.IDLE_DEFAULT_MS` (753-755), `dom.WAIT_EXPRS` (1078), `dom.mode_of` (1118).
  - The reverse direction is just as private: dom.py reaches `tabs._brief` (browser.py:951) and `tabs._one_tab` (browser.py:2089).
- proposal: state the boundary explicitly — in `lib/browser.py` promote `brief`/`one_tab` to public names (and keep `_brief`/`_one_tab` as aliases for one release so tests keep passing), and in `lib/dom/__init__.py` export a named surface (`__all__`) where `frames(...)` takes `(tab=..., browser=...)` like every other verb, so `cli/main.py:705` stops calling a private.
- dependencies: F-3.1, F-3.2; overlaps the browser.py lane.
- risk: low — pure renaming plus one signature addition; the CLI contract is untouched.
- acceptance:
  - `grep -c "SLF001" browser_control/lib/dom/*.py` == 0.
  - `grep -n "dom\._" browser_control/cli/main.py` is empty.
  - `lib/dom/__init__.py` declares `__all__` and every name in it is exercised by at least one CLI subcommand.

### F-3.12 The hermetic tests pin module attributes and verb signatures — the split must preserve both
- category: test-coupling
- locations: tests/test_unit.py:233 — attribute-patching of `dom.*`; tests/test_unit.py:902 — recorded call tuples; tests/test_unit.py:1979 — `dom.frames_of` reassignment; tests/test_unit.py:348 — private-name reaching
- evidence:
  - Tests replace module attributes wholesale: `(cli_main.activate, dom.hover, dom.check, dom.select, dom.dialog, dom.screenshot) = (fake_…)` (test_unit.py:233-236), `dom.js, dom.find, dom.text, dom.wait = (…)` (883-884), `dom.click, dom.scroll = fake_click, fake_scroll` (969-970), `(dom.focus, dom.press, dom.insert, dom.type_text, dom.upload) = (…)` (1248-1251), `dom.media = fake_media` (1327-1328), `dom.frames_of = lambda …` (1979, 1990, 1997, 2003).
  - Restoring those patches also writes to `cdp` by name: `cdp.evaluate = lambda ws, expr, timeout=15.0: census` (1913) and `setattr(cdp, name, fn)` (2034).
  - Call shapes are asserted token-for-token (test_unit.py:902-908, 986-993), so a change from `dom.find(text, selector, cap, tab, browser)` to a method-based API breaks the suite.
  - 40+ assertions reach privates with `# noqa: SLF001` (`dom._well_formed` 931, `dom._int` 935, `dom._png_size` 348, `dom._frame_summary` 1980, `dom._at_point` 2062, `dom._extract_schema` 2670, `dom._playback_verdict` 1296, …).
  - `tests/live_test.py` never imports `dom` (it goes through the CLI and `import_module("browser_control.lib.browser"|"cdp"|"audit")`, live_test.py:45-47), so it is contract-level and unaffected by any split.
- proposal: treat `dom.<verb>`, `dom.<CAP>`, `dom.frame`, `dom.frame_resolved`, `dom.mode_of`, `dom.FRAME_VERBS`, `dom.WAIT_EXPRS` as the module's frozen re-export surface in `lib/dom/__init__.py` (attribute lookups in `cli/main.py` then still resolve and monkeypatching still works), and add one new hermetic check that the surface is complete (every CLI `tab` dom subcommand has exactly one corresponding callable). Privates used by tests either stay importable under a `dom._internal` alias or are migrated with the test in the same change.
- dependencies: F-3.1, F-3.2, F-3.11.
- risk: medium — a split that moves functions without re-exporting them passes `python3 -m compileall` and fails 5 test functions; the CLI's attribute lookups are what make patching work today, so keep calling through the module.
- acceptance:
  - `python3 tests/test_unit.py` green with edits limited to private-import paths (`dom._x` → `dom.compat._x` or the new module) and none to the recorded call tuples.
  - a new hermetic check asserts the re-export surface: every name in `dom.__all__` exists and every CLI dom subcommand resolves to one of them.
  - `python3 tests/live_test.py` green (requires a browser — supervisor must run it).

## Proposed target layout for this slice
- browser_control/lib/dom/__init__.py — the frozen public surface only: 18 verb functions plus `frame`, `frame_resolved`, `mode_of`, `FRAME_VERBS`, `WAIT_EXPRS`, the caps, and `__all__`.
- browser_control/lib/dom/tab.py — `Tab` (row/tab_row resolution, ONE `cdp.Session`, frame scope, `facts()`, `target()`, `node()`, `aim()`, `reply()`) and `PageState`.
- browser_control/lib/dom/scripts.py — `PRELUDE`, every page-side expression (the 18 constants + the 4 promoted inline literals), `WAIT_EXPRS`, and the single `fill(expression, **values)`.
- browser_control/lib/dom/pagedata.py — page-value coercion with no browser: `as_int`, `as_num`, `ints`, `names`, `well_formed`, `safe_text`, `viewport`.
- browser_control/lib/dom/frames.py — `Frames` (scope, census cache), `frames`, `frames_of`, `frame_target`, `frame_summary`, `frames_note`.
- browser_control/lib/dom/queries.py — `find`, `text`, `extract`, `js`, `wait` and their parsers (`extract_field`, `extract_schema`, `extract_records`).
- browser_control/lib/dom/actions.py — `click`, `hover`, `check`, `select`, `focus`, `upload` and the shared `aim()`/`click_point()`.
- browser_control/lib/dom/scroll.py — `scroll`, `wheel`, `reveal`, `settle`, and the `--at` syntax helpers.
- browser_control/lib/dom/text_input.py — `insert`, `type_text`, `press`, `preflight`, `text_target`, `is_secret`, `text_verdict`, `text_reply`.
- browser_control/lib/dom/keys.py — `KEYS` and `key_event(name, kind)`.
- browser_control/lib/dom/media.py — `media`, `media_reply`, `poll_media`, `playback_verdict` (with the two media scripts owned here, including the `window.__bcMediaError` channel).
- browser_control/lib/dom/dialog.py — `dialog`, `NO_DIALOG_NOTE`, `NO_DIALOG_MARK`.
- browser_control/lib/dom/screenshot.py — the `screenshot` verb: capture, geometry comparison, reply.
- browser_control/lib/dom/result.py — `Verdict` and the reply/`changed` helpers.
- browser_control/lib/dom/poll.py — `until(read, done, timeout, pause)` for the six poll loops.
- browser_control/lib/images.py — PNG header parse, pixel arithmetic, output-path validation, atomic write; imports neither `cdp`, `browser`, nor `dom`.

## Open questions
- Package or flat siblings? `lib/dom/` (recommended: it keeps the import path stable and lets `__init__.py` carry the frozen surface) versus `lib/dom_reads.py`/`lib/dom_actions.py`/… — a human call, because tests import `browser_control.lib.dom` by name in ~60 places.
- Does `Tab` stay internal (each verb builds one; verb signatures and the fakes in test_unit.py:902-993 untouched) or do the verbs become methods on it (cleaner, but the fakes and the CLI call shapes must change)? I recommend internal, and the acceptance criteria above assume it.
- Should the `--frame` scope follow the `--profile` idiom and become a shared `lib/scope.py` object rather than two module globals (`dom.FRAME` 86, `browser.SCOPE` browser.py:121)? That decision crosses into the browser.py lane.
- Do we want one `Verdict`/`PageState` convention package-wide (browser.py has its own `moved`/`loaded`/`url_read` read-back dialect in `nav`), or is the outcome type confined to the DOM tier for now?
- Is `lib/images.py` the right home, or should screenshot's file layer sit under a `lib/fs.py` that also owns `audit`'s log rotation and `profile`'s copy/rename? Prompting the question because three lib modules now do atomic-write/path-validation of their own.
- Operational: the live battery (`python3 tests/live_test.py`) needs a real browser and is the only check that proves the frozen stdout/stderr discipline still holds after the split; a supervisor must run it.