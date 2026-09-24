# The CLI reference

One verb per process. **stdout** carries one JSON object; **stderr** carries
`ERR[code]: message` and the process exits 2 on a refusal. Success exits 0.
Long-running calls are fine (`tab wait --for load --timeout 30`), and anything
that blocks is bounded and named. `browser-control-cli --help` lists every flag
on the command line.

## Browser level

| verb | what it does |
| --- | --- |
| `open [URL…] [--headless]` | start — or adopt — the managed browser; each URL opens as a tab |
| `close [--force] [--port N \| --pid N \| --profile DIR]` | stop the managed browser, or the one NAMED |
| `list` | every Chromium-family browser running here, ours or not |
| `info` | the browser this CLI would drive, and its endpoint |
| `attach …` / `attach --list` / `detach …` | grant or revoke tab writes on a browser it did not start |
| `profile info` | managed profiles: weight, age, running, attached |
| `profile logins` | what logins are in a profile — hosts, names, counts, never values |
| `profile seed` | copy a source profile's logins into a managed one |
| `profile reset` | wipe a managed profile, logins included |
| `selftest` | prove the install, and report the policy in force; one of the TWO verbs the gate never consults |
| `help` (`-h` / `--help`) | this text plus the plugin usage lines; never gated like `selftest`, and still audited |

## Page level — `tab`

| verb | what it does |
| --- | --- |
| `tab [URL…]` | open one tab per URL |
| `tab list` | every drivable browser's page tabs, by browser |
| `tab info SPEC` | one tab: id, title, url, index |
| `tab close …` | close every tab named, verified as a set (`--dry` resolves only) |
| `tab nav URL` | navigate, then read the address back |
| `tab back` / `forward` / `reload` | history and reload, verified by the address or a new document |
| `tab activate [SPEC]` | bring a tab forward (it raises its window) |
| `tab frames` | this page's iframes, and which of them can be driven |
| `tab find TEXT \| --selector CSS` | a visible element, with its geometry |
| `tab text [--selector CSS]` | the rendered text, truncated in the page |
| `tab extract …` | repeated items as records — see [Extraction](extraction.md) |
| `tab js EXPR` | the escape hatch: the page's own value, unverified |
| `tab wait --for load\|idle\|element\|js` | poll one predicate to a wall-clock deadline |
| `tab click` / `hover` | real input (CDP) at an element or a point |
| `tab check` / `select` | drive a checkbox or a `<select>` and read it back |
| `tab scroll` | a wheel event, a wheel to the edge, or reveal one element |
| `tab focus` / `press` / `insert` / `type` | put the caret, send keys, write text |
| `tab upload FILE` | attach a file to an `<input type=file>` |
| `tab screenshot PATH` | write a PNG vouched by its own header |
| `tab dialog state\|accept\|dismiss` | read or answer a JavaScript dialog |
| `tab media state\|play\|pause` | read or drive the page's video/audio |

## Handles and scope

- `--tab SPEC` addresses one tab: `id:<prefix>`, the reserved word `active`
  (the tab whose page reports itself visible **among the browsers this CLI
  drives**), or a title/url substring. Ambiguity refuses and names the
  candidates — no verb ever picks among tabs nobody named.
- Without `--tab`, a page verb acts on the **only** page tab in a browser this
  CLI drives. Several tabs refuse `tab-ambiguous`.
- `--browser NAME` picks the browser (its executable, or the profile's name).
  With no flag, a live managed browser is used.
- `--profile DIR` picks the **instance**: a profile directory under the root,
  which is how two instances of one browser — two logins, two sessions — are
  addressed, on every verb.
- `--frame VALUE` acts inside one iframe, by an index from `tab frames` or a
  URL substring. A cross-origin frame is a target of its own, so every content
  verb works inside it. A **same-process** frame has no target of its own (a
  same-origin or `srcdoc` one), so the reads that need none — `tab text`,
  `tab extract` — take its text through the page itself, and the reply's
  `frame_resolved.same_process` says that is what answered; input still needs a
  target (`tab click --at X,Y`), and `find`/writes refuse
  `frame-not-separate`. `nav`, `back`, `forward`, `reload`, `list`, `frames`,
  `info`, `close` and `activate` act on the tab.
- `--allow CLASSES` / `--deny CLASSES` gate the call by capability class; see
  [Trust](trust.md) for the classes and the session-wide environment
  equivalents.
- `tab close` takes the same SPEC, or `--like V` (a declared substring sweep),
  `--title V` / `--url V` (exact), `--all [--except SPEC…]`, and `--dry`
  (resolve and report `would_close`, close nothing).

## Writing a caller

- Read **stdout only on exit 0**; it always holds exactly one JSON object.
  On exit 2, read stderr for `ERR[code]` and branch on the code, not the
  message.
- A mutation's read-back is inside the reply (`clicked`, `changed`, `checked`,
  `selected`, `moved`, `visibility`, `verified`), so a caller never has to
  follow up to know whether it worked — see [Trust](trust.md).
- Anything that blocks is bounded and named: `eval-timeout`, `blocked` (a
  parked renderer, with the dialog named when there is one), `profile-busy`,
  `wait-timeout`.
- Reads see the top document and open shadow roots; `tab frames` says which
  frames were skipped, `--frame` reaches into a cross-origin one, and a
  same-process frame's text reads with `--frame` too — no `tab js`, so no
  `code` capability is needed for it.

Every refusal code, grouped by cause, is in [Refusal codes](refusals.md).
