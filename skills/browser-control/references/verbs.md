# browser-control-cli — verb reference

Every invocation is ONE verb. Global flags may appear anywhere in the argument
list. Output: one JSON object on stdout; refusal `ERR[code]: message` on
stderr with exit 2. `browser-control-cli --help` prints the same surface.

## Global flags

- `--browser NAME` — the browser to drive (open/close/tab) or to narrow
  (`info`, `tab list`, and the instance verbs `profile logins`/`profile seed`/
  `profile reset`). `profile info` refuses it: its report covers every managed
  profile, so the flag cannot narrow it. A binary name
  (`google-chrome-stable`) or a managed profile name. Default: a live managed
  browser, else the first Chromium-family binary on PATH.
- `--profile DIR` — the INSTANCE: a profile directory under the root. This is
  how two instances of ONE browser (two logins, two sessions) are addressed, on
  every verb.
- `--frame VALUE` — the frame inside the tab: a URL substring, or an index from
  `tab frames`. A cross-origin frame is a target of its own, so every verb that
  acts on a page's CONTENT works inside it. Not `nav|back|forward|reload|list|frames|info|close|activate`,
  which act on the tab.
- `--allow CLASSES` / `--deny CLASSES` — capability classes (`read`, `write`,
  `code`, `file`, `egress`, or `*`). A verb is refused `not-allowed` when ANY of
  its classes fails the policy. `BROWSER_CONTROL_ALLOW` /
  `BROWSER_CONTROL_DENY` set the same for a session.
- `--tab SPEC` — the tab a page verb acts on. See "Handles".

## Handles (SPEC)

- `id:<prefix>` — a CDP target id prefix from `open` / `tab list` / `tab info`.
- `active` — the tab whose page reports itself visible (only one per window).
- any other string — a title or URL **substring**; ambiguous or empty matches
  refuse (`tab-ambiguous` / `no-page-tab`) and name the candidates.

Without `--tab`, a page verb acts on the ONLY page tab in a browser this CLI
**drives** (managed or attached). Several tabs refuse `tab-ambiguous`.

## Browser level

### `open [URL...] [--headless]`

Start (or adopt) the managed browser. Each URL opens (first is the startup page
on a fresh start; the rest become tabs). A fresh `open` with no URL opens
`about:blank`.

