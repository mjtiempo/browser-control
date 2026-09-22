"""tabs — tab addressing, the tab verbs, and the spec matcher.

A spec (`id:<prefix>` or a title/URL substring) resolves to exactly one tab
per browser; the bulk verbs (`close --title`, `close --url`) use the same
matcher.
"""
from __future__ import annotations

import os

from browser_control.lib import browser as _pkg
from browser_control.lib import (
    cdp,
)
from browser_control.lib.browser.constants import (
    ACTIVE_SPEC,
)
from browser_control.lib.browser.machine import (
    may_write,
)
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    ERR_CLOSE_TAB_NOT_VERIFIED,
    ERR_NO_PAGE_TAB,
    ERR_NOT_MANAGED,
    ERR_TAB_AMBIGUOUS,
    ControlError,
    fail,
)
from browser_control.lib.text import (
    flat,
)


def _id_prefix(spec: str) -> str | None:
    """The id prefix an `id:` spec names: "" when it names none, None when the
    spec is not an `id:` spec at all.

    ONE rule, because two matchers read it: `_match_spec` refuses the empty
    prefix, and `_exact_spec_match` — the NAMED form `tab close` uses — must
    not match it. `"".startswith("")` is True, so an unchecked empty prefix
    matched every tab: measured, `tab close id:` closed the last tab on the
    page and Chromium took the window with it.
    """
    needle = str(spec or "").strip()
    if not needle.lower().startswith("id:"):
        return None
    return needle[3:].strip().lower()

def _match_spec(tabs: list[dict], spec: str) -> list[dict]:
    """The tabs of ONE browser a spec names — 0, 1 or several.

    `id:<prefix>` matches ids, anything else a title/URL substring; both
    case-insensitive. Pure and per-browser, so the cross-browser resolver can
    ask every browser and decide on the whole picture.
    """
    needle = str(spec or "").strip()
    if not needle:
        fail(ERR_BAD_ARGS, "a TAB spec is required (id:<prefix> or a title/url "
                         "substring)")
    want = _id_prefix(needle)
    if want is not None:
        if not want:
            fail(ERR_BAD_ARGS, "id: needs a target id prefix")
        return [t for t in tabs
                if str(t.get("id") or "").lower().startswith(want)]
    low = needle.lower()
    return [t for t in tabs
            if low in str(t.get("url") or "").lower()
            or low in str(t.get("title") or "").lower()]

def resolve_tab(rows: list[dict], spec: str) -> dict:
    """One page row for a spec, within ONE browser's tabs.

    Nothing, or several, refuses — never a silent first match.
    """
    needle = str(spec or "").strip()
    hits = _match_spec(rows, spec)
    if not hits:
        if needle.lower().startswith("id:"):
            fail(ERR_NO_PAGE_TAB, f"no tab with id {needle[3:]!r} "
                                "(the tab was probably closed)")
        have = ", ".join(flat(r.get("title"), 30)
                          for r in rows[:4]) or "none"
        fail(ERR_NO_PAGE_TAB, f"no tab matches {needle!r} (have: {have})")
    if len(hits) > 1:
        titles = ", ".join(flat(r.get("title"), 30) for r in hits[:5])
        fail(ERR_TAB_AMBIGUOUS, f"{needle!r} matches {len(hits)} tabs: {titles}")
    return hits[0]

def _visible(profile: str, target_id: str) -> bool | None:
    """Does that tab's page report itself visible? None when it cannot say.

    `document.visibilityState` is what makes a tab "the active one": measured
    in this project, a hidden tab still has a viewport, layout and hit-testing,
    so geometry cannot tell the two apart — this can.
    """
    try:
        return _pkg._eval(profile, target_id, "document.visibilityState",
                     timeout=5) == "visible"
    except ControlError:
        return None

def _spec_hits(rows: list[dict], tabs_of: dict,
               spec: str) -> list[tuple[dict, dict, int]]:
    """Every tab of every drivable browser a spec names.

    `active` is a RESERVED spec — the tab whose page answers `visible`, at most
    one per window, among the browsers this CLI DRIVES — so a page whose title
    merely contains the word is reached by `id:` or a longer substring, the
    same way `tab list` can never mean a site called "list".
    """
    if str(spec).strip().lower() == ACTIVE_SPEC:
        return [(row, tab, index)
                for row in rows
                for index, tab in enumerate(tabs_of[row["pid"]])
                if _visible(str(row["profile"]), str(tab["id"]))]
    hits: list[tuple[dict, dict, int]] = []
    for row in rows:
        tabs = tabs_of[row["pid"]]
        positions = {str(t["id"]): index for index, t in enumerate(tabs)}
        hits += [(row, tab, positions[str(tab["id"])])
                 for tab in _match_spec(tabs, spec)]
    return hits

