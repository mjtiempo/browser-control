"""browser-control-cli — argv in, service calls out, JSON printed.

One adapter per verb in `HANDLERS`; `main` parses `--browser`, dispatches,
prints the reply as one JSON object on stdout, and maps a refusal to
`ERR[code]: message` on stderr with exit 2. Nothing else prints, and no
caller-producible input can produce a traceback.
"""
from __future__ import annotations

import json
import math
import platform
import shutil
import sys
from collections.abc import Callable

# The project-level pyright run resolves these imports; the line-level ignores
# are for pi-lens's fallback index, which does not see the sibling modules.
from browser_control import __version__
from browser_control.lib import browser as browser_lib  # pyright: ignore[reportMissingImports]
from browser_control.lib import cdp  # pyright: ignore[reportMissingImports]
from browser_control.lib import audit  # pyright: ignore[reportMissingImports]
from browser_control.lib import dom  # pyright: ignore[reportMissingImports]
from browser_control.lib.browser import (  # pyright: ignore[reportMissingImports]
    activate,
    attach,
    attachments,
    browser_info,
    close_tabs,
    detach,
    history,
    launch,
    list_browsers,
    list_tabs,
    nav,
    new_tab,
    reload,
    stop,
    tab_info,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ControlError,
    fail,
)

USAGE = """usage: browser-control-cli VERB [ARGS]

  open [URL...]      start (or adopt) the managed browser; each URL opens
  close              stop the managed browser this CLI started
  list               every Chromium-family browser running here, ours or not
  info               the browser this CLI would drive, and its endpoint
  attach [--port N|--pid N|--profile DIR]
                     allow TAB writes to a browser this CLI did not start
  attach --list      what is attached, and whether it is still up
  detach [--port N|--pid N|--profile DIR|--all]
                     revoke that authorization
  tab [URL...]       open one tab per URL (about:blank when none)
  tab list           every drivable browser's page tabs, by browser
  tab info SPEC      one tab: `id:<prefix>` or a title/url substring
  tab close SPEC...  close every tab the specs name, verified as a set
  tab nav URL [--tab SPEC]       navigate, then read the address back
  tab back|forward [--tab SPEC]  history, verified by the address changing
  tab reload [--tab SPEC]        a NEW document, verified
  tab activate [SPEC]            bring a tab forward (it raises its window)
  tab hover TEXT|--selector CSS [--index N] [--tab SPEC]
                                 put the pointer on an element (`:hover`)
  tab check TEXT|--selector CSS [--index N] [--uncheck] [--tab SPEC]
                                 check a box or radio with real input
  tab select TEXT|--selector CSS --value V [--index N] [--tab SPEC]
                                 choose one <option> with real arrow keys
  tab dialog [state|accept|dismiss] [--text VALUE] [--tab SPEC]
                                 read, accept or dismiss a JavaScript dialog
  tab screenshot PATH [--full] [--force] [--tab SPEC]
                                 write a PNG of the page (its header vouches)
  tab js EXPR [--tab SPEC]       evaluate an expression (can write; unverified)
  tab find TEXT|--selector CSS [--cap N] [--tab SPEC]
                                 a visible element, in PAGE coordinates
  tab text [--selector CSS] [--chars N] [--tab SPEC]
                                 the rendered text, truncated in the page
  tab wait --for load|idle|element|js [--selector CSS] [--expr EXPR]
           [--timeout S] [--idle-ms MS] [--tab SPEC]
                                 poll a predicate to a wall-clock deadline
  tab click TEXT|--selector CSS [--index N] [--tab SPEC]
                                 real input (CDP) at the element's centre
  tab scroll --by N [--at X,Y] [--tab SPEC]
                                 one wheel event; nested scrollers included
  tab scroll --edge top|bottom [--tab SPEC]
                                 wheel until the edge is reached, verified
  tab scroll TEXT|--selector CSS [--index N] [--tab SPEC]
                                 bring one element into view (a CDP method)
  tab focus TEXT|--selector CSS [--index N] [--tab SPEC]
                                 put the DOM focus (the caret) on an element
  tab press KEY [--tab SPEC]     one key event at the focus (enter, tab, …)
  tab insert TEXT [--tab SPEC]   insert TEXT atomically at the focus
  tab type TEXT [--tab SPEC]     type TEXT as real per-character key events
  tab upload FILE [--selector CSS] [--index N] [--tab SPEC]
                                 attach a file to an <input type=file>
  tab media state|play|pause [--index N] [--tab SPEC]
                                 read or drive the page's video/audio
  selftest           prove the install: interpreter, websockets, verbs

SPEC   a CDP target id prefix (`id:2D4BC76C`), `active` (the tab whose page
       reports itself visible — only one per window), or a title/url
       substring; a spec matching nothing, or several tabs, refuses and names
       them
flags: --browser NAME   the browser to drive (open/close/tab) or to narrow
                        (info/tab list); default: a live managed browser, else
                        the first Chromium-family one on PATH
       --tab SPEC       the tab a page verb acts on (nav/back/forward/reload);
                        without it the verb acts on the ONLY page tab there is
reads: every drivable browser.  writes: a managed browser, or an attached one
       — `attach` grants TAB writes only, `close` never stops one; `tab js`
       and `tab wait --for js` count as writes (they run caller code)
out:   one JSON object on stdout; ERR[code]: message on stderr, exit 2"""


