"""browser-control-cli — argv in, service calls out, JSON printed.

One adapter per verb in `HANDLERS`; `main` parses `--browser`, dispatches,
prints the reply as one JSON object on stdout, and maps a refusal to
`ERR[code]: message` on stderr with exit 2. Nothing else prints, and no
caller-producible input can produce a traceback.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable

# The project-level pyright run resolves these imports; the line-level ignore
# is for pi-lens's fallback index, which does not see the sibling modules.
from browser_control.lib.browser import (  # pyright: ignore[reportMissingImports]
    close_tab,
    launch,
    new_tab,
    stop,
    tabs,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ControlError,
    fail,
)

USAGE = """usage: browser-control-cli VERB [ARGS]

  open [URL]        start (or adopt) the managed browser; URL optional
  close             stop the managed browser this CLI started
  tabs              list the browser's page tabs
  new-tab [URL]     open a tab (about:blank when no URL)
  close-tab SPEC    close ONE tab: `id:<prefix>` or a title/url substring

flags: --browser NAME   the installed Chromium-family browser to drive
                        (default: the first on PATH; a live managed browser wins)
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


def cmd_open(rest: list[str], browser: str) -> dict:
    return launch(_one(rest, "open"), browser=browser)


def cmd_close(rest: list[str], browser: str) -> dict:
    _none(rest, "close")
    return stop(browser=browser)


def cmd_tabs(rest: list[str], browser: str) -> dict:
    _none(rest, "tabs")
    return tabs(browser=browser)


def cmd_new_tab(rest: list[str], browser: str) -> dict:
    return new_tab(_one(rest, "new-tab"), browser=browser)


def cmd_close_tab(rest: list[str], browser: str) -> dict:
    return close_tab(_one(rest, "close-tab", required=True), browser=browser)


Handler = Callable[[list[str], str], dict]

HANDLERS: dict[str, Handler] = {
    "open": cmd_open,
    "close": cmd_close,
    "tabs": cmd_tabs,
    "new-tab": cmd_new_tab,
    "close-tab": cmd_close_tab,
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
