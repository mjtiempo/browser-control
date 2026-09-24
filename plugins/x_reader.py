"""x-reader — read X (Twitter) search results through the core's verbs.

A READ-ONLY adapter: it navigates one tab, waits for the page, reads the
rendered `article` rows with `tab extract`, and — because X recycles its
rendered window — wheels the timeline down and merges what mounts next, by
post id, until `--cap` posts are read or X stops yielding. The only input it
sends is that wheel; it does not click, type, post, like or follow.

Install: copy this file into ``~/.local/share/browser-control/plugins/`` (or
any directory in ``BROWSER_CONTROL_PLUGIN_PATH``). The browser must be logged
in on the tab's profile for anything beyond the public wall — seed the managed
profile with ``profile seed`` first.

    browser-control-cli x search "bitcoin price" --latest --cap 20

`--cap` is a TARGET, not a slice of the first render: measured, X keeps only
3–9 articles mounted at a time and replaces rows as the timeline moves, so one
extraction cannot answer a 20-post request. The reply's `loading` block says
what the loading took (`reads`, `scrolls`) and why it stopped (`stop`): `cap`
(enough posts), `exhausted` (X stopped yielding), `max-scrolls` (the budget,
which `--max-scrolls 0` sets to "the first render only"), `no-posts` (a wall,
or nothing matched — scrolling cannot help), `scroll-failed` (the wheel could
not be sent).

The selector map below is the part that breaks when X's DOM changes, so it is
kept in one place at the top. `sort` reports what the page's own tab strip says
is selected, because no verb can PROVE a site's ordering.
"""
from __future__ import annotations

import contextlib
import re
import time
import urllib.parse

from browser_control import plugin_api
from browser_control.plugin_api import (
    ControlError,
    errors,
    fail,
    int_arg,
    pop,
    switch,
    tab_arg,
    text_arg,
)

DEFAULT_CAP = 10
MAX_CAP = 50
DEFAULT_CHARS = 1_200
DEFAULT_SCROLLS = 10        # wheel steps spent trying to fill `--cap`
MAX_SCROLLS = 60            # and the most a caller may ask for
SCROLL_PIXELS = 2_400       # one wheel event, a few viewports
SCROLL_PAUSE_S = 1.0        # X mounts the next window after the wheel
STALL_PAUSE_S = 0.4         # a second read before calling a round empty
STALL_ROUNDS = 2            # empty rounds in a row = X stopped yielding
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


def _posts(records: list[dict], seen: set[str] | None = None) -> list[dict]:
    """The extraction's rows as posts: author, id, canonical URL, time, text.

    A row without a status link is not a post this verb can name (an ad or a
    malformed row) and is dropped. `seen` carries post ids ACROSS
    extractions: X recycles rows as the timeline moves, and the same post must
    come home once, first occurrence.
    """
    posts: list[dict] = []
    if seen is None:
        seen = set()
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


def _read(tab: str, browser: str, cap: int, chars: int) -> dict:
    """One extraction of what X currently has mounted."""
    return plugin_api.extract(each=POST, fields=FIELDS, cap=cap, chars=chars,
                              tab=tab, browser=browser)


def _collect(tab: str, browser: str, cap: int, chars: int,
             max_scrolls: int) -> tuple[list[dict], dict, bool]:
    """The posts, what loading them took, and whether more may exist.

    The loop a caller used to run by hand: extract what is mounted, wheel the
    timeline down, extract again, merge by post id — one extraction can only
    ever answer the few rows X keeps in its recycled window. Bounded by
    `max_scrolls`, and stopped early when X stops yielding (`STALL_ROUNDS`
    empty rounds, each given a second read after a beat, so a slow mount is
    not mistaken for the end).

    `stop` names the cause, the way the core names every other one, and
    `truncated` is the OR of every extraction's own cut flag plus "stopped
    short": a reply never claims to hold everything when an extraction was
    cut or the loading ran out.
    """
    seen: set[str] = set()
    data = _read(tab, browser, cap, chars)
    posts = _posts(data.get("matches") or [], seen)
    truncated = bool(data.get("truncated"))
    reads, scrolls, stall = 1, 0, 0
    stop = ""
    if not posts:
        # a wall, or a genuinely empty result: the wheel has nothing to load
        stop = "no-posts"
    while not stop and len(posts) < cap and scrolls < max_scrolls:
        try:
            plugin_api.scroll(by=SCROLL_PIXELS, tab=tab, browser=browser)
        except ControlError:
            stop = "scroll-failed"
            break
        scrolls += 1
        time.sleep(SCROLL_PAUSE_S)
        data = _read(tab, browser, cap, chars)
        reads += 1
        truncated = truncated or bool(data.get("truncated"))
        fresh = _posts(data.get("matches") or [], seen)
        if not fresh:
            time.sleep(STALL_PAUSE_S)
            data = _read(tab, browser, cap, chars)
            reads += 1
            truncated = truncated or bool(data.get("truncated"))
            fresh = _posts(data.get("matches") or [], seen)
        if fresh:
            posts.extend(fresh)
            stall = 0
        else:
            stall += 1
            if stall >= STALL_ROUNDS:
                stop = "exhausted"
    if len(posts) > cap:
        # a window mounts several posts at once, so the round that reaches the
        # target can overshoot it; `--cap` bounds the REPLY like every other
        # cap in the core, while `loading` still says what the loading took
        posts = posts[:cap]
    if not stop:
        stop = "cap" if len(posts) >= cap else "max-scrolls"
    # an exhausted read is the whole result; anything else stopped short, so
    # more posts may exist (and the extraction's own cut still counts)
    return posts, {"reads": reads, "scrolls": scrolls,
                   "max_scrolls": max_scrolls, "stop": stop}, (
        truncated or stop not in ("exhausted", "no-posts"))


