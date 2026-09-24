# browser-control — progress

Status: **the CLI surface is delivered, packaged, and covered by a committed
battery.** `python3 tests/test_unit.py` → 165 passed, 0 failed; the live battery
(`tests/live_test.py`) runs against a real browser — run it before a release,
and remember skip ≠ pass.
Plan: [`docs/plan.md`](plan.md). This file is the state of the work: what
exists, what it does *not* do yet, and what comes next.

> **Path note (post-refactor):** the sections below were written while the code
> was still monolithic. The packages are now `lib/cdp/`, `lib/browser/`,
> `lib/dom/`, `lib/profile/`, `lib/plugins/` and `cli/`; an older path maps to
> its package (`lib/cdp.py` → `lib/cdp/`, `lib/browser.py` → `lib/browser/`,
> `lib/dom.py` → `lib/dom/`).

## 0. The adversarial review round (latest revision)

Seven independent adversarial reviews (transport, lifecycle, DOM oracles,
profile/secrets, CLI/plugins, tests) found — and this revision fixed — the
defects below. The theme is the project's own stance applied to itself: where
a read-back, a deadline, a lock or a bound was *claimed*, it is now enforced.

- **Two unbounded hangs / OOMs**: the CDP endpoint probe now honours its
  wall-clock deadline against a peer that dribbles bytes (it blocked forever in
  `HTTPResponse.close()`), and `port_of`/`pid_of` read a bounded REGULAR file
  (`O_NOFOLLOW|O_NONBLOCK`, ≤ 64 bytes) — a FIFO hung them and a symlink to
  `/dev/zero` raised `MemoryError`.
