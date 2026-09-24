# Trust — what it touches, and what it refuses

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

- **The oracle for every DOM verb is the page's own JavaScript.** `hit`,
  `:hover`, the geometry, `checked`, `currentTime` — a page can shadow all of
  them, so `verified: true` means "the page says so". The page-INDEPENDENT
  oracles are the screenshot's own PNG bytes, `/proc` (who holds the port, who
  runs the profile), and the browser's own protocol errors.
- **A read enables the Page domain on the tab it reads** (that is what makes a
  suppressed dialog real instead of a wedge — see `tab dialog state`), so
  reading a tab is not perfectly side-effect-free.

Refusals go to stderr as `ERR[code]: message` with exit status 2; every code
is in [Refusal codes](refusals.md).

## What it will and will not do to your browser

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
- **`close` asks before it takes tabs.** A browser with page tabs refuses
  `tabs-open` unless `--force`, because Chromium exits with its last window.
  Nothing is ever `SIGKILL`ed; a process that ignores `SIGTERM` is reported,
  not hunted. A browser can also be stopped *by name* — `close --pid N` —
  which is how another tool's browser goes.
- **Every drive is checked against the kernel.** The port comes from a file;
  the process holding the listening socket is verified first, and anything
  else refuses `cdp-not-local` with nothing sent to it.
- **Only this CLI's own root is touchable.** `BROWSER_CONTROL_ROOT` moves
  the managed root; `reset --force` and `seed --force` recursively delete
  under it, so a profile directory under a MOVED root is wiped only when this
  CLI created it — `open`/`profile seed` drop a `.browser-control.profile`
  marker, and a directory without one refuses `not-managed`. (Under the
  default root, profiles are this tool's own by construction.)
- **The DevTools endpoint is loopback TCP and carries NO authentication.**
  The browser listens on `127.0.0.1` with an ephemeral port, and CDP has no
  token: any local process — including a different user on this machine,
  because loopback is not a per-user boundary — can find the port
  (`/proc/net/tcp`) and drive the browser, session cookies included. `selftest`
  reports this as `cdp_exposure`. Treat a running managed browser on a shared
  machine the way you would treat any process holding your sessions.
- **`tab js` and `tab wait --for js` run code you supply** — the declared
  escape hatch, and a write. The reply is the page's OWN value: a string that
  parses as JSON is still that string (this CLI's own probes decode, because
  they stringify on purpose; the escape hatch does not).
- **`tab screenshot` writes a file you name; `tab upload` reads one.**
- The **action log** is JSONL (`BROWSER_CONTROL_LOG`, default
  `~/.local/state/browser-control/actions.jsonl`, `off` to disable). A password
  a verb *proves* it touched is written as a length, never as text.

## Capabilities, machine-readable

`selftest` reports what every verb can reach, per resolved action, so a policy
check or a reviewer does not have to hardcode a verb list:

| class | means |
| --- | --- |
| `read` | state and page CONTENT — `/proc`, loopback CDP, and the rendered page of every drivable browser |
| `write` | the page, the browser, or this CLI's authorization |
| `code` | runs caller-supplied code: `tab js`, `tab wait --for js` |
| `file` | a path the CALLER named: `screenshot`, `upload` |
| `egress` | would reach the network: it hands a URL to the browser (`open`, `tab`, `tab nav`) or runs caller code that can `fetch` (`tab js`, `tab wait --for js`); the shipped site plugins declare it too |

Gate a call, or a whole session:

```bash
browser-control-cli --deny write tab click "Buy"      # ERR[not-allowed]
BROWSER_CONTROL_DENY=write browser-control-cli tab insert "x"
browser-control-cli --allow read tab text
browser-control-cli --deny egress tab nav https://example.com   # ERR[not-allowed]
browser-control-cli selftest | jq '.capabilities.by_class'
```

Two notes on the policy inputs, so a deny never *looks* wider than it is:
`--deny egress` refuses the verbs in that row and nothing else — `tab text` is
`read` only, so `--deny file` (or `--deny egress`) leaves it alone; and
`tab wait --for js` carries `code`+`write`+`egress`, so `--deny write`,
`--deny code` or `--deny egress` stops it exactly as it stops `tab js`.
`selftest` and `help` are the TWO verbs the gate does not consult — neither
performs an action, and a gate that blocked its own explanation would be a
trap — and both still write the one audit line every invocation owes. Plugin
verbs report their classes under `plugin_declared`: those are the plugin's OWN
declarations, not something this tool verified.
