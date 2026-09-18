# browser-control — plan

Status: agreed scope, not yet implemented.
Derived from the `omctrl` browser-control review (see "Defects designed out").

A Python library that drives this machine's Chromium over CDP on a **managed
profile of its own**, plus a thin CLI. A verb that cannot confirm its effect
refuses; the user's own profile is never touched; one browser identity, one
profile, one endpoint, resolved the same way everywhere.

## 1. Decisions

| Item | Decision |
| --- | --- |
| Name / layout | `browser-control` repo, Python package `browser_control/` with `lib/` (logic) + `cli/` (adapters) inside it; `pyproject.toml` installs it |
| Browsers | Chromium family only (chrome, chromium, brave, edge, vivaldi + headless shell) |
| Scope | Browser only — no desktop/compositor integration |
| Window-scoped tabs | **Not in v1** (no `--window`, no `--workspace`, no screen coordinates) |
| Site adapters | Not in core — plugin system on top |
| Interface | `lib` = importable services, `cli` = one adapter per verb, JSON in/out, `ERR[code]` |
| CLI command | `bctl` (+ `python -m browser_control`) — only name still changeable |

## 2. `lib/` modules

### `cdp/` — transport

- Endpoint: explicit profile → `DevToolsActivePort` → named fallback; env
  override still ownership-checked. `get_json` (capped), `evaluate`
  (returnByValue), `call`, `raw`, `batch(one connection)`.
- Payload bounds, host-checked websockets (loopback only), deadlines checked
  *inside* the receive loop, result caps.
- Retry split: reads retry with backoff; **mutations never replay**; one
  wall-clock deadline per call.
- Ownership guard: port → inode → pid → exe; Chromium family only →
  `cdp-not-local`.
- Target resolution: `/json` rows, `id:<prefix>`, title/URL substring,
  `active` = the tab whose document reports `visible` (0 or >1 refuses).
- Typed codes: `cdp-unreachable`, `cdp-error`, `cdp-not-local`,
  `result-too-large`, `no-target`, `target-ambiguous`, `no-page-tab`,
  `tab-ambiguous`.

### `profile.py` — managed profile

- Per-binary root (default `~/.local/share/browser-control/cdp-profiles/<binary>`),
  env override.
- Seed from the user's own profile: reflink-first, atomic swap, honest
  `copied: reflink|copy|empty`, `--no-seed` refuses.
- Sanitize copies (Chrome singleton files, our port/pid/lock), one-way
  divergence (never written back).
- Launch flags always include `--remote-debugging-port=0`,
  `--user-data-dir=…`, **`--no-first-run`**, `--no-default-browser-check` (so a
  never-run/empty profile becomes drivable in ~2 s, not never).
- Port/pid/lock files with stale-vs-live semantics; lock is fail-closed; stop
  only a verified browser exe; `status` reports path/source/size/age/in_use.
- **One resolver**: the browser is resolved once (launch plan `argv[0]`
  basename) and every path keys the profile off that same name — no
  `google-chrome` vs `google-chrome-stable` split.

### `browser.py` — lifecycle

- `ensure` (start if needed; windowless only when truly no URL),
  `open [URL]` (window/tab), `stop`, `restart`, `status`.
- `ensure URL` works whether or not an instance is already up — if a
  windowless instance is running it opens a tab for the URL instead of
  refusing.
- Launch is direct `subprocess` (no desktop launcher); delegation to a running
  instance is understood and handled.
- Every reply names `{browser, profile, cdp, port, pid, launched, seeded}`.

### `tabs.py`

- `list`, `new_tab [URL]`, `close_tab` (one/many ids), `activate`, `info`.
- Handles are re-read after the mutation; a close reports survivors; a failed
  placement keeps the tab and names it.

### `nav.py`

- `nav URL [--tab]`, `back`, `forward`, `reload`.
- Read-back: observed URL + `readyState` + not a `chrome-error://` page;
  refuse `nav-failed` / `nav-not-verified` (normalization-aware, so a
  trailing-slash difference is not a false failure).
- One URL validator for every verb: http(s) + `about:blank`; no `file://`,
  `data:`, `chrome://`.

### `dom.py`

- `js EXPR` (escape hatch, L0), `wait --for load|idle|element|js` (bounded),
  `text`/`html`/`attr`.
- `find TEXT | --selector CSS [--cap N]` →
  `{tag, role, name, text, href, vbox, vx, vy}` in **page coordinates only**
  (no screen math, no monitor checks, no click).
- All page-supplied rows shape-validated; malformed rows dropped, never a
  traceback.

### `input.py`

- `focus_el`, `press KEY`, `insert_text`, `type_keystrokes` (one connection
  per string).
- Password detection (fail-closed, iframe-aware) → audit-log redaction.

### `forms.py`

- `upload FILE [--selector CSS]` — local path checked first, one-connection
  DOM sequence, `input.files` read back.