def _one(rest: list[str], verb: str, required: bool = False) -> str:
    """The verb's single positional argument, or a refusal."""
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"{verb}: unknown flag {arg!r}")
    if len(rest) > 1:
        fail("bad-args", f"{verb}: one argument at most, got {len(rest)}")
    if not rest:
        if required:
            fail("bad-args",
                 f"{verb}: a TAB spec is required (id:<prefix> or a "
                 "title/url substring)")
        return ""
    return rest[0]


def _none(rest: list[str], verb: str) -> None:
    for arg in rest:
        fail("bad-args", f"{verb}: takes no arguments, got {arg!r}")


def _urls(rest: list[str], verb: str) -> list[str]:
    """Every URL this verb was given, in order — none is dropped.

    A flag is refused rather than ignored (the tab is `--tab`, and these verbs
    take no other), and a caller that asks for three sites gets three.
    """
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"{verb}: unknown flag {arg!r}")
    return list(rest)


def _no_browser_flag(verb: str, browser: str) -> None:
    """Refuse `--browser` on a verb that reports every browser it finds."""
    if browser:
        fail("bad-args",
             f"{verb}: --browser does not apply — this verb reports every "
             "browser on the machine")


def _int(value: str, what: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        fail("bad-args", f"{what} needs a number, got {value!r}")


def _selector(rest: list[str], verb: str, allow: tuple[str, ...]) -> dict:
    """`--port N | --pid N | --profile DIR`, plus `--list`/`--all` where a
    verb allows them. Pure argv work: an unknown flag is refused, and the
    combinations that mean two different things are refused too."""
    out = {"port": 0, "pid": 0, "profile": "", "list": False,
           "all": False}
    index = 0
    while index < len(rest):
        arg = str(rest[index])
        if arg == "--list" and "list" in allow:
            out["list"] = True
            index += 1
            continue
        if arg == "--all" and "all" in allow:
            out["all"] = True
            index += 1
            continue
        if arg in ("--port", "--pid", "--profile"):
            if index + 1 >= len(rest):
                fail("bad-args", f"{verb}: {arg} needs a value")
            key, value = arg[2:], str(rest[index + 1])
            if key in ("port", "pid"):
                out[key] = _int(value, f"{verb}: {arg}")
            else:
                out[key] = value
            index += 2
            continue
        known = ", ".join(["--port N", "--pid N", "--profile DIR"]
                          + [f"--{name}" for name in allow])
        fail("bad-args",
             f"{verb}: unknown argument {arg!r} (flags: {known})")
    if out["list"] and (out["all"] or out["port"] or out["pid"]
                        or out["profile"]):
        fail("bad-args", f"{verb}: --list takes no other argument")
    if out["all"] and (out["port"] or out["pid"] or out["profile"]):
        fail("bad-args", f"{verb}: --all takes no other selector")
    return out


def cmd_attach(rest: list[str], browser: str) -> dict:
    """`attach --port N|--pid N|--profile DIR` / `attach --list`."""
    _no_browser_flag("attach", browser)
    selector = _selector(rest, "attach", ("list",))
    if selector["list"]:
        return attachments()
    return attach(port=selector["port"], pid=selector["pid"],
                  profile=selector["profile"])


def cmd_detach(rest: list[str], browser: str) -> dict:
    """`detach ...` / `detach --all` — revoke a tab-write authorization."""
    _no_browser_flag("detach", browser)
    selector = _selector(rest, "detach", ("all",))
    return detach(port=selector["port"], pid=selector["pid"],
                  profile=selector["profile"], detach_all=selector["all"])


def _specs(rest: list[str], verb: str) -> list[str]:
    """Every TAB spec this verb was given, in order — at least one.

    A flag is refused rather than ignored (`--browser` is taken by `main`),
    and a caller that names three tabs gets three closed.
    """
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"{verb}: unknown flag {arg!r}")
    if not rest:
        fail("bad-args",
             f"{verb}: at least one TAB spec is required (id:<prefix> or a "
             "title/url substring)")
    return list(rest)


