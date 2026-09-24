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

Browser level — `open [--headless]`, `close`, `list`, `info`, `attach`, `detach`,
`profile info|logins|seed|reset`, `selftest`.

Page level, under `tab` — `list`, `info`, `close`, `nav`, `back`, `forward`,
`reload`, `activate`, `frames`, `find`, `text`, `extract`, `js`, `wait`,
`click`, `hover`, `check`, `select`, `scroll`, `focus`, `press`, `insert`,
`type`, `upload`, `screenshot`, `dialog`, `media`.

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
- **`open --headless` starts it with no window.** `--headless=new` is the
  browser's own windowless mode, and every verb speaks CDP, so the surface is
  unchanged: `tab text`, `click`, `screenshot`, `activate` all drive it. The
  mode belongs to the PROCESS, so `open`, `list` and `info` report the mode a
  running browser is in (read from its own command line), and asking for
  `--headless` when a windowed browser is already up refuses — `close` it and
  `open --headless` again to change mode.
- **A launch never announces itself as automation.** Every start — windowed
  or headless — carries the UA a windowed run would send: the binary's own
  version, read with `--version` and written in Chrome's reduced form
  (`Chrome/153.0.0.0`), plus `--disable-blink-features=AutomationControlled`
  (Chrome 153 sets `navigator.webdriver` when the DevTools port is `0`).
  Measured: a stricter bot check (Cloudflare on `pna.gov.ph`) looped its
  challenge forever for an instance whose UA said `HeadlessChrome/…` and
  loaded the page with the spoof — a windowed instance already sent that same
  string. If the version cannot be read, no UA flag is added; the browser
  keeps its own.
- **Managed instances load no extensions.** A managed profile is often
  seeded from yours (`profile seed`), so it can carry your extensions on
  disk — but the browser always starts with `--disable-extensions`, windowed
  or headless. That is not tidiness: a wallet extension's background pages
  were measured spinning a headless instance's main thread at ~126% CPU
  until its own DevTools endpoint answered in 7 s instead of 4 ms, which
  starves every verb. The files stay in the profile; only the code is never
  loaded.
- **`attach` is that consent.** `attach --port N` (or `--pid`, `--profile`)
  makes a browser this CLI did not start writable — **tab writes only**:
  `close` never stops an attached browser. The verification is the browser's
  own command line naming the profile (`--user-data-dir=<profile>`), so a
  browser started on its vendor default profile — with no such flag — cannot
  be verified and `attach` refuses it; start it with an explicit
  `--user-data-dir` to make it attachable.
- **`close` asks before it takes tabs.** A browser with page tabs refuses
  `tabs-open` unless `--force`, because Chromium exits with its last window.
  Nothing is ever `SIGKILL`ed; a process that ignores `SIGTERM` is reported,
  not hunted. A browser can also be stopped *by name* — `close --pid N` — which
  is how another tool's browser goes.
- **Every drive is checked against the kernel.** The port comes from a file; the
  process holding the listening socket is verified first, and anything else
  refuses `cdp-not-local` with nothing sent to it.
- **`tab js` and `tab wait --for js` run code you supply** — the declared escape
  hatch, and a write. The reply is the page's OWN value: a string that parses
  as JSON is still that string (this CLI's own probes decode, because they
  stringify on purpose; the escape hatch does not).
- **`tab screenshot` writes a file you name; `tab upload` reads one.**
- Everything is serialized: one profile lock for `open`/`close`, one for the
  attach records, so two calls cannot race a profile (`profile-busy` names the
  holder).
- The **action log** is JSONL (`BROWSER_CONTROL_LOG`, default
  `~/.local/state/browser-control/actions.jsonl`, `off` to disable). A password
  a verb *proves* it touched is written as a length, never as text.

## Is this profile seeded?

`profile seed` copies logins in; `profile logins` says what is actually there —
without opening a browser, and without reading a single value:

```console
$ browser-control-cli profile logins --site x.com
{"ok": true, "profile": "…/google-chrome-stable", "exists": true,
 "profile_dirs": ["…/Default"],
 "stores": {"cookies": {"present": true, "readable": true, "rows": 12,
                        "hosts": 2, "error": ""},
            "passwords": {"present": true, "readable": true, "rows": 0,
                          "origins": 0, "error": ""}},
 "sites": [{"host": ".x.com", "count": 10,
            "cookies": ["auth_token", "ct0", "twid", …],
            "expires": "2027-10-25T22:42:05", "expired": false, "more": 0}],
 "site": "x.com", "snapshot": false, …}
```

