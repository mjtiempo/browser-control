# Plugins — site actions on top of the core

The core is generic; a **plugin** teaches it one site: which URL, which
selectors, what to call the fields, and the capability classes the action
holds. Plugins are Python files loaded from `BROWSER_CONTROL_PLUGIN_PATH`
(colon-separated; setting it replaces the default) or
`~/.local/share/browser-control/plugins/`. `selftest` lists what loaded and
what did not; `--help` appends their usage lines.

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
    google search "araghchi speaking in UN" --cap 5
{"ok": true, "entry": "https://www.google.com/",
 "typing": {"chars": 23, "wpm": 90, "measured_wpm": 85.4, "verified": true},
 "count": 5, "results": [{"title": "…", "url": "…", "snippet": "…"}, …]}
```

- Usage: `google search QUERY [--cap N] [--wpm N] [--tab SPEC]`.
- `--wpm N` moves the cadence; `measured_wpm` reports what the whole type
  actually took, CDP round trips included.
- `landed_on` is the `/search?q=…` address the SITE put in the bar after the
  submit — the query went into the page's own field first, which is what a
  person does and what a site's handlers see.

## Writing one

The contract, the supported `browser_control.plugin_api` surface, and the
capability-class rules live in [`plugins/README.md`](../plugins/README.md). A
plugin is local code running in the CLI's process with the CLI's own access;
install plugins the way you install the tool itself.