def cmd_open(rest: list[str], browser: str) -> dict:
    return launch(_urls(rest, "open"), browser=browser)


def cmd_close(rest: list[str], browser: str) -> dict:
    _none(rest, "close")
    return stop(browser=browser)


def cmd_list(rest: list[str], browser: str) -> dict:
    _none(rest, "list")
    _no_browser_flag("list", browser)
    return list_browsers()


def cmd_info(rest: list[str], browser: str) -> dict:
    _none(rest, "info")
    return browser_info(browser=browser)


def cmd_tab(rest: list[str], browser: str) -> dict:
    """`tab [URL...]` opens tabs; `tab list|info|close` are subcommands.

    A subcommand is a reserved word, so `tab list` can never mean "open the
    site `list`" — a URL carries a scheme (`https://…`, `about:blank`), which
    is what the URL policy enforces anyway.
    """
    handler = TAB_SUBCOMMANDS.get(str(rest[0]) if rest else "")
    if handler is not None:
        return handler(rest[1:], browser)
    return new_tab(_urls(rest, "tab"), browser=browser)


def cmd_selftest(rest: list[str], browser: str) -> dict:
    """Prove the install without a browser: interpreter, dependency, verbs.

    The one thing that FAILS here is a missing `websockets`: without it no
    verb can speak CDP, and an install that cannot reach a browser should say
    so at once rather than at the first `tabs`. A machine with no browser is
    reported, not failed — the command is installed either way.
    """
    _none(rest, "selftest")
    if cdp.websockets is None:
        fail("no-websockets",
             "the `websockets` package is required to speak CDP "
             "(pip install websockets)")
    found = []
    for name in browser_lib.BROWSER_BINS:
        path = shutil.which(name)
        if path:
            found.append({"name": name, "path": path})
    reply = {"ok": True, "command": "browser-control-cli",
             "version": __version__,
             "python": sys.executable,
             "python_version": platform.python_version(),
             "websockets": getattr(cdp.websockets, "__version__", "unknown"),
             "profile_root": browser_lib.root(),
             "verbs": sorted(HANDLERS),
             "browsers": found}
    if browser:
        reply["requested"] = {"name": browser,
                              "path": shutil.which(browser) or ""}
    if not found:
        reply["warning"] = ("no Chromium-family browser on PATH — `open` "
                            "will refuse until one is installed")
    return reply


Handler = Callable[[list[str], str], dict]


def _pop(rest: list[str], flag: str, verb: str) -> tuple[list[str], str | None]:
    """Remove `--flag VALUE` (or `--flag=VALUE`) from argv, once.

    None means the flag was NOT given, which a verb must be able to tell apart
    from an empty value.
    """
    out: list[str] = []
    value: str | None = None
    index = 0
    while index < len(rest):
        arg = str(rest[index])
        if arg == flag:
            if index + 1 >= len(rest):
                fail("bad-args", f"{verb}: {flag} needs a value")
            value = str(rest[index + 1])
            index += 2
            continue
        if arg.startswith(flag + "="):
            value = arg.split("=", 1)[1]
            index += 1
            continue
        out.append(arg)
        index += 1
    return out, value