def resolve_across(specs: list[str], browser: str,
                   for_write: bool) -> list[tuple[dict, dict, int]]:
    """[(browser row, tab, index)] for every spec, across the drivable ones.

    EVERY spec is resolved before any of them is acted on, so an ambiguous or
    missing one cannot leave a half-applied change; two specs that name the
    same tab collapse into one entry. `for_write` refuses a tab in a browser
    this CLI did not start: reads cover every drivable browser, writes only
    its own.
    """
    rows = _pkg._drivable(browser)
    if not rows:
        _pkg._no_drive(browser)
    tabs_of = {row["pid"]: _pkg._tabs_or_fail(row) for row in rows}
    found: list[tuple[dict, dict, int]] = []
    for spec in specs:
        # `active` is about the browser this CLI DRIVES: the user's own Chrome
        # has a visible tab in its own window as well, and counting it would
        # make every `--tab active` refuse — the same reason an unqualified tab
        # verb picks among OUR browsers instead of every browser on the machine
        if str(spec).strip().lower() == ACTIVE_SPEC:
            own = [row for row in rows if may_write(row)]
            hits = _spec_hits(own, tabs_of, spec)
            if not hits:
                fail(ERR_NO_PAGE_TAB,
                     "no tab of a browser this CLI drives reports itself "
                     "VISIBLE — read one with `tab list`, or name it with "
                     "`--tab SPEC`")
        else:
            hits = _spec_hits(rows, tabs_of, spec)
        if not hits:
            have = ", ".join(f'{flat(t["title"], 20) or flat(t["url"], 20)} '
                             f'({r["exe"]})'
                             for r in rows for t in tabs_of[r["pid"]][:2])
            fail(ERR_NO_PAGE_TAB,
                 f"no tab matches {spec!r} (have: {have or 'none'})")
        if len(hits) > 1:
            # pid in the message: two browsers can share an executable name
            where = ", ".join(
                f'{flat(t["title"], 20) or flat(t["id"], 8)} in {r["exe"]}'
                f':{os.path.basename(str(r["profile"]))} (pid {r["pid"]})'
                for r, t, _index in hits[:4])
            fail(ERR_TAB_AMBIGUOUS,
                 f"{spec!r} matches {len(hits)} tabs: {where}")
        row, tab, index = hits[0]
        if for_write and not may_write(row):
            fail(ERR_NOT_MANAGED,
                 f"{spec!r} is in {row['exe']} on {row['profile']}, which "
                 "this CLI neither manages nor has attached — `tab list` "
                 "and `tab info` read every drivable browser, but a write "
                 "needs `attach --port N` (or a browser this CLI started)")
        if not any(str(t["id"]) == str(tab["id"]) for _r, t, _i in found):
            found.append((row, tab, index))
    return found

def _tab_count() -> int:
    """How many page tabs the drivable browsers show right now."""
    # a READ-BACK after a close must not turn into a refusal: `tab close --all`
    # on the last tab makes the browser exit, and its endpoint can stop
    # answering before the close is reported — the strict form then said
    # `cdp-not-local` about an unrelated browser AFTER the tabs were gone (a
    # review flagged it).
    return sum(len(_pkg._tabs_or_fail(row)) for row in _pkg._drivable(strict=False))

def tab_info(spec: str, browser: str = "") -> dict:
    """`tab info`: one tab, resolved across the drivable browsers.

    The read side of a handle: which browser owns it, and what it is now.
    """
    (row, tab, index), = _pkg._resolve_across([spec], browser, for_write=False)
    return {"ok": True, "tab": {**tab, "index": index},
            "browser": _pkg._brief(row)}

def _exact_matches(field: str, value: str,
                   browser: str) -> tuple[list[tuple[dict, dict, int]],
                                         list[dict]]:
    """EXACT (case-insensitive) title/URL matches, ours and the rest.

    Exact means the whole title (or URL): `--title a` is the tab called "a",
    not every tab with an "a" somewhere in it.
    """
    wanted = str(value).lower()
    ours: list[tuple[dict, dict, int]] = []
    foreign: list[dict] = []
    for row in _pkg._closeable(browser):
        for index, tab in enumerate(_pkg._tabs_or_fail(row)):
            if str(tab.get(field) or "").lower() != wanted:
                continue
            if may_write(row):
                ours.append((row, tab, index))
            else:
                foreign.append(_pkg._foreign_row(row, tab))
    return ours, foreign

