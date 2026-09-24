---
name: browser-control-plugins
description: Use and write site plugins for browser-control-cli — the shipped google and x search actions, and the plugin contract for adding a site adapter (URL, selectors, capability classes). Use when asked to search Google through the browser by typing, read X/Twitter search results, or add/port a site adapter for this CLI.
---

# browser-control plugins

Plugins teach `browser-control-cli` one site: which URL, which selectors, what
the fields are called, and the capability classes the action holds. They are
loaded in-process and are trusted local code (the declared classes are
declarations, not a sandbox).

This skill builds on the `browser-control` skill (lifecycle, handles, refusal
codes). For plain page extraction, prefer the `browser-control-extract` skill —
a reader plugin is normally a thin wrapper around `tab extract`.

## Loading plugins

```bash
# option A: point at a directory for one session (REPLACES the default dir)
BROWSER_CONTROL_PLUGIN_PATH=/path/to/dir browser-control-cli selftest

# option B: install by copying top-level *.py files into the default dir
cp plugins/*.py ~/.local/share/browser-control/plugins/
```

- `BROWSER_CONTROL_PLUGIN_PATH` is colon-separated; when set it **replaces**
  the default `~/.local/share/browser-control/plugins/`.
- Only top-level `*.py` load, in sorted order; names starting with `_` are
  skipped. A plugin that fails to import, declares the wrong API, or collides
  with an existing verb is skipped, never fatal.
- Verify: `selftest | jq '{plugins, plugin_errors}'` — `plugins` lists what
  loaded, `plugin_errors` what did not. `--help` appends installed usage lines.
- The shipped plugins live in this repository's `plugins/` directory. When the
  skills are installed as symlinks into `~/.pi/agent/skills/`, that directory
  is `../../plugins` relative to this skill's own directory; on this machine
  the checkout is `/home/mark/Documents/pidev/mark-browser-control`, so:

```bash
BROWSER_CONTROL_PLUGIN_PATH=/home/mark/Documents/pidev/mark-browser-control/plugins \
  browser-control-cli selftest | jq '.plugins'
```

## `google search` — type the query, never build a URL

```bash
BROWSER_CONTROL_PLUGIN_PATH=/home/mark/Documents/pidev/mark-browser-control/plugins \
browser-control-cli google search "araghchi speaking in UN" --cap 5 --wpm 90
```

The query goes into Google's own search box as real per-character key events at
a human cadence, then Enter. This is what a site's handlers (autocomplete,
consent, bot checks) actually see — no `/search?q=` URL is ever constructed.

- Usage: `google search QUERY [--cap N] [--wpm N] [--tab SPEC]`
- `--cap` default 10, max 50; `--wpm` default 90, max 600 (5 chars per word).
- Reply: `query`, `entry` (the homepage), `typing` (`chars`, `wpm`,
  `measured_wpm`, `elapsed_s`, `verified`), `landed_on` (the address the SITE
  put in the bar after submit), `count`, `truncated`,
  `results[{title, url, snippet}]`, `note`.
- Classes `read`+`write` (it navigates and types), so `--deny write google
  search …` refuses `not-allowed`. Zero results can be the site changing its
  DOM, a consent wall, or a challenge — read `landed_on`.

## `x search` — read-only X results

```bash
BROWSER_CONTROL_PLUGIN_PATH=/home/mark/Documents/pidev/mark-browser-control/plugins \
browser-control-cli x search "bitcoin price" --latest --cap 20
```

- Usage: `x search QUERY [--latest | --top] [--cap N] [--chars N] [--max-scrolls N] [--tab SPEC]`
- `--latest` is X's "Latest" (by date) and the default; `--top` is its
  relevance ranking. `--cap` default 10 (max 50); `--chars` default 1200 per
  text field; `--max-scrolls` default 10 (0 = first render only).
- **`--cap` is a target, not a slice of the first render.** X keeps only 3–9
  articles mounted and recycles the rows as the timeline moves, so the plugin
  wheels the page and merges what mounts, by post id, until it has `--cap`
  posts, X stops yielding, or the budget runs out. Do not hand-roll a
  scroll/extract/dedupe loop; that loop *is* the plugin now.
- Reply: `query`, `query_url`, `sort` (what the page's own tab strip reports as
  selected — no verb can prove a site's ordering), `count`, `truncated`,
  `loading` (`reads`, `scrolls`, `max_scrolls`, `stop` = `cap` | `exhausted` |
  `max-scrolls` | `no-posts` | `scroll-failed`), `posts[{id, handle, url,
  time, text}]`, `note`. `truncated` is true whenever loading stopped short,
  since more posts may exist.
- Classes `read`+`write` (it resolves the tab for a write and sends one wheel
  event); post content is only ever read.
- The browser must be logged in on the tab's profile for anything beyond the
  public wall — seed the managed profile first (see the
  `browser-control-sessions` skill), or pass a `--tab` on an attached session.

## Writing a plugin

A plugin is one Python file exporting `PLUGIN`:

```python
import contextlib

from browser_control import plugin_api

def run(rest, browser):
    rest, cap = plugin_api.pop(rest, "--cap", "mysite search")
    query = plugin_api.text_arg(rest, "mysite search")
    plugin_api.nav("https://mysite.example/", browser=browser)
    with contextlib.suppress(plugin_api.ControlError):
        plugin_api.wait("element", selector=".results", timeout=15,
                        browser=browser)
    data = plugin_api.extract(each=".result", fields=["title=h3", "url=a@href"],
                              cap=int(cap or 10), browser=browser)
    return {"ok": True, "query": query, "count": data["count"],
            "results": data["matches"]}

PLUGIN = {
    "api": 1,
    "name": "mysite",
    "description": "read-only mysite search",
    "actions": {
        "mysite": {                       # the top-level verb
            "run": run,                   # (rest, browser) -> one JSON dict
            "classes": ("read", "write"), # capability classes the gate uses
            "usage": "mysite search QUERY [--cap N]",
        },
    },
}
```

Rules and gotchas:

- `run(rest, browser)` receives argv AFTER the global flags were stripped
  (`--browser`, `--profile`, `--frame`, `--allow`, `--deny`); `rest` is what
  follows the verb word, so `mysite search foo` gives `["search", "foo"]`.
- Import `browser_control.plugin_api` (the supported surface), not
  `browser_control.lib` at large. It exports: `fail`, `ControlError`, `errors`,
  `PLUGIN_API`, `pop`, `switch`, `text_arg`, `int_arg`, `float_arg`, `nav`,
  `wait`, `extract`, plus the interaction verbs `focus`, `type_text`, `press`,
  `click`, `scroll`. (`extract` is the generic `tab extract` engine — a reader
  plugin is usually a thin wrapper; `scroll` sends one real wheel event, which
  is how a lazy or virtualized list is made to render past its first window.)
- Refuse with `plugin_api.fail(plugin_api.errors.ERR_BAD_ARGS, "…")`; use the
  named code constants, never bare strings (the vocabulary is closed and
  scanned).
- `classes` must come from the closed vocabulary `read`, `write`, `code`,
  `file`, `egress`; the gate applies exactly as to built-ins. Declare honestly:
  a plugin that navigates declares `write` even if it only reads.
- Test without installing: `BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins
  browser-control-cli selftest` and then run the action; a broken plugin shows
  up in `plugin_errors` instead of breaking the CLI.
- The brittle part of any plugin is its selector map — keep it at the top of
  the file and expect to update it when the site ships a redesign.