def _switch(rest: list[str], flag: str) -> tuple[list[str], bool]:
    """Pull a VALUE-LESS flag (`--uncheck`, `--full`, `--force`) out of argv."""
    given = any(str(arg) == flag for arg in rest)
    return [arg for arg in rest if str(arg) != flag], given


def _tab_flag(rest: list[str], verb: str) -> tuple[list[str], str]:
    """`--tab SPEC`, or "" — the tab a page verb acts on."""
    rest, spec = _pop(rest, "--tab", verb)
    return rest, spec or ""


def _float(value: str, what: str) -> float:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        fail("bad-args", f"{what} needs a number, got {value!r}")
    if not math.isfinite(number):
        fail("bad-args", f"{what} must be a finite number, got {value!r}")
    return number


def cmd_tab_list(rest: list[str], browser: str) -> dict:
    _none(rest, "tab list")
    return list_tabs(browser=browser)


def cmd_tab_info(rest: list[str], browser: str) -> dict:
    return tab_info(_one(rest, "tab info", required=True), browser=browser)


def cmd_tab_close(rest: list[str], browser: str) -> dict:
    return close_tabs(_specs(rest, "tab close"), browser=browser)


def cmd_tab_nav(rest: list[str], browser: str) -> dict:
    """`tab nav URL [--tab SPEC]` — navigate, then read the address back."""
    rest, spec = _tab_flag(rest, "tab nav")
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"tab nav: unknown flag {arg!r}")
    if not rest:
        fail("bad-args",
             "tab nav: a URL is required (http(s) or about:blank)")
    if len(rest) > 1:
        fail("bad-args",
             f"tab nav: one URL at most, got {len(rest)} — the tab is "
             "--tab SPEC")
    return nav(rest[0], tab=spec, browser=browser)


def cmd_tab_back(rest: list[str], browser: str) -> dict:
    """`tab back [--tab SPEC]` — the history move, verified by the address."""
    rest, spec = _tab_flag(rest, "tab back")
    _none(rest, "tab back")
    return history("back", tab=spec, browser=browser)


def cmd_tab_forward(rest: list[str], browser: str) -> dict:
    rest, spec = _tab_flag(rest, "tab forward")
    _none(rest, "tab forward")
    return history("forward", tab=spec, browser=browser)


def cmd_tab_reload(rest: list[str], browser: str) -> dict:
    """`tab reload [--tab SPEC]` — a NEW document, verified."""
    rest, spec = _tab_flag(rest, "tab reload")
    _none(rest, "tab reload")
    return reload(tab=spec, browser=browser)


def cmd_tab_activate(rest: list[str], browser: str) -> dict:
    """`tab activate [SPEC]` — make that tab the frontmost one, verified."""
    return activate(_one(rest, "tab activate"), browser=browser)


