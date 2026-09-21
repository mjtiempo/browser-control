"""x-reader — read X (Twitter) search results through the core's verbs.

A READ-ONLY plugin: it navigates one tab, waits for the page, and uses
`tab extract` — the core's generic, declarative extraction engine — with X's
selector map. It runs no page JavaScript of its own and never writes, clicks or
types.

Install: copy this file into ``~/.local/share/browser-control/plugins/`` (or
any directory in ``BROWSER_CONTROL_PLUGIN_PATH``). The browser must be logged
in on the tab's profile for anything beyond the public wall — seed the managed
profile with ``profile seed`` first.

    browser-control-cli x search '"Pardon Snowden"' --latest --cap 5

The selector map below is the part that breaks when X's DOM changes, so it is
kept in one place at the top. `total`/`truncated` come from the extraction;
`sort` reports what the page's own tab strip says is selected, because no verb
can PROVE a site's ordering.
"""
from __future__ import annotations

import contextlib
import re
import urllib.parse

from browser_control import plugin_api
from browser_control.plugin_api import (
    ControlError,
    errors,
    fail,
    pop,
    switch,
)

DEFAULT_CAP = 10
MAX_CAP = 50
DEFAULT_CHARS = 1_200
WAIT_S = 20.0
POST_WAIT_S = 15.0          # how long the first rendered post is waited for

# --- X's rendered post shape (the one place these selectors live) ----------
POST = "article"
FIELDS = [
    'text=[data-testid="tweetText"]',
    "time=time@datetime",
    'url=a[href*="/status/"]@href',
]
SEARCH_URL = "https://x.com/search?q={query}&src=typed_query&f={sort}"
_STATUS = re.compile(r"^/([^/]+)/status/(\d+)")
_SORT_TAB = {
    "latest": "latest",
    "top": "top",
}


def _search_url(query: str, latest: bool) -> str:
    """The search URL: `f=live` is X's "Latest" (by date), `f=top` relevance."""
    return SEARCH_URL.format(
        query=urllib.parse.quote(query, safe=""),
        sort="live" if latest else "top")


def _number(value: str | None, flag: str, default: int, top: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        fail(errors.ERR_BAD_ARGS, f"x search: {flag} needs a number, got {value!r}")
    if number < 1:
        fail(errors.ERR_BAD_ARGS, f"x search: {flag} must be at least 1, got {number}")
    return min(number, top)


def _selected_sort(tab: str, browser: str, fallback: str) -> str:
    """The sort the page's own tab strip reports as selected.

    `dom.extract` reads the tab roles; if the strip is not there (or cannot be
    read), the answer is the sort we ASKED for, not a claim the page never
    made. This is the honest way to say "sorted by date": ask the UI.
    """
    try:
        data = plugin_api.extract(each='[role="tab"]',
                           fields=["label=:scope", "selected=@aria-selected"],
                           cap=12, chars=40, tab=tab, browser=browser)
    except ControlError:
        return fallback
    for row in data.get("matches") or []:
        if str(row.get("selected")).lower() != "true":
            continue
        label = str(row.get("label") or "").strip().lower()
        if label:
            return _SORT_TAB.get(label, label)
    return fallback


def _posts(records: list[dict]) -> list[dict]:
    """The extraction's rows as posts: author, id, canonical URL, time, text.

    A row without a status link is not a post this verb can name (an ad or a
    malformed row) and is dropped; a repeated post id is kept once, first
    occurrence.
    """
    posts: list[dict] = []
    seen: set[str] = set()
    for record in records:
        href = str(record.get("url") or "")
        match = _STATUS.match(href)
        if not match:
            continue
        handle, post_id = match.group(1), match.group(2)
        if post_id in seen:
            continue
        seen.add(post_id)
        posts.append({
            "id": post_id,
            "handle": handle,
            "url": "https://x.com" + href.split("?")[0],
            "time": str(record.get("time") or ""),
            "text": str(record.get("text") or ""),
        })
    return posts


def run(rest: list[str], browser: str) -> dict:
    """`x search QUERY [--latest|--top] [--cap N] [--chars N] [--tab SPEC]`."""
    args = [str(arg) for arg in rest]
    if not args or args[0] != "search":
        # the verb's own subcommand: `x` is the plugin's noun, `search` is the
        # action, so a future read (`x post URL`) can sit beside it
        fail(errors.ERR_BAD_ARGS,
             "x: the subcommand is required — `x search QUERY ...` "
             "(have: search)")
    args = args[1:]
    # default is `--latest`: this verb is for reading what was JUST said; the
    # site's own relevance sort is one explicit `--top` away.
    args, latest = switch(args, "--latest")
    args, top = switch(args, "--top")
    args, cap = pop(args, "--cap", "x search")
    args, chars = pop(args, "--chars", "x search")
    args, tab = pop(args, "--tab", "x search")
    tab = tab or ""
    if latest and top:
        fail(errors.ERR_BAD_ARGS,
             "x search: --latest and --top are two sorts — pick one")
    if not args:
        fail(errors.ERR_BAD_ARGS,
             "x search: a QUERY is required, e.g. "
             "x search '\"Pardon Snowden\"' --latest --cap 5")
    if len(args) > 1:
        fail(errors.ERR_BAD_ARGS, f"x search: one QUERY at most, got {len(args)}")
    query = args[0].strip()
    if not query:
        fail(errors.ERR_BAD_ARGS, "x search: an empty QUERY is not a search")

    url = _search_url(query, latest=(not top))
    plugin_api.nav(url, tab=tab, browser=browser)
    # X keeps long-lived connections, and the extraction below is the real
    # read-back: a page that never reports "idle" is not fatal here.
    with contextlib.suppress(ControlError):
        plugin_api.wait("idle", timeout=WAIT_S, tab=tab, browser=browser)
    # ...but idle does NOT mean rendered: X builds the result list after the
    # network quiets, so wait for the first post itself. No results (or a
    # wall) times out, and the extraction below then answers zero, honestly.
    with contextlib.suppress(ControlError):
        plugin_api.wait("element", selector=POST, timeout=POST_WAIT_S,
                 tab=tab, browser=browser)
    data = plugin_api.extract(each=POST, fields=FIELDS,
                       cap=_number(cap, "--cap", DEFAULT_CAP, MAX_CAP),
                       chars=_number(chars, "--chars", DEFAULT_CHARS, 20_000),
                       tab=tab, browser=browser)
    posts = _posts(data.get("matches") or [])
    return {
        "ok": True,
        "query": query,
        "query_url": url,
        "sort": _selected_sort(tab, browser, "latest" if not top else "top"),
        "count": len(posts),
        "truncated": bool(data.get("truncated")),
        "posts": posts,
        "note": ("read-only: the page's own rendered posts; `sort` is what "
                 "the search tab strip reports as selected, and the values "
                 "are the page's, not this tool's"),
    }


PLUGIN = {
    "api": 1,
    "name": "x-reader",
    "description": "read-only X (Twitter) search results",
    "actions": {
        "x": {
            "run": run,
            # nav resolves the tab for_write=True, so the honest declaration
            # is read+write: a `--allow read` gate would otherwise authorise a
            # write (a review found the gate's answer misleading)
            "classes": ("read", "write"),
            "usage": ("x search QUERY [--latest|--top] [--cap N] [--chars N] "
                      "[--tab SPEC] — the page's rendered posts as records"),
        },
    },
}
