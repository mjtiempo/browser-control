# Plugins

The core of browser-control is generic: it opens browsers, drives tabs, and
reads pages. A **plugin** teaches it one site — which URL, which selectors,
what to call the fields, and which capability classes the action holds — so the
site knowledge lives outside the tool and can change without a tool release.

## Installing one

Copy the file into either place:

* any directory in `BROWSER_CONTROL_PLUGIN_PATH` (colon-separated; when set,
  this **replaces** the default below), or
* `~/.local/share/browser-control/plugins/` (the default; not created for you).

Only top-level `*.py` files are loaded, in sorted order; names starting with
`_` are skipped. `selftest` reports what loaded (`plugins`) and what did not
(`plugin_errors`) — a plugin that fails to import, declares the wrong API or
collides with a verb already taken is skipped, never fatal. `--help` appends
the installed actions' usage lines.

## The contract

```python
PLUGIN = {
    "api": 1,                          # this file's API version
    "name": "my-site",
    "description": "read-only …",
    "actions": {
        "mysite": {                    # the top-level verb
            "run": run,                # (rest, browser) -> one JSON reply dict
            "classes": ("read",),      # capability classes the gate enforces
            "usage": "mysite search QUERY [--cap N]",
            # optional: this action acts on a page's CONTENT, so the caller's
            # global `--frame` applies to it (without this, `--frame` refuses
            # for a plugin verb exactly as it does for `list` or `open`)
            # "frames": True,
        },
    },
}
```

An action that browses declares `egress` in its `classes`: it hands a URL to
the browser, so `--deny egress` must be able to stop it.

`run(rest, browser)` receives the argv **after** the global flags were
stripped (`--browser`, `--profile`, `--frame`, `--allow`, `--deny`) and the
value of `--browser`. The verb is what comes after the top-level word — for
`mysite search foo`, `rest` is `["search", "foo"]`. Refuse with
`fail(errors.ERR_BAD_ARGS, …)`, or raise `ControlError`;
`main` maps it to `ERR[code]` and exit 2 like any built-in refusal. Name a
code with the constant, never a bare string: the vocabulary is closed, and the
hermetic check scans `plugins/` for an unregistered one.

The supported surface is `browser_control.plugin_api` (`fail`,
`ControlError`, `errors`, `pop`, `switch`, `text_arg`, `int_arg`, `float_arg`,
`tab_arg`, `nav`, `wait`, `extract`, `focus`, `type_text`, `press`, `click`,
`scroll`, `frame`, `PLUGIN_API`) — import that, not `browser_control.lib` at
large.
`pop` and `switch` are the CLI's OWN argv readers: `rest, cap = pop(rest,
"--cap", "mysite search")` and `rest, given = switch(rest, "--latest")` parse
a plugin's arguments the way the built-ins are parsed, missing-value refusal
included — including the bare `--` end-of-flags marker, so `mysite search --
-spam` searches for `-spam`. `tab_arg` is the `--tab SPEC` reader beside them:
it refuses an empty `--tab ""` rather than reading it as "the only page tab",
exactly as the built-in page verbs do. `focus`, `type_text`, `press`,
`click` and `scroll` are the drive-it-by-hand verbs: `scroll` sends one real
wheel event, which is how a lazy or virtualized list (`x_reader.py`) is made
to render past its first window. `frame` sets, clears or reads the frame scope
(a plugin that sets one for its own read must RESTORE it: `saved =
frame(); frame("0"); … ; frame(saved)`). The capability classes a
plugin declares must come from the closed vocabulary (`read`, `write`, `code`,
`file`, `egress`), and the gate applies to them exactly as it does to the
built-ins (`--deny read mysite …` refuses `not-allowed`).

## Trust

A plugin is local code running in the CLI's process with the CLI's own access.
The declared classes are **declarations**, not a sandbox — the same stance the
core's own capability table takes. Install plugins the way you install the
tool itself.

## Shipped: `slack_reader.py`

Read a Slack message — and its thread — from the permalink a message's own
"Copy link" gives:

```bash
BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins browser-control-cli \
    slack message https://<workspace>.slack.com/archives/<CHANNEL>/p<TS> --thread