def cmd_tab_hover(rest: list[str], browser: str) -> dict:
    """`tab hover TEXT | --selector CSS [--index N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab hover")
    rest, selector = _pop(rest, "--selector", "tab hover")
    rest, index = _pop(rest, "--index", "tab hover")
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"tab hover: unknown flag {arg!r}")
    if len(rest) > 1:
        fail("bad-args", f"tab hover: one TEXT at most, got {len(rest)}")
    needle = rest[0] if rest else None
    if (needle is None) == (selector is None):
        fail("bad-args", "tab hover: give TEXT or --selector CSS, not both")
    return dom.hover(needle, selector=selector,
                     index=_int(index, "tab hover --index")
                     if index is not None else None,
                     tab=spec, browser=browser)


def cmd_tab_check(rest: list[str], browser: str) -> dict:
    """`tab check TEXT | --selector CSS [--index N] [--uncheck] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab check")
    rest, selector = _pop(rest, "--selector", "tab check")
    rest, index = _pop(rest, "--index", "tab check")
    rest, uncheck = _switch(rest, "--uncheck")
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"tab check: unknown flag {arg!r}")
    if len(rest) > 1:
        fail("bad-args", f"tab check: one TEXT at most, got {len(rest)}")
    needle = rest[0] if rest else None
    if (needle is None) == (selector is None):
        fail("bad-args", "tab check: give TEXT or --selector CSS, not both")
    return dom.check(needle, selector=selector,
                     index=_int(index, "tab check --index")
                     if index is not None else None,
                     uncheck=uncheck, tab=spec, browser=browser)


def cmd_tab_select(rest: list[str], browser: str) -> dict:
    """`tab select TEXT | --selector CSS --value V [--index N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab select")
    rest, selector = _pop(rest, "--selector", "tab select")
    rest, index = _pop(rest, "--index", "tab select")
    rest, value = _pop(rest, "--value", "tab select")
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"tab select: unknown flag {arg!r}")
    if len(rest) > 1:
        fail("bad-args", f"tab select: one TEXT at most, got {len(rest)}")
    needle = rest[0] if rest else None
    if (needle is None) == (selector is None):
        fail("bad-args", "tab select: give TEXT or --selector CSS, not both")
    if value is None:
        fail("bad-args",
             "tab select: --value is required — the option's value, or its "
             "exact label")
    return dom.select(needle, selector=selector, value=value,
                      index=_int(index, "tab select --index")
                      if index is not None else None,
                      tab=spec, browser=browser)


def cmd_tab_dialog(rest: list[str], browser: str) -> dict:
    """`tab dialog [state|accept|dismiss] [--text VALUE] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab dialog")
    rest, text = _pop(rest, "--text", "tab dialog")
    mode = _one(rest, "tab dialog") or "state"
    if text is not None and mode != "accept":
        fail("bad-args",
             "tab dialog: --text is the answer to a prompt — it goes with "
             "`accept`")
    return dom.dialog(mode, text=text, tab=spec, browser=browser)


def cmd_tab_screenshot(rest: list[str], browser: str) -> dict:
    """`tab screenshot PATH | --path PATH [--full] [--force] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab screenshot")
    rest, path = _pop(rest, "--path", "tab screenshot")
    rest, full = _switch(rest, "--full")
    rest, force = _switch(rest, "--force")
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"tab screenshot: unknown flag {arg!r}")
    if len(rest) > 1:
        fail("bad-args", f"tab screenshot: one PATH at most, got {len(rest)}")
    if rest and path is not None:
        fail("bad-args",
             "tab screenshot: give PATH or --path PATH, not both")
    target = path if path is not None else (rest[0] if rest else "")
    if not target:
        fail("bad-args",
             "tab screenshot: a PATH is required (an absolute path ending in "
             ".png)")
    return dom.screenshot(target, full=full, force=force, tab=spec,
                          browser=browser)


def cmd_tab_js(rest: list[str], browser: str) -> dict:
    """`tab js EXPR [--tab SPEC]` — the escape hatch, declared unverified."""
    rest, spec = _tab_flag(rest, "tab js")
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"tab js: unknown flag {arg!r}")
    if not rest:
        fail("bad-args", "tab js: an EXPRESSION is required")
    if len(rest) > 1:
        fail("bad-args",
             f"tab js: one expression at most, got {len(rest)} — the tab is "
             "--tab SPEC")
    return dom.js(rest[0], tab=spec, browser=browser)


def cmd_tab_find(rest: list[str], browser: str) -> dict:
    """`tab find TEXT | --selector CSS [--cap N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab find")
    rest, selector = _pop(rest, "--selector", "tab find")
    rest, cap = _pop(rest, "--cap", "tab find")
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"tab find: unknown flag {arg!r}")
    if len(rest) > 1:
        fail("bad-args", f"tab find: one TEXT at most, got {len(rest)}")
    needle = rest[0] if rest else None
    if (needle is None) == (selector is None):
        fail("bad-args", "tab find: give TEXT or --selector CSS, not both")
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
        fail("bad-args", "tab wait: --for is required (load|idle|element|js)")
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
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"tab click: unknown flag {arg!r}")
    if len(rest) > 1:
        fail("bad-args", f"tab click: one TEXT at most, got {len(rest)}")
    needle = rest[0] if rest else None
    if (needle is None) == (selector is None):
        fail("bad-args", "tab click: give TEXT or --selector CSS, not both")
    return dom.click(needle, selector=selector,
                     index=_int(index, "tab click --index")
                     if index is not None else None,
                     tab=spec, browser=browser)


