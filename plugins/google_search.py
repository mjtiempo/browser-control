"""google-search — search Google by TYPING the query, the way a person does.

A WRITING plugin built on the core's real-input verbs: it opens Google's
homepage, puts the caret in the search box (`focus`), types the query as
per-character key events at a HUM AN cadence (90 WPM by default — 12 / WPM
seconds between keystrokes), presses Enter, and reads the rendered results with
`tab extract`.

No query URL is ever built. The only address this plugin navigates to is the
homepage, and the `/search?q=…` URL in `landed_on` is the one the SITE put in
the address bar after the submit. That is the point: a URL is a request any
caller can forge for anything, while typed input lands in the page's own field
first — which is what a person does, and what a site's handlers (autocomplete,
consent, bot checks) actually see. A caller that types a query has asked the
site a question the way a human asks it.

Install: copy this file into ``~/.local/share/browser-control/plugins/`` (or
any directory in ``BROWSER_CONTROL_PLUGIN_PATH``).

    browser-control-cli google search "araghchi speaking in UN" --cap 5

The selector map below is the part that breaks when Google's DOM changes, so it
is kept in one place at the top. The container asks only for result cards that
HAVE a heading (`:has(h3)`): the panels around them are not results, and a row
that still has no title or no link is dropped rather than reported unnamed.
"""
from __future__ import annotations

import contextlib
import time

from browser_control import plugin_api
from browser_control.plugin_api import (
    ControlError,
    errors,
    fail,
    pop,
)

# --- Google's rendered result shape (the one place these selectors live) ---
HOME = "https://www.google.com/"
SEARCH_BOX = 'textarea[name="q"]'
RESULTS = "#search div.MjjYud:has(h3)"
FIELDS = [
    "title=h3",
    "url=a@href",
    "snippet=div.VwiC3b, div[data-sncf]",
]

DEFAULT_CAP = 10
MAX_CAP = 50
DEFAULT_WPM = 90            # the cadence this verb types at unless told
MAX_WPM = 600
CHARS = 600                 # per-field slice for the extraction
WAIT_S = 20.0               # the search box's / the results' deadline
SUBMIT_PAUSE_S = 0.6        # a person's beat between the last key and Enter


def _delay(wpm: int) -> float:
    """Seconds between keystrokes at a given WPM (5 characters to a word)."""
    return 12.0 / wpm


def _number(value: str | None, flag: str, default: int, top: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        fail(errors.ERR_BAD_ARGS,
             f"google search: {flag} needs a number, got {value!r}")
    if number < 1:
        fail(errors.ERR_BAD_ARGS,
             f"google search: {flag} must be at least 1, got {number}")
    return min(number, top)


def _results(records: list[dict]) -> list[dict]:
    """The extraction's rows as results: title, url, snippet.

    The selector already asks for cards WITH a heading, so this is the safety
    net around it: a row with no title or no URL is not a result this verb can
    name and is dropped, and a repeated URL is kept once, first occurrence.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for record in records:
        title = str(record.get("title") or "").strip()
        url = str(record.get("url") or "").strip()
        if not title or not url.startswith("http"):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"title": title, "url": url,
                    "snippet": str(record.get("snippet") or "").strip()})
    return out


def run(rest: list[str], browser: str) -> dict:
    """`google search QUERY [--cap N] [--wpm N] [--tab SPEC]`."""
    args = [str(arg) for arg in rest]
    if not args or args[0] != "search":
        # the verb's own subcommand: `google` is the plugin's noun, `search`
        # is the action, so a later read (`google result URL`) can sit beside
        fail(errors.ERR_BAD_ARGS,
             "google: the subcommand is required — `google search QUERY ...` "
             "(have: search)")
    args = args[1:]
    args, cap = pop(args, "--cap", "google search")
    args, wpm_flag = pop(args, "--wpm", "google search")
    args, tab = pop(args, "--tab", "google search")
    tab = tab or ""
    if not args:
        fail(errors.ERR_BAD_ARGS,
             "google search: a QUERY is required, e.g. "
             "google search 'araghchi speaking in UN' --cap 5")
    if len(args) > 1:
        fail(errors.ERR_BAD_ARGS,
             f"google search: one QUERY at most, got {len(args)}")
    query = args[0].strip()
    if not query:
        fail(errors.ERR_BAD_ARGS, "google search: an empty QUERY is not a search")
    cap_n = _number(cap, "--cap", DEFAULT_CAP, MAX_CAP)
    wpm = _number(wpm_flag, "--wpm", DEFAULT_WPM, MAX_WPM)

    # the HOMEPAGE, never a query URL: the query is typed below, into the
    # page's own field, and the site builds the address from it
    plugin_api.nav(HOME, tab=tab, browser=browser)
    with contextlib.suppress(ControlError):
        plugin_api.wait("element", selector=SEARCH_BOX, timeout=WAIT_S,
                        tab=tab, browser=browser)
    plugin_api.focus(selector=SEARCH_BOX, tab=tab, browser=browser)
    started = time.monotonic()
    typed = plugin_api.type_text(query, tab=tab, browser=browser,
                                 delay_s=_delay(wpm))
    elapsed = time.monotonic() - started
    verified = typed.get("verified")
    # False is the page saying the text did NOT land (None is a field the core
    # could not read, which is not proof either way)
    if verified is not None and not verified:
        fail(errors.ERR_TYPE_NOT_VERIFIED,
             f"google search: the {len(query)}-character query did not land in "
             f"{SEARCH_BOX} (the field reads {typed.get('length_after')!r} "
             "chars) — refusing to submit a query the page does not hold")
    time.sleep(SUBMIT_PAUSE_S)          # a person's beat before Enter
    plugin_api.press("enter", tab=tab, browser=browser)
    # idle is not rendered: the results panel is the real read-back, and no
    # results (or a wall) answers zero below, honestly
    with contextlib.suppress(ControlError):
        plugin_api.wait("element", selector=RESULTS, timeout=WAIT_S,
                        tab=tab, browser=browser)
    data = plugin_api.extract(each=RESULTS, fields=FIELDS, cap=cap_n,
                              chars=CHARS, tab=tab, browser=browser)
    results = _results(data.get("matches") or [])
    measured = round(len(query) / elapsed * 12, 1) if elapsed > 0 else 0.0
    return {
        "ok": True,
        "query": query,
        "entry": HOME,
        "typing": {
            "target": SEARCH_BOX,
            "chars": len(query),
            "wpm": wpm,
            "delay_s": round(_delay(wpm), 4),
            "elapsed_s": round(elapsed, 2),
            "measured_wpm": measured,
            "verified": verified,
        },
        "landed_on": str(data.get("url") or ""),
        "count": len(results),
        "truncated": bool(data.get("truncated")),
        "results": results,
        "note": ("typed into the page's own search box at a human cadence and "
                 "submitted with Enter — no query URL was built; `landed_on` "
                 "is the address the SITE put in the bar, and the values are "
                 "the page's, not this tool's"),
    }


PLUGIN = {
    "api": 1,
    "name": "google-search",
    "description": "search Google by typing the query into its search box",
    "actions": {
        "google": {
            "run": run,
            # nav resolves the tab for_write=True and this verb types into the
            # page, so the honest declaration is read+write
            "classes": ("read", "write"),
            "usage": ("google search QUERY [--cap N] [--wpm N] [--tab SPEC] — "
                      "the rendered results as records; the query is TYPED "
                      "into the search box at a human cadence (default 90 "
                      "WPM), never put in a URL"),
        },
    },
}
