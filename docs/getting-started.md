# Getting started

`browser-control-cli` drives **this machine's** Chromium-family browser over
CDP, on a managed profile of its own. One verb per process: one JSON object on
stdout, and a refusal is `ERR[code]: message` on stderr with exit status 2.

## Requirements

- **Linux.** The CLI reads `/proc` to verify who holds a DevTools port and who
  runs a profile.
- **Python ≥ 3.11** with `websockets`.
- **A Chromium-family browser on `PATH`** — Chrome, Chromium, Brave, and the
  rest are candidates. The managed profile is keyed by the binary's name.

## Install

```bash
python3 -m pip install .        # the `browser-control-cli` console script
./browser-control-cli selftest  # or straight from the checkout, no install
python3 tests/test_unit.py      # hermetic: no browser needed
python3 tests/live_test.py      # the live battery: needs a browser
```

`selftest` is the machine-readable answer to "what is this install":
interpreter, `websockets` version, profile root, action-log path, the browsers
it found, the verbs it has, the plugins that loaded, the capability classes
(both ways round), and the policy in force. It is the one verb that is never
gated.

```bash
browser-control-cli selftest | jq '{ok, browsers, plugins, policy}'
browser-control-cli selftest --classes   # what every action may reach
```

## First session

```console
$ browser-control-cli open --headless https://example.com
{"ok": true, "started": true, "browser": "/usr/bin/google-chrome-stable",
 "profile": "…/cdp-profiles/google-chrome-stable", "port": 39911, …}

$ browser-control-cli tab text --tab active --chars 60
{"ok": true, "text": "Example Domain\nThis domain is for use in illustrative…", …}

$ browser-control-cli tab click "Learn more" && browser-control-cli close --force
{"ok": true, "clicked": true, "element": {…}, "changed": true, …}
{"ok": true, "stopped": true, "pid": 334917, "tabs": 1, "forced": true}
```

`open` starts the browser — or adopts one that is already running — and
`--headless` gives it no window. While exactly one page tab is open, page
verbs address it without a `--tab`; otherwise name the tab (`active`,
`id:<prefix>`, or a title/URL substring). `close --force` stops the browser
this CLI manages; it never touches your everyday one.

## Where next

- [The CLI reference](verbs.md) — every verb, flag, and handle.
- [Extraction](extraction.md) — turn repeated page items into records.
- [Sessions and profiles](sessions.md) — logged-in browsers, multiple accounts.
- [Plugins](plugins.md) — site actions such as Google-by-typing and X search.
- [Trust](trust.md) — what it touches, what it refuses, and how to gate it.
- [Refusal codes](refusals.md) — what `ERR[code]` means and what to do.

The design lives in [The design](plan.md) and the state of the work in
[Progress](progress.md).