def _exact_spec_match(tab: dict, spec: str) -> bool:
    """Does this spec NAME that tab — exactly?

    `id:<prefix>`, the whole URL (a trailing slash is not a different page,
    the same rule `_same_page` uses), or the whole title, case-insensitively.
    A SUBSTRING is not a name: `tab close a` closed a tab whose title merely
    contained an `a` (measured, on a real browser), which is why the sweeping
    form has to be asked for by name (`--like`).
    """
    needle = str(spec or "").strip()
    if not needle:
        return False
    prefix = _id_prefix(needle)
    if prefix is not None:
        # `id:` with no prefix names NOTHING: `_id_prefix` returns "" and
        # `_match_spec` refuses that spec, so a match here would be the one
        # way an empty prefix still swept every tab.
        return bool(prefix) and str(tab.get("id") or "").lower().startswith(
            prefix)
    low = needle.lower()
    return (str(tab.get("url") or "").rstrip("/").lower()
            == low.rstrip("/")
            or str(tab.get("title") or "").strip().lower() == low)

def _spec_matches(specs: list[str], browser: str, loose: bool = False) -> tuple[
        list[tuple[dict, dict, int]], list[dict]]:
    """Every tab a SPEC set names — the union, deduplicated.

    Two rules, and the difference matters for a verb that DESTROYS tabs:

    * a SPEC NAMES a tab (`_exact_spec_match`): `tab close http://a` closes
      every tab on `http://a/`, `tab close a` closes the tabs titled `a`, and
      neither touches a tab that merely mentions them;
    * `loose=True` (the `--like` flag) matches a SUBSTRING, which is a sweep —
      every tab whose title or URL contains it — and exists because a caller
      sometimes means exactly that, asked for out loud.

    A spec that matches nothing refuses instead of reading as "nothing to do",
    and one that matches only foreign tabs refuses `not-managed`.

    Closing the LAST page tab of a browser takes its window with it, and
    Chromium exits with its last window — so `--all` on the only tab stops the
    browser as well. The ids are verified gone either way, which is why that
    reads as a success with `count: 0` and not as a lost endpoint.
    """
    rows = _pkg._closeable(browser)
    # ONE tab-list read per browser, not one per spec: the sibling resolver
    # hoists the same way, and each read here is a loopback GET of `/json`
    tabs_by_pid = {row["pid"]: _pkg._tabs_or_fail(row) for row in rows}
    ours: list[tuple[dict, dict, int]] = []
    foreign: list[dict] = []
    seen: set[str] = set()
    for spec in specs:
        if not loose and not str(spec).strip():
            fail(ERR_BAD_ARGS, "tab close: a TAB spec cannot be empty")
        hits = 0
        for row in rows:
            for index, tab in enumerate(tabs_by_pid[row["pid"]]):
                named = (bool(_match_spec([tab], spec)) if loose
                         else _exact_spec_match(tab, spec))
                if not named:
                    continue
                hits += 1
                if str(tab["id"]) in seen:
                    continue
                if may_write(row):
                    seen.add(str(tab["id"]))
                    ours.append((row, tab, index))
                else:
                    foreign.append(_pkg._foreign_row(row, tab))
        if not hits:
            have = ", ".join(f'{flat(t["title"], 20) or flat(t["url"], 20)}'
                             for r in rows
                             for t in tabs_by_pid[r["pid"]][:2])
            if loose:
                fail(ERR_NO_PAGE_TAB,
                     f"no tab contains {spec!r} in its title or URL "
                     f"(have: {have or 'none'})")
            fail(ERR_NO_PAGE_TAB,
                 f"no tab is NAMED {spec!r}: a SPEC names a tab exactly — its "
                 "whole URL, its whole title, or id:<prefix> — and for a "
                 f"substring sweep use `tab close --like {spec!r}` "
                 f"(have: {have or 'none'})")
        if not ours and foreign:
            fail(ERR_NOT_MANAGED,
                 f"every tab matching {spec!r} is in a browser this CLI did "
                 f"not start ({foreign[0]['exe']} on "
                 f"{foreign[0]['profile']}) — a write needs "
                 "`attach --port N`, or `--browser NAME` to narrow it")
    return ours, foreign