Every store is read from a **copy** of the profile's own `Cookies` / `Login
Data` file (Chrome holds the real one open), and only hosts, cookie NAMES,
expiry and COUNTS are reported: never a cookie value, never a username, never a
password. `--site HOST` narrows by host suffix — so `x.com` answers `.x.com`
and `www.x.com`, and `notx.com` is neither — and `--cap N` bounds the hosts
listed. `profile seed` reports the same facts for what it just copied, so
"what landed" is answered by the login stores rather than by file sizes alone.

An existing target refuses `profile-exists` unless `--force`, and `--force`
**wipes it first**: what is left is the source's content, never a mix of two
profiles. A `--dry` run reports both what would land and what `--force` would
destroy, and writes nothing.

It is evidence, not a verdict: a session-only cookie has no expiry on disk, a
cookie can be revoked server-side, and `snapshot: true` says a browser is
running on the profile, so what is on disk may lag it. A store that cannot be
read (a `Cookies` that is not a database, a schema this CLI does not know) is
reported as `readable: false` with the reason in `stores.<name>.error` — its
rows are unknown, not zero.

## Extraction — records, no code

`tab extract` turns a page's repeated items into records, declaratively:

```console
$ browser-control-cli tab extract --each article \
    --field 'text=[data-testid="tweetText"]' \
    --field time=time@datetime \
    --field url='a[href*="/status/"]@href' --cap 5
{"ok": true, "count": 5, "total": 32, "truncated": true,
 "matches": [{"text": "…", "time": "2026-09-20T07:37:24.000Z", "url": "/…/status/…"}, …]}
```

- `--each CSS` names the repeated element; `--field NAME=SPEC` names one value
  inside it — `NAME=SELECTOR` (innerText), `NAME=SELECTOR@attr` (attribute),
  `NAME=@attr` (attribute of the match) or `NAME=:scope` (the match's text).
- `--cap N`, `--chars N`, `--visible`, `--unique FIELD` bound and filter the
  answer; values are sliced **in the page**, so a huge document cannot flood
  the reply.
- It is a **read**: CSS selectors only, no JavaScript, classified `read`, so a
  host running `--deny code` can still extract. The values are the page's own
  DOM text/attributes — the same oracle as `find`/`text`, with no claim beyond
  "this is what the page showed".

## Plugins — site actions on top of the core

The core is generic; a **plugin** teaches it one site: which URL, which
selectors, what to call the fields, and the capability classes the action
holds. Plugins are Python files loaded from `BROWSER_CONTROL_PLUGIN_PATH`
(colon-separated; setting it replaces the default) or
`~/.local/share/browser-control/plugins/`. `selftest` lists what loaded and
what did not; `--help` appends their usage lines.

This repo ships two. `plugins/x_reader.py` is read-only X search — and
`--cap` is a TARGET, not a slice of the first render: measured, X keeps only
3–9 articles mounted and recycles the rows as the timeline moves, so the
plugin wheels the page and merges what mounts, by post id, until it has enough
results (bounded by `--max-scrolls`):

```console
$ BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins browser-control-cli \
    x search '"Pardon Snowden"' --latest --cap 5
{"ok": true, "sort": "latest", "count": 5, "truncated": false,
 "loading": {"reads": 3, "scrolls": 2, "max_scrolls": 10, "stop": "cap"},
 "posts": [{"handle": "…", "time": "…", "text": "…"}, …]}
```

`--latest` is X's "Latest" sort (by date), `--top` its relevance ranking, and
`sort` reports what the page's own tab strip says is selected — no verb can
prove a site's ordering. `loading.stop` names why the loading ended (`cap`,
`exhausted`, `max-scrolls`, `no-posts`, `scroll-failed`), and `--max-scrolls 0`
reads only what the first render holds.

`plugins/google_search.py` searches Google by TYPING: the query goes into the
site's own search box as real per-character key events at a human cadence
(90 WPM by default), then Enter — no query URL is ever built:

```console
$ BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins browser-control-cli \
    google search "araghchi speaking in UN" --cap 5
{"ok": true, "entry": "https://www.google.com/", "typing": {"chars": 23, "wpm": 90, "measured_wpm": 85.4, "verified": true}, "count": 5, "results": [{"title": "…", "url": "…", "snippet": "…"}, …]}
```

See `plugins/README.md` for the plugin contract and both shipped plugins.

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

Two notes on the policy inputs, so a deny never *looks* wider than it is:
`--deny egress` is accepted but currently names no action — nothing carries
that class yet; and `tab wait --for js` is classified `code` alone, so
`--deny write` does not stop it (`--deny code` stops both escape hatches).

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