- `fill` (focus + insert + optional submit) built from primitives;
  `click_element` by hit-test on a page point.

### `media.py`

- `media_state|play|pause` (read-back judged), `ad_state`, `skip_ad`
  (structural detection; skip gate matched on button shape/class, not the
  English word; page-coordinate click only).

### `search.py` — headless search

- DDG / Google / SearXNG; throwaway vs persistent cookie profile, own port,
  per-profile lock + pacing, real UA override.
- Explicit verdicts: empty vs unrendered vs wall/consent vs unreachable.
- SearXNG fallback when the chosen engine refuses; process-group kill +
  profile cleanup on every exit path.

### `plugins.py` — adapter API

- A plugin gets verified primitives (`tab`, `js`, `wait`, `find`) and returns
  **candidate data + a declared verification predicate**; the core runs the
  predicate and owns the verdict.
- A plugin can fail but cannot claim success. Plugin replies are
  shape-validated like page data.

### Cross-cutting

`errors.py` (`ControlError`), `util.py`, `audit.py` (JSONL, per-action
redaction, fail-open), `config.py` (env + config file), `__init__.py` exposing
`BrowserService`.

## 3. `cli/` verbs (v1)

`status` · `ensure [URL] [--relaunch] [--no-seed]` · `open [URL]` · `stop` ·
`restart` · `profile-status` · `profile-sync [--source DIR] [--browser NAME]
[--force]` · `tabs` · `new-tab [URL]` · `close-tab [--tab S]…` ·
`activate-tab [--tab S]` · `nav URL [--tab S]` · `back` · `forward` ·
`reload` · `js EXPR [--tab S]` · `wait --for …` · `find TEXT|--selector CSS` ·
`text` · `focus-el` · `press KEY` · `insert-text TEXT` ·
`type-keystrokes TEXT` · `upload FILE` · `media-state|play|pause` ·
`ad-state` · `skip-ad` · `search QUERY [--engine E] [--limit N]` ·
`cdp METHOD [PARAMS_JSON] [--target ID|--browser]` · `selftest` · `help`.

Shape: one `cmd_*` per verb in a `HANDLERS` table; `main` parses `--tab`,
dispatches, logs, prints `ERR[code]: message` on stderr, exit 2. Unknown flag
or extra positional → `bad-args` (never dropped).

## 4. Verification appetite

"Verification appetite" = how much evidence a verb must gather before it is
allowed to say the operation succeeded. It trades two failure modes:

- **overclaim** — `ok: true` for an effect that never happened;
- **false refusal** — reporting failure when the effect happened but the
  *oracle* could not confirm it.

Appetite: **bias hard against overclaim on mutations, and never convert an
unclear oracle into a claim of absence.**

### Levels

| Level | Evidence | Cost | Catches |
| --- | --- | --- | --- |
| L0 — none | return the protocol reply | 0 | nothing; caller owns the meaning |
| L1 — transport ack | the call returned without an error envelope | ~0 | refused calls, dead sockets |
| L2 — read-back | re-read the observable state once and judge it against the request | 1 extra round-trip (~1–5 ms local) | "ack ≠ effect" |
| L3 — read-back + settle | L2 inside a bounded poll until the state settles or the deadline passes | up to 0.4–5 s | eventual consistency |
| L4 — independent proof | a second, differently-derived oracle agrees | more code + a second failure path | self-consistent lies, wrong-target actions |

### Rules

1. **Reads: L2.** Shape-validate what came back; refuse on malformed, never
   traceback.
2. **Mutations: L2 mandatory.** No mutation returns `ok: true` on ack alone.
3. **Asynchronous effects: L3, bounded.** One wall-clock deadline for the
   whole wait, `retries=1` per sample (a poll loop is its own retry).
4. **Synchronous DOM effects: L2 immediate.** No polling where the state is
   already available.
5. **L4 only where wrong-target is plausible.** Close-by-match, upload target,
   click hit-test.
6. **`ok` ≠ `verified`.** Mutations never return `{ok: true, verified: false}`;
   the honest alternative is `unverifiable` + a note, never a claim of
   absence.
7. **Unclear oracle ≠ absent effect.** Refuse only when the missing effect can
   be named; otherwise report `unverifiable` with the reason.
8. **Every refusal carries the rung.** `effect`/`rung`/`code` in the reply and
   the audit log.
9. **Escape hatches are declared unverified.** `js` and `cdp` return the
   protocol's own answer; the caller owns judgment.

### Per-verb appetite