def _filter_keys(reply: dict, *, all_tabs: bool, excepts: list[str],
                 likes: list[str], filter_used: dict,
                 skipped: list[dict]) -> dict:
    """Add the keys that say WHAT was selected, and answer `reply`.

    ONE place, because the `--dry` preview and the real reply are two halves
    of one contract: a filter key added to one path only would make the
    preview disagree with the call it previews, and the suite exercises each
    path separately. The key that says WHICH half a reply is (`would_close`
    vs `closed`) stays at the call sites, where the difference is the point.
    """
    if all_tabs or excepts:
        reply["all"] = True
    if excepts:
        reply["except"] = list(excepts)
    if likes:
        reply["like"] = likes
    if filter_used:
        reply["filter"] = {**filter_used, "exact": True}
    if skipped:
        reply["skipped"] = skipped
    return reply

def _resolve_close_set(specs: list[str], browser: str, title: str | None,
                       url: str | None, all_tabs: bool, excepts: list[str],
                       likes: list[str]) -> tuple[
                           list[tuple[dict, dict, int]], list[dict], dict]:
    """Which tabs a `tab close` call names: (ours, skipped, filter_used).

    The five ways to name tabs, one per call, resolved in ONE place and ALL of
    them BEFORE anything closes — so a bad argument, an unmatched `--except` or
    a typo cannot leave a half-applied close. `skipped` is the FOREIGN tabs a
    filter matched (named, never closed) and `filter_used` is the
    `{field: value}` a `--title`/`--url` filter carried, which the reply echoes.
    """
    ours: list[tuple[dict, dict, int]]
    skipped: list[dict] = []
    filter_used: dict = {}
    if all_tabs or excepts:
        ours, foreign = _pkg._split(_pkg._closeable(browser))
        keepers: set[str] = set()
        for spec in excepts:
            if not str(spec).strip():
                fail(ERR_BAD_ARGS, "tab close: a --except spec cannot be empty")
            matched = [tab for _r, tab, _i in ours if _match_spec([tab], spec)]
            matched += [tab for tab in foreign if _match_spec([tab], spec)]
            if not matched and (ours or foreign):
                fail(ERR_NO_PAGE_TAB,
                     f"tab close: --except {spec!r} matches no tab, so it "
                     "would keep nothing — `tab list` shows what is open")
            keepers |= {str(tab["id"]) for tab in matched}
        ours = [(row, tab, index) for row, tab, index in ours
                if str(tab["id"]) not in keepers]
        skipped = [tab for tab in foreign if str(tab["id"]) not in keepers]
    elif likes:
        ours, skipped = _spec_matches(likes, browser, loose=True)
    elif title is not None or url is not None:
        field = "title" if title is not None else "url"
        value = str(title if title is not None else url)
        ours, skipped = _exact_matches(field, value, browser)
        filter_used = {field: value}
        if not ours:
            if skipped:
                fail(ERR_NOT_MANAGED,
                     f"{len(skipped)} tab(s) match {field} {value!r}, and "
                     f"every one of them is in a browser this CLI did not "
                     f"start ({skipped[0]['exe']} on "
                     f"{skipped[0]['profile']}) — a write needs "
                     "`attach --port N`, or `--browser NAME` to narrow it"
                     )
            fail(ERR_NO_PAGE_TAB,
                 f"no tab in a browser this CLI drives has {field} exactly "
                 f"{value!r} — `tab list` shows what is open")
    else:
        ours, skipped = _spec_matches(list(specs), browser)
    return ours, skipped, filter_used

def _close_and_verify(ours: list[tuple[dict, dict, int]]) -> None:
    """Close every resolved tab, then prove each id is GONE.

    The close goes per browser, and each profile's endpoint is re-verified
    once first — `browser_call` re-reads the port FILE at call time, so a
    close must not go wherever it points. Every requested id is then read
    back: a survivor is a refusal that names it, never a silent success.
    """
    by_profile: dict[str, list[str]] = {}
    for row, tab, _index in ours:
        by_profile.setdefault(str(row["profile"]), []).append(str(tab["id"]))
    for profile, ids in by_profile.items():
        # closing a tab is a WRITE, and `browser_call` re-reads the port FILE at
        # call time: ask the kernel once per profile first (a review measured
        # that this loop went wherever the port file pointed)
        _pkg._verify_profile_endpoint(profile)
        for target_id in ids:
            cdp.browser_call(profile, "Target.closeTarget",
                             {"targetId": target_id})
    survivors: list[str] = []
    for profile, ids in by_profile.items():
        survivors += _pkg._wait_ids_gone(profile, ids)
    if survivors:
        fail(ERR_CLOSE_TAB_NOT_VERIFIED,
             f"{len(survivors)} of {len(ours)} tabs are still open: "
             + ", ".join(str(i)[:10] for i in survivors[:4]))