- **Oracle lies**: `tab media --index N` now reads back the element it drove
  (an unrelated player's clock used to certify the write); `insert`/`type`
  report a SHORT landing as `verified: false` with `inserted`; `tab click`'s
  `changed`, `close`'s `tabs-open` guard and `tab wait`'s timeout are pinned by
  tests (a review proved all three shipped green when broken).
- **Fail-open seams closed**: a plugin that raises `SystemExit`/
  `KeyboardInterrupt` can no longer end the process (exit 0 with no JSON); an
  unopenable lock refuses `profile-unusable` instead of proceeding unlocked;
  `--deny egress` now denies the verbs that reach the network.
- **Bounds that were not bounds**: a crafted credential store is read with a
  hard row cap and truncated values; `extract` checks its budget before pushing
  a row and caps `--field`; oversize CDP replies are `result-too-large`, not a
  transport error; the PNG oracle requires a complete file.
- **One oracle per fact**: the CDP port is resolved once per verb (a browser
  started with an explicit `--remote-debugging-port=N` writes no
  `DevToolsActivePort`, so the census falls back to the process's own flag) and
  the port's holder is re-judged immediately before each connection; `seed`'s
  read-back compares against the sizes recorded as the copy ran.
- Smaller: `open` reports a redirected startup page instead of SIGTERMing the
  browser it started; frames report an unbound target as `null`/`matched:
  false` and bind by elimination; `--` ends the flags; a relative
  `BROWSER_CONTROL_PLUGIN_PATH` is refused; `help` writes its audit line;
  `scroll` sees a horizontal move; `insert`/`type` accept a frame or canvas
  focus (the documented `verified: false` path) instead of refusing `no-focus`.

The SAME seven reviews then ran a second pass over the fixes. Everything they
re-opened is fixed too, and the round-2 findings are the ones worth naming:

- `tab media` binds every read to a CONCRETE element (the index the preferred
  read settled on, not the "preferred" rule re-evaluated after the action), and
  a page that swaps the element under the verb refuses instead of certifying
  from the new one's clock.
- A LENGTH that did not change is an UNCLEAR verdict, not a refusal:
  `Input.insertText` REPLACES a selection, so a landed write can leave a field
  the same length or shorter. Only a focus the probe PROVES takes no text
  refuses `insert-not-verified`/`type-not-verified`.
- `verify_port_owner` re-judges the port at EVERY page websocket, including
  `tab wait --for js` and the frame census (the two doors the first round
  missed); a port rebound to a stranger refuses before a byte is sent.
- The port oracle is "answers AND verifies": a stale `DevToolsActivePort` held
  by somebody else no longer shadows the browser's own
  `--remote-debugging-port`.
- The same-URL frames case is refused rather than paired by row order; a
  symlinked `Default/` is reported as a location with a reason; `profile logins`
  has a wall-clock budget on the store read (a crafted store that used to OOM
  now cannot hang it either); the credential-store copy lives under this CLI's
  own 0700 root and stale copies are swept; a FIFO at the log path can no
  longer block a verb; `page read`'s frame scope, a bad `--cap` before any
  navigation, an audited `help`, and a value that is itself a flag
  (`--frame --deny=egress`) are all refused rather than silently dropped.

## 1. What exists

The distributable package is `browser_control` (`browser_control/lib` is the
logic, `browser_control/cli` is the argv adapter); `pyproject.toml` installs
it and puts one console script on PATH.

| Path | Owns |
| --- | --- |
| `pyproject.toml` | distribution `browser-control`, console script, `websockets`, MIT (SPDX) + `license-files` |
| `README.md` | the pitch and the manual index — the manual itself lives in `docs/` |
| `docs/` | the manual: `getting-started.md`, `verbs.md`, `extraction.md`, `sessions.md`, `plugins.md`, `trust.md`, `refusals.md` (the reader-facing docs), plus `plan.md` (the design) and `progress.md` (the state of the work) |
| `LICENSE` | MIT, `Copyright (c) 2026 Mark Tiempo` |
| `browser-control-cli` | the command as a checkout script (no install needed) |
| `browser_control/cli/` | the argv adapter: `main.py` (dispatch, the policy gate, the action log), `registry.py` (the `HANDLERS`/`tab`/`profile` tables, `POLICY`, `PLUGINS`), `argv.py`, `verbs/{browser,tab,profile}.py` |
| `browser_control/lib/browser/` | managed profile, launch/stop, discovery, attach records, tabs, `nav`/`activate`, the `/proc` endpoint guard, the lifecycle locks, the read-back waiters |
| `browser_control/lib/dom/` | **the DOM tier**: `js`, `wait`, `find`, `text`, `extract`, `click`, `hover`, `scroll`, `focus`, `press`, `insert`, `type`, `upload`, `check`, `select`, `dialog`, `screenshot`, `media`, frames |
| `browser_control/lib/cdp/` | endpoint, capped JSON GET, `evaluate`/`evaluate_until`, `target_ws`, `Session` (Page domain, events, parked tabs) |
| `browser_control/lib/profile/` | **the `profile` noun**: `info` (weight, age, liveness), `logins` (store census, never values), `seed` (logins copied in, caches skipped, read back), `reset` (wipe, on purpose) |
| `browser_control/lib/plugins/` + `browser_control/plugin_api.py` | the plugin loader, the typed spec, and the named seam a plugin imports |
| `browser_control/lib/` (cross-cutting) | `errors.py` (the refusal type + code registry), `capabilities.py` (the declared surface), `policy.py` (the gate), `audit.py` (the JSONL log), `paths.py`, `proc.py`, `locks.py`, `poll.py`, `scope.py`, `instance.py`, `seedtree.py`, `images.py`, `attachments.py`, `coerce.py`, `text.py`, `logfile.py` |
| `tests/test_unit.py` | the hermetic battery — no browser needed |
| `tests/live_test.py` | the live battery on a throwaway root — skip ≠ pass |
| `plugins/x_reader.py` | the shipped read-only X search plugin |
| `skills/` | Agent Skills for the harness: `browser-control` (core + refs), `browser-control-extract`, `browser-control-sessions`, `browser-control-plugins` |

The surface is a noun and its verb: `tab` owns everything about a page tab,
the other verbs own the browser.

| Verb | Does | Verified by (the read-back) |
| --- | --- | --- |
| `open [URL...] [--headless] [--profile DIR]` | starts the managed browser (or adopts the running one) and opens every URL given — the first as the startup page when starting fresh, the rest as tabs; `--headless` starts it with NO WINDOW (`--headless=new`), which every verb then drives through the same CDP surface; `--profile DIR` names the INSTANCE, so one root can hold several (two Chrome profiles, two sessions) | the endpoint must **answer**, then the startup page is matched by an explicit ladder — the requested URL, else the same HOST answering (an `http→https` upgrade, a canonicalised or redirected path), else the first real page row — and each `opened` entry carries `matched`: false plus a `note` naming both addresses when the browser landed somewhere else. A URL the browser changed is a FACT TO REPORT, never a reason to stop the healthy browser (`no-page-tab` is reserved for an empty tab list, a review measured the SIGTERM); the reply also reports `headless` — the mode launched on a fresh start, the mode the RUNNING browser is in on an adoption, read from its own cmdline (asking `--headless` when a windowed browser is already up refuses `bad-args` rather than overrule the argv) |
| `close [--force] [--port N\|--pid N\|--profile DIR]` | stops the browser this CLI started — or, when NAMED, exactly that one, which is how another tool's browser goes | the pid dies **and** the endpoint stops answering; never SIGKILLs, never signals a pid whose own cmdline does not name that profile, and refuses `tabs-open` while page tabs are open unless `--force`; a named browser must be a live, answering, VERIFIED Chromium-family process |
| `list` | **every** Chromium-family browser running here — ours or the user's, drivable or not — with pid, exe, profile, whether the profile is ours, whether CDP answers, whether the endpoint VERIFIED, and (when it did) the listener pid/exe the kernel names (+ its tab count) | one `/proc` pass, a CDP probe on the port each one names, and the socket's owner from `/proc/net/tcp` + `/proc/<pid>/fd` |
| `info` | the browser this CLI would drive (or the one `--browser` names) and its endpoint — `cdp.version`, `protocol`, `user_agent`, tab count; `running: false` names the profile `open` would use | `/proc` + `/json/version` read from the browser itself |
| `attach --port N \| --pid N \| --profile DIR` | makes a running browser this CLI did not start writable (a `tab` write target) | the profile comes from a live Chromium-family process of this machine that ANSWERS on its endpoint, never from the caller |
| `attach --list` | what is attached, and whether it is still running | each record re-checked against `/proc` + a CDP probe |
| `detach --port N \| --pid N \| --profile DIR \| --all` | revokes that authorization | `not-attached` when there is nothing to revoke |
| `tab [URL...]` | one tab per URL (`about:blank` when none), every id named | each id from `Target.createTarget`, re-read from the tab list |
| `tab list [--browser NAME]` | the page tabs of every **drivable** browser, grouped and sorted by browser | the same tab list the verbs use, read per browser |
| `tab info SPEC` | one tab: which browser owns it, its `{id,title,url,index}` now | the spec resolves to exactly one tab or refuses |
| `tab close SPEC...` / `--like V` / `--title V` / `--url V` / `--all` / `--except SPEC...` / `--dry` | closes the WHOLE set each selector names (`--dry` reports it as `would_close` and closes nothing): a SPEC **names** a tab (its whole URL, its whole title, or `id:<prefix>`), `--like` sweeps substrings, `--all` is every page tab this CLI drives, `--except` keeps the tabs it names (implies `--all`) | every requested id must be **absent** afterwards, else `close-tab-not-verified` names the survivors; a match in a browser this CLI does not drive is reported in `skipped`, never closed; a selector or an `--except` matching nothing refuses, so a typo cannot close the tab it meant to keep |
| `tab nav URL [--tab SPEC]` | navigates one tab | the address **as observed** (`url_read`) + `moved` + `loaded`; `chrome-error://` → `nav-failed`; a tab that never left a page it was not on → `nav-not-verified`; a redirect is a success |
| `tab back` / `tab forward` | moves the tab's history | the address actually changed (`nav-not-verified` when it did not) |
| `tab reload` | reloads one tab | `performance.timeOrigin` changed: a NEW document, not a guess |
| `tab js EXPR` | evaluates an expression — the escape hatch, and it can write; the reply is the page's OWN value, so a string that parses as JSON stays that string | **unverified** (`verified: false`), the value capped at 64 k (`result-too-large`), a page exception is `js-error`, a page that stops answering `eval-timeout` |
| `tab wait --for load\|idle\|element\|js` | polls ONE predicate to a wall-clock deadline | `{ok, for, waited_s, samples}` or `wait-timeout` naming what and how long; one connection for the whole poll, `waited_s` measured on the MONOTONIC clock the deadline uses, no sample started past the deadline, and a negative `--idle-ms` refused (`bad-args`) rather than silently inverting the idle predicate |
| `tab find TEXT \| --selector CSS` | a human target → visible elements, in **page** coordinates | `no-match` (naming the candidate count) · `no-viewport` when the tab has no viewport · each match carries `point` and `viewport` (viewport coordinates), `in_viewport`, `hit` (a real hit-test), `hit_element`, `clipped` |
| `tab text [--selector CSS]` | the rendered text | truncated **in the page**, so the reply is bounded and `length` still reports the full size |
| `tab click TEXT \| --selector CSS [--index N]` | **real input** (`Input.dispatchMouseEvent` move+press+release) at the element's viewport centre | `occluded` when the point reaches something else · `no-viewport-target` when it is off-screen (with the remedy) · `ambiguous-element` for several matches · the reply carries `changed` (url/title/focus/scroll before and after) |
| `tab scroll --by N \| --edge top\|bottom \| TEXT` | **real wheel input** (`mouseWheel`) or `DOM.scrollIntoViewIfNeeded` for one element | `scroll-not-verified` when a requested edge was not reached, or a wheel moved nothing and the document was not already at that end; the reply names which scroller moved (`document` / `nested`) |
| `tab focus TEXT \| --selector CSS [--index N]` | the DOM focus (the CARET, not the tab's frontmost position) — `DOM.focus` | `document.activeElement === el`; `focus-not-verified` names why (the protocol says "not focusable", a disabled control) |
| `tab press KEY` | one key event at the focus — `Input.dispatchKeyEvent` | the dispatch is verified and the reply says `verified: false`: the effect belongs to the page (`tab text`/`tab info`/`tab js` read it); an unknown key is `bad-args` naming the table |
| `tab insert TEXT` | `Input.insertText` — ONE atomic event | the focused field's **length grew by ALL of the text** (`verified: true`), and the reply carries `inserted` (the delta); a landing SHORTER than the text (a `maxlength`, an input handler that filters) is `verified: false` with a note naming the shortfall, never a certified write; a readable field that did not change refuses `insert-not-verified`, an unreadable one (frame/canvas) reports `verified: false`; `no-focus` only when nothing is focused |
| `tab type TEXT` | real per-character key events (`keyDown`, `char`, `keyUp`) on one connection | same oracle and codes as `insert` (`type-not-verified`) |
| `tab upload FILE [--selector CSS] [--index N]` | `DOM.setFileInputFiles` (an objectId, so shadow roots work) | `input.files` read back: one file, same name **and size**; `no-file`/`upload-not-verified` |
| `tab media state\|play\|pause [--index N]` | drives the `<video>`/`<audio>` element (no CDP playback method exists — see 5.7) | `play` needs the **clock to move** (a source-less element reports `paused: false` and never plays a frame), `pause` needs `paused: true` — and the clock read is the element the ACTION drove (`--index` binds every read to it, so an unrelated player's clock can no longer certify the write; an index the page cannot satisfy refuses `bad-args` naming the count); `no-media` when there is none, `media-blocked` with the page's own reason when the promise rejects |
| `tab frames [--tab SPEC]` | this page's iframes, and which can be driven (a cross-origin frame is a target of its own; `--frame` reaches it; a same-process frame has no target and `--frame` reads its document) | the DOM's own iframe census, correlated with the `/json` iframe targets; an unbound SEPARATE frame reports `target: null, matched: false` (never a bare `""` claiming it has no target when the browser merely did not say), exactly one unmatched row and one unclaimed target are bound by elimination (`matched: "elimination"`), and anything ambiguous refuses; `no-frame` names what exists · `frame-ambiguous` names the indices · input into a same-process frame refuses `frame-not-separate` (the reads work) |
| `tab activate [SPEC]` | brings a tab forward (it raises its window) | the page reports itself **visible**; `activate-not-verified` says why not |
| `tab hover TEXT \| --selector CSS [--index N] \| --at X,Y` | puts the pointer on an element (`:hover`) | the element-at-point probe; `hover-not-verified` when the page does not report the hover |
| `tab check TEXT \| --selector CSS [--index N] [--uncheck]` | checks/unchecks a box or radio with **real input** | `checked` flipped, bounded poll; `check-not-verified`, `not-checkable` |
| `tab select TEXT \| --selector CSS --value V [--index N]` | chooses one `<option>` with real arrow keys | the selected value/index read back; `select-not-verified`, `not-a-select`, `ambiguous-option` |
| `tab dialog [state\|accept\|dismiss] [--text V]` | reads, accepts or dismisses a JavaScript dialog | `accept`/`dismiss` verify by the renderer returning; `state` may answer `open: null` (a suppressed dialog cannot be seen — §4.9); `no-dialog`, `dialog-not-verified` |
| `tab screenshot PATH \| --path PATH [--full] [--force] [--tab SPEC]` | writes a PNG of the page | the file's **own PNG header** and size, not the page's geometry — a COMPLETE PNG is required (signature, a 13-byte IHDR with its CRC, a following chunk and a trailing IEND: a header-only stub is refused, a review measured it accepted as real); `screenshot-not-verified`, `file-exists` (without `--force`) |
| `tab extract --each CSS --field NAME=SPEC [--cap N] [--chars N] [--visible] [--unique FIELD]` | a page's repeated items as records; CSS only, no code (a `read`) | the page's own DOM text/attributes, sliced **in the page**; same oracle as `find`/`text`, no claim beyond "this is what the page showed" — the reply budget is checked BEFORE a row is pushed (so `truncated` means the budget, not the budget plus one row) and the field count is bounded (`bad-args` past the maximum) |
| `selftest` | proves the install without a browser | interpreter, `websockets`, verb table, browsers on PATH; **refuses** `no-websockets` when the dependency is missing |
| `profile info [--profile DIR]` | the managed profiles: weight, age, whether a browser is on one, whether it is attached | one filesystem read |
| `profile logins [--site HOST] [--cap N]` | the hosts a profile's cookie store names (with expiry) and how many saved logins — **counts and names only, never values** | the profile's own `Cookies`/`Login Data` read from a COPY, with a hard row cap and per-value truncation: a capped read says so (`capped: true`, `rows_limit`) instead of being read out unbounded (a crafted store drove the old read to `MemoryError`); a store that cannot be read is `readable: false` with the reason, its rows unknown, not zero; a symlinked store is never read through |
| `profile seed --from DIR [--force] [--dry]` | copies a source profile's logins into a managed one (no caches, no lock files); an existing target refuses unless `--force`, and `--force` WIPES it first — what is left is the source, never a mix of two; `--dry` counts both AND reports `would_refuse` (`profile-exists`/`not-managed`) so a green dry run cannot gate a call that will refuse; a source that reaches the target through a SYMLINK is refused by real path | what landed is read back against the sizes recorded AS IT WAS COPIED (never a re-stat of a live source, which made a running browser's own writes look like files that "did not land"), with files whose source changed mid-copy reported as `changed`; the `--force` wipe is verified gone before the copy runs; the marker lands before the copy, so a failed seed is still cleanable |
| `profile reset [--force]` | wipes a managed profile, logins included | the profile is emptied and recreated; `reset-not-verified`/`reset-failed` name a wipe that did not land |

Reads span every drivable browser; **writes go to a managed one (a profile
under this invocation's root) or to an attached one** (`attach` grants TAB
writes only — `close` never stops an attached browser, and `detach` is how the
authorization goes). A tab in a browser that is neither is readable (`tab
list`, `tab info`) and refused for `tab close`/`tab` with `ERR[not-managed]`.
`--browser NAME` narrows a read to one browser and picks the one a write goes
to; when two running browsers share the name, ours wins. Two *writable*
browsers refuse `ambiguous-browser` rather than picking one — `detach` or
`--browser NAME` resolves it. `list` and `info` carry both flags on every
browser: `managed` and `attached`.

Contract: one JSON object on stdout; `ERR[code]: message` on stderr; exit 2 on
a refusal. `--browser NAME` selects the Chromium; otherwise the first on PATH,
and a live managed browser wins. Two live managed browsers refuse
`ambiguous-browser` rather than picking one.

## 2. Evidence

**Hermetic** — `python3 tests/test_unit.py` → **165 passed, 0 failed**: URL
policy, tab-spec resolution (incl. `tab-ambiguous`), launch flags (incl. the
headless ones), headless detection from a cmdline, profile
keyed by the resolved binary, port-file edge cases, `/json` reading against a
fake endpoint, CLI dispatch and grammar, the capability surface, audit
redaction, endpoint ownership, lock semantics, pid liveness.

**Live** (throwaway root `/tmp/bc-live-verify`, user's own browsers untouched):

| Run | Result |
| --- | --- |
| `open https://example.com` | `{started: true, port: 33299, pid: 334917, tabs: [example.com]}` on Chrome 152 |
| `open https://example.org` (browser already up) | `{started: false, tab: "id:8259…"}` — the running instance got the tab |
| `new-tab https://example.net` → `close-tab example.net` | id re-read, then closed; count 2→1 |
| `close-tab id:2D4BC76C` | closed the matching tab by prefix |
| `close` | `{stopped: true, pid: 334917}`; `tabs` after → `ERR[cdp-unreachable]`; `close` again → `{stopped: false, reason: "no managed browser was running"}` |
| refusals | `tab-ambiguous` names both candidates; `no-page-tab` lists what exists; `unknown-command` lists verbs; `file://` refused; extra positionals refused |

**Packaging** — `python3 -m pip wheel .` builds
`browser_control-0.1.0-py3-none-any.whl` carrying
`browser-control-cli = browser_control.cli.main:main`, `Requires-Python:
>=3.11` and `Requires-Dist: websockets>=12`. Installing that wheel into a
fresh venv (which resolved `websockets 17.1`) and running the *installed*
command from `/tmp` — nothing of the repo on the path — drove a real Chrome
through open → new-tab → tabs → close-tab → close, with `tabs` afterwards
refusing `cdp-unreachable`.

**Static** — the package was clean under an active LSP probe (0
diagnostics) at the last full run.

**Live battery** — `python3 tests/live_test.py` on a throwaway root (the suite
has grown past the 39 checks first recorded here; run it for the current
count): it starts a real Chrome and reads
independent state back — a raw socket connect, a direct `/json` GET, `/proc`
for the pid — for open, `list`, `tab list`/`tab info`, `tab` (single and
several URLs), open with several URLs, `tab close` by id and by substring,
an ambiguous spec (refused with **nothing** closed), a two-spec close in one
call, `tab nav` (a redirect, a dead endpoint, `--tab` naming the one tab),
`tab back`/`tab forward`, `tab reload`, `tab text` (with a 20-char cap),
`tab find` (a hidden twin skipped, a shadow-root button found, the iframe's
button not), `tab js` (a value, an object, a JSON-looking string kept a
string, `js-error`, `result-too-large`),
`tab wait` (a button that appears after 2 s, then a timeout), `tab click`
(a trusted press, an occluded target, an off-screen target), `tab scroll`
(a document wheel, a nested-scroller wheel, both edges), `tab focus`/`insert`/
`type` (lengths and keydowns), `tab press` (Enter reaching a form handler),
`tab upload` (a hidden file input), a password absent from the whole log,
`tab media` (play → the clock moves, pause, a source-less video refused),
refusals, `info`, adoption of the running browser, the verified close, the
idempotent close, a browser with no debugging port (listed, never driven), a
**foreign drivable** browser (read → refused → attached → written →
detached, with `close` still refusing to stop it) and "nothing left
running". With the command missing every check skips and it exits 2.

**Not proven yet**: the live battery still runs by hand — it needs a real
browser, so CI keeps it out by design (§5.28: `.github/workflows/ci.yml` runs
ruff, pyright and the hermetic suite on every push, with the project installed
first). The earlier "no lock" and "one browser at a time" gaps are closed
(§5.18, §5.20).

## 3. Deviations from the plan

| Plan said | Now | Why |
| --- | --- | --- |
| `lib/cdp/` package; `lib/dom.py`, `lib/tabs.py`, `lib/profile.py` | packages `lib/cdp/`, `lib/browser/`, `lib/dom/`, `lib/profile/`, `lib/plugins/` | the refactor landed: transport, lifecycle, the DOM tier, profiles and plugins each own a package |
| profile **seeding** from the user's own profile | DONE (§5.22): `profile seed --from DIR` | explicit source, caches/lock files skipped, read back, `--dry` first |
| port → inode → pid ownership guard | DONE (§5.16) | every drive of an unverified endpoint refuses `cdp-not-local` |
| `ensure` (windowless start) | not built; `open` always makes a page | windowless was built, measured and rejected (§5.11); `open` is idempotent, which is what `ensure` was for |
| plugin tier, nav, dom, input, forms, media | DONE (§5.13, §5.24, §5.27) | `input`/`forms`/`media` live in `lib/dom/`; plugins load from a path, not entry points |
| `cdp METHOD` raw escape hatch | dropped by decision | `tab js` is the declared escape hatch; a raw protocol verb would widen the authorization surface |
| `search QUERY` (headless) | deferred by decision (§5.8) | a caller-supplied engine or the plugin tier serves the read |
| console script `bctl` | `browser-control-cli` only | answered (§6.4) |
| profile snapshots (`profile save`/`load`) | not built | the plan said "if they ever earn it"; they have not |

## 4. Known gaps and debt

1. ~~**No seeding**~~ — DONE (§5.22): `profile seed --from DIR` copies a
   source profile's logins INTO a managed instance (caches and lock files
   skipped, read back, `--dry` first). The caveat is in the verb's reply: the
   OS keyring holds the key, so it decrypts on this machine and this user.
2. ~~**No lock/serialization**~~ — DONE (§5.18): `open`/`close` hold a
   per-profile `flock` across their check-then-act and `attach`/`detach` hold
   one for the root's records, so `started: true` happens exactly once and no
   verb leaves others guessing.
3. ~~**No fixed-port ownership guard**~~ — DONE (§5.16): the process holding
   the port is checked in `/proc`, every drive of an unverified endpoint
   refuses `cdp-not-local`, and `list`/`tab list` name the pid and exe that
   actually own it.
4. ~~**One browser at a time**~~ — DONE (§5.20): `--profile DIR` is the
   instance selector on EVERY verb (it feeds one process scope that every
   `--browser` path already funnelled through), so two instances of the same
   browser under one root are addressable instead of refusing.
5. ~~**Not published**~~ — MIT + README + declared metadata DONE (§5.21);
   what remains is an INDEX to publish to, and a distribution name that does
   not collide with the public `browser-control` project (the CLI name stays).
6. ~~**No CI**~~ — DONE (§5.28): `.github/workflows/ci.yml` runs ruff,
   pyright and the hermetic suite on a push, with the project installed first
   (the transport needs `websockets` at both check and run time); the live
   battery stays out of CI by design — it needs a real browser.
7. **No human output** — every verb prints JSON; there is no `--json` switch
   because there is no alternative format yet.
8. ~~**`stop` refuses when the pid cannot be identified**~~ — DONE (§5.19):
   `close --pid N`/`--port N`/`--profile DIR` stops exactly the browser the
   caller NAMES, after verifying it is a live, answering, VERIFIED
   Chromium-family process — so another tool's browser can be closed on purpose,
   while an implicit `close` still never touches it.
9. **A suppressed dialog parks a tab for good** — measured: Chrome suppresses a
   JavaScript dialog no client was attached to answer, and then nothing on the
   CDP side can clear it (`tab dialog accept` says `no-dialog`, which IS the
   answer). `tab nav URL` replaces the document and its renderer, which is the
   way out; `tab close` ends the tab. A page that alerts between two CLI
   invocations therefore costs that tab its state — the honest limit of a
   one-shot CLI, and the reason `tab dialog` reports `open: null` instead of
   guessing.
10. ~~**Frames are still out of scope**~~ — DONE (§5.24): `tab frames` lists
    them, `--frame` drives a CROSS-ORIGIN one through its own target with every
    page verb unchanged, and reads report a frame census so a partial read says
    so. A same-process frame has no target and refuses `frame-not-separate`.

(Fixed since it was listed here: the default action-log directory was never
created, so the fail-open log silently dropped every write. `write` now makes
the directory — mode 0700 — and falls back to
`/tmp/browser-control-<timestamp>/actions.jsonl` when it cannot, and the
hermetic suite asserts a line really lands on disk instead of turning the log
off.)

## 5. What is next

Ordered by "unblocks the most with the least". **5.1–5.7 and 5.10 through 5.29
have landed** (§1, §2); **5.8 (search) and the ad functions are deferred by
decision**, and **5.11 was built, measured and rejected**. Nothing is left in
the core: 5.10's hardening items landed in §5.16–§5.18. Every later verb adds
its check to `tests/live_test.py`; the CLI grammar is settled (§1).

### 5.1 `selftest` verb — done
Landed as `browser-control-cli selftest`: interpreter, `python_version`,
`websockets`, `profile_root`, the verb table and the browsers it can find on
PATH — and it refuses `ERR[no-websockets]` when the dependency is missing,
the one thing that makes an install unusable. Covered hermetically (that
refusal included) and by the battery's first check.

### 5.2 The live battery — done
`tests/live_test.py` (787 lines, 25 checks): a temp `BROWSER_CONTROL_ROOT`, a
local page server (with a redirect route) so tabs have distinct URLs without
the network, a skip-if-prereq that exits 2, and mandatory cleanup — close what
you opened, kill what you started (and wait for it), remove the temp root.
Every check reads independent state (raw socket, direct `/json`, `/proc`)
instead of trusting the reply, and two of them launch browsers this CLI does
**not** manage — one with no debugging port, one drivable — to prove the
read/write boundary.

Three lessons the battery taught by leaking or flaking, all now fixed:

1. **The module must be INERT when imported.** The temp root was created at
   import time, so any collector that imported the file (pytest, an analyzer)
   left an orphan root — the "leftovers" this file kept reporting were its own
   import side effect, and a concurrent run under that load is the likeliest
   cause of the one intermittent failure seen. The root is made in `main()`.
2. **A killed run leaves a root**: `sweep_stale_roots()` removes a sibling
   root that no running process mentions and that is old enough not to be a
   run starting up — and SAYS so, because a leftover means a run did not
   finish.
3. **Timings that assume an idle machine flake**: the cold-start waits for the
   battery's own foreign browsers are 30–40s now, and the "dead endpoint" the
   `nav-failed` check uses is verified closed rather than assumed free.

### 5.3 Profile seeding — done (as `profile seed`, §5.22)
Built as one of three verbs under a new browser-level noun, and with the
environment's rules rather than the old plan's: an explicit `--from DIR` (no
guessing which of your profiles to read), a copy that skips lock files and
caches by name, a read-back of what landed, and `--dry` to weigh it first. The
policy question is answered too: seeding is **explicit only** — `open` never
seeds behind your back. Not done from the old sketch: reflink and the atomic
swap (a copy plus a refusal to run on a live profile is what the verification
actually needs).

### 5.4 `tab nav` + history — done
`tab nav URL [--tab SPEC]`, `tab back`, `tab forward`, `tab reload`. The
assignment returns before the document moves, so the reply carries the address
**as observed** (`url_read`), `moved` and `loaded`; Chromium's own error page is
`nav-failed`; a redirect is a success (the test is that it MOVED, not that it
arrived at the literal string); and `--tab` names the tab, with the only-tab
rule refusing `tab-ambiguous` when several are open.

The bug the first live run found, worth remembering: waiting for
`readyState == complete` returns IMMEDIATELY for the document being left, so
the read-back raced the navigation and a redirect came back as
`nav-not-verified`. The order is now **move first** (`performance.timeOrigin`
changed, or the address did), **then load** — and `reload` uses the same
document-time oracle, because a fast page is complete again before any read.

### 5.5 DOM reads — done
`tab js`, `tab wait --for load|idle|element|js`, `tab find TEXT|--selector CSS`,
`tab text`. The tier is its own module (`lib/dom.py`) over the tab resolution
in `browser.py`, sharing ONE element prelude, so what `find` matches and what
`wait --for element` accepts cannot drift apart.

What the evaluation settled, now in the code:

* **A read never activates a tab.** Measured: a hidden tab has a viewport, a
  layout and a working hit-test, so `find` reports `visibility` + `viewport`
  and leaves the desktop alone. A tab with a **degenerate viewport** (0×0)
  refuses `no-viewport` — a box in no viewport is not a target.
* **No `rendered` flag**: it fused three questions that have direct answers.
* **`js` is the escape hatch and says so** (`verified: false`), and its result
  is capped: past 64 k it refuses `result-too-large` rather than handing back
  half a value.
* **The matcher is accessibility-first** (label: `aria-label`, placeholder,
  title, alt, name, value, text) and **shadow-piercing** (the shared `query`
  walks open shadow roots). iframes stay top-document-only, and the battery
  proves it by finding the frame's button nowhere.
* **The no-`--tab` scope is the browser this CLI DRIVES**, not every drivable
  browser — the user's own Chrome running beside ours would otherwise make
  every unqualified read `tab-ambiguous`. A spec still reaches any drivable
  browser; a write to one that is neither managed nor attached refuses
  `not-managed` (`tab js` counts as a write, because it runs caller code).
* **`wait` keeps ONE connection** (`cdp.evaluate_until`) instead of a
  websocket per sample, and keeps "the page threw" (`js-error`), "the page
  did not answer" (`eval-timeout`) and "the transport died" (`cdp-error`)
  apart.

### 5.12 `tab click` + `tab scroll` — done (CDP-native first)
The action half of the DOM tier, taken with the principle "whatever CDP can do
natively, do that":

* **`tab click TEXT|--selector CSS [--index N]`** — `Input.dispatchMouseEvent`
  (move, press, release) at the element's VIEWPORT centre. Measured side by
  side: a CDP press is `isTrusted: true`, `element.click()` is
  `isTrusted: false` — the whole reason this is a verb and not a `js` one-liner.
  The point must hit-test to the element first (`occluded` names what is
  actually there), an off-screen target refuses `no-viewport-target` with the
  remedy, and the reply carries `before`/`after` (url, title, focus, scroll)
  with `changed: false` as an honest FACT rather than a failure.
* **`tab scroll --by N [--at X,Y]`** — a real `mouseWheel`, which moves what is
  under the point: the reply distinguishes the DOCUMENT's position from the
  nearest nested scroller's (`nested`), because those are different facts and a
  wheel at the wrong point moves the wrong one.
* **`tab scroll --edge top|bottom`** — repeated wheels until the document stops
  moving, verified against the real maximum; a wheel that moves nothing at the
  edge is `moved: false`, not a refusal.
* **`tab scroll TEXT|--selector CSS [--index N]`** —
  `DOM.scrollIntoViewIfNeeded` (a CDP method, via `Runtime.evaluate` →
  `DOM.requestNode` → the method), then the matcher proves the element now has
  an in-viewport box.

Two measurements shaped the implementation: a wheel's effect is
ASYNCHRONOUS (scrollY 0 → 600 between 0.0 s and 0.2 s), so every mode polls to
a deadline instead of reading once; and `find` now returns elements BELOW the
fold with `in_viewport: false` (plus an `offscreen` count) instead of hiding
them behind `no-match`, so "not there" and "not in view" are different answers.
`cdp.Session` keeps ONE connection per verb — a click is three dispatches and
two reads, a scroll-to-edge is a wheel and a read per step.

### 5.6 Input and forms — done
The typing half: `tab focus`, `tab press`, `tab insert`, `tab type`,
`tab upload` — all five CDP-native (`DOM.focus`, `Input.dispatchKeyEvent`,
`Input.insertText`, `Input.setFileInputFiles`), with JavaScript only in the
read-backs.

* **`tab focus`** is the CARET, not the tab's frontmost position (that is `tab
  activate`) — it needs no coordinates, no window focus and no hit-test, so it
  works on a background tab and while a layer surface owns the pointer. The
  read-back is `document.activeElement`, and the protocol's own words survive:
  a heading answers `ERR[focus-not-verified]: … cannot take the DOM focus:
  DOM.focus: Element is not focusable`.
* **`tab press`** sends one key from a fixed table (enter/tab/escape/arrows/
  home/end/page up-down/space/backspace/delete — Enter and Space carry `text`,
  without which a form never submits). The dispatch is verified and the reply
  declares `verified: false`: the effect is the page's, and the battery reads
  it back (Enter reached the form's submit handler).
* **`tab insert`** (atomic) and **`tab type`** (per key) share one oracle: a
  **stable DOM path** plus the field's text LENGTH before and after — never the
  value, so a password cannot ride home in the reply. A readable field that did
  not change refuses (`insert-not-verified`/`type-not-verified`); an unreadable
  one (a frame, a canvas) reports `verified: false`, because an unclear oracle
  is not proof the text was absent; nothing focused refuses `no-focus` with the
  remedy (`Input.insertText` into no focus is a silent no-op).
* **`tab upload`** fills the one control JavaScript cannot (`input.files` is
  read-only, and the input is usually hidden) via `DOM.setFileInputFiles`,
  which takes an OBJECT id — so it works through a shadow root — and is the one
  matcher that does not require visibility.
* **The action log** (`lib/audit.py`, 108 lines): one JSONL line per CLI call,
  refusals included, at `~/.local/state/browser-control/actions.jsonl`
  (`BROWSER_CONTROL_LOG` names it, `off` disables). **Fail-open** — a log that
  cannot be written never fails a verb — and a verb that PROVES the text it
  handled is secret marks that exact text, so the line carries
  `<redacted: N chars>` and `redacted: true` while the rest of it (the verb,
  the tab spec, the refusal code) survives. Password detection fails CLOSED:
  a `type=password` field, a focus inside an iframe, or a probe that cannot
  answer all count as a secret.

The first live run found one bug in the oracle: the "did the focus move?"
guard compared descriptions that **included the field's value**, so typing
changed the identity and every `insert` reported `verified: false` while the
text landed. Fixed with the stable DOM path — and the battery now asserts the
lengths, the key count, and that the secret is absent from the whole log.

### 5.7 Media — done (the ad functions are deferred)
`tab media state|play|pause [--index N]` — the base media verbs, and **only**
those: `tab ad-state` and `tab skip-ad` are deferred by decision (the site
adapters belong to the plugin tier anyway).

* **There is no CDP method for playback**, which is worth stating because the
  principle here is native-first: the `Media` domain is experimental and
  event-only, and the media KEY is a toggle aimed at whichever session has
  focus (a background tab's audio, or nothing). So the verb drives the
  ELEMENT and then VERIFIES what the page reports.
* **The play oracle is the clock**, and the first battery run proved why: with
  a real gesture in the tab, `play()` on a video with NO source resolves and
  the element reports `paused: false` — `readyState: 0`, never a frame. "Not
  paused" is not playing; `time` advancing is. The refusal names which it was
  ("the element has nothing to play (readyState 0: no supported source)").
* **A `play()` the browser rejects refuses `media-blocked` with the page's own
  words** (the autoplay policy, no supported source) — and when that reason is
  the autoplay policy the message names the remedy (`tab click` the player).
* `state` is a read; `play`/`pause` are writes. Several players on a page are
  picked by rule (the playing one, else the largest) with `--index` to choose,
  and `count` says how many the page has.

The battery gets its media **offline**: the fixture records a canvas with
`MediaRecorder` into a blob and hands it to a muted, looping `<video>`, so
`play` has something the browser will actually start without a gesture.

### 5.13 Pointer, form, dialog and screenshot verbs — done (A–D, F)

Six verbs, each verified by the page's own answer rather than by a call that
returned:

| Verb | Oracle (what makes it `verified: true`) |
| --- | --- |
| `tab activate [SPEC]` | the page's own `document.visibilityState`, before and after `Page.bringToFront`; a tab that still reports `hidden` refuses `activate-not-verified` |
| `tab hover TEXT\|--selector CSS` | the engine's `:hover` state on the element (or something inside it) **and** the point still hit-testing into it; an occluded or moved point refuses before any event is sent |
| `tab check TEXT\|--selector CSS [--uncheck]` | the control's own `checked`, read before and after a real click; already in the wanted state means NO click (a click would toggle it away) |
| `tab select TEXT\|--selector CSS --value V` | the control's own `value`/`selectedIndex` after REAL arrow keys — by value first, then exact label |
| `tab dialog [state\|accept\|dismiss] [--text V]` | the renderer answering again (accept/dismiss), or the tab answering at all (`open: false`); the browser's own "No dialog is showing" is what makes a refusal definitive |
| `tab screenshot PATH [--full] [--force]` | the PNG's own IHDR against the page's reported geometry × `devicePixelRatio`; a mismatch writes nothing |

Measured while building them (Chrome 152, headed), and what shaped the design:

1. **Dialogs are suppressed unless a client has the Page domain enabled.**
   `open`-time alone is not enough: a dialog that opens while nobody is
   attached parks the renderer **forever** — `Page.handleJavaScriptDialog`
   reports "No dialog is showing", every `Runtime.evaluate` times out, and
   even `location.href = …` cannot run. With the domain enabled first, the
   dialog is announced (`type`, `message`), handled in 0.0 s, and the renderer
   returns. **Every session this CLI opens now enables the domain**, which is
   why a dialog raised during a verb is NAMED in the refusal (in ~1.1 s, not
   the 15 s budget) instead of becoming a mystery timeout.
2. **`Page.navigate` is the way out of a parked tab** (0.0 s reply, the
   renderer is replaced, the tab answers again) — which is why `tab nav` is
   now a browser-side command instead of a `location.href` assignment. It also
   removes the old "an assignment the page ignored" ambiguity, and a URL the
   browser turns into a download is named as one.
3. **`<select>` needs no popup**: focusing the control and pressing ArrowDown /
   ArrowUp moves the selection, and the page's `change` handler sees
   `isTrusted: true` — real input, not a DOM write.
4. **`:hover` is observable**: after one `mouseMoved` the element matches
   `:hover` (ancestors match too, hence the hit-test half of the oracle).
5. **Screenshot geometry is exactly `dim × devicePixelRatio`**: viewport mode
   matches `innerWidth/innerHeight`, `--full` matches
   `scrollWidth/scrollHeight` (1852×4202 in the battery's window), so the
   IHDR comparison is a real oracle and not a ritual.
6. **`active` is scoped to the browsers this CLI DRIVES.** The user's own
   Chrome has a visible tab in its window as well; counting it made every
   `--tab active` refuse `tab-ambiguous` (measured). It is a RESERVED spec,
   like `tab list` is a reserved subcommand.

Battery: six new checks (activation round-trip, `:hover`, check/uncheck with
trusted events, select by value and by label, PNG on disk vs the page,
and the dialog naming + `tab nav` recovery). Hermetic: the six verbs' argv,
every page expression's `JSON.stringify((() => {…})())` wrap and placeholder
filling (a missing bracket is invisible to Python and shipped once as
`js-error: SyntaxError`), the PNG helpers, and the dialog-mode check.

### 5.14 Bulk closing — `tab close` names SETS — done

| form | matches | closes |
| --- | --- | --- |
| `tab close SPEC...` | `id:<prefix>`, the WHOLE URL (a trailing slash is not a different page) or the WHOLE title, case-insensitive | every tab it names — the union of the specs, deduplicated |
| `tab close --like V` | a SUBSTRING of the title or URL | every match (the sweep, declared) |
| `tab close --title V` / `--url V` | the whole value in that one field | every match |
| `tab close --all` | every page tab this CLI drives | all of them |
| `tab close --except SPEC...` | (implies `--all`) matches LOOSELY | every tab the specs do NOT name |

The rules that hold for all of them: everything resolves **before** anything
closes, so a bad argument or an unmatched exception cannot leave a half-applied
close; every id is read back, and a survivor is `close-tab-not-verified` naming
it; a match in a browser this CLI neither manages nor has attached is REPORTED
(`skipped`: id, title, url, pid, exe, profile) and never closed, with
`not-managed` when every match is foreign; a selector that matches nothing
refuses `no-page-tab` — **including an `--except` spec**, so a typo cannot keep
what should have gone; and mixing selectors is `bad-args`.

**A SPEC names a tab; a sweep must be asked for.** The first version of this
made the substring form bulk, and it closed the wrong tab in the field:
`tab close a` swept up a tab whose title read “X. It’s wh*a*t’s h*a*ppening /
X”. So `--like VALUE` is now the declared loose form, `--title`/`--url` stay
exact, and `--except` matches loosely on purpose (erring toward KEEPING is the
safe direction). The refusal teaches the difference: “no tab is NAMED ‘a’ … for
a substring sweep use `tab close --like 'a'`”.

Two more behaviours worth knowing:

* **`id:` with no prefix names nothing.** `"".startswith("")` is True, so the
  first version matched EVERY tab: `tab close id:` closed the last tab on the
  page and Chromium took the window with it (the battery caught it — the
  fixture browser died mid-run). It refuses `bad-args` now.
* **Closing the last page tab stops the browser**, since Chromium exits with
  its last window: `tab close --all` on a single-tab browser ends it. The ids
  are verified gone either way, so that reads as a success with `count: 0` plus
  a note.

Hermetic: argv for all five forms (a repeated `--except`/`--like`, `--except=x`,
`--like=x`), a value-less flag, the service's pure refusals (none, two, empty
spec, `id:`, SPEC+filter, `--all`+selector, `--like`+anything, empty
exception), and `_exact_spec_match` itself — including the assertion that
`a` does NOT name a tab titled “X. It’s what’s happening / X”. Battery: a
substring sweep by request, `a` sparing `/aaa`, exact `--title` sparing
`twinx`, two specs in one call, a refusal that suggests `--like`, `--all
--except`, and `--except` alone.

### 5.15 A dry run, and a `close` that cannot take tabs by surprise — done

Two things built together, because the first makes the second inspectable.

**`tab close ... --dry`** resolves exactly as it would — same selectors, same
refusals — and returns the set instead of closing it: `dry: true`, the rows
under `would_close` (never `closed`, so a preview cannot be mistaken for an
action), and `note`. Both accidents that prompts this (`tab close a` matching a
title; `tab close id:` matching everything) would have been visible in one line
before anything was taken.

**`close` grew three guards, each because something went wrong without it:**

* **`tabs-open` unless `--force`.** Stopping a browser closes its tabs with it,
  and Chromium exits with its last window, so `close` refuses while page tabs
  are open, naming the count, and the reply carries `tabs` and `forced`. An
  endpoint that no longer answers cannot be asked, so nothing is claimed about
  its tabs (`tabs: null`).
* **The recorded pid is a hint, not an order.** `_pid_of` now requires the
  process's own cmdline to carry `--user-data-dir=<profile>` before that pid is
  signalled; otherwise the process is re-found by the same marker, or the stop
  refuses exactly as it did before (“refusing to signal a process this CLI did
  not start”). Measured in the battery with a decoy process recorded as the
  browser: the decoy stays alive, the real browser is stopped.
* **Ambiguity messages name pid and profile PATH**, not just the basename — two
  browsers can share an executable name, and `(google-chrome-stable,
  google-chrome-stable)` is a message nobody can act on.

Hermetic 31 (the `--dry` flag, `close --force`, and one more pure refusal:
`--dry` with nothing named); live 47 → 49 (`--dry` previews the same set and
refuses the same typos; a recycled pid is ignored). Two mistakes of my own on
the way are worth recording: the first recycled-pid check wrote a pid-file name
the code never reads, and called the library in-process where
`BROWSER_CONTROL_ROOT` is unset — so it passed while testing nothing. It now
uses `_pid_file()` and sets the root for the duration of those calls.

### 5.16 Endpoint ownership — the port is checked against the kernel — done

The port came from a FILE (`DevToolsActivePort` in the profile), and a file can
be stale or its port can be taken by something else. Before this, a stranger
answering there would have received our clicks, keystrokes, uploads and
screenshots. Now the PORT ITSELF has one resolver per verb: the census uses
the file only when it ANSWERS and otherwise the port the process names on its
own `--remote-debugging-port` (Chrome writes the file only when the port is
0, so a browser started with an explicit port writes none), the row's port is
threaded down to every page verb, and the port's holder is re-judged
immediately before each connection (`verify_ws_owner`) because the endpoint
can be rebound between the check and the traffic. The ownership chain is:

1. port → the LISTENING socket's inode (`/proc/net/tcp` + `tcp6`);
2. inode → the process holding it (`/proc/<pid>/fd` for `socket:[inode]`);
3. that process → **verified** when its cmdline carries
   `--user-data-dir=<profile>`, or when it is a Chromium-family executable AND
   the profile's main process is on the machine (a helper can own the socket);
4. anything else is **unverified**, which is where the verbs divide:

| caller | what it does with an unverified endpoint |
| --- | --- |
| every DRIVE (`tab …, tab info`, `close`, `tab close`) | refuses `cdp-not-local`, naming the pid and exe that hold the port, and says the mundane thing: the port file is stale or the port was taken — `close --force`, then `open` |
| `open` | refuses when a process on that profile is running; IGNORES a stale file (nothing on the profile) and launches, with a `warning` in the reply |
| `list` | reports the row with `verified: false` + `listener: {pid, exe}` + `reason` |
| `tab list` | leaves it out of `browsers` and lists it under `unverified` — never silently absent |
| `close`'s tab count | `null`: a stranger's tab list is not this browser's |

What it cannot do, said plainly: a local process that names itself `chrome`
passes the exe test, and a same-uid attacker could edit the profile (they could
edit `attached.json` too). Its value is the mundane case — a stale file plus a
recycled port must not hand our input to whoever holds it — plus making
`list`'s "drivable" a verified claim rather than "something answered".

Evidence: hermetic (a listener of the test process stands in for the stale
port: `cdp.listener_of` finds it, `endpoint_owner` calls it not a browser, the
drive refusal names the advice, and a closed port is `{}`); battery (a real
browser, its port file overwritten with the battery process's own listener →
`tab nav` and `open` refuse `cdp-not-local` naming pid `os.getpid()`, `list`
reports `verified: false` with that pid, `tab list` shows it under `unverified`,
and putting the file back drives the real browser again; plus `list` naming the
verified listener for a healthy browser).

### 5.17 The capability surface — what every verb can DO — done

The plan's *Kind* column turned into something a program can read. A policy
gate, an agent's own guardrails or a reviewer can now ask "may this run?"
without hardcoding a verb list that rots the first time a verb is added.

`lib/capabilities.py` declares one entry per RESOLVED ACTION, because a
subcommand can change the answer — `tab dialog state` reads while
`tab dialog accept` writes, `tab media state` reads while `tab wait --for load`
reads and `tab wait --for js` runs caller code:

| class | means | how many |
| --- | --- | --- |
| `read` | reads state; /proc and loopback CDP only | 15 |
| `write` | changes the page, the browser, or this CLI's authorization | 29 |
| `code` | runs caller-supplied code: `tab js`, `tab wait --for js` | 2 |
| `file` | touches a path the CALLER named: `tab screenshot`, `tab upload` | 3 |
| `egress` | would reach the network: hands a URL to the browser (`open`, `tab`, `tab nav`) or runs caller code that can `fetch` (`tab js`, `tab wait --for js`); the shipped site plugins declare it | 5 |

`file` is about the caller's data, not infrastructure: every verb may append to
the action log and `open` writes a profile, which is the tool's own business.

Three things keep it true rather than decorative:

* **`selftest` reports it** — `capabilities: {classes, by_class,
  unclassified}`, so the answer comes from the tool itself;
* **one function, two askers** — `capabilities.unclassified(HANDLERS,
  TAB_SUBCOMMANDS)` is what `selftest` prints AND what the hermetic test
  asserts is empty, so a new verb cannot exist in one place and be missing from
  the other;
* **the battery checks the shipped surface** — every verb `selftest` lists is
  covered by a class in the same reply.

It is a DECLARATION, not a sandbox: it says what a verb can reach, and
enforcement (if ever wanted — `--allow read,file`) belongs to whoever reads it.
That is why it was the prerequisite for the policy gate and not the gate
itself.

### 5.18 The lifecycle lock — `started: true` happens exactly once — done

`launch` is check-then-act with a window between them:

```
owner   = endpoint_owner(profile, port) if cdp.reachable(profile) else {}
already = bool(owner.get("verified"))            # the check
...
_record_pid(profile, _spawn([path, *flags(profile), first]))    # the act
```

Two `open`s at once both found "nothing running", both started a browser on ONE
profile — the corruption Chrome's own "profile appears to be in use" warning
describes — and both reported `started: true`. Chrome's process singleton made
that *usually* harmless, which is exactly the problem: our invariants (one
managed instance per profile, the recorded pid identifies it, every
authorization is recorded) were guaranteed by a third party's behaviour rather
than by us, and where its singleton differs the outcome is a corrupted profile.

`flock` on a file now serializes it, chosen over an `O_EXCL` file precisely
because the kernel drops it when the holder dies: nothing to clean up, nothing
to trust. Held across the WHOLE decision:

| verb | lock | held across |
| --- | --- | --- |
| `open` | `<root>/.locks/<profile>.lock` | read the port → check the endpoint → spawn → wait for it → record the pid |
| `close` | same | the pid decision, `SIGTERM`, and the death + endpoint waits |
| `attach` / `detach` | `<root>/.locks/_root.lock` | the read-modify-write of `attached.json` |

Lock files live BESIDE the profiles, not inside them (`paths.lock_path`). The
in-profile lock was deleted by `profile reset`'s `rmtree` while `reset` was
HOLDING it, and the next `open` re-created the path as a fresh inode and took
it at once — the mutual exclusion the table above describes was believed, not
held, for the one verb that wipes a profile. A lock a wipe cannot reach closes
that; a lock file is never deleted (an unlinked lock can be re-created and
double-taken).

A caller that cannot take it waits (up to 20 s, a cold launch's budget), then
refuses `profile-busy` naming the pid, verb and start time the holder wrote
into the file. A lock that cannot be OPENED or TAKEN at all (a directory or a
symlink at the lock path, a filesystem without `flock`) refuses
`profile-unusable` naming the path and the OS error. The earlier form carried
that failure as a `warning` inside an `ok: true` reply and ran the
check-then-act with no lock at all — two `open`s onto one profile, and
`seed`/`reset` unlocked (a review measured it).

The check found a real bug, which is the point of adding one: the losing call
read the port BEFORE taking the lock, so inside it `cdp.reachable(profile)` was
true while its own `port` was still `0` — and the guard faithfully reported "no
process holds port 0", turning a race into `cdp-not-local`. The port is now read
inside the lock, and a zero port is not treated as an endpoint to judge.

Evidence: hermetic (a held lock makes the second caller wait 0.3 s and refuse
`profile-busy` naming `pid … (open)`; it is free again afterwards with nothing
to clean up; an unopenable path REFUSES `profile-unusable` naming the path);
battery (two
`open`s launched at once on one root → **one started, one adopted the same
pid, both URLs in the tab list, exactly one live browser for that profile** —
and the whole battery then closes what it started).

### 5.19 Stopping a browser by NAME — consent, then verification — done

`close` had one way in: the managed browser on this CLI's own root. That keeps
the rule "attaching grants tab writes, not the right to stop somebody else's
browser", but it also meant a browser another tool started — or one this CLI
merely attached to — could not be stopped at all.

Now there are two ways in, and the difference is CONSENT:

| `close` | stops | verification |
| --- | --- | --- |
| no selector | the managed browser on this root | pid file (identity-checked) → `/proc` marker → endpoint verified |
| `--pid N` / `--port N` / `--profile DIR` | exactly the browser NAMED | a live Chromium-family MAIN process of this machine that answers CDP and whose endpoint VERIFIES (the same bar `attach` uses) |

Naming it is the consent, and the verification is what makes the name mean
something: `close --pid 999999` refuses `no-browser` with nothing signalled, a
name that points at an endpoint which is not that process's refuses
`cdp-not-local`, and the reply says which path ran (`named`, `managed`). The
implicit path is unchanged, so the attach check next to it still proves that a
plain `close` will not touch a foreign browser.

Everything else holds on both paths: the profile lock, `tabs-open` unless
`--force` (checked in the battery against a foreign browser — its own tab count,
through its own verified endpoint), SIGTERM only, and the process AND the
endpoint gone before `stopped: true`.

A detail worth keeping in mind for tests, learned here: a browser spawned by a
test is that test's unreaped CHILD, so `/proc/<pid>` survives as a zombie after
it dies. The library's `_pid_alive` treats a zombie as dead (its docstring says
why: `kill(pid, 0)` on a zombie answers, which would refuse a stop that
actually worked), so the battery check asserts with `_pid_alive`, not with bare
`/proc` existence.

Hermetic: `close` argv for all three selectors (and `--pid` + `--port` together,
`--list`, a bare word — all `bad-args`). Battery: a foreign browser, started by
the check on its own profile, refuses `no-browser` for a meaningless pid,
refuses `tabs-open` for its tabs, then stops **by name** with `--force` — pid
gone, port closed, gone from `list`.

### 5.20 `--profile DIR` — the instance selector — done

One root, one binary, one profile per binary: that was the model, and it made a
second Chrome (another login, another session) impossible. `--profile DIR` names
the instance instead, on EVERY verb, because it feeds a single process scope
(`browser.scope`) that every path already funnelled through `_narrow` — the same
place `--browser NAME` is applied. So:

* `open --profile <root>/work` and `open --profile <root>/personal` are two
  managed instances of one browser, each with its own tabs, and an unqualified
  `tab` verb refuses `ambiguous-browser` (correctly) while the refusal NAMES the
  new selector: "pass --profile DIR to name the instance, --browser NAME, or
  `detach` one";
* `tab text --profile <root>/work` reads that instance, `close --profile …` stops
  that one and leaves the other running, `info --profile …` reports it;
* `attach`/`detach`/`close` read the same scope, so the flag means one thing
  everywhere — and `close --pid N --profile X` is two selectors, refused.

Three details that are policy, not plumbing:

* **Inside the root only.** `open --profile /tmp/elsewhere` refuses `bad-args`:
  outside its root the CLI would start a browser it then refuses to write to.
  A browser started elsewhere is what `attach` is for.
* **Reset per invocation.** The CLI sets (or clears) the scope on every call, so
  a verb never inherits another's instance — which the hermetic suite caught the
  moment the first version only *set* it: five checks failed because `--profile`
  leaked from one `run_cli` call into the next.
* **`info` lost its hand-rolled narrowing.** It used to match rows itself and so
  ignored the scope; it goes through `_narrow` now, and its answer stays honest
  for a browser it cannot drive (`running: false` — a bug the battery caught when
  the broadened fallback reported the user's own Chrome as "running").

Hermetic: `--profile` + `--pid` refuses as two selectors, and the argv still
reaches `attach`/`detach`/`close` through the scope. Battery: two instances of
one browser, addressed, read, one of them stopped, the other still answering.

### 5.21 MIT, a README, and declared metadata — done

The repository was `all rights reserved` by default (no `LICENSE`) and its
packaging said `License: UNKNOWN`. Both are fixed, and the choice was argued
from this project's own shape rather than habit:

* the **plugin tier** is deferred, not dropped — copyleft in the core would
  infect every plugin author, so permissive keeps that door open;
* nothing here is patentable (it drives a documented protocol), so Apache's
  grant buys little for the NOTICE machinery it brings;
* **MIT** it is, which is also what a Python CLI's readers expect.

`pyproject.toml` now declares `license = "MIT"` (an SPDX expression, PEP 639)
with `license-files = ["LICENSE"]` and `readme = "README.md"`, and the build
requirement went to `setuptools>=77` for the metadata version that supports it.
Verified on the ARTIFACT, not on the source: the built wheel reports
`Metadata-Version: 2.4`, `License-Expression: MIT`, `License-File: LICENSE`,
carries `browser_control-0.1.0.dist-info/licenses/LICENSE`, and embeds the
README as the description — so a redistributor has the notice without asking.

The README is written for the person who has never seen this: what it is, the
three rules of the stance (**a mutation is never `ok: true` on a bare ack**; an
unclear oracle is not proof of absence; a refusal names the cause and the fix),
install and the two suites, the verbs, the handles (`--tab SPEC`/`active`,
`--browser`, `--profile`), **what it will and will not do to your browser**
(its own profile; `attach` is the consent; `close` asks before taking tabs and
never `SIGKILL`s; every drive checked against `/proc`; `tab js` runs caller
code; the log redacts proven secrets), the capability table `selftest` reports,
and how to write a caller. It also states the honest limit up front: `find` and
`text` do not see iframes.

Not done, and named in §4.5: an index to publish to, and a distribution name
that does not collide with the public `browser-control` project.

### 5.22 `profile info|seed|reset` — the profiles, managed — done

A new browser-level noun, the way `tab` is a page-level one, with the three
verbs a caller needs before `open`:

| verb | classes | what it does |
| --- | --- | --- |
| `profile info [--profile DIR]` | `read` | every managed profile: weight, files, last change, whether a browser is on it (pid/port/exe/verified), whether it is attached, and whether it is the DEFAULT instance for a browser binary |
| `profile seed --from DIR [--force] [--dry]` | `write`, `file` | copies a source profile's LOGINS into a managed instance |
| `profile reset [--force]` | `write` | wipes one, so the next `open` starts clean |

Three rules, and they are why this is not a `copytree` call:

1. **A running profile is never written.** Both destructive verbs refuse
   `profile-live`, naming the pid, because copying under a live browser is how a
   profile gets corrupted — the thing Chrome's own singleton warning is about.
2. **Only this CLI's root is touchable.** `profile reset --profile
   /home/you/.config/google-chrome` refuses `not-managed`: reading a source
   profile is just a copy, but wiping one is destroying somebody's real browser.
3. **What landed is read back.** Every file the source has (minus the skips) is
   checked for in the target by size; a mismatch refuses `seed-not-verified`
   rather than reporting a login that is not there. `--dry` counts first, so
   `--force` can be a decision instead of a hope.

The engine's details are the honest ones: the skip list is by NAME at any depth
(lock files, `DevToolsActivePort`, our own records, and the caches that are
hundreds of megabytes of nothing) so a future Chrome that adds a login store
still gets seeded; symlinks are counted and NOT followed, because a profile can
contain links into the filesystem; and the reply says what it is worth — bytes,
files, dirs, what it skipped, how many links it left alone, and the caveat that
matters: the cookie and password keys live in the OS keyring, so the copy
decrypts **on this machine, for this user**, and the verb does not pretend
otherwise.

Also: `profile seed` answers the deferred policy question from the old plan —
seeding is **explicit only**; `open` never seeds behind your back. Not carried
over from that sketch: reflink and the atomic swap (a copy plus "refuse to run
on a live profile" is what the verification actually needs).

Hermetic (34): the surface check now covers `profile <sub>` through one
`unclassified(HANDLERS, {"tab": …, "profile": …})` call, so a noun added without
classes fails the same way a verb does. Battery (55): a source fixture of two
"login" files plus a lock file and a cache dir — seeded in, the two land and the
other two are skipped, reseeding refuses `profile-exists`, `--dry` writes
nothing, a live profile refuses `profile-live`, a wipe needs `--force` and then
really wipes — and a profile outside the root refuses `not-managed`.

### 5.23 The policy gate, and four warts — done

**The gate.** The declared surface (§5.17) said what each verb CAN do; this says
what this CALL may do, before it does it. `--allow read,write` / `--deny code`
(or `BROWSER_CONTROL_ALLOW` / `BROWSER_CONTROL_DENY` for a whole session, which
is how a host would set it once) refuse `not-allowed`, naming the classes the
action holds and the rule that blocked them:

```
$ browser-control-cli tab js "1" --allow read,write
ERR[not-allowed]: tab js is code+write, and 'code' is not allowed by --allow/--deny
```

Three decisions worth recording:

* **Fail closed.** An action with no classes is refused as unclassified, and a
  policy naming a class that does not exist is `bad-args` — a typo in a policy
  must not quietly allow what it was written to stop.
* **`selftest` and `help` are never gated.** `selftest` is the verb that
  reports the policy, and a gate that blocked its own explanation would be a
  trap; `help` prints the usage and performs no action. Both are still AUDITED:
  every invocation writes its one line, gate or no gate.
* **The MODE decides.** `tab wait --for js` is code+write while `tab wait`
  reads, `tab dialog accept` writes while `state` reads — so the gate resolves
  the action from argv, and `tab dialog` with no mode maps to `state`
  (otherwise a READ would be refused as unclassified). No policy set means no
  gate, which is what every call did before one existed.

**Four warts, all of them places the tool claimed more than it knew:**

1. `_one_tab` refused `cdp-not-local` — "the endpoint is not the browser it
   claims to be" — when NOTHING was running at all. It now asks the strict form
   first (an endpoint that answered but did not verify is that refusal) and
   otherwise says there is nothing to drive.
2. A scoped call with nothing up said "no drivable browser" without naming the
   instance it was about. `_no_drive` names it and the command that starts it.
3. `list --profile DIR` silently DROPPED the scope: the caller asked about one
   instance and got the whole machine. It refuses `bad-args` now, the way it
   already refused `--browser`.
4. `profile seed` said nothing when the SOURCE profile was live. It warns now
   ("what is on disk may lag its live state"), because a live source is a
   legitimate snapshot while a live TARGET is a refusal.

Hermetic 34 -> 35 (the gate's rules, the resolver's mode cases, the exemption,
and `list`'s refusal), live 55 -> 56 (an allowed read works while a denied write
refuses `not-allowed` with nothing sent; a live source warning; a policy typo
refused). 0 skipped, no leftovers.

### 5.24 Frames — cross-origin ones, driven through their own target — done

Measured first, because the design depended on it: a cross-origin frame is its
own CDP target (`iframe CAFE5E1DDC http://localhost:8942/inner.html` in
`/json/list`), `cdp.Session` on it reads that frame's DOM, and a real
`Input.dispatchMouseEvent` at FRAME-LOCAL coordinates fires the frame's own
handler. So the whole existing verb set works inside a frame unchanged — which
made this plumbing rather than new verbs:

| piece | what it is |
| --- | --- |
| `tab frames [--tab SPEC]` | every frame of the page: index, url, name, box, `same_process`, `visible`, and the CDP `target` it can be driven through |
| `--frame VALUE` (global, like `--profile`) | a URL substring or an index from `tab frames`; `dom._session` attaches to that frame's target, so `text`/`find`/`click`/`js`/… run inside it |
| the frame census in reads | `tab text` and `tab find` carry `frames: {total, separate, same_process, visible, bound}`, so a read that omits frame content SAYS so — `separate` is the CENSUS fact (the page says the frame is not same-process) while `bound` counts the frames a `--frame` can actually attach to, and a frame the browser cannot attribute is `target: null, matched: false`, never a bare `""` claiming it has no target |
| `tab click\|hover --at X,Y` | real input at a POINT, for what no selector can reach (a canvas): `verified: false`, with `under` reporting what the point actually reaches |
| `frame-ambiguous` / `no-frame` / `frame-not-separate` | several frames match → the indices are named; none → what exists is named; a frame sharing the page's PROCESS has no target to drive, and the refusal says what to do instead (`tab text`/`tab extract --frame N` read it; `--at` hits it) |

How common frames are, measured on this machine (6 pages): 0 on static content
(example.com, MDN, GitHub login), and — exactly where an agent has to ACT — 2 on
the Guardian (1 visible, a CMP at sourcepoint), 3 on the reCAPTCHA demo, 10 on
Stripe's docs (9 cross-origin). A cross-origin `contentDocument` is null, so
`tab js` cannot help there; OOPIF attach or a raw point are the only ways in.

**One anomaly, logged rather than smoothed over**: on the battery's `/frames`
fixture, inside the battery, a click reaches the control (`under` = the button)
and its handler does NOT fire. In isolation the same page, the same command
order, live cross-origin frames, a background tab and the same commands all
fire — five reproductions. The check therefore asserts the dispatch and the
point's reach (provable) and not the firing (not reproducible there), and this
line is the record: the cause is still unknown, and the suspects ruled out are
stray browsers occluding the window (four of mine were alive — cleaned up),
a hidden tab, the command order, and OOPIF presence.

Hermetic 58 (the scope is set AND cleared per invocation like `--profile`, the
point syntax with a refusal naming the verb that asked, and the verb list: only
tab subcommands may claim a frame, and `nav`/`list`/`activate` may not), live 58
(three frames with two separate: driven by index and by URL, a click inside one
fires its own handler, the census in `text`, the ambiguity named with indices,
and the two refusals).

### 5.25 The review campaign — what five lanes found, and what changed

§4's appetite ("verify, or refuse") had been applied by hand until now; this is
the first time the work was reviewed by someone who did not write it. Five
read-only lanes (transport/process, DOM tier, gate/argv, filesystem/audit,
adversarial + tests) read the tree against the documented contract, and every
finding was re-verified here before it was fixed — two of the lanes' findings
were refuted that way, and one was downgraded after measurement.

* **The gate could be walked through.** `--for JS`, `--for " js"` and
  `--for load --for js` were authorised as reads and then ran caller code;
  `--deny X` voided `BROWSER_CONTROL_ALLOW`; an argv of only global flags raised
  an IndexError out of `main`; `--allow ,` switched the gate off, and so did a
  blank policy value in the environment.
* **Writes could enter a browser nobody handed over**: with no `--tab`, scoped
  to a profile outside the root, or through `Target.createTarget` on a port file
  that named another endpoint.
* **`--frame` could drive another tab's frame** (URL matching across the whole
  browser, then a fallback that trusted a URL appearing once), and `tab wait`
  never applied the scope while its reply said `frame: 1`.
* **Claims that could be false**: `clicked: true` for a press that landed
  outside the element (the hit-test clamped, the press did not), `verified: true`
  for a point hover, `moved: true` by tautology on a parked tab, "no tab matches
  … have: none" for a tab list that could not be read, `separate` counting two
  different facts at once, and a census failure reading as "no frames".
* **Filesystem and process**: seed/reset took the root lock while `open` took
  the profile lock (a reset could delete a starting browser's directory),
  liveness compared paths by exact string, `_is_managed` was lexical and `_copy`
  wrote through a planted symlink, the seed read-back re-walked the source
  instead of the copy's manifest, the audit scratch path was predictable and the
  log file's mode was the umask's.
* **Transport**: the HTTP read followed redirects off-loopback, `Session._connect`
  escaped `call()`'s try, a `Page.enable` protocol refusal was recorded as
  "parked", and `/proc` cmdlines were truncated at 4096 bytes — the text every
  identity decision reads.
* **Tests**: the whole websocket transport was untested (including the loopback
  host check), two assertions could not fail, the battery's stale-root sweep
  could not see four of its five throwaway prefixes, and three live oracles
  would have passed with the feature broken.

Every fix carries its own check, and what could NOT be proved is written down
where it lives instead of being smoothed over: §5.24's click anomaly on the
`/frames` fixture (the identical sequence navigates in isolation, five ways
over), the DOM tier's page-owned oracle (README), and the two branches this
harness cannot exercise at all —

* a **prompt's** `--text`: a dialog opened by a CLI-dispatched click is
  suppressed before any client can answer it (`c_tab_dialog` measures that for
  an alert too), so the live check asserts the suppression and the `--text`
  argv is pinned hermetically;
* a screenshot at **devicePixelRatio ≠ 1**: the ratio belongs to the display,
  and the CLI exposes no launcher flag for it, so the arithmetic is only
  exercised at dpr 1 (the PNG's own IHDR is still what proves every shot).

### 5.26 `profile seed` lands where Chrome reads it — done

Found in use, not by a lane: seeding a real login (`profile seed --from
~/.config/google-chrome/Default`, the documented source) put the cookies in the
managed INSTANCE root, while `open` launches Chrome with `--user-data-dir=`
that instance and Chrome reads its profile from the `Default/` subdirectory. The
seeded login was real (`auth_token`, `ct0`, `twid` all present) and completely
ignored — x.com showed the login wall on a "seeded" browser.

The layout rule is now explicit (`profile._seed_destination`): a source that is
a USER-DATA directory (`Default/` child, or `Local State` beside it) copies into
the instance root, bringing `Default/` with it; a source that is a single
PROFILE directory (`~/.config/google-chrome/Default`, `Profile 1`, a snap or
Flatpak path) copies into `<instance>/Default/`, where Chrome reads it. The
reply names both paths (`profile` the instance, `profile_dir` the destination),
and the same-tree refusal compares against the real destination.

The gap in the evidence was the real lesson: the battery seeded a fixture and
asserted two files landed in the target, but never LAUNCHED a browser on the
seeded profile. It now does: after seeding a profile directory, it opens the
instance, closes it, and requires the seeded `profile.name` to still be there in
`Default/Preferences` — the file Chrome actually used — then wipes it.

### 5.27 Extraction, plugins, and the first one (`x`) — done

The core gained the generic half of "scrape a page" and a home for the
site-specific half:

* **`tab extract`** — `--each SELECTOR` names the repeated item and each
  `--field NAME=SELECTOR[@ATTR]` names a value inside it (innerText by default,
  an attribute with `@ATTR`, `@ATTR` alone for the match itself, `:scope` for
  the match's own text). `--cap`, `--chars`, `--visible` and `--unique` bound
  it; values are sliced IN THE PAGE and the whole answer is budgeted, so a
  document cannot flood the reply. It is a **read**: CSS only, no code, so
  `--deny code` can still extract. The page's rows are shape-filtered exactly
  like `find`'s (`_extract_records`), never raised.
* **`lib/plugins.py`** — plugins load from `BROWSER_CONTROL_PLUGIN_PATH`
  (override) or `~/.local/share/browser-control/plugins`; each declares
  `PLUGIN = {api, name, description, actions:{verb:{run, classes, usage}}}`.
  Loading is fail-open for the CLI (broken/colliding/wrong-API plugins are
  recorded and skipped), `selftest` reports `plugins`/`plugin_errors`, `--help`
  appends usage, and the declared classes go into the gate's surface
  (`capabilities.PLUGIN_ACTIONS`) while staying out of the built-in `ACTIONS`
  table the hermetic check validates. A plugin action runs in-process and is
  trusted local code; its classes are declarations, not a sandbox.
* **`plugins/x_reader.py`** — the first plugin: read-only X search.
  `x search QUERY [--latest|--top] [--cap N] [--chars N] [--tab SPEC]`
  navigates the tab, waits for idle (suppressed if X never idles — the
  extraction is the real read-back) and then for the first rendered post
  (idle does not mean rendered on an SPA), calls `tab extract` with X's
  selector map
  (`article`, `tweetText`, `time@datetime`, the status link), maps rows to
  `{id, handle, url, time, text}` (dropping link-less rows and duplicate ids),
  and reports `sort` from the page's own selected tab. The map is the one place
  that breaks when X's DOM changes.

Tests: hermetic coverage for the schema parser, the record shaper, the
`tab extract` argv, the plugin loader (valid/broken/colliding/wrong-API) and
the X plugin's URL building and record mapping (stubbed, no network); the
battery extracts from a real local fixture — four articles, one `hidden` — so
projection, order, `--visible`, `--chars` and the cap are proven in a browser.

### 5.28 The quality campaign — suppressions, a cycle, a monolith, duplication — done

§5.25's review lanes grew a fourth, quality lens (architecture, type checking,
duplication, dead weight). What it found there overlaps the bug/security lanes
and is in the `review fixes` commit before this one (the lock that did not
survive its own wipe, wall-clock deadlines, symlink-following writes, 0755
profile trees, the URL prefix match, the empty profile that normalised to the
CWD). The hygiene it found on its own:

* **169 stale `pyright: ignore[reportMissingImports]` suppressions, gone.**
  `pyrightconfig.json` already carried `extraPaths: ["."]` and the tree was
  clean WITHOUT them — they suppressed nothing that fires, while disabling the
  one error class that config exists to catch (a renamed module, a moved
  symbol). Removed repo-wide, nothing restored, pyright stays at 0. The one
  real failure underneath was provisioning, not code: `import websockets` does
  not resolve in an interpreter that has not installed the project, which is
  what CI now does before checking anything.
* **A ruff config, committed.** The tree's own 140-plus `# noqa` markers named
  a rule set that existed nowhere in the repository (`.gitignore` listed
  `.ruff_cache/` and nothing configured ruff): `select =
  ["E","F","W","SLF","BLE","S","N","ANN"]`, `ANN401` ignored (`Any` is
  the house type at the page-supplied boundary — `coerce.py` is what narrows
  it), `S603`/`S607` ignored (the one vetted spawn: `proc.spawn`, list argv, no
  shell), per-file-ignores for the suites (they *are* assertions) and for the
  `lib/browser/`+`lib/dom/` facade seam (the deliberate `_pkg._x` indirection
  fires `SLF001` 167 times), and 12 justified markers for the residue. `uvx
  ruff@0.14.9 check .` went from 1118 findings to 0. Nothing was renamed or
  re-annotated to satisfy it: signatures and messages stay frozen.
* **CI.** `.github/workflows/ci.yml`: a `static` job (ruff + pyright) and a
  `unit` job (the hermetic suite), both installing the project first; the live
  battery is excluded with its reason written in the file. §4.6 is paid.
* **Dead weight and the plugin vocabulary.** `frame_rows()` and
  `AttachmentStore.get()` (zero callers, verified) are gone; `coerce.as_ints`'
  `count=0` footgun ("0 means no limit", not "none") is `None`; `plugin_api`
  now re-exports the whole `errors` module so a plugin writes
  `fail(errors.ERR_BAD_ARGS, …)` instead of a raw string, `x_reader.py` was
  refactored onto it, and the hermetic code-vocabulary scan now reads
  `plugins/` too (proven both ways: an unregistered literal and an f-string
  code each fail it).
* **The `cli.main ↔ cli.verbs.browser` cycle, broken by moving the tables
  home.** `HANDLERS`, `TAB_SUBCOMMANDS`, `PROFILE_SUBCOMMANDS`, the gate's
  `POLICY` and the plugin set `PLUGINS` live in `cli/registry.py`, filled by
  one `registry.register(…)` call from `cli.main` at import. `cmd_selftest`
  reads them there, so the deferred `from browser_control.cli import main` —
  a reach into another module's privates, honest only because it was deferred —
  is gone, along with the two `noqa: SLF001` markers that apologised for it.
  `registry` imports no verb module, so the graph is a DAG (main → verbs →
  registry, main → registry) and importing `registry` alone pulls in zero verb
  modules (measured). The tables are empty until `cli.main` is imported and
  their only reader that could observe that is reachable solely through main's
  dispatch — the registry docstring records that rather than leaving it to be
  rediscovered.
* **The `profile` monolith split behind its facade.** `lib/profile/__init__.py`
  was 674 lines: four verbs, the SQLite login-store readers and the tree
  walkers, while `browser/`, `dom/` and `cdp/` were each split by
  responsibility. It is now `stores.py` (the store census, hosts and counts,
  never values), `trees.py` (the shared walkers), `seed.py`, `reset.py` and a
  facade `__init__` that keeps the small `info` verb and re-exports the rest.
  A pure internal split — the public surface, every refusal text and the
  lock-invariant comments are unchanged.
* **Duplication consolidated, one spelling per concept.** `argv.py`'s three
  `--flag VALUE|--flag=VALUE` readers are one `_scan` helper (refusal texts
  byte-identical); the `websockets is None` guard spelled five times is one
  runtime `require_websockets()` (runtime, because the suites flip the binding
  to prove the refusal); `_opt_int` replaced eight hand-written `_int(…) if …
  is not None else None` calls in `verbs/tab.py`; `dom/actions.py`'s
  ten-fold element prelude is one `_target` context manager (open for writes →
  session → match → pick, in the one order the contract needs), carried by the
  five verbs that shared it; `errors.CODES` is derived from the module's own
  `ERR_*` namespace instead of hand-repeating 72 names (a frozenset now;
  membership consumers and the hermetic check are unaffected). The facade
  aliases were inventoried one by one — every remaining alias has a caller, so
  the patch seam stays.

Not done, deliberately: an upper bound on `websockets` (no matrix to justify
one) and memoizing `machine.browsers()`'s per-invocation `/proc` census (a
staleness trade for a cost no measurement has shown to matter).

### 5.29 The report-only pair — `seed --force` overwrites, `tab js` returns the page's value — done

Both were left report-only by §5.25's review, and both were a promise the code
did not keep.

* **`profile seed --force` now OVERWRITES.** The refusal had always promised
  "overwrites it (logins and all)", but the copy wrote only what the SOURCE
  held: a file unique to the target survived it, so seeding A then
  `--force --from B` left a MIX of the two profiles. `--force` now runs
  `reset`'s wipe under the lock the verb already holds — the attach record
  dropped when one named the profile, `rmtree`, the pid file removed, and the
  "still exists after the wipe" read-back — and only then copies, so what is
  left is the source's content. The reply says what was destroyed, not only
  what arrived (`wiped`, `wiped_files`, `wiped_bytes`, `detached`), and a
  `--dry --force` run reports the wipe it WOULD cause under `would_wipe`
  rather than claiming one it did not do. The copy engine's "never write
  through a link that is already there" guard is now reachable only for a
  link planted between the wipe and the copy, so it is pinned at the engine
  in the hermetic suite; the verb-level check plants a link, and now asserts
  the wipe destroyed it and the file it pointed at was never touched.
* **`tab js` returns the page's OWN value.** `_value_of` ran `json.loads` on
  every string answer, so a page string that merely PARSED as JSON was
  reported as the value it looked like — `localStorage.getItem('k')` holding
  "null" answered `null`, "true" answered `true` (a review measured it). The
  decode is load-bearing for this CLI's own probes, which `JSON.stringify`
  their findings on purpose, so it stays the DEFAULT and the escape hatch asks
  for the new `raw=True` seam (`rpc._value_of` → `Session.evaluate` →
  `cdp.evaluate`, with `dom/queries.js` the one caller that passes it;
  `tab wait --for js` keeps the decode). Structure is not lost:
  `returnByValue` serializes an object or array expression itself, so
  `({a: 1})` answers an object while `JSON.stringify({a: 1})` answers its
  string. The live check that asserted the old decode
  (`tab js "JSON.stringify({a: 1, b: [2, 3]})"` == the object) encoded the bug;
  it now asserts the object expression, the stringified one, and the "null"
  string that started it.

Evidence: 73 hermetic checks green (two new — `seed --force overwrites, never
merges`, including the engine-level link guard, and `tab js keeps a
JSON-looking string a string`), pyright 0, `ruff check .` 0, live battery
60/0/0, the `tab js` check now reading "a value, an object, a JSON-looking
string kept a string, js-error, result-too-large".

### 5.30 `open --headless` — the same verbs, no window — done
`open --headless [URL…]` starts the managed browser with `--headless=new`, the
browser's own windowless mode (§5.11 measured the alternatives: a headed
browser cannot hold a page without a window, and `--no-startup-window` buys
only a warm endpoint). The verbs need no change to work against it — every one
speaks CDP, which does not need a screen — so the feature is the mode flag,
its read-back, and the one rule that keeps the answer honest.

The mode belongs to the PROCESS, not the call: `flags(profile, headless)`
appends the flag at spawn, `open`'s reply carries `headless`, and adopting a
browser that is already up reports the mode THAT one is in, read from its own
`/proc/<pid>/cmdline` (`proc.is_headless_cmd`: bare `--headless`, any
`--headless=…`, or the dedicated `chrome-headless-shell` binary, which needs
no flag). `list`, `info` and `tab list` report the same field on every row, so
a caller can tell a windowless browser from a windowed one without guessing.

Asking for `--headless` when a windowed browser is already on the profile
REFUSES `bad-args` and names the fix (`close` it, then `open --headless`
again): `open` adopts what is running, and silently answering a headless
request with a windowed browser would overrule the caller's argv. The other
wrong direction cannot happen — `open` with no flag adopts either mode and
reports which.

*Done when* one live check starts a headless browser on its own profile,
proves `--headless=new` in its `/proc` cmdline, drives `tab text`, `click`,
`screenshot` and `activate` against it, sees it in `tab list` as
`headless: true`, and stops it again.

Evidence: 75 hermetic checks green (two new — `open --headless reaches
launch`, `headless detection reads the process`), pyright 0, `ruff check .` 0,
live battery 61/0/0 — the new `open --headless drives the same verbs,
windowless` check started a second instance on `<ROOT>/headless`, read
`--headless=new` from its own `/proc/<pid>/cmdline`, drove `text`, `nav`,
`click`, `screenshot` (PNG header verified), `activate` and `tab list`
(`headless: true`) against it, saw a plain `open` adopt and report
`headless: true`, saw `--headless` against the HEADED instance refuse
`bad-args` naming the fix, and stopped it verified.

### 5.31 A seeded profile's extensions are never loaded — done
`flags()` now carries `--disable-extensions` in BOTH modes. A managed profile
is routinely SEEDED from the user's own (`profile seed` copies the tree minus
caches and locks, `Default/Extensions` included), so a "managed" instance was
loading third-party code the CLI never chose — and under `--headless=new` one
of them broke the browser it was loaded into.

