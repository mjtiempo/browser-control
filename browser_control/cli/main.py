"""browser-control-cli — argv in, service calls out, JSON printed.

One adapter per verb in `HANDLERS`; `main` parses `--browser`, dispatches,
prints the reply as one JSON object on stdout, and maps a refusal to
`ERR[code]: message` on stderr with exit 2. Nothing else prints, and no
caller-producible input can produce a traceback.
"""
from __future__ import annotations

import json
import platform
import shutil
import sys
from collections.abc import Callable

# The project-level pyright run resolves these imports; the line-level ignores
# are for pi-lens's fallback index, which does not see the sibling modules.
from browser_control import __version__
from browser_control.lib import browser as browser_lib  # pyright: ignore[reportMissingImports]
from browser_control.lib import cdp  # pyright: ignore[reportMissingImports]
from browser_control.lib.browser import (  # pyright: ignore[reportMissingImports]
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
  selftest           prove the install: interpreter, websockets, verbs

SPEC   a CDP target id prefix (`id:2D4BC76C`) or a title/url substring; a
       spec matching nothing, or several tabs, refuses and names them
flags: --browser NAME   the browser to drive (open/close/tab) or to narrow
                        (info/tab list); default: a live managed browser, else
                        the first Chromium-family one on PATH
       --tab SPEC       the tab a page verb acts on (nav/back/forward/reload);
                        without it the verb acts on the ONLY page tab there is
reads: every drivable browser.  writes: a managed browser, or an attached one
       — `attach` grants TAB writes only, `close` never stops one
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


def _tab_flag(rest: list[str], verb: str) -> tuple[list[str], str]:
    """Pull `--tab SPEC` (or `--tab=SPEC`) out of a verb's arguments.

    `--tab` names the tab a page verb acts on; without it the verb acts on the
    only page tab there is, and refuses `tab-ambiguous` when there are more.
    """
    out: list[str] = []
    spec = ""
    index = 0
    while index < len(rest):
        arg = str(rest[index])
        if arg == "--tab":
            if index + 1 >= len(rest):
                fail("bad-args", f"{verb}: --tab needs a SPEC")
            spec = str(rest[index + 1])
            index += 2
            continue
        if arg.startswith("--tab="):
            spec = arg.split("=", 1)[1]
            index += 1
            continue
        out.append(arg)
        index += 1
    return out, spec


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
    try:
        rest, browser = _browser_flag(args)
        verb, rest = rest[0], rest[1:]
        handler = HANDLERS.get(verb)
        if handler is None:
            raise ControlError("unknown-command",
                               f"{verb} (have: {', '.join(HANDLERS)})")
        print(json.dumps(handler(rest, browser)))
        return 0
    except ControlError as e:
        print(f"ERR[{e.code}]: {e.message}", file=sys.stderr)
        return 2
