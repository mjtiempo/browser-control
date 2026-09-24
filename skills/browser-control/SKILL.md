---
name: browser-control
description: Drive Chromium on this machine through browser-control-cli — open pages headless or windowed, read rendered text, click, type, fill forms, wait for state, and take verified screenshots. Use for browser automation or page inspection in an isolated managed profile — checking what a site actually shows, testing a web UI, or running a multi-step page flow. For the user's own already-open browser with their live logins, prefer the pi-chrome tools unless the user asks for this CLI.
---

# Browser control (browser-control-cli)

`browser-control-cli` drives a Chromium-family browser over CDP on a **managed
profile of its own** (default root `~/.local/share/browser-control/cdp-profiles`).
Your everyday browser is listed by `list` and readable, but is never written to
unless the user explicitly attaches it (see the `browser-control-sessions` skill).

One verb per process. **stdout**: exactly one JSON object. **stderr**:
`ERR[code]: message`, exit status 2 on any refusal. Success exits 0.

## Stance: verify, or refuse

- A mutation is never `ok: true` on a bare acknowledgement. The read-back is in
  the reply: `changed` after a click, `checked` after check, `selected` after
  select, `length_after` after insert/type, `moved` after nav, `visibility`
  after activate, the PNG's own dimensions after screenshot.
- `verified: false` is honest, not a failure: `tab press` and `tab js` cannot
  know the page's reaction — read the effect with `tab text`/`tab info`.
- A refusal names the cause and, when known, the fix. **Branch on the code**,
  not on the message text.
- For DOM verbs the oracle is the page's own JavaScript. The page-independent
  oracles are the screenshot's PNG bytes, `/proc` checks, and CDP protocol
  errors. `verified: true` means "the page says so".

## When to use this

- Read/check what a page shows, its structure, its rendered state.
- Automate a flow: search, fill a form, click through, upload, screenshot.
- End-to-end test a local or remote web UI.
- Extract repeated items into JSON records → `browser-control-extract` skill.
- Use seeded logins / multiple accounts / attach the user's browser →
  `browser-control-sessions` skill.
- Site adapters (Google-by-typing, X search) → `browser-control-plugins` skill.

Prefer the pi-chrome tools when the task needs the user's live session, a window
they can watch, or an extension. Use this CLI for isolated, headless, verified
automation.

## First call: prove the install

```bash
browser-control-cli selftest | jq '{ok, python_version, browsers, plugins, plugin_errors, policy}'
```

`selftest` reports browsers found, loaded plugins, capability classes, and the
policy in force. It is the one verb that is never gated. If `browsers` is empty,
`open` will refuse `no-browser` until one is installed.

## Canonical session

```bash
# 1. start (headless is right for automation; drop it if a site fights headless)
browser-control-cli open --headless https://example.com

# 2. read / act — no --tab needed while exactly one page tab is open
browser-control-cli tab text --chars 2000 | jq -r .text
browser-control-cli tab find --selector 'button.primary' | jq '.matches[0]'
browser-control-cli tab click --selector 'button.primary' | jq '{clicked, changed}'
browser-control-cli tab wait --for load --timeout 15 | jq '{for, waited_s}'

# 3. leave the machine as you found it
browser-control-cli close --force
```

`open` returns `started`, `port`, `pid`, `profile`, `headless`, `tabs`,
`opened`, and `tab: "id:…"` when it opened exactly one tab. Keep that id or
use `active`. `open` **adopts** an already-running managed browser instead of
starting a second one; `started` says which happened.

`--headless` belongs to the running PROCESS: asking for headless while a
windowed managed browser is up refuses. `close --force` then `open --headless`.

## Addressing things

| flag | meaning |
| --- | --- |
| `--tab SPEC` | `id:<prefix>` (from `open` / `tab list`), the word `active` (the visible tab), or a title/url substring |
| `--browser NAME` | executable name or managed profile name; default = the live managed browser, else the first Chromium on PATH |
| `--profile DIR` | the **instance** (a profile dir under the root); how two instances of one browser — two logins — are told apart |
| `--frame VALUE` | act inside one iframe: an index from `tab frames`, or a URL substring |

Rules that save a round trip:

- With no `--tab`, a page verb acts on the **only** page tab in a browser this
  CLI drives. Several tabs → `tab-ambiguous`; name one.
- A spec matching nothing (`no-page-tab`) or several tabs (`tab-ambiguous`)
  refuses and names the candidates. It never picks for you.
- Writes need a browser the CLI **manages or has attached**; a write elsewhere
  refuses `not-managed`. Reads work on any drivable browser.
- Tab-level verbs (`nav`, `back`, `forward`, `reload`, `activate`, `list`,
  `frames`, `info`, `close`) act on the tab; every content verb acts inside
  `--frame` when given.

## Command map

**Browser:** `open`, `close`, `list`, `info`, `attach`, `detach`, `profile …`,
`selftest`.