Same binary (Chrome 153), same flags, the profile the only variable:

| profile at launch | main-thread CPU | `/json/version` |
| --- | --- | --- |
| scratch (`/tmp`) | 0.0% | 5 ms |
| seeded (managed) | 126% | 7.4 s |
| seeded + `--disable-extensions` | ~7% | 4 ms |

The spinner was in the target census before the endpoint starved: MetaMask's
`offscreen.html` background page plus extension service workers
(`nkbihfbeogaeaoehlefnkodbefgpgknn`, `nngceckbapebfimnlniiiahkandclblb`,
`bnccfnkpnedbcganaoiaiancmfddjedl`). A browser whose own DevTools HTTP
endpoint answers in 7 s is not drivable: `open --headless` on the seeded
profile left the process at 114–126% CPU, every call a 5–15 s timeout
(`cdp-unreachable`), and the keystrokes that did land arrived mangled — a
query typed as `araghchi speaking in UN` reached the box as `hchi speak in
UN`.

The flag rides along in both modes on purpose: the mode is a property of the
PROCESS, and a headed instance is otherwise the same browser as the headless
one — one launch contract, not two. The extensions stay on disk; only the
code is never loaded.

*Done when* the live check reads the contract off a real process in both
modes, and the previously-failing case (`open --headless` on the seeding
profile) drives the verb surface with no timeouts and no dropped keystrokes.