| Verb | Kind | Oracle | Level | On failure |
| --- | --- | --- | --- | --- |
| `tabs` | read | `/json` shape + endpoint ownership | L2 | `cdp-unreachable`, `no-page-tab` |
| `find` | read | rect + visibility | L2 | `no-match` |
| `js` | read/write | none (returns the value) | L0 | `cdp-error` only |
| `wait --for …` | read | the predicate itself, polled | L3 | `wait-timeout` |
| `nav` | mutation | `readyState` + observed URL + not an error page | L3 | `nav-failed`, `nav-not-verified` |
| `new-tab` | mutation | the tab row exists, id re-read from the list | L3 | `no-page-tab` (never orphan the tab) |
| `close-tab` | mutation | requested ids absent from the re-read list | L3 | `close-tab-not-verified` (report survivors) |
| `activate-tab` | mutation | target row re-read + visibility | L2 | `verified:false` + note (a fact, not a refusal) |
| `focus-el` | mutation | `document.activeElement === el` | L2 | `focus-not-verified` |
| `press` | mutation | strict envelope only | L1 | `cdp-error` (caller re-reads the effect) |
| `insert-text`, `type-keystrokes` | mutation | strict envelope + password detection | L1 | `cdp-error` |
| `upload` | mutation | `input.files` read back (name + size) | L2 | `upload-not-verified` |
| `media-play/pause` | mutation | `video.paused` read back | L2 | `media-not-verified` |
| `open` | mutation | window + tab identity re-read | L4 | `launch-failed`, `tab_refused` (window kept, reason named) |
| `ensure` | mutation | port answers + pid recorded | L3 | `cdp-unreachable` with the real reason |
| `profile-sync` | mutation | destination exists + file/byte count + copy kind | L2 | `profile-sync-failed` |
| `search` | read | rendered vs empty vs wall vs unreachable | L3 | `search-denied`, `search-cdp` |
| `cdp` | escape | none | L0 | `cdp-error`, `result-too-large` |

## 5. Testing tiers

1. **Pure/unit** — URL policy, tab spec resolution, verdict functions, reply
   shapes, argv strictness (no browser). Verdict functions are pure
   (`verdict(readback, request) -> ok | refused | unverifiable`) so they are
   testable without a browser.
2. **Hermetic CDP** — fake `/json`, fake websocket peer (deadlines, duplicate
   ids, error envelopes, oversized results, host-check bypass).
3. **Live** — ensure/open/new-tab/nav/find/upload/media/search/cookies/
   dead-domain/PDF on a real desktop, each with a skip-if-prereq (skip ≠ pass)
   and mandatory cleanup (close what you opened, kill what you started).

## 6. Defects designed out (from the omctrl review)

| omctrl defect | Rule here |
| --- | --- |
| One browser resolved under two names → two managed profiles; `sync-profile` refreshed a profile nothing ran | One resolver; profile keyed off the resolved launch binary everywhere |
| Endpoint picked by directory sort order when several instances were live | Explicit endpoint per browser identity; multiple live instances never silently collapse |
| Windowless launch without `--no-first-run` never published CDP on a never-run profile | `--no-first-run --no-default-browser-check` always in launch flags |
| `ensure URL` refused when a windowless instance was already up | `ensure URL` opens a tab for the URL instead of refusing |
| `browser-open` accepted `file://` while `nav`/`new-tab` refused it | One URL validator for every URL-taking verb |
| `products` silently dropped a second positional | Strict argv: extra argument is `bad-args` |
| `skip-ad` matched the English word "skip" | Match the button's shape/class, not a locale string |
| `yt-first-video` used a narrower extractor than `first-video` | One extractor per site (moved to the plugin tier) |
| `nav` false-refused a normalization-only change | Compare net change (normalized URL), not string equality |
| Batch CDP errors read as `{}` success | Per-call error envelope checked; mutations verified by read-back |

## 7. Explicitly out of v1

Desktop/compositor integration · window-scoped tabs · `--workspace`
placement · screen coordinates/monitors/clicks · site adapters in core ·
remote/forwarded CDP · writes to the user's profile · `file://`/`data:`/
`chrome://` navigation · shelling out to a launcher.

## 8. Build order

1. `lib/errors.py`, `util.py`, `config.py`, `audit.py` — contracts first.
2. `lib/cdp/` — endpoint, ownership guard, JSON reads, websocket I/O with the
   retry split and deadlines. Hermetic tests.
3. `lib/profile.py` — resolution, flags, port/pid/lock, seed/sync. Hermetic
   tests + live launch test.
4. `lib/browser.py` — ensure/stop/status/open. Live test.
5. `lib/tabs.py`, `lib/nav.py` — addressing contract + read-back verdicts.
6. `cli/` — `HANDLERS` table for what exists; `selftest`, `help`;
   `test_imports`/verb-table contract tests.
7. `lib/dom.py`, `input.py`, `forms.py`, `media.py`.
8. `lib/search.py`.
9. `lib/plugins.py` — adapter API + one reference plugin outside the core.
10. Live battery on a real desktop; docs (`README`, `docs/`), packaging
    (`pyproject.toml`, console script `bctl`).