def close_tabs(specs: list[str], browser: str = "", title: str | None = None,
               url: str | None = None, all_tabs: bool = False,
               excepts: list[str] | None = None,
               like: list[str] | None = None, dry: bool = False) -> dict:
    """`tab close`: close every tab the arguments name, and prove it.

    Five ways to name tabs, one per call:

    * **SPECs** — `id:<prefix>`, the whole URL (a trailing slash is not a
      different page) or the whole title, case-insensitively; every match
      closes, so `tab close http://a/` takes all of them;
    * **`--like VALUE`** — a SUBSTRING sweep over titles and URLs: the loose
      form, which has to be asked for by name because `tab close a` used to
      sweep up a tab whose title merely CONTAINED an `a` (measured);
    * **`--title VALUE` / `--url VALUE`** — an exact match in one field;
    * **`--all`** — every page tab this CLI drives;
    * **`--except SPEC`** (which implies `--all`) — everything but the tabs
      those specs name; the KEEP side matches loosely (erring toward keeping is
      the safe direction), several are allowed, and one that matches NOTHING
      refuses, so a typo cannot silently keep what should have gone.

    `dry=True` resolves exactly as it would and returns the set instead of
    closing it — same arguments, same refusals, no `Target.closeTarget`. The
    reply says `dry: true` and carries `would_close` (never `closed`), so a
    caller cannot mistake a preview for an action.

    Everything to close resolves FIRST, so a bad argument, an unmatched
    exception or an ambiguous filter cannot leave a half-applied close. The
    close then goes per browser and every requested id is read back: a
    survivor is a refusal that names it. Tabs in browsers this CLI neither
    manages nor has attached are reported in `skipped`, never closed.
    """
    excepts = list(excepts or [])
    likes = list(like or [])
    named = bool(specs) or title is not None or url is not None or bool(likes)
    for flag, value in (("--title", title), ("--url", url)):
        if value is not None and not str(value):
            fail(ERR_BAD_ARGS,
                 f"tab close: {flag} needs a value — an exact title or URL")
    if any(not str(spec) for spec in excepts):
        fail(ERR_BAD_ARGS, "tab close: --except needs a value — a TAB spec")
    if any(not str(value) for value in likes):
        fail(ERR_BAD_ARGS,
             "tab close: --like needs a value — the substring to sweep for")
    if any(not str(spec).strip() for spec in specs):
        fail(ERR_BAD_ARGS, "tab close: a TAB spec cannot be empty")
    if any(str(spec).strip().lower() in ("id:", "id: ")
           for spec in specs):
        fail(ERR_BAD_ARGS,
             "tab close: `id:` needs a target id prefix — without one it "
             "names every tab")
    if title is not None and url is not None:
        fail(ERR_BAD_ARGS,
             "tab close: name tabs by --title or by --url, not both")
    if specs and (title is not None or url is not None):
        fail(ERR_BAD_ARGS,
             "tab close: name tabs by SPEC or by --title/--url, not both")
    if likes and (specs or title is not None or url is not None):
        fail(ERR_BAD_ARGS,
             "tab close: --like is a substring sweep, so it takes no SPEC, "
             "--title or --url — name tabs one way per call")
    if all_tabs and named:
        fail(ERR_BAD_ARGS,
             "tab close: --all takes no SPEC, --like, --title or --url — it "
             "already names every tab")
    if excepts and named:
        fail(ERR_BAD_ARGS,
             "tab close: --except means EVERY tab but those, so it takes no "
             "SPEC, --like, --title or --url (add --all to say it explicitly)")
    if not named and not all_tabs and not excepts:
        fail(ERR_BAD_ARGS,
             "tab close: name tabs with SPEC, --like VALUE, --title VALUE, "
             "--url URL, or --all [--except SPEC]")
    ours, skipped, filter_used = _resolve_close_set(
        specs, browser, title, url, all_tabs, excepts, likes)
    closing: list[dict] = [{"id": tab["id"], "title": tab["title"],
                            "url": tab["url"], "pid": row["pid"],
                            "managed": row["managed"]}
                           for row, tab, _index in ours]
    if dry:
        # a preview: same resolution, same refusals, no Target.closeTarget —
        # and a different KEY, so `closed` never means "would have closed"
        return _filter_keys(
            {"ok": True, "dry": True, "would_close": closing,
             "count": _tab_count(),
             "note": ("nothing was closed: --dry resolves and reports "
                      "the set the same call would take")},
            all_tabs=all_tabs, excepts=excepts, likes=likes,
            filter_used=filter_used, skipped=skipped)
    _close_and_verify(ours)
    reply = _filter_keys({"ok": True, "closed": closing,
                          "count": _tab_count()},
                         all_tabs=all_tabs, excepts=excepts, likes=likes,
                         filter_used=filter_used, skipped=skipped)
    if not ours:
        reply["note"] = ("there was no tab to close: every one was kept by "
                          "--except, or none matched")
    return reply

