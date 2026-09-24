---
name: browser-control-extract
description: Scrape structured records from web pages with browser-control-cli — repeated items into JSON with tab extract, full rendered text, element search, iframe traversal, and pagination loops. Use when collecting data (lists, tables, search results, feeds, catalogs) into records rather than driving a single page interaction.
---

# Structured extraction with browser-control-cli

This skill covers turning pages into records. It builds on the `browser-control`
skill: the lifecycle, handles (`--tab`, `--frame`, `--browser`, `--profile`),
capability classes, and refusal codes are documented there and in its
`references/verbs.md` / `references/errors.md`.

The rule that keeps extraction honest: **values are the page's own DOM text and
attributes.** A selector that stops matching yields `null`, not an error; the
selector map is the part that breaks when a site changes its DOM.

## Pick the right read

| need | use |
| --- | --- |
| the page's rendered text | `tab text [--selector CSS] [--chars N]` |
| locate a visible element, with geometry | `tab find TEXT \| --selector CSS [--cap N]` |
| one repeated item → one record | `tab extract --each CSS --field NAME=SPEC …` |
| anything the above cannot express | `tab js EXPR` (classes `code`+`write`; last resort, see below) |

`tab text` and `tab extract` see the top document **plus open shadow roots**,
not iframes. `tab frames` lists iframes; `--frame <index|url-substring>` acts
inside one (cross-origin included).

## `tab extract` — records, no code

```console
$ browser-control-cli tab extract --each '#posts article' \
    --field 'text=.body' \
    --field 'time=time@datetime' \
    --field 'url=a@href' \
    --field 'author=h2 a' --cap 5
{"ok": true, "each": "#posts article", "fields": ["text","time","url","author"],
 "count": 4, "total": 4, "truncated": false,
 "matches": [{"text": "First post body", "time": "2026-01-02T03:04:05.000Z",
              "url": "/alice/status/101", "author": "Alice"}, …], …}
```

Field spec:

- `NAME=SELECTOR` — innerText of the first match of SELECTOR **inside** the item.
- `NAME=SELECTOR@attr` — that element's attribute (e.g. `a@href`, `time@datetime`).
- `NAME=@attr` — the item element's **own** attribute (e.g. `--field url=@href`).
- `NAME=:scope` — the item's own text (descendants included). `:scope` composes
  the usual CSS way: `NAME=:scope@attr` reads the item's own attribute, and
  `NAME=:scope h2 a` still names a descendant. The plain `NAME=@attr` form
  reads an attribute of the item without a selector at all.

Bounds (all applied **in the page**, so a huge document cannot flood the reply):

- `--cap N` — records returned; default 10, maximum 500.
- `--chars N` — chars kept per field; default 1000, maximum 20000.
- `--visible` — skip items the page does not render (display/visibility/size).
- `--unique FIELD` — keep the first record per value of FIELD.
- whole-reply budget: 20000 chars across fields. Long fields mean fewer records
  before `truncated` flips true; lower `--chars` to fit more records.

Reply: `each`, `fields`, `count` (records returned), `total` (items the selector
reached), `truncated` (cap, budget, or `--unique` dropped records), `matches[]`.
`count < total` is not an error — it is the cap.

Classes: `read` only. A `--deny code` policy can still extract — prefer it over
`tab js` for exactly that reason.

### Worked shapes

The item's own text and its own attribute:

```bash
browser-control-cli tab extract --each 'article.card' \
  --field 'text=:scope' \
  --field 'id=:scope@data-id' --cap 50
```

Rows of a table:

```bash
browser-control-cli tab extract --each 'table#results tbody tr' \
  --field 'name=td:nth-child(1)' \
  --field 'status=td.status' \
  --field 'link=td:nth-child(1) a@href' --cap 100
```

Cards, with the match's own attributes:

```bash
browser-control-cli tab extract --each 'article.card' \
  --field 'href=@data-href' \
  --field 'title=h2' \
  --field 'price=.price' \
  --field 'img=img@src' --visible --cap 50
```

X/Twitter-style items (the shipped `x_reader` plugin is a thin wrapper around
this):

