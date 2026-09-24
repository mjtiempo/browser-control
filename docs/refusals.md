# Refusal codes

A refusal is data. `browser-control-cli` writes `ERR[code]: message` to
stderr and exits 2; the message names the cause and, when known, the fix.
Branch on `code` — it is a closed vocabulary, and every code the tool may
speak is registered in `browser_control/lib/errors.py`.

The codes below are grouped by cause. `selftest | jq .policy` shows the
capability gate in force, and [Trust](trust.md) explains the classes.

## Install and environment

| code | meaning |
| --- | --- |
| `no-websockets` | the `websockets` package is missing — no verb can speak CDP; reinstall the tool |
| `no-browser` | nothing drivable on this machine — run `open`, or install a Chromium-family browser |
| `internal` | a bug in the tool, not your call — include the message in a report |
| `broken-pipe` | stdout closed early (the caller went away); not actionable |
| `unknown-command` | verb not found — typo, or a plugin that is not loaded (`selftest`) |
| `bad-args` | argument shape/syntax wrong; the message says which flag |

## Browser lifecycle

| code | meaning |
| --- | --- |
| `launch-failed` | the browser was started but never answered CDP; the failed start is stopped and named by pid |
| `browser-not-stopped` | SIGTERM did not end the process; it is reported (by pid), never hunted |
| `cdp-unreachable` | the endpoint did not answer — including a port that lost its holder between the check and the connection (a re-judged port nobody holds any more) |
| `cdp-not-local` | the DevTools port is held by something that is not that profile's browser; `close --force` then `open` |
| `profile-busy` | another call holds the profile lock; wait for it and retry |
| `profile-live` | a browser is running on the profile — close it first |
| `profile-unusable` | the profile directory cannot be created/read (permissions), or its lock cannot be opened/taken at all |
| `profile-exists` | `profile seed` target already exists — pass `--force` (it wipes) or pick another |
| `not-managed` | a write needs a browser this CLI started/attached — `open`, `attach`, or read with `--tab` |
| `not-attached` | `detach` found no attachment for that selector — check `attach --list` |
| `attach-failed` | `attach`/`detach` could not write `attached.json` (a leftover `.new` scratch file, or an unwritable path) |
| `tabs-open` | `close` refused because the browser holds page tabs — `--force` only for a browser YOU opened |
| `close-tab-not-verified` | the tab was still there after `tab close` |
| `ambiguous-browser` | `--browser NAME` matched several — use a fuller name |

## Tab and frame addressing

| code | meaning |
| --- | --- |
| `no-page-tab` | no page tab matches (or none exist) — `open URL` / `tab URL`, or a different `--tab SPEC` |
| `tab-ambiguous` | several tabs match the spec — use `id:<prefix>` or a longer substring |
| `no-frame` | `--frame VALUE` matched no iframe — run `tab frames` |
| `frame-ambiguous` | `--frame` matched several frames — when the frames have different URLs, use an index or a fuller URL; when several frames share one URL (and the browser gives their targets the same URL), they cannot be told apart from here: reach the one you mean by COORDINATE (`tab click --at X,Y`) or drive it from the tab that owns it |
| `frame-not-separate` | that frame has no target to attach to: it shares the page's process (a same-origin/`srcdoc` frame), or its committed URL differs from the `src` (a redirect). `tab text`/`tab extract --frame N` read a same-process one; input needs `tab click --at X,Y` |
| `frame-unattributable` | the browser does not report which tab owns an iframe target, so a SEPARATE frame cannot be reached — a same-process frame still reads (`tab text --frame N`); run `tab frames` and re-name it |
| `no-viewport` | the page reports a zero viewport (windowless/never shown); nothing can be measured |
| `no-viewport-target` | no target in the browser has a viewport |

## Element and page resolution

| code | meaning |
| --- | --- |
| `no-match` | nothing matched the TEXT/selector/spec — check with `tab text`/`tab find`; for `select`, the available option labels are listed |
| `ambiguous-element` | several elements match — add `--index N` or tighten the selector |
| `ambiguous-option` | several `<option>`s match `--value` — use the exact value, not the label |
| `not-checkable` | the target is not a checkbox/radio |
| `not-a-select` | the target is not a single `<select>` (a multiple select needs `tab js`) |
| `no-focus` | `insert`/`type` with nothing editable focused — `tab focus` first |
| `no-dialog` | nothing to accept/dismiss |
| `no-file` | the file a verb was given does not exist (upload/seed source) |
| `file-exists` | `tab screenshot` will not overwrite an existing path — pass `--force` or pick another |
| `no-media` | no video/audio element on the page |
| `occluded` | the element's centre is covered — scroll it into view, use `--at`, or pick another target |

## Verification failures (the mutation did not take)

| code | meaning |
| --- | --- |
| `activate-not-verified` | the page still reports `hidden` after bring-to-front (hidden window/workspace) |
| `check-not-verified` | the control's `checked` did not reach the wanted state |
| `dialog-not-verified` | accept/dismiss was sent but the tab never answered again (another dialog, or a spinning script) |
| `focus-not-verified` | the element did not become `document.activeElement` |
| `hover-not-verified` | the element did not report `:hover` |
| `insert-not-verified` | the field did not grow after `Input.insertText` |
| `type-not-verified` | the field did not grow after per-character typing |
| `select-not-verified` | the control's value/index did not reach the wanted option |
| `scroll-not-verified` | nothing moved, the edge was not reached, or the element did not become visible |
| `screenshot-not-verified` | the PNG bytes did not match the page's geometry, or the file could not be read back |
| `upload-not-verified` | `input.files` did not take the file |
| `media-not-verified` | `play` did not advance the clock / `pause` left it playing |
| `media-blocked` | `play()` was rejected or blocked (autoplay policy, no supported source) |
| `reload-not-verified` | no new document within the timeout |
| `nav-not-verified` | the address did not change (e.g. a beforeunload prompt) — `tab dialog state` |
| `seed-not-verified` | `profile seed` could not read back what it copied |
| `seed-failed` | `profile seed` could not copy the stores at all (missing source, permissions) |
| `reset-not-verified` / `reset-failed` | `profile reset` did not complete/verify |
| `write-failed` | the filesystem refused the write (screenshot path, seed target) |

## Navigation

| code | meaning |
| --- | --- |
| `nav-failed` | the browser could not load the URL: DNS, refused connection, cert problem, or the URL became a download |
| `cdp-error` | the browser's protocol returned an error; the message carries the browser's own reason |

## Waits and JavaScript

| code | meaning |
| --- | --- |
| `wait-timeout` | the predicate never passed before `--timeout`; the message names it and the samples taken |
| `eval-timeout` | an evaluation did not return in budget — often a parked renderer; `tab dialog state`, then `tab nav` |
| `blocked` | the tab is parked (a dialog, or a script that does not yield); enabling domains/reading blocks on it |
| `js-error` | your `tab js`/`--expr` threw in the page |
| `result-too-large` | your JavaScript value is too big for a reply — narrow it in the page |

## Policy

| code | meaning |
| --- | --- |
| `not-allowed` | the capability gate refused this call; `selftest.policy` shows what is in force, and `selftest --classes` prints what every action may reach |