**Reads (safe):** `tab list`, `tab info SPEC`, `tab text`, `tab find`,
`tab extract`, `tab frames`, `tab dialog state`, `tab media state`, `tab wait`,
`tab screenshot` (a read that also writes a PNG file the caller names).

**Writes:** `tab nav|back|forward|reload|activate`, `tab click|hover|check|select|scroll|focus|press|insert|type|upload`, `tab dialog accept|dismiss`, `tab media play|pause`, `tab close`, `tab URL` (new tab), `tab js` (code — runs caller-supplied JavaScript).

`browser-control-cli --help` lists every flag; the per-verb details and reply
fields are in `references/verbs.md`; the refusal codes are in
`references/errors.md`.

## Recipes

### Read what a page shows

```bash
browser-control-cli open --headless https://example.com >/dev/null
browser-control-cli tab text | jq -r '.text'            # body text, 40k cap
browser-control-cli tab text --selector 'main h1' --chars 200
browser-control-cli tab find --selector 'table' | jq '.matches'
```

`tab text` returns `text`, `length` (full length, before truncation) and
`truncated`. `tab find` returns visible interactive/labelled elements with
`box`, `center`, `in_viewport`, `hit` and `hit_element` — use it to confirm a
target exists and a click would land before clicking.

### Find and act

```bash
browser-control-cli tab click "Sign in"                 # by visible text
browser-control-cli tab click --selector '#submit'      # by CSS
browser-control-cli tab click --selector '.row' --index 2
browser-control-cli tab click --at 420,310              # a canvas/point target
browser-control-cli tab hover --selector '.menu' | jq '{verified, hovered}'
```

`click` by text/selector returns `clicked`, `changed`, `before`/`after`
(url/title/scroll) and the `element` it hit; a covered element refuses
`occluded`. `click --at X,Y` is for what a selector cannot name; it reports
`verified: false` plus what the point actually reaches (`under`), so read
`changed` yourself.

### Fill and submit a form

```bash
browser-control-cli tab focus --selector '#name' | jq .focused
browser-control-cli tab insert "Ada Lovelace" | jq '{verb, verified, length_after}'
browser-control-cli tab select --selector '#color' --value Blue | jq '{selected, value, by}'
browser-control-cli tab check --selector '#agree' | jq '{checked, changed}'
browser-control-cli tab press enter      # or submit: click the button below
browser-control-cli tab click --selector 'button[type=submit]'
browser-control-cli tab wait --for load --timeout 15
browser-control-cli tab text --selector '#result'
```

- `tab focus` first: `insert`/`type` refuse `no-focus` with nothing editable
  focused.
- `tab insert` is one atomic `Input.insertText` — the durable way to fill text.
- `tab type` sends real per-character key events — use it when the page's own
  key handlers must run (autocomplete, masks). Slower, more faithful.
- `tab select --value V` matches an option's **value first, then its exact
  label**; the reply's `by` says which.
- `tab press KEY` dispatches one key at the focus (`enter`, `tab`, `escape`, …)
  and is honest: `verified: false`. Confirm with the next read. Enter submits
  only while a form field holds the focus — otherwise click the button.

### Screenshot

```bash
browser-control-cli tab screenshot /tmp/page.png | jq '{path, bytes, width, height, verified}'
browser-control-cli tab screenshot /tmp/full.png --full
```

The reply dimensions come from the PNG's own IHDR header, not the page's
geometry, so `verified: true` is page-independent. An existing path refuses
`file-exists` unless `--force`.

### Wait for state

```bash
browser-control-cli tab wait --for load    --timeout 15
browser-control-cli tab wait --for idle    --timeout 20 --idle-ms 500
browser-control-cli tab wait --for element --selector '.results' --timeout 10
browser-control-cli tab wait --for js --expr 'document.querySelectorAll(".r").length > 5'
```

`wait` polls one predicate to a wall-clock deadline and refuses `wait-timeout`
naming what it waited for and for how long. `--for js` runs caller code
(capability class `code` — only `--deny code` stops it, unlike `tab js`).
Prefer `element`/`load`/`idle` over `js`.

### JavaScript dialog (alert/confirm/prompt)

```bash
browser-control-cli tab dialog state | jq '{open, verified}'
browser-control-cli tab dialog accept
browser-control-cli tab dialog accept --text "answer to a prompt"
browser-control-cli tab dialog dismiss
```

`state` answers `open: null` rather than guessing when the tab cannot report
(unreadable ≠ absent). Accept/dismiss are verified by the tab answering again.

### Recover a parked tab

A wedged renderer answers `blocked` or `eval-timeout`. `tab nav` uses the
browser's own `Page.navigate` (no page JavaScript), which replaces the document
*and* its renderer — the way out:

```bash
browser-control-cli tab dialog state        # is a dialog the cause?
browser-control-cli tab nav https://example.com --tab active
```

## Capability classes and the policy gate

