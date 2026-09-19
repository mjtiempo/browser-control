# browser-control — progress

Status: **slice 1 delivered, packaged, and covered by a committed battery.**
Repo `main`, worktree clean, 36 hermetic + 57 live checks passing.
Plan: [`docs/plan.md`](plan.md). This file is the state of the work: what
exists, what it does *not* do yet, and what comes next.

## 1. Slice 1 — what exists

The distributable package is `browser_control` (`browser_control/lib` is the
logic, `browser_control/cli` is the argv adapter); `pyproject.toml` installs
it and puts one console script on PATH.

| File | Lines | Owns |
| --- | --- | --- |
| `pyproject.toml` | 31 | distribution `browser-control`, console script, `websockets`, MIT (SPDX) + `license-files` |
| `README.md` | 190 | the stranger's greeting: the stance, quickstart, the safety contract, capabilities |
| `LICENSE` | 21 | MIT, `Copyright (c) 2026 Mark Tiempo` |
| `browser-control-cli` | 13 | the command as a checkout script (no install needed) |
| `browser_control/cli/main.py` | 1060 | `HANDLERS` table, `tab` subcommands, `--browser`/`--tab`, attach argv, the action log |
| `browser_control/lib/dom.py` | 2300 | **the DOM tier**: one element prelude, `js`, `wait`, `find`, `text`, `click`, `hover`, `scroll`, `focus`, `press`, `insert`, `type`, `upload`, `check`, `select`, `dialog`, `screenshot`, `media` |
| `browser_control/lib/audit.py` | 158 | the JSONL action log: fail-open, directory created on the first write, scratch fallback in `/tmp/browser-control-<timestamp>`, and a proven secret written as a length |
| `browser_control/lib/browser.py` | 2165 | managed profile, launch, stop, discovery, attach records, tabs, `nav`/`activate`, the `/proc` endpoint guard and the lifecycle locks |
| `browser_control/lib/cdp.py` | 740 | endpoint, capped JSON GET, `evaluate`/`evaluate_until`, `target_ws`, `Session` (Page domain, events, parked tabs) |
| `browser_control/lib/errors.py` | 21 | `ControlError(code, message)` + `fail()` |
| `browser_control/lib/capabilities.py` | 217 | **the declared surface**: what each verb can do (`read`/`write`/`code`/`file`/`egress`), reported by `selftest` and checked against the handler tables |
| `tests/test_unit.py` | 1616 | 36 hermetic checks, no browser needed |
| `tests/live_test.py` | 1900 | 57 live checks on a throwaway root, skip ≠ pass |
| `browser_control/lib/profile.py` | 360 | **the `profile` noun**: `info` (weight, age, liveness), `seed` (logins copied in, caches skipped, read back), `reset` (wipe, on purpose) |

Five verbs, browser-only:

The surface is a noun and its verb: `tab` owns everything about a page tab,
the other verbs own the browser.

| Verb | Does | Verified by (the read-back) |
| --- | --- | --- |
| `open [URL...] [--profile DIR]` | starts the managed browser (or adopts the running one) and opens every URL given — the first as the startup page when starting fresh, the rest as tabs; `--profile DIR` names the INSTANCE, so one root can hold several (two Chrome profiles, two sessions) | the endpoint must **answer**, then every opened tab must be in the tab list |
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
6. **No CI** — both suites run by hand; nothing runs them on a push.
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

