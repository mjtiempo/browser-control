# browser-control

Drive **this machine's** Chromium-family browser over CDP — on a managed
profile of its own, from a CLI that prints one JSON object and nothing else.

It is built for callers that cannot look at the screen: an agent, a script, a
test. So every verb is expected to prove what it did, and a refusal is a
first-class answer with a code you can branch on.

```console
$ browser-control-cli open https://example.com
{"ok": true, "started": true, "browser": "/usr/bin/google-chrome-stable",
 "profile": "…/cdp-profiles/google-chrome-stable", "port": 39911, …}

$ browser-control-cli tab text --tab active --chars 60
{"ok": true, "text": "Example Domain\nThis domain is for use in illustrative…", …}

$ browser-control-cli tab click "More information" && browser-control-cli close --force
{"ok": true, "clicked": true, "element": {…}, "changed": true, …}
{"ok": true, "stopped": true, "pid": 334917, "tabs": 1, "forced": true}
```

## The stance: verify, or refuse

Three rules run through every verb, and they are the reason the JSON has the
fields it has.

1. **A mutation is never `ok: true` on a bare acknowledgement.** The read-back
   is in the reply: `checked` after a click, `visibility` after `activate`,
   the file's own PNG header after `screenshot`, `moved` after `nav`.
2. **An unclear oracle is not proof of absence.** A tab that cannot answer
   reports `verified: false` with a note; `tab dialog state` answers
   `open: null` rather than guess.
3. **A refusal names the cause and, when there is one, the fix.** Not
   "failed" — `occluded: … reaches div#cover instead`, `tabs-open: 3 page
   tab(s) are open … pass --force`, `cdp-not-local: port 9222 … is not that
   profile's browser … run close --force and then open`.

Two limits of that stance are worth saying out loud, because a caller who
assumes otherwise will be wrong in a way that matters:

* **The oracle for every DOM verb is the page's own JavaScript.** `hit`,
  `:hover`, the geometry, `checked`, `currentTime` — a page can shadow all of
  them, so `verified: true` means "the page says so". The page-INDEPENDENT
  oracles are the screenshot's own PNG bytes, `/proc` (who holds the port, who
  runs the profile), and the browser's own protocol errors.
* **A read enables the Page domain on the tab it reads** (that is what makes a
  suppressed dialog real instead of a wedge — see `tab dialog state`), so
  reading a tab is not perfectly side-effect-free.

Refusals go to stderr as `ERR[code]: message` with exit status 2.

## Install

```bash
python3 -m pip install .        # the `browser-control-cli` console script
./browser-control-cli selftest  # or straight from the checkout, no install
python3 tests/test_unit.py      # hermetic: no browser needed
python3 tests/live_test.py      # the live battery: needs a browser
```

`selftest` is the machine-readable answer to "what is this install": interpreter,
`websockets` version, profile root, log path, the browsers it found, the verbs it
has, and the **capability classes** below.

Requires Linux (it reads `/proc`), Python ≥ 3.11, and `websockets`. Every
Chromium-family browser on `PATH` is a candidate; the managed profile is keyed
by the binary's name.

## Verbs

Browser level — `open`, `close`, `list`, `info`, `attach`, `detach`, `selftest`.

Page level, under `tab` — `list`, `info`, `close`, `nav`, `back`, `forward`,
`reload`, `activate`, `frames`, `find`, `text`, `js`, `wait`, `click`,
`hover`, `check`,
`select`, `scroll`, `focus`, `press`, `insert`, `type`, `upload`,
`screenshot`, `dialog`, `media`.

`browser-control-cli --help` lists every flag; `docs/plan.md` has the design and
`docs/progress.md` the state of the work.

## Handles and scope

- `--tab SPEC` addresses one tab: `id:<prefix>`, the reserved word `active`
  (the tab whose page reports itself visible **among the browsers this CLI
  drives**), or a title/url substring. Ambiguity refuses and names the
  candidates — no verb ever picks among tabs nobody named.
- `--browser NAME` picks the browser (its executable, or the profile's name).
  With no flag, a live managed browser is used.
- `--profile DIR` picks the **instance**: a profile directory under the root,
  which is how two instances of one browser — two logins, two sessions — are
  addressed, on every verb.
- `tab close` takes the same SPEC, or `--like V` (a declared substring sweep),
  `--title V` / `--url V` (exact), `--all [--except SPEC…]`, and `--dry`
  (resolve and report `would_close`, close nothing).

## What it will and will not do to your browser

- **Its own profile, not yours.** Managed browsers run on
  `BROWSER_CONTROL_ROOT` (default `~/.local/share/browser-control/cdp-profiles`).
  Your everyday browser is listed by `list` and readable by `tab list` / `tab
  text`, but it is never written to unless you say so.
- **`attach` is that consent.** `attach --port N` (or `--pid`, `--profile`)
  makes a browser this CLI did not start writable — **tab writes only**:
  `close` never stops an attached browser.
- **`close` asks before it takes tabs.** A browser with page tabs refuses
  `tabs-open` unless `--force`, because Chromium exits with its last window.
  Nothing is ever `SIGKILL`ed; a process that ignores `SIGTERM` is reported,
  not hunted. A browser can also be stopped *by name* — `close --pid N` — which
  is how another tool's browser goes.
- **Every drive is checked against the kernel.** The port comes from a file; the
  process holding the listening socket is verified first, and anything else
  refuses `cdp-not-local` with nothing sent to it.
- **`tab js` and `tab wait --for js` run code you supply** — the declared escape
  hatch, and a write.
- **`tab screenshot` writes a file you name; `tab upload` reads one.**
- Everything is serialized: one profile lock for `open`/`close`, one for the
  attach records, so two calls cannot race a profile (`profile-busy` names the
  holder).
- The **action log** is JSONL (`BROWSER_CONTROL_LOG`, default
  `~/.local/state/browser-control/actions.jsonl`, `off` to disable). A password
  a verb *proves* it touched is written as a length, never as text.

## Capabilities, machine-readable

`selftest` reports what every verb can reach, per resolved action, so a policy
check or a reviewer does not have to hardcode a verb list:

| class | means |
| --- | --- |
| `read` | state only — `/proc` and loopback CDP |
| `write` | the page, the browser, or this CLI's authorization |
| `code` | runs caller-supplied code: `tab js`, `tab wait --for js` |
| `file` | a path the CALLER named: `screenshot`, `upload` |
| `egress` | would reach the network — nothing today |

```bash
browser-control-cli selftest | jq '.capabilities.by_class'
```

## Writing a caller

One verb per process, one JSON object on stdout, `ERR[code]` on stderr, exit 2.
Long-running is fine (`tab wait --for load --timeout 30`), and anything that
blocks is bounded and named: `eval-timeout`, `blocked` (a parked renderer, with
the dialog named when there is one), `profile-busy`, `wait-timeout`.

`docs/progress.md` lists the known limits, including the honest one: `find` and
`text` see the top document and open shadow roots — `tab frames` says which
frames were left out, and `--frame` reaches into a cross-origin one.

## Licence

MIT — see [LICENSE](LICENSE).