```bash
browser-control-cli tab extract --each article \
  --field 'text=[data-testid="tweetText"]' \
  --field 'time=time@datetime' \
  --field 'url=a[href*="/status/"]@href' --cap 20
```

## Pagination

Everything is one-shot per call; loop in the shell (each call is one process).

**URL pagination** — the cleanest when the site accepts `?page=N`:

```bash
: > /tmp/rows.jsonl
for p in 1 2 3 4 5; do
  browser-control-cli tab nav "https://example.com/list?page=$p" >/dev/null
  browser-control-cli tab wait --for idle --timeout 15 >/dev/null || true
  browser-control-cli tab extract --each 'table tr' \
      --field 'name=td:first-child' --field 'url=a@href' --cap 200 \
    | jq -c '.matches[]' >> /tmp/rows.jsonl
done
jq -s 'unique' /tmp/rows.jsonl
```

**Infinite scroll** — stop when the page stops moving. `tab scroll --edge
bottom` refuses `scroll-not-verified` if it cannot reach the bottom, so allow
failure and break on `moved:false`:

```bash
: > /tmp/rows.jsonl
for i in $(seq 1 30); do
  browser-control-cli tab extract --each 'article' \
      --field 'text=.body' --field 'url=a@href' --cap 200 \
    | jq -c '.matches[]' >> /tmp/rows.jsonl
  moved=$(browser-control-cli tab scroll --edge bottom 2>/dev/null \
          | jq -r '.moved // "false"')
  [ "$moved" = "true" ] || break   # nothing moved (or it refused): the edge
  sleep 1                            # let the next page land
done
jq -s 'unique' /tmp/rows.jsonl
```

**Next button** — click it, wait for the DOM to settle, repeat:

```bash
for i in $(seq 1 10); do
  browser-control-cli tab extract --each 'article' --field 'url=a@href' --cap 100 \
    | jq -c '.matches[]' >> /tmp/rows.jsonl
  browser-control-cli tab click "Next" >/dev/null || break
  browser-control-cli tab wait --for idle --timeout 15 >/dev/null || true
done
jq -s 'unique' /tmp/rows.jsonl
```

`tab wait --for idle` waits until no resource finished in the last
`--idle-ms` (default 500 ms). For a list that renders after XHR, wait for the
element that proves it: `tab wait --for element --selector '.row' --timeout 10`.

## Frames

```bash
browser-control-cli tab frames | jq '.frames[] | {index, url, same_process}'
browser-control-cli tab extract --frame 2 --each 'tr' --field 'v=td' --cap 100
browser-control-cli --frame 'checkout' tab text --selector 'h1'
```

A cross-origin frame is a target of its own: every content verb (`text`, `find`,
`extract`, `click`, `screenshot`, …) works inside it with `--frame`. A
**same-process** frame (`same_process: true`, no target) has its document read
by `--frame` too — `text`/`extract` are rooted there instead of attaching —
while `find`/`click` still need a target (`click --at X,Y`). `nav`,
`back`, `forward`, `reload`, `list`, `frames`, `info`, `close`, `activate` act
on the tab and ignore frames.

## When you must use `tab js`

Only for what CSS cannot express (computed styles, canvas pixels, values the
DOM does not expose). It is class `code`, can write, and the reply's `value` is
the page's own value — a JSON string stays a string:

```bash
browser-control-cli tab js "JSON.stringify(Array.from(document.querySelectorAll('article'), el => ({text: el.innerText, href: el.querySelector('a')?.href})))" \
  | jq -r '.value | fromjson'
```

Keep the expression small; a big result refuses `result-too-large`. If a page
blanket-overrides DOM built-ins, a JS read can be shadowed too — that is the
same oracle caveat as everywhere else.

## Ground rules

- Open once, extract many times: `open --headless <url>` then repeated
  `tab extract`/`tab nav`. Close when done (`close --force`).
- Never trust one read for "everything": compare `count`/`total`/`truncated`,
  and re-read `tab frames` when a page looks emptier than expected.
- Keep selectors in one place (a variable or a small script) — that is the part
  that breaks when the site changes, not the CLI.