def new_tab(urls: list[str] | None = None, browser: str = "") -> dict:
    """Open one tab per URL (`about:blank` when none was given), all named.

    A write, so it goes to a MANAGED browser: the one `--browser` names, else
    the live managed one — never a browser this CLI did not start.
    """
    # the URL policy comes FIRST: an address this tool will never open is
    # wrong whether or not a browser is running (the order `launch` uses)
    wanted = [_pkg.safe_url(url) for url in (urls or [])] or ["about:blank"]
    profile = _pkg._writable_profile(browser)
    _pkg.ensure_up(profile)
    opened = _pkg._open_tabs(profile, wanted)
    reply: dict = {
        "ok": True, "count": len(_pkg._rows(profile)),
        "opened": [{"requested": url, "id": row["id"], "url": row["url"],
                    "title": row["title"]}
                   for url, row in zip(wanted, opened, strict=True)]}
    if len(opened) == 1:
        reply.update({"tab": f"id:{opened[0]['id']}", "id": opened[0]["id"],
                      "url": opened[0]["url"], "title": opened[0]["title"]})
    return reply

def one_tab(spec: str, browser: str, for_write: bool) -> tuple[dict, dict]:
    """(browser row, tab row) for the ONE tab a page verb acts on.

    With a spec: the same resolution every other verb uses (one match, or a
    refusal naming the candidates) — which is also how a tab in a browser
    this CLI only READS is reached.

    Without one: the tab of a browser this CLI drives (managed, or attached).
    Not "the only tab on the machine": a second drivable browser — the user's
    own, running beside ours — would otherwise make every unqualified read
    refuse `tab-ambiguous`. Several tabs in that browser still refuse: a page
    verb must never pick among tabs nobody named. For a WRITE, a browser
    nobody handed over is not a candidate at all: it is named and refused.
    """
    if spec:
        row, tab, _index = _pkg._resolve_across([spec], browser, for_write)[0]
        return row, tab
    rows = _pkg._writable(browser) if for_write else _pkg._readable(browser)
    if not rows:
        # ask `_drivable` in its STRICT form first: an endpoint that answered
        # but did not verify has to refuse `cdp-not-local`. Then a WRITE with
        # only a stranger's browser up refuses `not-managed`, naming it and the
        # two ways to hand it over — never a silent write into a session nobody
        # offered. Only when nothing answered at all is the answer "there is
        # nothing to drive": the first version blamed the endpoint for both,
        # which is a lie when no browser is running.
        strangers = _pkg._drivable(browser)
        if for_write and strangers:
            fail(ERR_NOT_MANAGED,
                 f"{_pkg._who(strangers)} is drivable, but this CLI neither manages "
                 "nor has attached it — a write needs a browser it started "
                 "(`open`), an `attach --port N` grant, or a --tab SPEC to "
                 "read a tab instead")
        _pkg._no_drive(browser)
    pairs = [(row, tab) for row in rows for tab in _pkg._tabs_or_fail(row)]
    if not pairs:
        fail(ERR_NO_PAGE_TAB,
             "no page tabs to act on — open one with "
             "`browser-control-cli tab URL`")
    if len(pairs) > 1:
        where = ", ".join(f'{flat(t["title"], 20) or flat(t["url"], 30)} '
                          f'({r["exe"]})' for r, t in pairs[:5])
        fail(ERR_TAB_AMBIGUOUS,
             f"{len(pairs)} page tabs are open — name one with --tab SPEC "
             f"(id:<prefix> or a title/url substring): {where}")
    return pairs[0]
