# Extraction — records, no code

`tab extract` turns a page's repeated items into records, declaratively. It is
a read: CSS selectors only, no JavaScript, classified `read`, so a host running
`--deny code` can still extract.

```console
$ browser-control-cli tab extract --each article \
    --field 'text=[data-testid="tweetText"]' \
    --field time=time@datetime \
    --field url='a[href*="/status/"]@href' --cap 5
{"ok": true, "each": "article", "count": 5, "total": 32, "truncated": true,
 "matches": [{"text": "…", "time": "2026-09-20T07:37:24.000Z",
              "url": "/…/status/…"}, …]}
```

## Field specs

- `NAME=SELECTOR` — innerText of the first match of SELECTOR inside the item.
- `NAME=SELECTOR@attr` — that element's attribute (e.g. `a@href`).
- `NAME=@attr` — the item element's **own** attribute.
- `NAME=:scope` — the item's **own** text (descendants included). `:scope`
  composes the usual CSS way: `:scope@attr` reads the item's own attribute,
  and `:scope h2 a` still names a descendant.

## Bounds

- `--cap N` — records returned; default 10, maximum 500.
- `--chars N` — characters kept per field; default 1000, maximum 20000.
- `--visible` — skip items the page does not render.
- `--unique FIELD` — keep the first record per value of FIELD.
- The whole reply is budgeted at 20000 characters of field text, and values
  are sliced **in the page**, so a huge document cannot flood the reply.
  Long fields mean fewer records before `truncated` flips true.

The reply says what happened: `count` is the records returned, `total` is what
the selector reached, and `truncated` is true when the cap or the budget cut
the list. `count < total` is not an error — it is the cap doing its job.

## What it is not

The values are the page's own DOM text and attributes — the same oracle as
`find` and `text`, with no claim beyond "this is what the page showed". A
selector that stops matching yields `null`, not an error; the selector map is
the part that breaks when a site changes its DOM. `tab extract` sees the top
document plus open shadow roots, not iframes; `tab frames` says which frames
were left out and `--frame` reaches into one.

For multi-page collection, drive it in a loop: `--cap` higher than one window
of results, `tab scroll` to load more, and dedupe what comes back. The shipped
`x` plugin shows the pattern end to end — see [Plugins](plugins.md).
