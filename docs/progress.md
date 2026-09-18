# browser-control — progress

Status: **slice 1 delivered, packaged, and covered by a committed battery.**
Repo `main`, worktree clean, 13 hermetic + 17 live checks passing.
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
| `browser_control/cli/main.py` | 218 | `HANDLERS` table, `--browser`, `selftest`, JSON out / `ERR[code]` exit 2 |
| `browser_control/lib/browser.py` | 652 | managed profile, resolve, launch, verified stop, tab ops, browser discovery |
| `browser_control/lib/cdp.py` | 214 | endpoint (`DevToolsActivePort`), capped JSON GET, explicit-port reads, one websocket call |
| `browser_control/lib/errors.py` | 21 | `ControlError(code, message)` + `fail()` |
| `tests/test_unit.py` | 340 | 13 hermetic checks, no browser needed |
| `tests/live_test.py` | 518 | 17 live checks on a throwaway root, skip ≠ pass |

Five verbs, browser-only:

| Verb | Does | Verified by (the read-back) |
| --- | --- | --- |
| `open [URL...]` | starts the managed browser (or adopts the running one) and opens every URL given — the first as the startup page when starting fresh, the rest as tabs | the endpoint must **answer**, then every opened tab must be in the tab list |
| `close` | stops the browser this CLI started | the pid dies **and** the endpoint stops answering; never SIGKILLs |
| `tabs` | the managed browser's page tabs, id-sorted | `/json` shape-checked |
| `list` | **every** Chromium-family browser running here — ours or the user's, drivable or not — with pid, exe, profile, whether the profile is ours, and whether CDP answers (+ its tab count) | one `/proc` pass, plus a CDP probe on the port each one names |
| `list-tabs` | the page tabs of every **drivable** browser, grouped by browser | the same tab list the verbs use, read per browser |
| `new-tab [URL...]` | one tab per URL (`about:blank` when none), every id named | each id from `Target.createTarget`, re-read from the tab list |
| `close-tab SPEC` | one tab: `id:<prefix>` or a title/url substring | the id must be **absent** afterwards, else `close-tab-not-verified` |
| `selftest` | proves the install without a browser | interpreter, `websockets`, verb table, browsers on PATH; **refuses** `no-websockets` when the dependency is missing |

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

**Live battery** — `python3 tests/live_test.py` → **17 passed, 0 failed, 0
skipped** (exit 0) on a throwaway root: it starts a real Chrome and reads
independent state back — a raw socket connect, a direct `/json` GET, `/proc`
for the pid — for open, `list`, the tab list, `list-tabs` grouping, new-tab
(single and several URLs), open with several URLs, both `close-tab` spec
forms, an ambiguous spec, five refusals, adoption of the running browser, the
verified close, the idempotent close, a browser with **no** debugging port
(listed, never driven) and "nothing left running". With the command missing it
reports 17 skips and exits 2.

**Not proven yet**: no concurrent-`open` test (there is no lock), no
multi-browser test (two live instances refuse), no CI.

## 3. Deviations from the plan

| Plan said | Slice 1 | Why / when to revisit |
| --- | --- | --- |
| `lib/cdp/` package | one `lib/cdp.py` | split when nav/dom land and the file grows |
| `lib/profile.py`, `lib/tabs.py` | folded into `lib/browser.py` | 5 verbs do not need three modules; split at ~`nav` |
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

Ordered by "unblocks the most with the least". **5.1 and 5.2 have landed**
(§1, §2) and **5.3 is deferred by decision** — the next actionable step is
5.4 (`nav`), and every later verb is expected to add its check to
`tests/live_test.py`.

### 5.1 `selftest` verb — done
Landed as `browser-control-cli selftest`: interpreter, `python_version`,
`websockets`, `profile_root`, the verb table and the browsers it can find on
PATH — and it refuses `ERR[no-websockets]` when the dependency is missing,
the one thing that makes an install unusable. Covered hermetically (that
refusal included) and by the battery's first check.

### 5.2 The live battery — done
`tests/live_test.py` (518 lines, 17 checks): a temp `BROWSER_CONTROL_ROOT`, a
local page server so tabs have distinct URLs without the network, a
skip-if-prereq that exits 2, and mandatory cleanup — close what you opened,
kill what you started (and wait for it), remove the temp root (the skip path
leaked one until it was fixed, and the removal has to outlive a dying
browser). Every check reads independent state (raw socket, direct `/json`,
`/proc`) instead of trusting the reply.

### 5.3 Profile seeding — deferred
Deliberately not being built yet, so the managed browser starts with none of
the user's logins: `open`, `tabs` and the verbs above them all work, but a
page that wants a session renders logged out. When it lands it is still the
plan's design — reflink-first copy of the user's own profile, atomic swap,
Chrome singleton files dropped, honest `copied: reflink|copy|empty`, and
`--status`/`--source DIR`/`--browser NAME`/`--force` (stopping only the
instance this CLI started). The policy question (auto-seed on first `open`
vs explicit only) is deferred with it.

### 5.4 `nav` + history
`nav URL [--tab]`, `back`, `forward`, `reload`. Assignment, then read back the
observed URL and `readyState`; `chrome-error://` → `nav-failed`; the
"net change" test, not string equality, so a normalization-only difference is
not a false `nav-not-verified`. One URL validator shared with `open`/`new-tab`.
*Done when* a dead domain refuses `nav-failed`, a redirect reports `url_read`,
and navigating to the page you are on is not an error.

### 5.5 DOM reads
`js EXPR` (declared unverified), `wait --for load|idle|element|js` with a
finite wall-clock deadline, `find TEXT|--selector CSS` returning **page**
coordinates (`vbox`, `vx`, `vy`) and shape-validated rows, plus `text`.
*Done when* `find` finds a labelled element on a real page and `wait` refuses
`wait-timeout` naming what it waited for.

### 5.6 Input and forms
`focus-el`, `press`, `insert-text`, `type-keystrokes`, `upload`. Focus proven
through `document.activeElement`; password detection (fail-closed) and the
audit log land here, because redaction has nowhere to write until then;
`upload` verified through `input.files`.
*Done when* a password typed through `insert-text` logs a length, not the
secret, and an upload read-back mismatches → `upload-not-verified`.

### 5.7 Media
`media-state|play|pause` (judged from the read-back), `ad-state`, `skip-ad`
(button matched by shape/class, not the English word; a page-coordinate click
with a hit-test). *Done when* a `play` that does not start refuses
`media-not-verified`.

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

## 6. Decisions needed

1. ~~**Seeding policy**~~ — deferred with the feature (5.3); the managed
   browser stays login-less until then.
2. **`open` semantics** — always make a page (current), or also support
   `ensure` (windowless, no page) for scripted use?
3. ~~**Install story**~~ — answered: `pyproject.toml` installs a console
   script, `browser-control-cli`. The checkout script stays for running
   without an install; note that the `~/.local/bin` symlink will shadow an
   installed wheel, so drop it if you pip-install.
4. **Command name** — the plan says `bctl`; the delivered command is
   `browser-control-cli`. Keep one, or ship both (long name for discovery,
   short alias for typing)?
5. **Output** — JSON-only (current) or JSON by default plus a human format?
6. **Plugin discovery** — entry points (packaging) or a scanned directory?

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
