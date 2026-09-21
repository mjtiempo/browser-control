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
        },
    },
}
```

`run(rest, browser)` receives the argv **after** the global flags were
stripped (`--browser`, `--profile`, `--frame`, `--allow`, `--deny`) and the
value of `--browser`. The verb is what comes after the top-level word — for
`mysite search foo`, `rest` is `["search", "foo"]`. Refuse with
`fail(errors.ERR_BAD_ARGS, …)`, or raise `ControlError`;
`main` maps it to `ERR[code]` and exit 2 like any built-in refusal. Name a
code with the constant, never a bare string: the vocabulary is closed, and the
hermetic check scans `plugins/` for an unregistered one.

The supported surface is `browser_control.plugin_api` (`fail`,
`ControlError`, `errors`, `pop`, `switch`, `nav`, `wait`, `extract`,
`PLUGIN_API`) — import that, not `browser_control.lib` at large. `pop` and
`switch` are the CLI's OWN argv readers: `rest, cap = pop(rest, "--cap",
"mysite search")` and `rest, given = switch(rest, "--latest")` parse a plugin's
arguments the way the built-ins are parsed, missing-value refusal included.
`browser` for `nav`, `dom` for
`wait` and the reads, and especially `dom.extract` — the generic extraction
engine (`--each` + `--field NAME=SELECTOR[@ATTR]`, CSS only, no code) that a
reader plugin is normally a thin wrapper around. The capability classes a
plugin declares must come from the closed vocabulary (`read`, `write`, `code`,
`file`, `egress`), and the gate applies to them exactly as it does to the
built-ins (`--deny read mysite …` refuses `not-allowed`).

## Trust

A plugin is local code running in the CLI's process with the CLI's own access.
The declared classes are **declarations**, not a sandbox — the same stance the
core's own capability table takes. Install plugins the way you install the
tool itself.

## Shipped: `x_reader.py`

Read-only X search, built on `tab extract`:

```bash
BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins browser-control-cli \
    x search '"Pardon Snowden"' --latest --cap 5
```

* `--latest` is X's "Latest" sort (by date); `--top` is its relevance ranking;
  the default is `--latest`.
* `sort` in the reply is what the page's own tab strip reports as selected —
  the adapter's honest answer to "is this sorted by date", since no verb can
  prove a site's ordering.
* The selector map at the top of the file (`article`, `tweetText`,
  `time@datetime`, the status link) is the part that breaks when X changes its
  DOM; that is why it lives here and not in the core.

The browser must be logged in on the tab's profile for anything beyond the
public wall — seed the managed profile with `profile seed` first.