Evidence: 75 hermetic checks green (the launch-flags check now asserts
`--disable-extensions` in the headed AND headless argv), pyright 0, `ruff
check .` 0, live battery 61/0/0 (`open --headless drives the same verbs,
windowless` now also asserts `--disable-extensions` on the headless process
and on the battery's headed one). On the seeding profile that failed before:
`open --headless` launches at 6–11% CPU (was 114–126%), `/json/list` reports
0 `chrome-extension://` targets (was 4), `tab info` answers in 125–131 ms
(was 5–15 s timeouts), `tab type "headless fix check"` verifies all 18
characters (that path dropped 11 of 23 before), and a headed instance,
launched with the same flag, drove `tab type` verified too.

### 5.32 `google search` — the query is typed, never put in a URL — done
`plugins/google_search.py` searches Google the way a person does: it opens the
homepage, puts the caret in the search box (`focus`), types the query as
per-character key events at 90 WPM (`12 / wpm` seconds between keystrokes,
`--wpm N` to move it), presses Enter, and reads the rendered cards with the
core's extraction engine. NO query URL is ever built — the one address the
plugin navigates to is the homepage, and the `/search?q=…` in `landed_on` is
the one the SITE put in the bar after the submit.

Two pieces of core grew for it, both additive:

* `dom.type_text(…, delay_s=…)` — the pause BETWEEN keystrokes. The default
  is still `TYPE_PAUSE_S` (as fast as a page's handlers take it), and a caller
  that wants a human cadence asks for one; a non-numeric or negative delay is
  `bad-args`, not a traceback.
* `plugin_api` re-exports the writing verbs a plugin like this needs —
  `focus`, `type_text`, `press`, `click` — beside the reads it already had
  (`nav`, `wait`, `extract`).

The selector map lives at the top of the plugin (`textarea[name="q"]`,
`#search div.MjjYud:has(h3)`, `h3`, `a@href`, `div.VwiC3b, div[data-sncf]`).
`:has(h3)` keeps the container to cards that HAVE a heading — measured on a
live SERP: 18 `div.MjjYud` containers, 9 with a heading — so `--cap N` counts
results rather than the panels around them.

*Done when* an offline check proves the input path (one navigation, to the
homepage; the query through `type_text` at the cadence; a real Enter; unnamed
rows dropped) and a live run answers with the page's rendered results.

Evidence: 76 hermetic checks green (one new — `google plugin types, never
builds a query URL`: the fakes record one `nav(HOME)`, `type_text(q, 12/90)`,
`focus(textarea[name=q])` and `press(enter)`, and the check also covers the
`--cap`/`--wpm` refusals, the `--deny write` refusal, and the
`type-not-verified` refusal when the read-back says the text did not land),
pyright 0, `ruff check .` 0 — and two live runs through the plugin on the
managed headless browser: `araghchi speaking in UN` (23 chars, 90 WPM
requested, 85.4 measured — the gap is the per-character CDP round trip,
reported, not hidden; 5 named results, `landed_on` the site's own
`/search?q=…`) and `xiaomi 18 pro max release date` (30 chars, 86.1 measured,
3 results).

### 5.33 The research round — challenging browser operations, and the suite's other half

Three delegated research lanes (deepseek provider, `deepseek-flash`) and one
delegated test review, written to `research/` and since folded into the fixes
below (the directory was cleaned up once its findings landed):
`cdp-hard-operations.md`
(dialogs/OOPIF/shadow DOM/uploads/downloads/nav races/trusted input/PNG
oracle, 91 cited URLs), `flake-and-client-testing.md` (actionability, polling,
hermetic tiers, quota and process hygiene, 88 URLs, primary sources fetched),
`security-and-lifecycle.md` (DevTools exposure, rebinding, `navigator.webdriver`
measured on this host's Chrome 153, `/proc` identity, 0700/0600 discipline,
58 URLs), and `current-tests-review.md` (the read-only review of both suites).

What the review found, and what is now tested:

* **P0 — the refusal half of "verify, or refuse".** Every mutation verb was
pinned on SUCCESS only; the branch where the read-back says no was reached by
no check. New `t_mutation_readbacks_refuse_when_the_page_says_no` scripts the
page's own probes and drives `insert`/`type` (`insert-not-verified`,
`type-not-verified`), `check` (`check-not-verified`), `hover`
(`hover-not-verified`), `upload` (`upload-not-verified`), `media play`
(`media-not-verified`), `ambiguous-element` and `no-match` — asserting the
real input WAS dispatched before the refusal.
* **P0 — a refused screenshot writes nothing.** New
`t_screenshot_refusals_write_nothing`: geometry mismatch, non-PNG bytes, a
non-finite dpr and an undecodable base64 each refuse with the target path
absent; the matching header writes the file and the reply equals its bytes.
* **P0 — a partial seed never says verified.** New
`t_seed_refuses_a_partial_copy`: an unreadable source directory refuses
`seed-not-verified`, and the manifest oracle (`seedtree.missing`) names a file
that did not land and one that landed the wrong size.
* **The lifecycle and form verb refusals.** New
`t_lifecycle_readbacks_refuse_when_the_page_says_no` (`nav-not-verified`,
`reload-not-verified`, `activate-not-verified`, `nav-failed`),
`t_scroll_and_viewport_refusals` (`scroll-not-verified`, `no-viewport`),
`t_dialog_verdicts_are_three_state` (`open: null` + `verified: false` when the
tab cannot answer, `dialog-not-verified`, `no-dialog`),
`t_select_and_ambiguous_option` (`ambiguous-option`, `not-a-select`),
`t_close_survivor_is_a_refusal` (`close-tab-not-verified`).
* **A skip is not a pass (hermetic too).** `t_page_expressions_compile` used to
print a note and return normally when `node` is absent, which `check()`
counted as PASS. The suite now records SKIP and pays for it in the exit
status, exactly as the battery always did.
* **`cdp.evaluate_until` had no test.** New
`t_evaluate_until_retries_a_silent_sample` drives the fake websocket peer so a
dialog parks the renderer mid-poll: the sample is swallowed and retried on the
same connection (`samples == 2`), and an already-parked session refuses
`blocked`.
* **Plugin specs fail closed.** New `t_plugin_specs_fail_closed`: no PLUGIN
dict, no name, no actions, a non-verb name, no `run`, no classes, an unknown
class, and two plugins colliding with each other — plus the enforcing half
(the classless action is not on the surface) and the plugin usage line in
`--help`.
* **The attach file is an authorization.** New
`t_attachment_store_fails_closed`: a malformed `attached.json` reads as "nothing
is attached" (never a holdover grant), record round-trip and drop-by-pid, and
a leftover `.new` scratch file refuses `attach-failed` without replacing the
records.
* **`write_atomic`'s own refusals.** New
`t_write_atomic_refuses_a_leftover_temp`: a planted symlink and a real
leftover `.part` file each refuse `write-failed`, and neither the victim nor
the leftover moves.
* **`_tab_count`'s positive half.** A verified row with two tabs sums to 2;
the stranger row is the zero case (the old check passed on a constant 0).
* **The seed walk's bookkeeping.** New
`t_seedtree_walk_counts_what_it_skips`: `links`, `special`, `unreadable` and
the `ours` filter that keeps a fresh profile from refusing `profile-exists`.
* **`poll` is monotonic.** New `t_poll_runs_on_a_monotonic_clock`: the wall
clock is stepped back an hour and the budget still expires on time.
* **The CDP input shapes.** New `t_input_dispatch_shapes`: `insert` sends ONE
`Input.insertText` and zero key events; `press enter` sends keyDown (with
`text`) then keyUp (without); `press arrowleft` sends `rawKeyDown` with no
text; `click` sends the moved → pressed → released triad at one point with
`buttons`/`clickCount`, and `upload` binds the element by **objectId**.
* **The anti-bot launch surface.** `t_launch_flags` now also pins
`--disable-blink-features=AutomationControlled`, no `--enable-automation` and
no `--remote-allow-origins`; the transport check asserts the frame set contains
no `Runtime.enable`/`Console.enable`/`Debugger.enable` (the documented
side-channel); the lock file is 0600.

A real defect in the suite was found on the way: `t_http_read_never_uses_a_proxy`
set `http_proxy` to its fake server and restored only the keys that had been
there before, so the dead proxy outlived the check — and the first check that
opened a websocket afterwards (`t_evaluate_until_…`) failed `ECONNREFUSED`
against it. The leak is fixed, and it is the reason the suite's later outbound
connections are now honest.

The battery gained the same treatment where the oracles are real: `env()` drops
the host's `BROWSER_CONTROL_ALLOW`/`DENY` and pins
`BROWSER_CONTROL_PLUGIN_PATH` to an empty directory (the hermetic suite always
did; a host with a session policy made write checks fail spuriously), its
`http_json` oracle uses a proxy-free opener, `open` reads the `.pid` record
back from disk and requires it gone after `close`, and the headless check now
reads the page's OWN `navigator.userAgent` (no `HeadlessChrome`) and
`navigator.webdriver` (false despite the DevTools port) — the anti-automation
contract the security research measured.

A second, independent review pass (delegated on `deepseek-flash`, over the
diff itself) then found and closed a further set: the monotonic-clock check
could not fail for the reason it named (it now arms `time.time` to raise), the
redundant no-`Runtime.enable` loop was folded into the exact frame-list
assertion, `insert`'s refusal is now driven through the VERB and not only its
reply builder, dead scaffolding in three scripted checks was removed, two
checks remove their temp roots and SKIP under root (where `chmod 000` proves
nothing), and the six refusal codes still never produced — `eval-timeout`,
`browser-not-stopped`, `profile-unusable`, `reset-failed`,
`reset-not-verified`, `seed-failed` — are now driven by their real paths (the
new `t_the_remaining_refusal_codes`). The battery's `env()` also drops the
host's `http_proxy` family (the transport resolves proxies via
`getproxies()`, so a host proxy would divert every websocket verb), and its
headless UA assertion is now gated on the process really carrying
`--user-agent=` (the launch is best-effort when `--version` cannot be parsed).

