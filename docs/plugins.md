# Plugins — site actions on top of the core

The core is generic; a **plugin** teaches it one site: which URL, which
selectors, what to call the fields, and the capability classes the action
holds. Plugins are Python files loaded from `BROWSER_CONTROL_PLUGIN_PATH`
(colon-separated; setting it replaces the default) or
`~/.local/share/browser-control/plugins/`. `selftest` lists what loaded and
what did not; `--help` appends their usage lines.

Every entry on that path must be an **absolute** path (or start with `~`). A
relative entry is REFUSED and reported in `selftest`'s `plugin_errors` — it
would import from whatever the current directory happens to hold, with the
CLI's own credentials and its access to the browser, so `plugins` in a shell rc
would run an untrusted tree's code on every invocation:

```console
$ BROWSER_CONTROL_PLUGIN_PATH=plugins browser-control-cli selftest \
    | jq '.plugin_errors'
["plugins: a RELATIVE plugin directory is refused — 'plugins' would import
  from '/wherever/you/are/plugins', …; name an absolute path or `~/…`"]
```

```bash
BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins browser-control-cli selftest \
  | jq '{plugins, plugin_errors}'
```

## Shipped: `x search` — read-only X results

`--cap` is a TARGET, not a slice of the first render: measured, X keeps only
3–9 articles mounted and recycles the rows as the timeline moves, so the
plugin wheels the page and merges what mounts, by post id, until it has enough
results (bounded by `--max-scrolls`):

```console
$ BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins browser-control-cli \
    x search '"Pardon Snowden"' --latest --cap 20
{"ok": true, "sort": "latest", "count": 20, "truncated": true,
 "loading": {"reads": 5, "scrolls": 4, "max_scrolls": 10, "stop": "cap"},
 "posts": [{"handle": "…", "time": "…", "text": "…"}, …]}
```

- Usage: `x search QUERY [--latest | --top] [--cap N] [--chars N]
  [--max-scrolls N] [--tab SPEC]`.
- `--latest` is X's "Latest" sort (by date) and the default; `--top` is its
  relevance ranking. `--cap` defaults to 10 (max 50); `--chars` to 1200 per
  text field; `--max-scrolls` to 10, and `0` reads only the first render.
- `sort` reports what the page's own tab strip says is selected — no verb can
  prove a site's ordering.
- `loading.stop` names why the loading ended (`cap`, `exhausted`,
  `max-scrolls`, `no-posts`, `scroll-failed`); `truncated` is true whenever
  loading stopped short, since more posts may exist below.
- The browser must be logged in on the tab's profile for anything beyond the
  public wall — seed the managed profile first, see
  [Sessions and profiles](sessions.md).

## Shipped: `google search` — search by typing

`google search` searches Google by TYPING the query into the site's own search
box — real per-character key events at a human cadence (90 WPM by default),
then Enter. No query URL is ever built:

```console
$ BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins browser-control-cli \
    google search "araghchi speaking in UN" --cap 20
{"ok": true, "entry": "https://www.google.com/",
 "typing": {"chars": 23, "wpm": 90, "measured_wpm": 85.4, "verified": true},
 "count": 20, "loading": {"pages": 3, "clicks": 2, "max_pages": 3,
 "stop": "cap"}, "selectors": {"results": "#search div.MjjYud:has(h3)",
 "matched": true}, "results": [{"title": "…", "url": "…",
 "snippet": "…", "page": 1}, …]}
```

- Usage: `google search QUERY [--cap N] [--wpm N] [--max-pages N] [--tab SPEC]`.
- `--wpm N` moves the cadence; `measured_wpm` reports what the whole type
  actually took, CDP round trips included.
