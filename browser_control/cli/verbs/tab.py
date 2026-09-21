"""verbs.tab — the `tab` subcommand handlers."""
from __future__ import annotations

from browser_control.cli.argv import (
    _float,
    _int,
    _needle,
    _no_flags,
    _none,
    _one,
    _opt_int,
    _pop,
    _pop_all,
    _switch,
    _tab_flag,
    _text_arg,
)
from browser_control.lib import browser as browser_lib
from browser_control.lib import dom
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    fail,
)


def _point_given(verb: str, at: str | None, needle: str | None,
                 selector: str | None, index: str | None) -> bool:
    """Was this a POINT call? `--at X,Y` REPLACES the spec instead of
    joining it, so a point given WITH a TEXT/--selector is refused by name
    rather than one of the two being silently preferred (`click` and `hover`
    are the only point-taking verbs, and they answer it the same way)."""
    if at is None:
        return False
    if needle is not None or selector is not None or index is not None:
        fail(ERR_BAD_ARGS,
             f"{verb}: --at is a POINT — give that or a TEXT/"
             "--selector (with --index), not both")
    return True


def cmd_tab_list(rest: list[str], browser: str) -> dict:
    _none(rest, "tab list")
    return browser_lib.list_tabs(browser=browser)

def cmd_tab_info(rest: list[str], browser: str) -> dict:
    return browser_lib.tab_info(_one(rest, "tab info", required=True), browser=browser)

def cmd_tab_close(rest: list[str], browser: str) -> dict:
    """`tab close SPEC... | --like V | --title V | --url V | --all [--except S]`.

    A SPEC NAMES a tab (its whole URL, its whole title, or `id:<prefix>`) and
    every tab named closes. `--like VALUE` is the sweep: every tab whose title
    or URL CONTAINS it — the loose form, asked for by name, because a spec
    that merely contains something must never be enough to close a tab.
    `--all` names every page tab this CLI drives and `--except SPEC` keeps the
    tabs those specs name (it implies `--all`; the keep side matches loosely).
    """
    rest, title = _pop(rest, "--title", "tab close")
    rest, url = _pop(rest, "--url", "tab close")
    rest, every = _switch(rest, "--all")
    rest, dry = _switch(rest, "--dry")
    rest, excepts = _pop_all(rest, "--except", "tab close")
    rest, likes = _pop_all(rest, "--like", "tab close")
    _no_flags(rest, "tab close")
    return browser_lib.close_tabs(rest, browser=browser, title=title, url=url,
                      all_tabs=every, excepts=excepts, like=likes, dry=dry)

def cmd_tab_nav(rest: list[str], browser: str) -> dict:
    """`tab nav URL [--tab SPEC]` — navigate, then read the address back."""
    rest, spec = _tab_flag(rest, "tab nav")
    _no_flags(rest, "tab nav")
    if not rest:
        fail(ERR_BAD_ARGS,
             "tab nav: a URL is required (http(s) or about:blank)")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS,
             f"tab nav: one URL at most, got {len(rest)} — the tab is "
             "--tab SPEC")
    return browser_lib.nav(rest[0], tab=spec, browser=browser)

def cmd_tab_back(rest: list[str], browser: str) -> dict:
    """`tab back [--tab SPEC]` — the history move, verified by the address."""
    rest, spec = _tab_flag(rest, "tab back")
    _none(rest, "tab back")
    return browser_lib.history("back", tab=spec, browser=browser)

def cmd_tab_forward(rest: list[str], browser: str) -> dict:
    rest, spec = _tab_flag(rest, "tab forward")
    _none(rest, "tab forward")
    return browser_lib.history("forward", tab=spec, browser=browser)

def cmd_tab_reload(rest: list[str], browser: str) -> dict:
    """`tab reload [--tab SPEC]` — a NEW document, verified."""
    rest, spec = _tab_flag(rest, "tab reload")
    _none(rest, "tab reload")
    return browser_lib.reload_page(tab=spec, browser=browser)

def cmd_tab_activate(rest: list[str], browser: str) -> dict:
    """`tab activate [SPEC]` — make that tab the frontmost one, verified."""
    return browser_lib.activate(_one(rest, "tab activate"), browser=browser)