Reply: `started` (true = this call started it, false = adopted), `browser`
(path), `profile`, `headless` (the PROCESS's actual mode), `port`, `pid`,
`tabs`, `opened` (`requested`/`id`/`url`/`title`), and `tab: "id:…"` when
exactly one tab was opened. `warning` when a stale port file was ignored.

The whole check-and-act is under the profile lock, so two concurrent `open`s
cannot double-start: the loser adopts or refuses `profile-busy`. `--headless`
uses `--headless=new`; the mode belongs to the process, and asking for headless
while a windowed managed browser is up refuses.

### `close [--force] [--port N | --pid N | --profile DIR]`

Stop the managed browser this CLI started — or the browser NAMED (that is how
another tool's browser is stopped too; the name is the consent). `--force` is
needed when page tabs are open (`tabs-open` otherwise), because Chromium exits
with its last window. Nothing is ever SIGKILLed; a process that ignores SIGTERM
is reported (`browser-not-stopped`), not hunted.

Reply: `stopped`, `pid`, `profile`, `tabs`, `forced`, `named`, `managed`.

### `list`

Every Chromium-family browser running on the machine, ours or not. Reply:
`count`, `drivable`, `browsers[]` with `pid`, `exe`, `path`, `profile`,
`managed`, `attached`, `headless`, `cdp` (`port`, `reachable`, `verified`).

### `info`

The browser this CLI would drive and its endpoint.

### `attach [--port N | --pid N | --profile DIR]` / `attach --list`

Make a browser this CLI did NOT start writable — **tab writes only** (`close`
never stops an attached browser). Verification is the browser's own command
line naming the profile (`--user-data-dir=<profile>`); a browser started on its
vendor default profile cannot be verified and refuses `cdp-not-local`. (An
`attach-failed` is a different failure: the attachment record could not be
written.)

`attach --list` shows every attachment and whether it is still up. `--profile`
cannot combine with `--list`.

### `detach [--port N | --pid N | --profile DIR | --all]`

Revoke that authorization.

### `profile info [--profile DIR]`

The managed profiles: weight, age, whether a browser is on one, whether it is
attached.

### `profile logins [--profile DIR] [--site HOST] [--cap N]`

What logins are IN a profile — read from a COPY of the profile's own `Cookies` /
`Login Data` files, without opening a browser and without reading values. Only
hosts, cookie NAMES, expiry and counts are reported; never a cookie value, a
username, or a password. `--site x.com` narrows by host suffix. A store that
cannot be read is `readable: false` with the reason in `stores.<name>.error`
(unknown, not zero). `snapshot: true` says a browser is running on the profile,
so disk may lag it.

### `profile seed --from DIR [--force] [--dry]`

Copy a source profile's LOGINS into a managed one (no caches, no lock files),
then read back what landed. `DIR` is one Chrome profile (`.../Default`,
`Profile 1`) or a whole user-data directory. An existing target refuses
`profile-exists` unless `--force`, and `--force` WIPES the target first — the
result is the source's content, never a mix. `--dry` reports what would land
and what `--force` would destroy, and writes nothing.

### `profile reset [--force]`

Wipe a managed profile, logins included.

### `selftest`

Prove the install: interpreter, `websockets`, profile root, log file, verbs,
capability classes (`capabilities.by_class`, `capabilities.unclassified`),
policy in force (`policy`), browsers found, loaded `plugins` and
`plugin_errors`. Never gated.

## Tab level

### `tab [URL...]`

Open one tab per URL in the running browser (`about:blank` when none).

### `tab list`

Every drivable browser's page tabs, grouped by browser.

### `tab info SPEC`

One tab: `{"ok": true, "tab": {"id", "title", "url", "index"}, "browser": …}`.

### `tab close SPEC... | --like V | --title V | --url V | --all [--except S...] [--dry]`

- A `SPEC` names a tab EXACTLY (whole URL, whole title, or `id:<prefix>`).
- `--like V` sweeps substrings; `--title V` / `--url V` match exactly.
- `--all` names every page tab this CLI drives; `--except SPEC` keeps tabs
  (loose match; implies `--all`).
- `--dry` resolves and reports `would_close`, closing nothing.

### `tab nav URL [--tab SPEC]`

Navigate and read the address back. Reply: `url` (requested), `url_read`
(observed), `loaded`, `moved` (`null` when there was no before-oracle).
`Page.navigate` runs no page JavaScript, so this is the way OUT of a parked
tab. A download, DNS failure, refused connection or cert problem refuses
`nav-failed`; a beforeunload prompt leaves `nav-not-verified`.

### `tab back | forward | reload [--tab SPEC]`

History moves are CDP's own (`Page.navigateToHistoryEntry`), verified by the
address changing (`url_before`/`url_read`) or, for reload, a NEW document
(`performance.timeOrigin`).

### `tab activate [SPEC]`

Bring a tab to the front (raises its window). Verified by the page's own
`document.visibilityState`; still `hidden` refuses `activate-not-verified`
(another workspace / iconified window).

### `tab frames [--tab SPEC]`

This page's iframes and which can be driven. Reply: `count`, `frames[]`
(each with `index`, `url`, `name`, `same_process`, `box`, `visible`),
`separate` (cross-origin targets of their own), `note`.

### `tab find TEXT | --selector CSS [--cap N] [--tab SPEC]`

Visible interactive/labelled elements with geometry. Reply: `query`, `total`,
`offscreen`, `truncated`, `viewport`, `matches[]`:
`tag`, `role`, `name`, `type`, `href`, `text`, `in_viewport`, `clipped`,
`box` (page coords), `center`, `viewport` (view coords), `point`, `hit`,
`hit_element`. Nothing matches → `no-match` naming the readyState and candidate
count. Default cap 10.

### `tab text [--selector CSS] [--chars N] [--tab SPEC]`

The rendered text (default body), truncated IN the page. Reply: `text`,
`length` (full length), `truncated`, `selector`, `found`. Default cap 40000.

### `tab extract --each CSS --field NAME=SPEC … [--cap N] [--chars N] [--visible] [--unique FIELD] [--tab SPEC]`

Repeated items as records. `NAME=SELECTOR` (innerText), `NAME=SELECTOR@attr`
(attribute), `NAME=@attr` (attribute of the match itself), `NAME=:scope` (the
match's own text — **broken in current Chrome**, see the extract skill). CSS
only, no code; capability class `read`.

Reply: `each`, `fields`, `count`, `total`, `truncated`, `matches[]` (one object
per item). Defaults: cap 10 (max 500), 1000 chars/field (max 20000), whole
reply budget 20000 chars, all sliced in-page. `--visible` skips unrendered
matches; `--unique FIELD` keeps the first of duplicates.

### `tab js EXPR [--tab SPEC]`

Run one expression in the page. Capability class `code`; can WRITE, so it needs
a managed/attached browser. Reply: `value` (the page's OWN value — a string
that parses as JSON is still that string), `verified: false`, `note`. Use
`({a: 1})` for an object; `JSON.stringify(...)` only when the string is wanted.

### `tab wait --for load | idle | element | js [--selector CSS] [--expr EXPR] [--timeout S] [--idle-ms MS] [--tab SPEC]`

Poll ONE predicate to a wall-clock deadline. `--for element` requires
`--selector`; `--for js` requires `--expr` and is class `code`. Reply: `for`,
`waited_s`, `samples`. Deadline missed → `wait-timeout` naming what and how
long. Default timeout 15 s; `--idle-ms` default 500.

### `tab click TEXT | --selector CSS [--index N] [--tab SPEC]` / `tab click --at X,Y [--tab SPEC]`

Real input (CDP) at the element's centre. Reply: `clicked`, `changed`, `under`,
`element`, `point`, `before`/`after` (url/title/scroll), `note`. A covered
centre refuses `occluded` naming what it hit. `--at X,Y` is a POINT (mutually
exclusive with TEXT/`--selector`/`--index`) for what a selector cannot name; it
reports `verified: false` with what the point reaches.

### `tab hover TEXT | --selector CSS [--index N] [--at X,Y] [--tab SPEC]`

Put the pointer on the target or point. Reply: `hovered`, `verified`, `under`,
`chain`.

### `tab check TEXT | --selector CSS [--index N] [--uncheck] [--tab SPEC]`

Check a checkbox/radio with real input. Reply: `checked`, `changed`, `verified`,
`checked_before`, `element`, `point`. Already in the wanted state → no click,
`changed: false`. Non-checkable → `not-checkable`.

### `tab select TEXT | --selector CSS --value V [--index N] [--tab SPEC]`

Choose one `<option>` with real arrow keys (the page's `change` handler sees
`isTrusted: true`). `--value` matches the option's value first, then its exact
label; the reply's `by` says which. Reply: `selected`, `changed`, `verified`,
`trusted`, `value`, `label`, `option_index`, `options`, `keys`,
`value_before`. Not a single select → `not-a-select` (a multiple select needs
`tab js`). No match → `no-match` with the available labels; several → `ambiguous-option`.

### `tab dialog [state | accept | dismiss] [--text VALUE] [--tab SPEC]`

`state` (default) reports `open` (`true`/`false`), `verified`, and `open: null`
when the tab cannot report — unreadable is NOT absent. `accept`/`dismiss` are
verified by the tab answering again; `--text` answers a prompt and goes only
with `accept`. Nothing to handle → `no-dialog`.

### `tab screenshot PATH | --path PATH [--full] [--force] [--tab SPEC]`

Write a PNG. Reply: `path`, `bytes`, `width`, `height`, `full`, `dpr`,
`css_size`, `verified`, `note`. Dimensions come from the file's own IHDR, so
`verified` is page-independent. Existing path → `file-exists` unless `--force`.

### `tab scroll --by N [--at X,Y] [--tab SPEC]` / `--edge top|bottom` / `TEXT | --selector CSS [--index N]`

One real wheel event (`--by`), wheel until the document stops (`--edge`), or
`DOM.scrollIntoViewIfNeeded` (element). Reply: `moved` or `revealed` +
`element`, `in_viewport`, `nested` scroller when one moved. Refuses
`scroll-not-verified` when nothing moved / the edge was not reached.

### `tab focus TEXT | --selector CSS [--index N] [--tab SPEC]`

Put the DOM focus (caret) on an element — the prerequisite for `insert`,
`type`, `press`. Works on background tabs; scrolls the element into view.
Reply: `focused`, `verified`, `element`, `active`.

### `tab press KEY [--tab SPEC]`

One key event at the focus (`enter`, `tab`, `escape`, arrows, …). Reply: `key`,
`target: "page"`, `verified: false` — read the effect yourself.

### `tab insert TEXT [--tab SPEC]`

Insert TEXT atomically at the focus (one `Input.insertText`, the way an IME
does). Refuses `no-focus` when nothing editable is focused. Reply: `verb`,
`chars`, `verified`, `length_before`/`length_after`, `active`. The text is never
echoed.

### `tab type TEXT [--tab SPEC]`

Type TEXT as real per-character key events — use when the page's own key
handlers must run. Same reply shape as `insert` with `verb: "type"`.

### `tab upload FILE [--selector CSS] [--index N] [--tab SPEC]`

Attach a file to an `<input type=file>` (default selector `input[type=file]`).
Reply: `file`, `verified`, `files` read back from the input. Class `file`.

### `tab media state | play | pause [--index N] [--tab SPEC]`

Read or drive the page's video/audio. `state` is a read; play/pause verify by
the CLOCK advancing (or the page reporting paused). Reply: `mode`, `element`,
`count`, `playing`, `paused`, `ended`, `muted`, `volume`, `rate`, `time`,
`duration`, `ready_state`, `src`, plus `time_before`/`advanced` for actions.
`media-blocked` names a rejected `play()` (autoplay policy, no source).

## Field spec (tab extract)

`NAME=SELECTOR` — innerText of the first match of SELECTOR inside the item.
`NAME=SELECTOR@attr` — that element's attribute.
`NAME=@attr` — the item element's own attribute.
`NAME=:scope` — the item's own text; `NAME=:scope@attr` its own attribute
(`:scope` composes like any CSS selector: `:scope h2 a` is a descendant).

A selector may contain `@` (e.g. `[href*="@"]`); only a trailing `@name` is
read as an attribute.

## Environment

- `BROWSER_CONTROL_ROOT` — managed profile root (default
  `~/.local/share/browser-control/cdp-profiles`).
- `BROWSER_CONTROL_LOG` — JSONL action log (default
  `~/.local/state/browser-control/actions.jsonl`; `off` disables).
- `BROWSER_CONTROL_ALLOW` / `BROWSER_CONTROL_DENY` — session policy gate.
- `BROWSER_CONTROL_PLUGIN_PATH` — colon-separated plugin dirs; when set it
  REPLACES the default `~/.local/share/browser-control/plugins/`.