def run(rest: list[str], browser: str) -> dict:
    """`x search QUERY [--latest|--top] [--cap N] [--chars N]
    [--max-scrolls N] [--tab SPEC]`."""
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
    args, scrolls_flag = pop(args, "--max-scrolls", "x search")
    args, tab = tab_arg(args, "x search")
    if latest and top:
        fail(errors.ERR_BAD_ARGS,
             "x search: --latest and --top are two sorts — pick one")
    # the positional rule is the CORE's (`text_arg`): a flag where the query
    # goes, a missing query or a repeated one refuses with the same message a
    # built-in verb gives, rather than a shape only this plugin speaks
    query = text_arg(args, "x search").strip()
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
    # wall) times out, and the collection below then answers zero, honestly.
    with contextlib.suppress(ControlError):
        plugin_api.wait("element", selector=POST, timeout=POST_WAIT_S,
                 tab=tab, browser=browser)
    # the parse and its "needs a number" refusal are the core's (`int_arg`);
    # only this verb's bounds stay — an absent flag is the default, a value
    # below the floor is refused, and one above the top clamps
    cap_n = (int_arg(cap, "x search: --cap")
             if cap is not None else DEFAULT_CAP)
    if cap_n < 1:
        fail(errors.ERR_BAD_ARGS,
             f"x search: --cap must be at least 1, got {cap_n}")
    cap_n = min(cap_n, MAX_CAP)
    chars_n = (int_arg(chars, "x search: --chars")
               if chars is not None else DEFAULT_CHARS)
    if chars_n < 1:
        fail(errors.ERR_BAD_ARGS,
             f"x search: --chars must be at least 1, got {chars_n}")
    chars_n = min(chars_n, 20_000)
    max_scrolls = (int_arg(scrolls_flag, "x search: --max-scrolls")
                   if scrolls_flag is not None else DEFAULT_SCROLLS)
    if max_scrolls < 0:
        # 0 is meaningful ("read the first render, wheel nothing"); a negative
        # budget is a mistake, not a smaller load
        fail(errors.ERR_BAD_ARGS,
             f"x search: --max-scrolls must be 0 or more, got {max_scrolls}")
    max_scrolls = min(max_scrolls, MAX_SCROLLS)

    posts, loading, truncated = _collect(tab, browser, cap_n, chars_n,
                                         max_scrolls)
    return {
        "ok": True,
        "query": query,
        "query_url": url,
        "sort": _selected_sort(tab, browser, "latest" if not top else "top"),
        "count": len(posts),
        "truncated": truncated,
        "loading": loading,
        "posts": posts,
        "note": ("read-only: the page's own rendered posts, merged across "
                 "the timeline's recycled window by post id; `sort` is what "
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
            # nav resolves the tab for_write=True and this verb wheels the
            # page, so the honest declaration is read+write: a `--allow read`
            # gate would otherwise authorise a write
            "classes": ("read", "write"),
            "usage": ("x search QUERY [--latest|--top] [--cap N] [--chars N] "
                      "[--max-scrolls N] [--tab SPEC] — the page's rendered "
                      "posts as records; --cap is a TARGET and the timeline "
                      "is wheeled until that many are read, X stops yielding, "
                      "or --max-scrolls runs out (0 = first render only)"),
        },
    },
}
