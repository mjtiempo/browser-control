# browser-control — progress

Status: **slice 1 delivered, packaged, and live-verified.** Repo `main`,
worktree clean, 10/10 hermetic checks passing.
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
| `browser_control/cli/main.py` | 138 | `HANDLERS` table, `--browser`, JSON out / `ERR[code]` exit 2 |
| `browser_control/lib/browser.py` | 462 | managed profile, resolve, launch, verified stop, tab ops |
| `browser_control/lib/cdp.py` | 189 | endpoint (`DevToolsActivePort`), capped JSON GET, one websocket call |
| `browser_control/lib/errors.py` | 21 | `ControlError(code, message)` + `fail()` |
| `tests/test_unit.py` | 246 | 10 hermetic checks, no browser needed |

Five verbs, browser-only:

| Verb | Does | Verified by (the read-back) |
| --- | --- | --- |
| `open [URL]` | starts the managed browser; if one is already up, hands it the URL as a new tab | the endpoint must **answer**, then a page tab must exist |
| `close` | stops the browser this CLI started | the pid dies **and** the endpoint stops answering; never SIGKILLs |
| `tabs` | page tabs, id-sorted | `/json` shape-checked |
| `new-tab [URL]` | one tab, named `id:<target id>` | the id is re-read from the tab list |
| `close-tab SPEC` | one tab: `id:<prefix>` or a title/url substring | the id must be **absent** afterwards, else `close-tab-not-verified` |

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

**Not proven yet**: no committed live battery (the run above was by hand), no
concurrent-`open` test, no multi-browser test, no CI.

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
6. **Live checks are not committed** — the battery that produced §2 lives in
   shell history, not in `tests/`.
7. **No human output** — every verb prints JSON; there is no `--json` switch
   because there is no alternative format yet.
8. **`stop` refuses when the pid cannot be identified** — honest, but it means
   a browser adopted from another tool cannot be closed by this CLI.

## 5. What is next

Ordered by "unblocks the most with the least": 5.1–5.3 are infrastructure the
other steps then lean on. **Packaging itself has landed** (§1, §2); what is
left of that step is 5.1.

### 5.1 `selftest` verb (small)
A `selftest` verb — interpreter, `websockets` version, verb table — so an
installed command can prove itself without a browser; it is what the plan's
packaging step promised beside the install. *Done when* an installed command
answers `selftest`, and it fails loudly when `websockets` is missing.
Runtime here is Python 3.14.7; the floor stays 3.11 because nothing uses newer
syntax.

### 5.2 The live battery, committed
`tests/live_test.py`: the §2 sequence, each check with a skip-if-prereq
(skip ≠ pass), a temp `BROWSER_CONTROL_ROOT`, and mandatory cleanup (close
what you opened, kill what you started). *Done when* it exits 0 on a machine
with a browser and 2 on skips, and every later verb adds its check here.

### 5.3 Profile seeding — `profile-sync`
Reflink-first copy of the user's own profile, atomic swap, Chrome singleton
files dropped, `copied: reflink|copy|empty` named honestly, `--status`,
`--source DIR`, `--browser NAME`, `--force` (stops only the instance this CLI
started). *Done when* a login made in the user's browser is present in the
managed one after a sync, and `--status` reports path/source/files/bytes/age/
`in_use` truthfully.
*Decision needed*: seed automatically on first `open`, or explicit only.

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

1. **Seeding policy** — auto-seed on first `open`, or explicit `profile-sync`
   only (current)?
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
browser-control-cli open https://example.com
browser-control-cli tabs
browser-control-cli new-tab https://example.net
browser-control-cli close-tab example.net
browser-control-cli close
```

Set `BROWSER_CONTROL_ROOT` to keep a session's profiles out of
`~/.local/share/browser-control/cdp-profiles` (the tests do).
