# Browser Control

Drive **this machine's** Chromium-family browser over CDP — on a managed
profile of its own, from a CLI that prints one JSON object and nothing else.

It is built for callers that cannot look at the screen: an agent, a script, a
test. Every verb proves what it did. Every refusal names its cause. Nothing is
a maybe.

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

## Why browser-control

- **Proof, not promises.** A mutation is never `ok: true` on a bare
  acknowledgement: the read-back is in the reply — the click's `changed`, the
  box's `checked`, the screenshot's own PNG header. When the page cannot
  answer, the reply says so instead of guessing.
- **A browser of its own.** It runs on a managed profile, headless or
  windowed. Your everyday browser is listed and read when you ask; it is
  written only when you attach it.
- **Made to be called.** One verb per process, one JSON object on stdout,
  `ERR[code]` on stderr with exit 2. No prompts, no screen, no parsing prose
  to find out what happened.
- **Records, not code.** `tab extract` turns repeated page items into JSON
  with CSS selectors alone — no JavaScript, no scraping framework, bounded at
  the page so a huge document cannot flood the reply.
- **Sessions when you need them.** Copy your logins into a managed profile,
  run two accounts side by side, or attach a browser you started yourself —
  for tab writes only.
- **One site at a time.** Site plugins add what a page needs: search Google
  by typing at a human cadence, or read X search results across its recycled
  timeline.

## The Manual

The manual lives in [`docs/`](docs/), which is its authoritative source.

- [Getting Started](docs/getting-started.md)

**Using it**

- [Verbs and handles](docs/verbs.md)
- [Extraction](docs/extraction.md)
- [Sessions and profiles](docs/sessions.md)
- [Plugins](docs/plugins.md)

**Trust**

- [What it touches, and what it refuses](docs/trust.md)
- [Refusal codes](docs/refusals.md)

**Design**

- [The design](docs/plan.md)
- [The state of the work](docs/progress.md)

## Install

```bash
python3 -m pip install .
browser-control-cli selftest
```

Requires Linux, Python ≥ 3.11, and `websockets`. See
[Getting Started](docs/getting-started.md) for the live battery, the first
session, and everything else.

## License

MIT — see [LICENSE](LICENSE).