def cmd_tab_scroll(rest: list[str], browser: str) -> dict:
    """`tab scroll --by N | --edge top|bottom | TEXT|--selector CSS`."""
    rest, spec = _tab_flag(rest, "tab scroll")
    rest, by = _pop(rest, "--by", "tab scroll")
    rest, edge = _pop(rest, "--edge", "tab scroll")
    rest, selector = _pop(rest, "--selector", "tab scroll")
    rest, index = _pop(rest, "--index", "tab scroll")
    rest, at = _pop(rest, "--at", "tab scroll")
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"tab scroll: unknown flag {arg!r}")
    if len(rest) > 1:
        fail("bad-args", f"tab scroll: one TEXT at most, got {len(rest)}")
    return dom.scroll(
        by=_int(by, "tab scroll --by") if by is not None else None,
        edge=edge, text=rest[0] if rest else None, selector=selector,
        index=_int(index, "tab scroll --index") if index is not None else None,
        at=at, tab=spec, browser=browser)


def cmd_tab_focus(rest: list[str], browser: str) -> dict:
    """`tab focus TEXT | --selector CSS [--index N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab focus")
    rest, selector = _pop(rest, "--selector", "tab focus")
    rest, index = _pop(rest, "--index", "tab focus")
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"tab focus: unknown flag {arg!r}")
    if len(rest) > 1:
        fail("bad-args", f"tab focus: one TEXT at most, got {len(rest)}")
    needle = rest[0] if rest else None
    if (needle is None) == (selector is None):
        fail("bad-args", "tab focus: give TEXT or --selector CSS, not both")
    return dom.focus(needle, selector=selector,
                     index=_int(index, "tab focus --index")
                     if index is not None else None,
                     tab=spec, browser=browser)


def cmd_tab_press(rest: list[str], browser: str) -> dict:
    """`tab press KEY [--tab SPEC]` — one key event at the DOM focus."""
    rest, spec = _tab_flag(rest, "tab press")
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"tab press: unknown flag {arg!r}")
    if not rest:
        fail("bad-args", "tab press: KEY is required (enter, tab, escape, …)")
    if len(rest) > 1:
        fail("bad-args", f"tab press: one KEY at most, got {len(rest)}")
    return dom.press(rest[0], tab=spec, browser=browser)


def _text_arg(rest: list[str], verb: str, tab: str) -> str:
    """The single TEXT a writing verb takes — nothing else, and no flags."""
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"{verb}: unknown flag {arg!r}")
    if not rest:
        fail("bad-args", f"{verb}: TEXT is required")
    if len(rest) > 1:
        fail("bad-args",
             f"{verb}: one TEXT at most, got {len(rest)} — quote it if it "
             "has spaces")
    return rest[0]


def cmd_tab_insert(rest: list[str], browser: str) -> dict:
    """`tab insert TEXT [--tab SPEC]` — atomic insert at the DOM focus."""
    rest, spec = _tab_flag(rest, "tab insert")
    return dom.insert(_text_arg(rest, "tab insert", spec), tab=spec,
                      browser=browser)


def cmd_tab_type(rest: list[str], browser: str) -> dict:
    """`tab type TEXT [--tab SPEC]` — real per-character key events."""
    rest, spec = _tab_flag(rest, "tab type")
    return dom.type_text(_text_arg(rest, "tab type", spec), tab=spec,
                         browser=browser)


def cmd_tab_upload(rest: list[str], browser: str) -> dict:
    """`tab upload FILE [--selector CSS] [--index N] [--tab SPEC]`."""
    """`tab upload FILE [--selector CSS] [--index N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab upload")
    rest, selector = _pop(rest, "--selector", "tab upload")
    rest, index = _pop(rest, "--index", "tab upload")
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"tab upload: unknown flag {arg!r}")
    if not rest:
        fail("bad-args", "tab upload: FILE is required (an absolute path)")
    if len(rest) > 1:
        fail("bad-args", f"tab upload: one FILE at most, got {len(rest)}")
    return dom.upload(rest[0], selector=selector,
                      index=_int(index, "tab upload --index")
                      if index is not None else None,
                      tab=spec, browser=browser)


def cmd_tab_media(rest: list[str], browser: str) -> dict:
    """`tab media state|play|pause [--index N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab media")
    rest, index = _pop(rest, "--index", "tab media")
    for arg in rest:
        if str(arg).startswith("-"):
            fail("bad-args", f"tab media: unknown flag {arg!r}")
    if not rest:
        fail("bad-args", "tab media: MODE is required (state, play or pause)")
    if len(rest) > 1:
        fail("bad-args", f"tab media: one MODE at most, got {len(rest)}")
    return dom.media(rest[0],
                     index=_int(index, "tab media --index")
                     if index is not None else None,
                     tab=spec, browser=browser)


# `tab`'s subcommands: a reserved first word, so a URL can never be mistaken
# for one (and vice versa).
TAB_SUBCOMMANDS: dict[str, Handler] = {
    "list": cmd_tab_list,
    "info": cmd_tab_info,
    "close": cmd_tab_close,
    "nav": cmd_tab_nav,
    "back": cmd_tab_back,
    "forward": cmd_tab_forward,
    "reload": cmd_tab_reload,
    "activate": cmd_tab_activate,
    "hover": cmd_tab_hover,
    "check": cmd_tab_check,
    "select": cmd_tab_select,
    "dialog": cmd_tab_dialog,
    "screenshot": cmd_tab_screenshot,
    "js": cmd_tab_js,
    "find": cmd_tab_find,
    "text": cmd_tab_text,
    "wait": cmd_tab_wait,
    "click": cmd_tab_click,
    "scroll": cmd_tab_scroll,
    "focus": cmd_tab_focus,
    "press": cmd_tab_press,
    "insert": cmd_tab_insert,
    "type": cmd_tab_type,
    "upload": cmd_tab_upload,
    "media": cmd_tab_media,
}

HANDLERS: dict[str, Handler] = {
    "open": cmd_open,
    "close": cmd_close,
    "list": cmd_list,
    "info": cmd_info,
    "attach": cmd_attach,
    "detach": cmd_detach,
    "tab": cmd_tab,
    "selftest": cmd_selftest,
}


def _browser_flag(args: list[str]) -> tuple[list[str], str]:
    """Pull `--browser NAME` (or `--browser=NAME`) out of argv, anywhere."""
    rest: list[str] = []
    browser = ""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--browser":
            if i + 1 >= len(args):
                fail("bad-args", "--browser needs a NAME")
            browser = args[i + 1]
            i += 2
            continue
        if arg.startswith("--browser="):
            browser = arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    return rest, browser


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if not args:
        print(USAGE, file=sys.stderr)
        print("ERR[bad-args]: a verb is required "
              f"(have: {', '.join(HANDLERS)})", file=sys.stderr)
        return 2
    verb = ""
    ok = False
    code: str | None = None
    try:
        rest, browser = _browser_flag(args)
        verb, rest = rest[0], rest[1:]
        audit.LOG.begin(verb)          # no secret is known yet
        handler = HANDLERS.get(verb)
        if handler is None:
            raise ControlError("unknown-command",
                               f"{verb} (have: {', '.join(HANDLERS)})")
        print(json.dumps(handler(rest, browser)))
        ok = True
        return 0
    except ControlError as e:
        code = e.code
        print(f"ERR[{e.code}]: {e.message}", file=sys.stderr)
        return 2
    finally:
        # one line per invocation, refusals included. A secret the verb PROVED
        # is written as a length, never as text (lib.audit), and a log that
        # cannot be written never fails a verb.
        audit.LOG.write(action=verb, ok=ok, code=code,
                        args=rest if verb else args)