```

* **The launch stub is detected by the ADDRESS, not by `load`.** A workspace
  permalink answers with a desktop-app stub whose document is already
  `complete` (`tab wait --for load` passes on it), so the plugin waits for the
  client's address (`--for url --match …`) and clicks the stub's own link only
  when the address never left the stub; `loading.stub_clicked` says which path
  ran.
* **`parts` is one payload, not one message.** Slack cuts a message at ~4 000
  characters and posts the overflow as the next message, and renders the
  author ONCE per group (the overflow messages have an empty sender cell).
  The walk reads Slack's own grouping and joins with no separator, because the
  cut lands mid-token.
* **`--thread` reads the replies beside the page's own claim.** `claimed` is
  the reply bar's number; `count` is what the pane actually rendered — a
  virtualized thread cannot look complete.
* **A permalink INTO a payload names its gap.** The client mounts nothing above
  a deep-linked message, so the reply says `head_missing: true` (and
  `truncated`) instead of passing the tail off as the whole payload.

`--chars` (default 4000, max 20000) bounds each part; `--timeout` (default
30 s) gates the render. The browser must be logged in to the workspace on the
tab's profile.

The same file also ships **`slack channel PERMALINK [--cap N] [--chars N]
[--max-scrolls N]`**, which reads a channel's recent messages: it extracts the
mounted window, wheels up, and merges by timestamp, because the client
unmounts what scrolls out (`--cap` is a target, and `loading.stop` says why the
walk ended). Slack renders the author once per group, so a name-less message
carries the one above it — a leading empty sender means the group's header is
above the loaded window.

## Shipped: `x_reader.py`

Read-only X search, built on `tab extract` — with `--cap` as a TARGET rather
than a slice of the first render:

```bash
BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins browser-control-cli \
    x search '"Pardon Snowden"' --latest --cap 20
```

* `--latest` is X's "Latest" sort (by date); `--top` is its relevance ranking;
  the default is `--latest`.
* **`--cap` is a target.** Measured, X keeps only 3–9 articles mounted and
  recycles the rows as the timeline moves, so one `tab extract` can never
  answer a 20-post request. The plugin wheels the page (`--max-scrolls`,
  default 10) and merges by post id until it has `--cap` posts, X stops
  yielding, or the budget runs out. `--max-scrolls 0` reads only the first
  render.
* The reply's `loading` block says what it took (`reads`, `scrolls`) and why
  it stopped (`stop`: `cap`, `exhausted`, `max-scrolls`, `no-posts`,
  `scroll-failed`). `truncated` is true whenever loading stopped short, since
  more posts may exist.
* `sort` in the reply is what the page's own tab strip reports as selected —
  the adapter's honest answer to "is this sorted by date", since no verb can
  prove a site's ordering.
* The selector map at the top of the file (`article`, `tweetText`,
  `time@datetime`, the status link) is the part that breaks when X changes its
  DOM; that is why it lives here and not in the core.

The browser must be logged in on the tab's profile for anything beyond the
public wall — seed the managed profile with `profile seed` first.

## Shipped: `google_search.py`

Search Google by TYPING the query into the site's own search box — real
per-character key events at a human cadence (90 WPM by default), then `Enter`,
then the rendered cards read with `tab extract`:

```bash
BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins browser-control-cli \
    google search "araghchi speaking in UN" --cap 5
```

* **No query URL is built.** The only address this plugin navigates to is
  `https://www.google.com/`; `landed_on` in the reply is the `/search?q=…`
  address the SITE put in the bar after the submit, and the `typing` block
  reports what was typed, at what cadence, and the page's own read-back
  (`verified`). That is the point of the plugin: typed input lands in the
  page's field first, which is what a person does and what a site's handlers
  (autocomplete, consent, bot checks) actually see.
* `--wpm N` moves the cadence (`N` words a minute, 5 characters to a word, so
  `12 / N` seconds between keystrokes); `measured_wpm` reports what the whole
  type actually took, CDP round trips included.
* `--cap N` is a TARGET, not a slice of page one: the plugin clicks the site's
  own Next control (never a built `&start=` URL), waits for the render to
  really swap, and merges by URL until the target is met, no Next is left, or
  the `--max-pages N` budget (default 3, max 10) runs out. The `loading` block
  says how many `pages`/`clicks` that took and which `stop` cause ended it
  (`cap`, `no-next`, `max-pages`, `no-growth`, `click-failed`, `wait-failed`);
  every result row carries the 1-based `page` it came from. Only a
  `wait-timeout` on the Next control means "there is no next page"
  (`no-next`); any other refusal (a closed tab, a dead browser) stops as
  `wait-failed`, names the refusal in `loading.wait_error` and sets
  `truncated` — a pagination failure is never reported as the end of the list.
* The selector map is ORDERED candidates, each asking only for cards that HAVE
  a heading (`:has(h3)`), so counts are results rather than the panels around
  them. The first candidate the page renders — `#search div.MjjYud:has(h3)`,
  then `div.g:has(h3)`, then `div[data-snc]:has(h3)`; likewise
  `textarea[name="q"]` then `input[name="q"]` — is the one extraction runs
  with, and the reply's `selectors` block names it. A zero-result reply can
  therefore say whether the map found nothing (`matched: false`) or the page
  really showed none; `selectors.next`/`next_matched` are the same pair for the
  pagination control.

It declares `read`+`write`+`egress` (it navigates for writes, types into the
page, and the browser reaches the network for Google), so
`--deny write google search …` and `--deny egress google search …` refuse it
like any built-in.