- `--cap N` is a TARGET, not a slice of page one: the plugin clicks the site's
  own Next control (never a built `&start=` URL), waits for the render to
  really swap, and merges what comes back by URL. `--max-pages N` (default 3,
  max 10) bounds the paging; the `loading` block reports `pages`, `clicks` and
  the `stop` cause (`cap`, `no-next`, `max-pages`, `no-growth`,
  `click-failed`, `wait-failed`), and every result carries the 1-based `page`
  it came from. Only a `wait-timeout` from the Next control is read as "there
  is no next page" (`stop: "no-next"`); any other refusal stops the paging as
  `wait-failed`, names it in `loading.wait_error`, and sets `truncated` —
  a closed tab was never the end of the list.
- The selector map is ordered candidates: the first the page renders is the
  one extraction runs with, and `selectors` names it — so a zero-result reply
  distinguishes a DOM change (`matched: false`) from a page that showed none.
  `selectors.next`/`next_matched` answer the same question about the
  pagination control.
- `landed_on` is the FIRST `/search?q=…` address the SITE put in the bar after
  the submit — the query went into the page's own field first, which is what a
  person does and what a site's handlers see.

## Shipped: `page read` — a list of URLs in one call

The loop this replaces is three CLI processes per page (`tab nav` → `tab wait`
→ `tab text`) plus the shell that carries the URL between them, paid once per
page when a caller reads a list of them (a SERP, a queue, a set of docs):

```console
$ BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins browser-control-cli \
    page read https://example.com https://example.org --chars 3000
{"ok": true, "count": 2, "chars": 3000, "errors": [],
 "pages": [{"url": "https://example.com", "moved": true,
            "title": "Example Domain", "length": 129, "truncated": false,
            "text": "…"}, …]}
```

- Usage: `page read URL... [--chars N] [--timeout S] [--tab SPEC]`. Each page
  is the CLI's own `tab text` answer for that URL, `length`/`truncated`
  included, so the cap is the page's own answer to "was there more".
- One URL's failure is listed in `errors` (with its refusal code) instead of
  sinking the call — a caller reading a list wants the rest of it. A call that
  could read **nothing** refuses with the first error's own code.
- A page whose words are inside a single **same-process** frame is read from
  that frame's document and the record NAMES it (`read_frame`), rather than
  reporting a page that plainly has words as empty; the page's own `frames`
  census rides along either way. That read is automatic — no `--frame` needed.
- The action declares `"frames": True`, so the global `--frame` applies to it
  as it does to the content verbs (`page read URL --frame 0`); the reply then
  carries `frame`/`frame_resolved`, and the verb saves and restores the scope
  around its own single-frame read instead of clearing the caller's.
- `--chars`/`--timeout` given an EMPTY value are refused (`needs a number`),
  like every other value-carrying flag in the tool, and an empty URL is passed
  to the url policy rather than dropped from the list.
- The browser must be running with a drivable tab: `open --headless URL` first
  (or `attach` to your own).

Every shipped site verb declares `egress` as well as `read`/`write`: they
hand URLs to the browser, so `--deny egress` refuses them. The class vocabulary
and the rest of the gate are in [Trust](trust.md).

## Shipped: `slack message` — one permalink, one call

The walk this replaces is a whole session's worth of CLI processes: navigate,
discover the desktop-app launch stub, click its link, wait for the client,
scrape the DOM for a message by its timestamp, notice the payload was split
across several messages, stitch them, click the reply bar, read the thread.

```console
$ BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins browser-control-cli \
    slack message https://<workspace>.slack.com/archives/C…/p1790340351996489 --thread
{"ok": true, "channel": "C…", "ts": "1790340351.996489",
 "sender": "AWS Notifications", "posted_at": "2026-09-25T12:45:51Z",
 "chunked": true, "parts": [{"ts": "1790340351.996489", "text": "…"}, …]}
```

- Usage: `slack message PERMALINK [--thread] [--chars N] [--timeout S]
  [--tab SPEC]`. The permalink is the one a message's own "Copy link" gives
  (`…/archives/<CHANNEL>/p<TS>`); the client's own
  `/client/<TEAM>/<CHANNEL>/<ts>` address is accepted too.