A genuinely hard operation from the CDP research joined the battery:
`c_beforeunload_is_bounded_and_named` arms a real `beforeunload` (a trusted
click), proves `tab nav` refuses `nav-failed` within its grace naming the
dialog and never hangs, and then watches the blocked move LAND once the
one-shot client detaches — on its own tab, so the battery's shared fixture is
never parked.

The first CI run on GitHub's runner then found a REAL portability bug in the
product: `proc.exe_path` read `/proc/<pid>/exe` without the OSError guard its
sibling `exe_name` carries, so a census row for a process this user cannot
inspect (pid 11, root-owned, on that runner) raised PermissionError out of
`browsers()` — `list`, `tab list`, `info` and every shared resolver died with
a traceback instead of reporting the machine. The guard is fixed and
`t_an_unreadable_proc_path_is_a_row` pins it (the row is built with `path: ""`
and what else was readable).

*Done when* every review P0 and every named refusal code has a check, the
research's input-shape and anti-bot contracts are pinned, and both suites are
green.

Evidence: 120 hermetic checks green (18 new), pyright 0, `ruff check .` 0, and
the live battery **62 passed, 0 failed, 0 skipped** (additions above, run
three times against google-chrome-stable 153.0.8010.52 on a throwaway root;
the beforeunload check was stable in a separate measurement too).

