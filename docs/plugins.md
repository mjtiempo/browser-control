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

Both shipped site verbs declare `egress` as well as `read`/`write`: they hand
URLs to the browser, so `--deny egress` refuses them. The class vocabulary and
the rest of the gate are in [Trust](trust.md).

## Writing one

The contract, the supported `browser_control.plugin_api` surface, and the
capability-class rules live in [`plugins/README.md`](../plugins/README.md). A
plugin is local code running in the CLI's process with the CLI's own access;
install plugins the way you install the tool itself.