- **The launch stub is detected by the ADDRESS, not by `load`.** A workspace
  permalink answers with "We've redirected you to the desktop app" — a document
  that is already `complete`, so `tab wait --for load` passes on it. The plugin
  waits for the client's address (`--for url --match …`) and clicks the stub's
  own link only when the address never left it; `loading.stub_clicked` says
  which path ran.
- **`parts` is one payload, not one message.** Slack cuts a message at ~4 000
  characters and posts the overflow as the next message (measured: four
  messages, 3 730–3 835 characters, 20–60 ms apart), and renders the AUTHOR
  once per group — the overflow messages have an EMPTY sender cell. The walk
  reads Slack's own grouping, joins with NO separator (the cut lands
  mid-token), and stops at the first different sender or a gap over a second.
- **A permalink INTO a payload names its gap.** The client mounts nothing above
  a deep-linked message, so such a payload's head is not on the page: the reply
  carries `head_missing: true` (and `truncated`), with a note naming the fix —
  open the payload's FIRST message — instead of passing the tail off as the
  whole payload.
- **`--thread` reads the replies beside the page's own claim.** It clicks the
  message's reply bar (`:has()` names the bar OF that message) and reads the
  pane; `claimed` is the bar's own number next to `count`, what the pane
  actually rendered, so a virtualized thread cannot look complete. A message
  that renders no bar answers `claimed: 0` with a note rather than clicking at
  nothing.
- `--chars` bounds each part (default 4000, max 20000 — `tab extract`'s own
  per-field ceiling); `--timeout` (default 30 s) gates the render. Every
  argument is validated before the first navigation.
- The browser must be logged in to the workspace on the tab's profile — seed
  the managed profile with `profile seed` first, or drive an attached session.

## Shipped: `slack channel` — a channel's recent messages

The timeline recycles its rows: the client mounts a window and unmounts what
scrolls out, so one extraction can never answer a `--cap` request. The verb
extracts, wheels UP (older messages are above), extracts again and merges by
timestamp — the same loading loop the X adapter uses, and the reason `--cap` is
a TARGET rather than a slice of the first render:

```console
$ BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins browser-control-cli \
    slack channel https://<workspace>.slack.com/archives/C… --cap 20
{"ok": true, "channel": "C…", "count": 20, "truncated": true,
 "loading": {"reads": 2, "scrolls": 1, "stop": "cap", …},
 "messages": [{"ts": "1790340351.996489", "posted_at": "2026-09-25T12:45:51Z",
               "sender": "AWS Notifications", "text": "…"}, …]}
```

- Usage: `slack channel PERMALINK [--cap N] [--chars N] [--max-scrolls N]
  [--timeout S] [--tab SPEC]`. `--cap` default 20 (max 50); `--chars` default
  500 per message (max 4000 — the page-side budget is `cap × chars`, so an
  over-budget read says `truncated` instead of quietly shortening);
  `--max-scrolls` default 5 (0 reads the first window only).
- The reply holds the NEWEST `messages`, oldest first, and `loading.stop` names
  why the walk ended: `cap` (enough read — more may exist), `exhausted` (the
  client stopped yielding), `max-scrolls` (the budget), `no-messages` (a wall or
  an empty channel), `scroll-failed`. `truncated` is true whenever the walk
  stopped short or a read was cut.
- **Slack renders the author once per message group**, so every message after
  the group's first has an empty sender cell. The reply carries the group's
  name down the list; a LEADING empty sender means that group's header sits
  above what the walk loaded, not that the message has no author.
- `--cap` is a target: a round can mount several messages at once and
  overshoot, in which case the newest `--cap` are kept and `truncated` is set.
- Same session requirement as `slack message`: the tab's profile must be
  logged in to the workspace.

## Writing one

The contract, the supported `browser_control.plugin_api` surface, and the
capability-class rules live in [`plugins/README.md`](../plugins/README.md). A
plugin is local code running in the CLI's process with the CLI's own access;
install plugins the way you install the tool itself.
