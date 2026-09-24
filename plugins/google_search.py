"""google-search — search Google by TYPING the query, the way a person does.

A WRITING plugin built on the core's real-input verbs: it opens Google's
homepage, puts the caret in the search box (`focus`), types the query as
per-character key events at a HUMAN cadence (90 WPM by default — 12 / WPM
seconds between keystrokes), presses Enter, and reads the rendered results with
`tab extract`.

No query URL is ever built. The only address this plugin navigates to is the
homepage, and the `/search?q=…` URL in `landed_on` is the one the SITE put in
the address bar after the submit. That is the point: a URL is a request any
caller can forge for anything, while typed input lands in the page's own field
first — which is what a person does, and what a site's handlers (autocomplete,
consent, bot checks) actually see. A caller that types a query has asked the
site a question the way a human asks it.

`--cap` is a TARGET, not a slice of the first page: when the first SERP yields
fewer, the plugin clicks the site's own Next control — the control a person
clicks, never a `&start=` URL — waits for the render to really swap, and merges
what comes back, by URL, until `--cap` results are read, no Next control is
left, the `--max-pages` budget runs out, or a click fails to move the page.
The reply's `loading` block says which of those stopped it (`cap`, `max-pages`,
`no-next`, `no-growth`, `click-failed`), and `loading.wait_error` is the refusal
that stopped a page turn for a reason that is NOT "there is no next page" (a
tab that closed, a browser that went away — `stop: "wait-failed"`); either of
the last three sets `truncated`, because the list is then not all there was.

Absence and failure are different answers: only `wait-timeout` — the refusal
`dom.wait` raises when a predicate never became true — is read as "the control
is not there". Every other refusal is re-raised or recorded, never folded into
`no-next`: a closed tab used to be reported as the honest end of the list.

The selector map below is the part that breaks when Google's DOM changes, so it
is kept in one place at the top — as ORDERED candidates: the first selector the
page renders is the one extraction runs with, and the reply's `selectors` block
names it. A zero-result reply can therefore say whether the map found nothing
(`matched: false`) or the page really showed none, and `next`/`next_matched`
answer the same question about the pagination control.

Install: copy this file into ``~/.local/share/browser-control/plugins/`` (or
any directory in ``BROWSER_CONTROL_PLUGIN_PATH``).

    browser-control-cli google search "araghchi speaking in UN" --cap 20
"""
from __future__ import annotations

import contextlib
import time

from browser_control import plugin_api
from browser_control.plugin_api import (
    ControlError,
    errors,
    fail,
    int_arg,
    pop,
    tab_arg,
    text_arg,
)

# --- Google's rendered result shape (the one place these selectors live) ---
HOME = "https://www.google.com/"
SEARCH_BOXES = ('textarea[name="q"]', 'input[name="q"]')
RESULT_CANDIDATES = (
    "#search div.MjjYud:has(h3)",
    "div.g:has(h3)",
    "div[data-snc]:has(h3)",
)
NEXT_CANDIDATES = ("a#pnnext", 'a[aria-label="Next page"]')
FIELDS = [
    "title=h3",
    "url=a@href",
    "snippet=div.VwiC3b, div[data-sncf]",
]

DEFAULT_CAP = 10
MAX_CAP = 50
DEFAULT_WPM = 90            # the cadence this verb types at unless told
MAX_WPM = 600
DEFAULT_PAGES = 3           # pages read, the first included, unless asked
MAX_PAGES = 10              # and the most a caller may ask for
PAGE_CAP = 20               # rows one SERP extraction takes per page
CHARS = 600                 # per-field slice for the extraction
WAIT_S = 20.0               # the search box's / the results' deadline
PROBE_S = 3.0               # a FALLBACK selector's deadline: by the time the
                            # page has missed the first candidate, the rest of
                            # the WAIT_S budget is already spent
NEXT_PROBE_S = 3.0          # the Next control's deadline: a last page or a
                            # wall has no next, and waiting longer only slows
                            # the honest end
SUBMIT_PAUSE_S = 0.6        # a person's beat between the last key and Enter
PAGE_PAUSE_S = 0.8          # and between one page and the next click
SWAP_WAIT_S = 15.0          # a clicked page's deadline to really swap
SWAP_POLL_S = 0.5           # ...polled this often; the read IS the oracle


