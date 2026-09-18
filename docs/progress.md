# browser-control — progress

Status: **slice 1 delivered, packaged, and covered by a committed battery.**
Repo `main`, worktree clean, 26 hermetic + 39 live checks passing.
Plan: [`docs/plan.md`](plan.md). This file is the state of the work: what
exists, what it does *not* do yet, and what comes next.

## 1. Slice 1 — what exists

The distributable package is `browser_control` (`browser_control/lib` is the
logic, `browser_control/cli` is the argv adapter); `pyproject.toml` installs
it and puts one console script on PATH.

| File | Lines | Owns |
| --- | --- | --- |
| `pyproject.toml` | 28 | distribution `browser-control`, console script, `websockets` |
| `browser-control-cli` | 13 | the command as a checkout script (no install needed) |
| `browser_control/cli/main.py` | 677 | `HANDLERS` table, `tab` subcommands, `--browser`/`--tab`, attach argv, the action log |
| `browser_control/lib/dom.py` | 1336 | **the DOM tier**: one element prelude, `js`, `wait`, `find`, `text`, `click`, `scroll`, `focus`, `press`, `insert`, `type`, `upload`, `media` |
| `browser_control/lib/audit.py` | 108 | the JSONL action log: fail-open, and a proven secret written as a length |
| `browser_control/lib/browser.py` | 1281 | managed profile, launch, stop, discovery, attach records, tabs, page verbs |
| `browser_control/lib/cdp.py` | 513 | endpoint, capped JSON GET, `evaluate`/`evaluate_until`, `target_ws`, `Session` |
| `browser_control/lib/errors.py` | 21 | `ControlError(code, message)` + `fail()` |
| `tests/test_unit.py` | 1002 | 26 hermetic checks, no browser needed |
| `tests/live_test.py` | 1151 | 39 live checks on a throwaway root, skip ≠ pass |

Five verbs, browser-only:

The surface is a noun and its verb: `tab` owns everything about a page tab,
the other verbs own the browser.