Ordered by "unblocks the most with the least". **5.1, 5.2, 5.4, 5.5, 5.6, 5.7,
5.12 through 5.24 have landed** (§1, §2); **5.3 (seeding), the ad functions, 5.8
(search) and 5.9 (plugins) are deferred by decision**, and **5.11 was built,
measured and rejected**. What is left in the CORE is **5.10: the launch/sync
lock, the `/proc` ownership guard, and a capability surface in `selftest`** —
all three deferred at the operator's request, not forgotten. Every later verb
is expected to add its check to `tests/live_test.py`; the CLI grammar is
settled (§1).

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
screenshots. Now:

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
| `read` | reads state; /proc and loopback CDP only | 11 |
| `write` | changes the page, the browser, or this CLI's authorization | 26 |
| `code` | runs caller-supplied code: `tab js`, `tab wait --for js` | 2 |
| `file` | touches a path the CALLER named: `tab screenshot`, `tab upload` | 2 |
| `egress` | would reach the network — nothing yet; the plugin tier will | 0 |

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
| `open` | `<profile>/.browser-control.lock` | read the port → check the endpoint → spawn → wait for it → record the pid |
| `close` | same | the pid decision, `SIGTERM`, and the death + endpoint waits |
| `attach` / `detach` | `<root>/.browser-control.lock` | the read-modify-write of `attached.json` |

A caller that cannot take it waits (up to 20 s, a cold launch's budget), then
refuses `profile-busy` naming the pid, verb and start time the holder wrote
into the file. A filesystem that cannot lock at all is a `warning` in the reply,
not a silent nothing — and not a failure either.

The check found a real bug, which is the point of adding one: the losing call
read the port BEFORE taking the lock, so inside it `cdp.reachable(profile)` was
true while its own `port` was still `0` — and the guard faithfully reported "no
process holds port 0", turning a race into `cdp-not-local`. The port is now read
inside the lock, and a zero port is not treated as an endpoint to judge.

Evidence: hermetic (a held lock makes the second caller wait 0.3 s and refuse
`profile-busy` naming `pid … (open)`; it is free again afterwards with nothing
to clean up; an unopenable path is a warning, never a failure); battery (two
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
* **`selftest` is never gated.** It is the verb that reports the policy, and a
  gate that blocks its own explanation is a trap.
* **The MODE decides.** `tab wait --for js` is code while `tab wait` reads,
  `tab dialog accept` writes while `state` reads — so the gate resolves the
  action from argv, and `tab dialog` with no mode maps to `state` (otherwise a
  READ would be refused as unclassified). No policy set means no gate, which is
  what every call did before one existed.

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
| the frame census in reads | `tab text` and `tab find` carry `frames: {total, separate, same_process, visible}`, so a read that omits frame content SAYS so |
| `tab click|hover --at X,Y` | real input at a POINT, for what no selector can reach (a canvas): `verified: false`, with `under` reporting what the point actually reaches |
| `frame-ambiguous` / `no-frame` / `frame-not-separate` | several frames match → the indices are named; none → what exists is named; a frame sharing the page's PROCESS has no target to drive, and the refusal says what to do instead (`tab js` reads it; `--at` hits it) |

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

Hermetic 36 (the scope is set AND cleared per invocation like `--profile`, the
point syntax with a refusal naming the verb that asked, and the verb list: only
tab subcommands may claim a frame, and `nav`/`list`/`activate` may not), live 57
(three frames with two separate: driven by index and by URL, a click inside one
fires its own handler, the census in `text`, the ambiguity named with indices,
and the two refusals).

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
that can, and that is the plugin tier's business (5.9).

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
6. ~~**Plugin discovery**~~ — answered: entry points. The plugin tier itself
   (5.9) is deferred.
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
python3 tests/test_unit.py            # hermetic, no browser (29 checks)
python3 tests/live_test.py            # the battery, needs a browser (45 checks)
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
directory instead — `/tmp/browser-control-<timestamp>/actions.jsonl`
(`audit.scratch_dir()`). Both suites log into such a directory rather than
`off`, so the log is exercised on every run and the user's own log is never
touched: the hermetic suite at `<scratch>/hermetic-actions.jsonl`, the battery
at `<scratch>/live-actions.jsonl` (its path is printed at the top of the run,
as `log`). `selftest` reports the path the CLI itself would use; `off` (or
`0`/`none`) disables the log entirely.