def cmd_tab_hover(rest: list[str], browser: str) -> dict:
    """`tab hover TEXT | --selector CSS [--index N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab hover")
    rest, selector = _pop(rest, "--selector", "tab hover")
    rest, index = _pop(rest, "--index", "tab hover")
    rest, at = _pop(rest, "--at", "tab hover")
    needle = _needle(rest, "tab hover")
    if _point_given("tab hover", at, needle, selector, index):
        return dom.hover(None, at=at, tab=spec, browser=browser)
    if (needle is None) == (selector is None):
        fail(ERR_BAD_ARGS, "tab hover: give TEXT, --selector CSS, or --at X,Y")
    return dom.hover(needle, selector=selector,
                     index=_opt_int(index, "tab hover --index"),
                     tab=spec, browser=browser)

def cmd_tab_check(rest: list[str], browser: str) -> dict:
    """`tab check TEXT | --selector CSS [--index N] [--uncheck] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab check")
    rest, selector = _pop(rest, "--selector", "tab check")
    rest, index = _pop(rest, "--index", "tab check")
    rest, uncheck = _switch(rest, "--uncheck")
    needle = _needle(rest, "tab check")
    if (needle is None) == (selector is None):
        fail(ERR_BAD_ARGS, "tab check: give TEXT or --selector CSS, not both")
    return dom.check(needle, selector=selector,
                     index=_opt_int(index, "tab check --index"),
                     uncheck=uncheck, tab=spec, browser=browser)

def cmd_tab_select(rest: list[str], browser: str) -> dict:
    """`tab select TEXT | --selector CSS --value V [--index N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab select")
    rest, selector = _pop(rest, "--selector", "tab select")
    rest, index = _pop(rest, "--index", "tab select")
    rest, value = _pop(rest, "--value", "tab select")
    needle = _needle(rest, "tab select")
    if (needle is None) == (selector is None):
        fail(ERR_BAD_ARGS, "tab select: give TEXT or --selector CSS, not both")
    if value is None:
        fail(ERR_BAD_ARGS,
             "tab select: --value is required — the option's value, or its "
             "exact label")
    return dom.select(needle, selector=selector, value=value,
                      index=_opt_int(index, "tab select --index"),
                      tab=spec, browser=browser)

def cmd_tab_dialog(rest: list[str], browser: str) -> dict:
    """`tab dialog [state|accept|dismiss] [--text VALUE] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab dialog")
    rest, text = _pop(rest, "--text", "tab dialog")
    mode = _one(rest, "tab dialog") or "state"
    if text is not None and mode != "accept":
        fail(ERR_BAD_ARGS,
             "tab dialog: --text is the answer to a prompt — it goes with "
             "`accept`")
    return dom.dialog(mode, text=text, tab=spec, browser=browser)

def cmd_tab_screenshot(rest: list[str], browser: str) -> dict:
    """`tab screenshot PATH | --path PATH [--full] [--force] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab screenshot")
    rest, path = _pop(rest, "--path", "tab screenshot")
    rest, full = _switch(rest, "--full")
    rest, force = _switch(rest, "--force")
    _no_flags(rest, "tab screenshot")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab screenshot: one PATH at most, got {len(rest)}")
    if rest and path is not None:
        fail(ERR_BAD_ARGS,
             "tab screenshot: give PATH or --path PATH, not both")
    target = path if path is not None else (rest[0] if rest else "")
    if not target:
        fail(ERR_BAD_ARGS,
             "tab screenshot: a PATH is required (an absolute path ending in "
             ".png)")
    return dom.screenshot(target, full=full, force=force, tab=spec,
                          browser=browser)

def cmd_tab_extract(rest: list[str], browser: str) -> dict:
    """`tab extract --each CSS --field NAME=SPEC ...` — records, no code."""
    rest, spec = _tab_flag(rest, "tab extract")
    rest, each = _pop(rest, "--each", "tab extract")
    rest, cap = _pop(rest, "--cap", "tab extract")
    rest, chars = _pop(rest, "--chars", "tab extract")
    rest, visible = _switch(rest, "--visible")
    rest, unique = _pop(rest, "--unique", "tab extract")
    rest, fields = _pop_all(rest, "--field", "tab extract")
    _none(rest, "tab extract")
    return dom.extract(
        each or "", fields,
        cap=(_int(cap, "tab extract --cap") if cap is not None
             else dom.EXTRACT_CAP),
        chars=(_int(chars, "tab extract --chars") if chars is not None
               else dom.EXTRACT_FIELD_CHARS),
        visible=visible, unique=unique or "", tab=spec, browser=browser)

def cmd_tab_js(rest: list[str], browser: str) -> dict:
    """`tab js EXPR [--tab SPEC]` — the escape hatch, declared unverified."""
    rest, spec = _tab_flag(rest, "tab js")
    _no_flags(rest, "tab js")
    if not rest:
        fail(ERR_BAD_ARGS, "tab js: an EXPRESSION is required")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS,
             f"tab js: one expression at most, got {len(rest)} — the tab is "
             "--tab SPEC")
    return dom.js(rest[0], tab=spec, browser=browser)

def cmd_tab_frames(rest: list[str], browser: str) -> dict:
    """`tab frames [--tab SPEC]` — this page's iframes, and which are drivable.

    The verb resolves its own tab (`dom.frames(tab=, browser=)`), like every
    other page verb: the CLI no longer reaches the private resolver.
    """
    rest, spec = _tab_flag(rest, "tab frames")
    _none(rest, "tab frames")
    return dom.frames(tab=spec, browser=browser)

def cmd_tab_find(rest: list[str], browser: str) -> dict:
    """`tab find TEXT | --selector CSS [--cap N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab find")
    rest, selector = _pop(rest, "--selector", "tab find")
    rest, cap = _pop(rest, "--cap", "tab find")
    needle = _needle(rest, "tab find")
    if (needle is None) == (selector is None):
        fail(ERR_BAD_ARGS, "tab find: give TEXT or --selector CSS, not both")
    return dom.find(needle, selector=selector,
                    cap=_int(cap, "tab find --cap") if cap is not None
                    else dom.FIND_CAP,
                    tab=spec, browser=browser)

def cmd_tab_text(rest: list[str], browser: str) -> dict:
    """`tab text [--selector CSS] [--chars N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab text")
    rest, selector = _pop(rest, "--selector", "tab text")
    rest, chars = _pop(rest, "--chars", "tab text")
    _none(rest, "tab text")
    return dom.text(selector=selector,
                    chars=_int(chars, "tab text --chars") if chars is not None
                    else dom.TEXT_CAP,
                    tab=spec, browser=browser)

def cmd_tab_wait(rest: list[str], browser: str) -> dict:
    """`tab wait --for load|idle|element|js …` — poll one predicate."""
    rest, spec = _tab_flag(rest, "tab wait")
    rest, mode = _pop(rest, "--for", "tab wait")
    rest, selector = _pop(rest, "--selector", "tab wait")
    rest, expr = _pop(rest, "--expr", "tab wait")
    rest, timeout = _pop(rest, "--timeout", "tab wait")
    rest, idle = _pop(rest, "--idle-ms", "tab wait")
    _none(rest, "tab wait")
    if mode is None:
        fail(ERR_BAD_ARGS, "tab wait: --for is required (load|idle|element|js)")
    return dom.wait(mode, selector=selector, expr=expr,
                    timeout=_float(timeout, "tab wait --timeout")
                    if timeout is not None else dom.WAIT_DEFAULT_S,
                    idle_ms=_int(idle, "tab wait --idle-ms")
                    if idle is not None else dom.IDLE_DEFAULT_MS,
                    tab=spec, browser=browser)

def cmd_tab_click(rest: list[str], browser: str) -> dict:
    """`tab click TEXT | --selector CSS [--index N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab click")
    rest, selector = _pop(rest, "--selector", "tab click")
    rest, index = _pop(rest, "--index", "tab click")
    rest, at = _pop(rest, "--at", "tab click")
    needle = _needle(rest, "tab click")
    # `--at X,Y` is a POINT: it replaces the spec instead of joining it
    if _point_given("tab click", at, needle, selector, index):
        return dom.click(None, at=at, tab=spec, browser=browser)
    if (needle is None) == (selector is None):
        fail(ERR_BAD_ARGS, "tab click: give TEXT, --selector CSS, or --at X,Y")
    return dom.click(needle, selector=selector,
                     index=_opt_int(index, "tab click --index"),
                     tab=spec, browser=browser)

def cmd_tab_scroll(rest: list[str], browser: str) -> dict:
    """`tab scroll --by N | --edge top|bottom | TEXT|--selector CSS`."""
    rest, spec = _tab_flag(rest, "tab scroll")
    rest, by = _pop(rest, "--by", "tab scroll")
    rest, edge = _pop(rest, "--edge", "tab scroll")
    rest, selector = _pop(rest, "--selector", "tab scroll")
    rest, index = _pop(rest, "--index", "tab scroll")
    rest, at = _pop(rest, "--at", "tab scroll")
    needle = _needle(rest, "tab scroll")
    return dom.scroll(
        by=_opt_int(by, "tab scroll --by"),
        edge=edge, text=needle, selector=selector,
        index=_opt_int(index, "tab scroll --index"),
        at=at, tab=spec, browser=browser)

def cmd_tab_focus(rest: list[str], browser: str) -> dict:
    """`tab focus TEXT | --selector CSS [--index N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab focus")
    rest, selector = _pop(rest, "--selector", "tab focus")
    rest, index = _pop(rest, "--index", "tab focus")
    needle = _needle(rest, "tab focus")
    if (needle is None) == (selector is None):
        fail(ERR_BAD_ARGS, "tab focus: give TEXT or --selector CSS, not both")
    return dom.focus(needle, selector=selector,
                     index=_opt_int(index, "tab focus --index"),
                     tab=spec, browser=browser)

def cmd_tab_press(rest: list[str], browser: str) -> dict:
    """`tab press KEY [--tab SPEC]` — one key event at the DOM focus."""
    rest, spec = _tab_flag(rest, "tab press")
    _no_flags(rest, "tab press")
    if not rest:
        fail(ERR_BAD_ARGS, "tab press: KEY is required (enter, tab, escape, …)")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab press: one KEY at most, got {len(rest)}")
    return dom.press(rest[0], tab=spec, browser=browser)

def cmd_tab_insert(rest: list[str], browser: str) -> dict:
    """`tab insert TEXT [--tab SPEC]` — atomic insert at the DOM focus."""
    rest, spec = _tab_flag(rest, "tab insert")
    return dom.insert(_text_arg(rest, "tab insert"), tab=spec,
                      browser=browser)

def cmd_tab_type(rest: list[str], browser: str) -> dict:
    """`tab type TEXT [--tab SPEC]` — real per-character key events."""
    rest, spec = _tab_flag(rest, "tab type")
    return dom.type_text(_text_arg(rest, "tab type"), tab=spec,
                         browser=browser)

def cmd_tab_upload(rest: list[str], browser: str) -> dict:
    """`tab upload FILE [--selector CSS] [--index N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab upload")
    rest, selector = _pop(rest, "--selector", "tab upload")
    rest, index = _pop(rest, "--index", "tab upload")
    _no_flags(rest, "tab upload")
    if not rest:
        fail(ERR_BAD_ARGS, "tab upload: FILE is required (an absolute path)")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab upload: one FILE at most, got {len(rest)}")
    return dom.upload(rest[0], selector=selector,
                      index=_opt_int(index, "tab upload --index"),
                      tab=spec, browser=browser)

def cmd_tab_media(rest: list[str], browser: str) -> dict:
    """`tab media state|play|pause [--index N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab media")
    rest, index = _pop(rest, "--index", "tab media")
    _no_flags(rest, "tab media")
    if not rest:
        fail(ERR_BAD_ARGS, "tab media: MODE is required (state, play or pause)")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab media: one MODE at most, got {len(rest)}")
    return dom.media(rest[0],
                     index=_opt_int(index, "tab media --index"),
                     tab=spec, browser=browser)