Every action is classified `read`, `write`, `code`, `file`, `egress` (nothing
carries `egress` today). Gate a call or a session:

```bash
browser-control-cli --deny write tab click "Buy"      # ERR[not-allowed]
BROWSER_CONTROL_DENY=write browser-control-cli tab insert "x"
browser-control-cli --allow read tab text
```

- `tab js` carries `write`+`code` (either `--deny write` or `--deny code`
  stops it); `tab wait --for js` carries `code` alone (only `--deny code`
  stops it).
- `tab extract` is `read` only — a `--deny code` session can still extract.
- `tab screenshot` is `read`+`file`; `tab upload` and `profile seed` are
  `write`+`file`.
- `selftest` is never gated.

## What the reply proves (quick table)

| field | where | means |
| --- | --- | --- |
| `changed` | click, check, select, scroll, activate | something observable moved |
| `before` / `after` | click | url/title/scroll snapshot on each side |
| `verified` | most mutations | read-back succeeded; `false` = read the effect yourself |
| `checked` / `checked_before` | check | the control's own property |
| `selected` / `value` / `by` | select | control's own value; matched by value or label |
| `length_before` / `length_after` | insert, type | the field grew by the text length |
| `url` / `url_read` / `loaded` / `moved` | nav | requested vs observed; `moved: null` = no before-oracle |
| `visibility` | activate | the page's own `document.visibilityState` |
| `matches` / `count` / `total` / `truncated` | find, extract | returned vs reached; truncation is explicit |
| `open` | dialog state | `true`/`false`, `null` = cannot tell |
| `bytes` / `width` / `height` | screenshot | PNG header bytes — page-independent |

## Failures → what to do

| code | do |
| --- | --- |
| `no-browser` | nothing drivable — run `open` |
| `no-page-tab` / `tab-ambiguous` | no / several tabs match — `open URL`, `tab URL`, or a longer `--tab SPEC` |
| `not-managed` | write at a browser the CLI doesn't drive — `open`, or `attach`, or read with an explicit `--tab` |
| `not-attached` | `detach` found no attachment for that selector — check `attach --list` |
| `cdp-not-local` | port belongs to something else — `close --force` then `open` |
| `profile-busy` | another call holds the profile lock — wait for it, retry |
| `no-match` / `ambiguous-element` | adjust text/selector, add `--index N` |
| `occluded` | centre is covered — scroll it in, use `--at`, or pick another target |
| `no-focus` | `tab focus` / `tab click` an editable field first |
| `blocked` / `eval-timeout` | renderer parked — `tab dialog state`, then `tab nav` to escape; `tab close` ends it |
| `no-dialog` | nothing to accept/dismiss |
| `wait-timeout` | predicate never passed — raise `--timeout`, verify the page state |
| `nav-failed` / `nav-not-verified` | bad URL/connection, or a beforeunload dialog — check `tab dialog state` |
| `tabs-open` | `close` refused: the browser holds tabs — `--force` only for a browser YOU opened |
| `file-exists` | pick a new path or `--force` |
| `not-allowed` | policy gate — inspect `selftest.policy`; ask before lifting a user policy |
| `js-error` / `result-too-large` | your expression threw / returned too much |

The full code vocabulary with meanings is `references/errors.md`.

## Safety rules

- `close` with no selector stops only the **managed** browser — that is the
  right cleanup after your own `open`, and `--force` is fine there because you
  opened it. Never use `close --pid/--port` to stop a browser you did not
  start, and never `--force` a browser holding the user's pages.
- `attach`, `profile seed`, `profile reset`, and `tab upload` touch user data
  or grant write access. Do not run them unless the user asked or confirms.
  `profile seed` copies cookies/passwords; `profile reset --force` wipes.
- `tab js` runs arbitrary page code. Use it last, keep expressions small.
- Do not echo secrets. `insert`/`type` never echo text; the audit log records a
  password it proves it touched as a length, not a value.
- Clean up when done: `close --force` (your managed browser only).

## Known limits

- **Every read enables the Page domain** on the tab it reads — the one
  side-effect reads have (that is what makes a suppressed dialog detectable).
- `tab find`/`tab text` see the top document plus **open shadow roots**;
  `tab frames` says which frames were skipped, and `--frame` reaches into one
  (including cross-origin).
- The page owns the DOM oracle: a page can shadow `hit`, `:hover`, geometry, or
  `checked`. Only `/proc`, the PNG header, and CDP errors are page-independent.
- `tab js` returns the page's **own** value: a string that parses as JSON is
  still a string. Use `({a: 1})` for an object, `JSON.stringify(...)` only if
  you want the string.
- Managed browsers load **no extensions** and are isolated from the user's
  profile. Sites can still bot-check; headless launches deliberately carry a
  windowed run's UA.
- `tab extract`'s `NAME=:scope` field spec is the item's own text;
  `NAME=:scope@attr` its own attribute. See the `browser-control-extract`
  skill for extraction details.