def _delay(wpm: int) -> float:
    """Seconds between keystrokes at a given WPM (5 characters to a word)."""
    return 12.0 / wpm


def _wait_element(selector: str, timeout: float, tab: str,
                  browser: str) -> bool:
    """Did the page render `selector` within `timeout` seconds?

    ONLY `wait-timeout` may answer "no": that is the refusal `dom.wait` raises
    when the predicate never became true. Every other refusal — a tab that was
    closed, a browser that went away, a malformed selector — is RE-RAISED,
    because reading those as "the page has nothing there" is how a pagination
    failure used to be reported as the honest end of the list.
    """
    try:
        plugin_api.wait("element", selector=selector, timeout=timeout,
                        tab=tab, browser=browser)
    except ControlError as e:
        if e.code != errors.ERR_WAIT_TIMEOUT:
            raise
        return False
    return True


def _rendered(candidates: tuple[str, ...], full_s: float, probe_s: float,
              tab: str, browser: str) -> tuple[str, bool]:
    """`(selector, matched)` — the FIRST candidate the page renders.

    Ordered means ordered: the caller extracts with the ONE selector this
    returns, never a merged pool (a CSS comma list is a document-order union,
    not a fallback chain). The first candidate gets the full deadline — the
    ordinary, slow-render path — and a fallback gets `probe_s`, because by the
    time the page has missed the first, the rest of that budget is spent.

    `("", False)` means NONE of them rendered in time — a genuine absence, the
    only thing a timeout proves. Any other refusal propagates (`_wait_element`),
    so a closed tab or a dead browser is never mistaken for an empty page.
    """
    for index, selector in enumerate(candidates):
        if _wait_element(selector, full_s if index == 0 else probe_s,
                         tab, browser):
            return selector, True
    return "", False