| Verb | Does | Verified by (the read-back) |
| --- | --- | --- |
| `open [URL...]` | starts the managed browser (or adopts the running one) and opens every URL given — the first as the startup page when starting fresh, the rest as tabs | the endpoint must **answer**, then every opened tab must be in the tab list |
| `close` | stops the browser this CLI started | the pid dies **and** the endpoint stops answering; never SIGKILLs |
| `list` | **every** Chromium-family browser running here — ours or the user's, drivable or not — with pid, exe, profile, whether the profile is ours, and whether CDP answers (+ its tab count) | one `/proc` pass, plus a CDP probe on the port each one names |
| `info` | the browser this CLI would drive (or the one `--browser` names) and its endpoint — `cdp.version`, `protocol`, `user_agent`, tab count; `running: false` names the profile `open` would use | `/proc` + `/json/version` read from the browser itself |
| `attach --port N \| --pid N \| --profile DIR` | makes a running browser this CLI did not start writable (a `tab` write target) | the profile comes from a live Chromium-family process of this machine that ANSWERS on its endpoint, never from the caller |
| `attach --list` | what is attached, and whether it is still running | each record re-checked against `/proc` + a CDP probe |
| `detach --port N \| --pid N \| --profile DIR \| --all` | revokes that authorization | `not-attached` when there is nothing to revoke |
| `tab [URL...]` | one tab per URL (`about:blank` when none), every id named | each id from `Target.createTarget`, re-read from the tab list |
| `tab list [--browser NAME]` | the page tabs of every **drivable** browser, grouped and sorted by browser | the same tab list the verbs use, read per browser |
| `tab info SPEC` | one tab: which browser owns it, its `{id,title,url,index}` now | the spec resolves to exactly one tab or refuses |
| `tab close SPEC...` | closes every tab the specs name, **all specs resolved before anything is closed** | every requested id must be **absent** afterwards, else `close-tab-not-verified` names the survivors |
| `tab nav URL [--tab SPEC]` | navigates one tab | the address **as observed** (`url_read`) + `moved` + `loaded`; `chrome-error://` → `nav-failed`; a tab that never left a page it was not on → `nav-not-verified`; a redirect is a success |
| `tab back` / `tab forward` | moves the tab's history | the address actually changed (`nav-not-verified` when it did not) |
| `tab reload` | reloads one tab | `performance.timeOrigin` changed: a NEW document, not a guess |
| `tab js EXPR` | evaluates an expression — the escape hatch, and it can write | **unverified** (`verified: false`), the value capped at 64 k (`result-too-large`), a page exception is `js-error`, a page that stops answering `eval-timeout` |
| `tab wait --for load\|idle\|element\|js` | polls ONE predicate to a wall-clock deadline | `{ok, for, waited_s, samples}` or `wait-timeout` naming what and how long; one connection for the whole poll |
| `tab find TEXT \| --selector CSS` | a human target → visible elements, in **page** coordinates | `no-match` (naming the candidate count) · `no-viewport` when the tab has no viewport · each match carries `point` and `viewport` (viewport coordinates), `in_viewport`, `hit` (a real hit-test), `hit_element`, `clipped` |
| `tab text [--selector CSS]` | the rendered text | truncated **in the page**, so the reply is bounded and `length` still reports the full size |
| `tab click TEXT \| --selector CSS [--index N]` | **real input** (`Input.dispatchMouseEvent` move+press+release) at the element's viewport centre | `occluded` when the point reaches something else · `no-viewport-target` when it is off-screen (with the remedy) · `ambiguous-element` for several matches · the reply carries `changed` (url/title/focus/scroll before and after) |
| `tab scroll --by N \| --edge top\|bottom \| TEXT` | **real wheel input** (`mouseWheel`) or `DOM.scrollIntoViewIfNeeded` for one element | `scroll-not-verified` when a requested edge was not reached, or a wheel moved nothing and the document was not already at that end; the reply names which scroller moved (`document` / `nested`) |
| `tab focus TEXT \| --selector CSS [--index N]` | the DOM focus (the CARET, not the tab's frontmost position) — `DOM.focus` | `document.activeElement === el`; `focus-not-verified` names why (the protocol says "not focusable", a disabled control) |
| `tab press KEY` | one key event at the focus — `Input.dispatchKeyEvent` | the dispatch is verified and the reply says `verified: false`: the effect belongs to the page (`tab text`/`tab info`/`tab js` read it); an unknown key is `bad-args` naming the table |
| `tab insert TEXT` | `Input.insertText` — ONE atomic event | the focused field's **length grew** (`verified: true`), a readable field that did not change refuses `insert-not-verified`, an unreadable one (frame/canvas) reports `verified: false`; `no-focus` when nothing is focused |
| `tab type TEXT` | real per-character key events (`keyDown`, `char`, `keyUp`) on one connection | same oracle and codes as `insert` (`type-not-verified`) |
| `tab upload FILE [--selector CSS] [--index N]` | `DOM.setFileInputFiles` (an objectId, so shadow roots work) | `input.files` read back: one file, same name **and size**; `no-file`/`upload-not-verified` |
| `tab media state\|play\|pause [--index N]` | drives the `<video>`/`<audio>` element (no CDP playback method exists — see 5.7) | `play` needs the **clock to move** (a source-less element reports `paused: false` and never plays a frame), `pause` needs `paused: true`; `no-media` when there is none, `media-blocked` with the page's own reason when the promise rejects |
| `selftest` | proves the install without a browser | interpreter, `websockets`, verb table, browsers on PATH; **refuses** `no-websockets` when the dependency is missing |

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

**Hermetic** — `python3 tests/test_unit.py` → **10 passed, 0 failed**: URL
policy, tab-spec resolution (incl. `tab-ambiguous`), launch flags, profile
keyed by the resolved binary, port-file edge cases, `/json` reading against a
fake endpoint, CLI dispatch, CLI argv strictness, pid liveness.

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

**Static** — all five Python files clean under an active LSP probe (0
diagnostics).

**Live battery** — `python3 tests/live_test.py` → **39 passed, 0 failed, 0
skipped** (exit 0) on a throwaway root: it starts a real Chrome and reads
independent state back — a raw socket connect, a direct `/json` GET, `/proc`
for the pid — for open, `list`, `tab list`/`tab info`, `tab` (single and
several URLs), open with several URLs, `tab close` by id and by substring,
an ambiguous spec (refused with **nothing** closed), a two-spec close in one
call, `tab nav` (a redirect, a dead endpoint, `--tab` naming the one tab),
`tab back`/`tab forward`, `tab reload`, `tab text` (with a 20-char cap),
`tab find` (a hidden twin skipped, a shadow-root button found, the iframe's
button not), `tab js` (value, decoded JSON, `js-error`, `result-too-large`),
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
running". With the command missing it reports 39 skips and exits 2.

**Not proven yet**: no concurrent-`open` test (there is no lock), no
multi-browser test (two live instances refuse), no CI.

## 3. Deviations from the plan

| Plan said | Slice 1 | Why / when to revisit |
| --- | --- | --- |
| `lib/cdp/` package | one `lib/cdp.py` | split when nav/dom land and the file grows |
| `lib/profile.py`, `lib/tabs.py` | folded into `lib/browser.py` (1281 lines) for the tabs, and the DOM tier split out as `lib/dom.py` when it arrived | the boundary is now where it should be: transport in `cdp.py`, resolution/lifecycle/nav in `browser.py`, page READING in `dom.py` |
| profile **seeding** from the user's own profile | not implemented — the managed profile starts empty | deliberate: no copying a multi-GB profile until the slice needs logins (see next) |
| port → inode → pid ownership guard | only the websocket-host check; `close` identifies its pid by cmdline + exe | the endpoint is `--remote-debugging-port=0` on our own profile, so there is no fixed port to forward yet |
| `ensure` (windowless start) | `open` always makes a page | the 4-verb scope does not need a windowless state |
| plugin tier, nav, dom, input, forms, media, search | not started | next (see §5) |
| console script `bctl` | console script `browser-control-cli`; the short name is still an open decision (§6) | packaging landed; the plan's `bctl` alias was not added |

## 4. Known gaps and debt

1. **No seeding** — the managed browser has none of the user's logins.
2. **No lock/serialization** — two concurrent `open` calls race the profile
   (Chrome's singleton makes it mostly benign; the plan's lock is not there).
3. **No fixed-port ownership guard** — a forwarded CDP endpoint would not be
   refused with `cdp-not-local` (see §3).
4. **One browser at a time** — there is no `--profile`/instance selector; two
   live instances refuse instead of being addressable.
5. **Not published** — the wheel builds and installs locally, but there is no
   README, no declared license, and no index to publish to.
6. **No CI** — both suites run by hand; nothing runs them on a push.
7. **No human output** — every verb prints JSON; there is no `--json` switch
   because there is no alternative format yet.
8. **`stop` refuses when the pid cannot be identified** — honest, but it means
   a browser adopted from another tool cannot be closed by this CLI.

## 5. What is next

Ordered by "unblocks the most with the least". **5.1, 5.2, 5.4, 5.5, 5.6 and
5.7 have landed** (§1, §2), **5.3 and the ad functions are deferred by
decision** — the next actionable step is 5.8 (headless search: `search QUERY
[--engine …]`) — and every later verb is expected to add its check to
`tests/live_test.py`. The CLI grammar is settled (§1): page work lives under
`tab`, browser work stays top-level.

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

### 5.3 Profile seeding — deferred
Deliberately not being built yet, so the managed browser starts with none of
the user's logins: `open`, `tabs` and the verbs above them all work, but a
page that wants a session renders logged out. When it lands it is still the
plan's design — reflink-first copy of the user's own profile, atomic swap,
Chrome singleton files dropped, honest `copied: reflink|copy|empty`, and
`--status`/`--source DIR`/`--browser NAME`/`--force` (stopping only the
instance this CLI started). The policy question (auto-seed on first `open`
vs explicit only) is deferred with it.

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
Launch/sync lock; the `/proc` ownership guard so a forwarded endpoint refuses
`cdp-not-local`; a `--profile`/instance selector; richer `stop` identity.

### 5.11 `open --windowless` (decided, not built)
A windowless start (no page tab) as an OPTION, never the default: a scripted
caller that wants CDP up and no window on screen asks for it. It is
`launch(..., windowless=True)` — the flags already carry `--no-startup-window`
the last time it was needed — plus the CLI flag and a battery check that no
page tab exists afterwards.

## 6. Decisions needed

1. ~~**Seeding policy**~~ — deferred with the feature (5.3); the managed
   browser stays login-less until then.
2. ~~**`open` semantics**~~ — answered: a **windowless** start is available
   but NOT the default (`open` keeps making a page). Not built yet — see 5.11.
3. ~~**Install story**~~ — answered: `pyproject.toml` installs a console
   script, `browser-control-cli`. The checkout script stays for running
   without an install; note that the `~/.local/bin` symlink will shadow an
   installed wheel, so drop it if you pip-install.
4. ~~**Command name**~~ — answered: keep `browser-control-cli` as the only
   name (no `bctl` alias).
5. ~~**Output**~~ — answered: JSON only.
6. ~~**Plugin discovery**~~ — answered: entry points. The plugin tier itself
   (5.9) is deferred.
7. ~~**`js` reply cap**~~ — answered: REFUSE past the cap
   (`result-too-large`); do not truncate.
8. ~~**`find` scope**~~ — answered: top document + open shadow roots
   (iframes out of scope, stated and tested).
9. **Policy gate** for code-executing verbs (`tab js`, `tab wait --for js`,
   `tab text` on a logged-in page) — still open, and it belongs to the agent
   frontend rather than to this CLI.
10. ~~**Native first**~~ — answered as a standing principle: **whatever CDP can
    do natively, do that** rather than reaching for page JavaScript. Hence
    `Input.dispatchMouseEvent` for clicks and wheels (trusted input, measured
    against `element.click()`'s `isTrusted: false`),
    `DOM.scrollIntoViewIfNeeded` for revealing an element, `DOM.focus`,
    `Input.insertText`/`dispatchKeyEvent` for text and keys, and
    `DOM.setFileInputFiles` for uploads. `js` stays the last resort, and where
    the protocol offers NOTHING (playback), the verb says so and verifies the
    effect instead.
11. ~~**The ad functions**~~ — deferred by decision: `tab ad-state` and `tab
    skip-ad` are not being built. The ad knowledge is site-specific, so it
    belongs to the plugin tier (5.9) rather than to the core.

Nothing here blocks 5.6.

## 7. How to run

```bash
python3 -m pip install .              # console script on PATH (pipx also works)
# or, from the checkout with no install:  ./browser-control-cli …
python3 tests/test_unit.py            # hermetic, no browser
python3 tests/live_test.py            # 12 live checks, needs a browser
browser-control-cli selftest          # what is installed, what can be driven
browser-control-cli open https://example.com
browser-control-cli tabs
browser-control-cli new-tab https://example.net
browser-control-cli close-tab example.net
browser-control-cli close
```

Set `BROWSER_CONTROL_ROOT` to keep a session's profiles out of
`~/.local/share/browser-control/cdp-profiles` (the tests do), and
`BROWSER_CONTROL_CLI` to point the battery at an installed command.