### 5.34 `x search` loads the timeline — the wheel and the merge are the plugin's job now

A real research run (`x search` for today's bitcoin-price posts) found the
plugin's own ceiling: X keeps only 3–9 `article` rows mounted and RECYCLES
them as the timeline moves, so one `tab extract` answered 3 posts for a
20-post request. The session worked around it by hand — wheel down, extract
again, append, dedupe by URL — and that loop is now the plugin's:

* **The wheel is on the plugin seam.** `plugin_api.scroll` joins `focus`,
  `type_text`, `press` and `click` (additive; `api: 1` stays, no existing
  plugin changes). It is a real wheel event: how a lazy or virtualized list is
  made to render past its first window. The X plugin still declares
  `read+write` and still only reads content — the wheel is its only input.
* **`--cap` is a TARGET.** `_collect` extracts, wheels `SCROLL_PIXELS`, waits
  `SCROLL_PAUSE_S` for the next window to mount, extracts again and merges by
  post id, until it has `--cap` posts (`--max-scrolls`, default 10, bounds the
  work; `0` restores the old first-render behavior). A round that mounts
  several posts at once is sliced back to the cap, like every other cap.
* **The stop is named.** The reply's `loading` block reports `reads`,
  `scrolls`, `max_scrolls` and `stop` (`cap`, `exhausted`, `max-scrolls`,
  `no-posts`, `scroll-failed`); `truncated` is true whenever loading stopped
  short, since more posts may exist below. Two empty rounds — each given a
  retry read after a beat, so a slow mount is not mistaken for the end — say
  `exhausted` and set `truncated: false`.
* **Measured on the real site**: `x search "bitcoin price" --latest --cap 20`
  → exactly 20 unique posts, `stop: cap` (runs took 7.4–8.7 s with 5–6 reads
  and 4–5 scrolls — X's window size varies run to run, which is exactly why
  the reply reports them); the same command with `--max-scrolls 0` → the 3
  rows of the first render. The `:scope` fix from the extraction round also
  made `sort` real: the page's own tab strip is read instead of always
  falling back.

New hermetic check `t_x_plugin_loads_more_on_scroll` replays a virtualized,
overlapping window sequence and pins the cap overshoot, the `exhausted` stop
and the retry cadence (sleeps recorded, not waited); `t_x_plugin_offline` pins
`--max-scrolls 0` (the wheel must never be sent) and the two new refusals; the
seam check now runs `p.scroll is dom.scroll` in a fresh process, so the export
cannot be lost without a failure.

Evidence: 121 hermetic checks green, pyright 0, `ruff check .` 0, live battery
**62 passed, 0 failed, 0 skipped**, and the two real X runs above (the
bitcoin-price search, google-chrome-stable on this host).

### 5.8 Headless search
`search QUERY [--engine duckduckgo|google|searxng]`: own profile and port,
per-profile lock and pacing, real UA override, explicit verdicts (empty vs
unrendered vs wall vs unreachable), SearXNG fallback, process-group kill and
profile cleanup on every exit path.

### 5.9 Plugin tier
Adapter API: verified primitives (`tab`, `js`, `wait`, `find`) plus a declared
verification predicate; the core runs the predicate and owns the verdict; a
plugin can fail but cannot claim success. *Done when* one out-of-core adapter
passes its own live check through the contract.

### 5.10 Hardening (parallel, any time)
DONE: the `/proc` ownership guard (§5.16), the capability surface (§5.17), the
launch/sync lock (§5.18), the instance selector (§5.20) and richer `stop`
identity (§5.19). Nothing is left in this section.

### 5.11 `open --windowless` — built, measured, REJECTED
A windowless start (`--no-startup-window`: CDP up, no window, no page) was built
and measured, then removed. It is a **warm endpoint, not a capability**:
headed Chromium gives a page a window, so the first tab after a windowless
start puts a window back on screen, and the option cost ~166 MB PSS (browser,
zygotes, GPU, utility, and three renderers already loaded for Chrome's own
component extensions) for nothing a caller could do with it.

The measurements are kept because the question will come back — *can a headed
browser hold a page with no window?* No. `--headless=new` is the only mode
that can — which is why it is now core `open --headless` (§5.30), not the
plugin tier's business.

## 6. Decisions needed

1. ~~**Seeding policy**~~ — deferred with the feature (5.3); the managed
   browser stays login-less until then.
2. ~~**`open` semantics**~~ — answered AND measured: a windowless start was
   built, found to buy nothing a caller can use, and removed (5.11). Every
   `open` makes a page, and in headed Chromium a page needs a window.
3. ~~**Install story**~~ — answered: `pyproject.toml` installs a console
   script, `browser-control-cli`. The checkout script stays for running
   without an install; note that the `~/.local/bin` symlink will shadow an
   installed wheel, so drop it if you pip-install.
4. ~~**Command name**~~ — answered: keep `browser-control-cli` as the only
   name (no `bctl` alias).
5. ~~**Output**~~ — answered: JSON only.
6. ~~**Plugin discovery**~~ — answered: path-based discovery
   (`BROWSER_CONTROL_PLUGIN_PATH`, or the default
   `~/.local/share/browser-control/plugins/`), and the tier itself landed
   (§5.27).
7. ~~**`js` reply cap**~~ — answered: REFUSE past the cap
   (`result-too-large`); do not truncate.
8. ~~**How to answer a JavaScript dialog**~~ — answered by measurement: with
   the Page domain enabled first, `accept`/`dismiss` answer a dialog the
   browser is still showing and verify it by the renderer returning; a dialog
   that was suppressed (nobody attached when it opened) has nothing left to
   answer, and the verb says exactly that (`no-dialog`) instead of pretending.
   `tab nav URL` is the recovery for the tab it parked (§4.9, §5.13).
9. ~~**What `--tab active` means**~~ — answered: the tab whose page reports
   itself visible **among the browsers this CLI drives** (managed or attached).
   The user's own browser has visible tabs too, and including them made the
   spec useless (§5.13).
10. ~~**`find` scope**~~ — answered: top document + open shadow roots
   (iframes out of scope, stated and tested).
11. ~~**Policy gate**~~ for code-executing verbs (`tab js`, `tab wait --for
    js`) — answered: the gate landed as `--allow`/`--deny` (§5.23); the agent
    frontend decides the policy, the CLI enforces it.
12. ~~**Native first**~~ — answered as a standing principle: **whatever CDP can
    do natively, do that** rather than reaching for page JavaScript. Hence
    `Input.dispatchMouseEvent` for clicks and wheels (trusted input, measured
    against `element.click()`'s `isTrusted: false`),
    `DOM.scrollIntoViewIfNeeded` for revealing an element, `DOM.focus`,
    `Input.insertText`/`dispatchKeyEvent` for text and keys, and
    `DOM.setFileInputFiles` for uploads. `js` stays the last resort, and where
    the protocol offers NOTHING (playback), the verb says so and verifies the
    effect instead.
13. ~~**The ad functions**~~ — deferred by decision: `tab ad-state` and `tab
    skip-ad` are not being built. The ad knowledge is site-specific, so it
    belongs to the plugin tier (5.9) rather than to the core.

## 7. How to run

```bash
python3 -m pip install .              # console script on PATH (pipx also works)
# or, from the checkout with no install:  ./browser-control-cli …
python3 tests/test_unit.py            # hermetic, no browser (121 checks)
python3 tests/live_test.py            # the battery, needs a browser (62 checks)
browser-control-cli selftest          # what is installed, what can be driven
browser-control-cli open https://example.com
browser-control-cli tab list
browser-control-cli tab https://example.net
browser-control-cli tab text --tab active
browser-control-cli tab screenshot /tmp/page.png
browser-control-cli close
```

Set `BROWSER_CONTROL_ROOT` to keep a session's profiles out of
`~/.local/share/browser-control/cdp-profiles` (the tests do), and
`BROWSER_CONTROL_CLI` to point the battery at an installed command.

**Where the logs go.** The action log is `BROWSER_CONTROL_LOG`, by default
`~/.local/state/browser-control/actions.jsonl`; its directory is created on the
first write, and when that cannot be written the line lands in a scratch
directory instead — `/tmp/browser-control-<stamp>-<rand>/actions.jsonl`
(`audit.scratch_dir()`). Both suites log into such a directory rather than
`off`, so the log is exercised on every run and the user's own log is never
touched: the hermetic suite at `<scratch>/hermetic-actions.jsonl`, the battery
at `<scratch>/live-actions.jsonl` (its path is printed at the top of the run,
as `log`). `selftest` reports the path the CLI itself would use; `off` (or
`0`/`none`) disables the log entirely.