def _results(records: list[dict]) -> list[dict]:
    """The extraction's rows as results: title, url, snippet.

    The selector already asks for cards WITH a heading, so this is the safety
    net around it: a row with no title or no URL is not a result this verb can
    name and is dropped, and a repeated URL is kept once, first occurrence —
    WITHIN one page. Merging across pages is `_absorb`'s job.
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


def _absorb(results: list[dict], seen: set[str], batch: list[dict],
            page: int) -> None:
    """Merge one page's rows: first occurrence wins, and it says which page.

    Google repeats rows between pages, so the cross-page rule is the URL, and
    every kept row is stamped with the 1-based page it came from — that is how
    a merged list stays explainable.
    """
    for row in batch:
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        row["page"] = page
        results.append(row)


def _next_page(before_url: str, seen: set[str], selector: str, cap: int,
               tab: str, browser: str) -> dict | None:
    """Read the SERP a click moved to, or `None` when nothing moved.

    A click's own `changed` flag is not proof the render swapped — the old DOM
    answers extractions for a while — so this polls the page's OWN facts (its
    address, its rows) until either moves, to a deadline. `None` is the honest
    "the click did not produce a page" the caller reports as `no-growth`.
    """
    deadline = time.monotonic() + SWAP_WAIT_S
    while True:
        data = plugin_api.extract(each=selector, fields=FIELDS, cap=cap,
                                  chars=CHARS, tab=tab, browser=browser)
        rows = _results(data.get("matches") or [])
        moved = str(data.get("url") or "") != before_url
        fresh = any(row["url"] not in seen for row in rows)
        if moved or fresh:
            return data
        if time.monotonic() >= deadline:
            return None
        time.sleep(SWAP_POLL_S)


def run(rest: list[str], browser: str) -> dict:
    """`google search QUERY [--cap N] [--wpm N] [--max-pages N] [--tab SPEC]`."""
    args = [str(arg) for arg in rest]
    if not args or args[0] != "search":
        # the verb's own subcommand: `google` is the plugin's noun, `search`
        # is the action, so a later read (`google result URL`) can sit beside it
        fail(errors.ERR_BAD_ARGS,
             "google: the subcommand is required — `google search QUERY ...` "
             "(have: search)")
    args = args[1:]
    args, cap = pop(args, "--cap", "google search")
    args, wpm_flag = pop(args, "--wpm", "google search")
    args, pages_flag = pop(args, "--max-pages", "google search")
    args, tab = tab_arg(args, "google search")
    # the positional rule is the CORE's (`text_arg`): a flag where the query
    # goes, a missing query or a repeated one refuses with the same message a
    # built-in verb gives, rather than a shape only this plugin speaks
    query = text_arg(args, "google search").strip()
    if not query:
        fail(errors.ERR_BAD_ARGS, "google search: an empty QUERY is not a search")
    # the parse and its "needs a number" refusal are the core's (`int_arg`);
    # only the bounds this verb alone knows stay here — an absent flag is the
    # default, a cadence below 1 would divide by zero, and a value above the
    # top is clamped rather than refused
    cap_n = (int_arg(cap, "google search: --cap")
             if cap is not None else DEFAULT_CAP)
    if cap_n < 1:
        fail(errors.ERR_BAD_ARGS,
             f"google search: --cap must be at least 1, got {cap_n}")
    cap_n = min(cap_n, MAX_CAP)
    wpm = (int_arg(wpm_flag, "google search: --wpm")
           if wpm_flag is not None else DEFAULT_WPM)
    if wpm < 1:
        fail(errors.ERR_BAD_ARGS,
             f"google search: --wpm must be at least 1, got {wpm}")
    wpm = min(wpm, MAX_WPM)
    max_pages = (int_arg(pages_flag, "google search: --max-pages")
                 if pages_flag is not None else DEFAULT_PAGES)
    if max_pages < 1:
        fail(errors.ERR_BAD_ARGS,
             f"google search: --max-pages must be at least 1, got {max_pages}")
    max_pages = min(max_pages, MAX_PAGES)

    # the HOMEPAGE, never a query URL: the query is typed below, into the
    # page's own field, and the site builds the address from it
    plugin_api.nav(HOME, tab=tab, browser=browser)
    box = _rendered(SEARCH_BOXES, WAIT_S, PROBE_S, tab, browser)[0]
    if not box:
        # no search box rendered: `focus` refuses with the core's own message,
        # exactly as the old single-selector path did after its suppressed wait
        box = SEARCH_BOXES[0]
    plugin_api.focus(selector=box, tab=tab, browser=browser)
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
             f"{box} (the field reads {typed.get('length_after')!r} "
             "chars) — refusing to submit a query the page does not hold")
    time.sleep(SUBMIT_PAUSE_S)          # a person's beat before Enter
    plugin_api.press("enter", tab=tab, browser=browser)
    results_sel, matched = _rendered(RESULT_CANDIDATES, WAIT_S, PROBE_S,
                                     tab, browser)
    if not matched:
        # no candidate rendered: extract anyway with the first, which answers
        # the honest zero — a consent wall and an empty query read alike, and
        # `selectors.matched` is the flag that tells them from DOM drift
        results_sel = RESULT_CANDIDATES[0]

    results: list[dict] = []
    seen: set[str] = set()
    pages, clicks = 1, 0
    data = plugin_api.extract(each=results_sel, fields=FIELDS,
                              cap=min(cap_n, PAGE_CAP), chars=CHARS,
                              tab=tab, browser=browser)
    current_url = str(data.get("url") or "")
    landed_on = current_url
    truncated = bool(data.get("truncated"))
    _absorb(results, seen, _results(data.get("matches") or []), pages)
    stop = "cap" if len(results) >= cap_n else "max-pages"
    # the last Next control the page was asked for, and whether it was there:
    # `next_matched: false` with `stop: "no-next"` is the honest end of the
    # list, while `wait_error` is a page turn that FAILED (never the same thing)
    next_sel, next_matched, wait_error = "", False, ""
    while len(results) < cap_n and pages < max_pages:
        try:
            next_sel, next_matched = _rendered(NEXT_CANDIDATES, NEXT_PROBE_S,
                                               NEXT_PROBE_S, tab, browser)
        except ControlError as e:
            # the WAIT itself failed — the tab was closed, the browser went
            # away. That is not "there is no Next control": recording it as
            # `no-next` reported a pagination failure as the end of the list,
            # so it is a stop of its own, it NAMES the refusal, and it sets
            # `truncated` below (the results read so far are real and kept)
            next_sel, next_matched = "", False
            wait_error = f"{e.code}: {e.message}"
            stop = "wait-failed"
            break
        if not next_matched:
            stop = "no-next"
            break
        # the Next link can sit below the fold: reveal it first, or the click
        # refuses `no-viewport-target` (measured driving Google by hand)
        with contextlib.suppress(ControlError):
            plugin_api.scroll(selector=next_sel, tab=tab, browser=browser)
        try:
            plugin_api.click(selector=next_sel, tab=tab, browser=browser)
        except ControlError:
            # the page no longer gives the click a target (or something covers
            # it): a stop reason, not a refusal — the results so far are real
            stop = "click-failed"
            break
        clicks += 1
        time.sleep(PAGE_PAUSE_S)        # a person's beat between pages
        data = _next_page(current_url, seen, results_sel, PAGE_CAP,
                          tab, browser)
        if data is None:
            stop = "no-growth"
            break
        pages += 1
        current_url = str(data.get("url") or "")
        truncated = truncated or bool(data.get("truncated"))
        _absorb(results, seen, _results(data.get("matches") or []), pages)
        stop = "cap" if len(results) >= cap_n else "max-pages"

    # `truncated` is "the list is not all there was": a page's own extraction
    # cut it, or the loop stopped short of the cap (a budget, a click that did
    # not move, a page turn whose wait FAILED, or a click the page would not
    # take). `no-next` is deliberately NOT here: a page with no Next control
    # was read to its end, and saying otherwise would be the same lie in the
    # other direction
    truncated = truncated or stop in ("max-pages", "no-growth", "click-failed",
                                      "wait-failed")
    results = results[:cap_n]
    measured = round(len(query) / elapsed * 12, 1) if elapsed > 0 else 0.0
    return {
        "ok": True,
        "query": query,
        "entry": HOME,
        "typing": {
            "target": box,
            "chars": len(query),
            "wpm": wpm,
            "delay_s": round(_delay(wpm), 4),
            "elapsed_s": round(elapsed, 2),
            "measured_wpm": measured,
            "verified": verified,
        },
        "landed_on": landed_on,
        "count": len(results),
        "truncated": truncated,
        # every row is the page's own, plus `page`: the 1-based SERP it came
        # from, so a merged multi-page list stays explainable
        "results": results,
        "loading": {"pages": pages, "clicks": clicks,
                    "max_pages": max_pages, "stop": stop,
                    # "" unless a page turn's wait FAILED, and then the refusal
                    # itself (`ERR[code]: message`), so `stop: "wait-failed"`
                    # is never a bare verdict
                    "wait_error": wait_error},
        "selectors": {"search_box": box, "results": results_sel,
                      "matched": matched,
                      # the pagination control, read the same way as `results`:
                      # `next_matched` false with `stop: "no-next"` is the page
                      # really having no Next, and `next` names the selector
                      "next": next_sel, "next_matched": next_matched},
        "note": ("typed into the page's own search box at a human cadence and "
                 "submitted with Enter — no query URL was built, and further "
                 "pages are the site's own Next control, clicked; `landed_on` "
                 "is the first address the SITE put in the bar, and the "
                 "values are the page's, not this tool's"),
    }


PLUGIN = {
    "api": 1,
    "name": "google-search",
    "description": "search Google by typing the query into its search box",
    "actions": {
        "google": {
            "run": run,
            # nav resolves the tab for_write=True, this verb types into the
            # page, and the browser reaches the network for Google, so the
            # honest declaration is read+write+egress (`--deny egress` stops it)
            "classes": ("read", "write", "egress"),
            "usage": ("google search QUERY [--cap N] [--wpm N] "
                      "[--max-pages N] [--tab SPEC] — the rendered results as "
                      "records; the query is TYPED into the search box at a "
                      "human cadence (default 90 WPM), never put in a URL, "
                      "and `--cap` is a target the site's own Next control "
                      "is clicked (up to `--max-pages`) to fill"),
        },
    },
}
