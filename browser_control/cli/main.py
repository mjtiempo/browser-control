"""browser-control-cli — argv in, service calls out, JSON printed.

One adapter per verb in `HANDLERS`; `main` parses `--browser`, dispatches,
prints the reply as one JSON object on stdout, and maps a refusal to
`ERR[code]: message` on stderr with exit 2. Nothing else prints, and no
caller-producible input can produce a traceback.
"""
from __future__ import annotations

import contextlib
import difflib
import json
import math
import os
import platform
import shutil
import sys
from collections.abc import Callable

# The project-level pyright run resolves these imports; pi-lens's fallback
# index does not see the sibling modules, and the per-line ignores it wanted
# pushed every line past the formatter's limit, so they are gone.
from browser_control import __version__
from browser_control.lib import audit, capabilities, cdp, dom
from browser_control.lib import browser as browser_lib
from browser_control.lib import plugins as plugins_lib
from browser_control.lib import policy as policy_lib
from browser_control.lib import profile as profile_lib
from browser_control.lib.browser import (
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
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    ERR_BROKEN_PIPE,
    ERR_INTERNAL,
    ERR_NO_WEBSOCKETS,
    ERR_NOT_ALLOWED,
    ERR_UNKNOWN_COMMAND,
    ControlError,
    fail,
)

USAGE = """usage: browser-control-cli VERB [ARGS]

  open [URL...]      start (or adopt) the managed browser; each URL opens
  close [--force] [--port N|--pid N|--profile DIR]
                     stop the managed browser this CLI started — or the one
                     NAMED, which is how another tool's browser goes too;
                     --force even when it holds tabs (they close with it)
  list               every Chromium-family browser running here, ours or not
  info               the browser this CLI would drive, and its endpoint
  attach [--port N|--pid N|--profile DIR]
                     allow TAB writes to a browser this CLI did not start
  attach --list      what is attached, and whether it is still up
  detach [--port N|--pid N|--profile DIR|--all]
                     revoke that authorization
  profile info [--profile DIR]
                     the profiles this CLI manages: weight, age, whether a
                     browser is on one, whether it is attached
  profile seed --from DIR [--force] [--dry]
                     copy a source profile's LOGINS into a managed one (no
                     caches, no lock files, read back; --dry counts first);
                     DIR is one Chrome profile (.../Default, Profile 1) or a
                     whole user-data directory — either lands where Chrome
                     reads it (a profile directory under the instance's
                     Default/)
  profile reset [--force]
                     wipe a managed profile, logins included
  tab [URL...]       open one tab per URL (about:blank when none)
  tab list           every drivable browser's page tabs, by browser
  tab frames [--tab SPEC]
                     this page's iframes, and which of them can be driven (a
                     cross-origin frame is a target of its own: name one with
                     the global --frame and the page verbs work inside)
  tab info SPEC      one tab: `id:<prefix>` or a title/url substring
  tab close SPEC... | --like V | --title V | --url V | --all [--except S...]
                     close every tab named, verified as a set; a SPEC names a
                     tab EXACTLY (whole URL or title, or id:<prefix>), --like
                     sweeps substrings, --all is everything, --except keeps;
                     --dry instead reports `would_close` and closes NOTHING
  tab nav URL [--tab SPEC]       navigate, then read the address back
  tab back|forward [--tab SPEC]  history, verified by the address changing
  tab reload [--tab SPEC]        a NEW document, verified
  tab activate [SPEC]            bring a tab forward (it raises its window)
  tab check TEXT|--selector CSS [--index N] [--uncheck] [--tab SPEC]
                                 check a box or radio with real input
  tab select TEXT|--selector CSS --value V [--index N] [--tab SPEC]
                                 choose one <option> with real arrow keys
  tab dialog [state|accept|dismiss] [--text VALUE] [--tab SPEC]
                                 read, accept or dismiss a JavaScript dialog
  tab screenshot PATH|--path PATH [--full] [--force] [--tab SPEC]
                                 write a PNG of the page; its own header
                                 vouches for the size, not the page's geometry
  tab js EXPR [--tab SPEC]       evaluate an expression (can write; unverified)
  tab find TEXT|--selector CSS [--cap N] [--tab SPEC]
                                 a visible element, in PAGE coordinates
  tab text [--selector CSS] [--chars N] [--tab SPEC]
                                 the rendered text, truncated in the page
  tab extract --each CSS --field NAME=SPEC [--field ...] [--cap N]
           [--chars N] [--visible] [--unique FIELD] [--tab SPEC]
                                 the repeated items as RECORDS: each --each
                                 match yields one object, and each --field is
                                 NAME=SELECTOR (innerText), NAME=SELECTOR@attr
                                 or NAME=@attr (the match itself); CSS only,
                                 no code, values sliced and budgeted
  tab wait --for load|idle|element|js [--selector CSS] [--expr EXPR]
           [--timeout S] [--idle-ms MS] [--tab SPEC]
                                 poll a predicate to a wall-clock deadline
  tab click TEXT|--selector CSS [--index N] [--tab SPEC]
                                 real input (CDP) at the element's centre
  tab click --at X,Y [--tab SPEC]
                                 real input at a POINT, for what a selector
                                 cannot name (a canvas); `verified: false`
                                 with what the point actually reaches
  tab hover TEXT|--selector CSS [--index N] [--at X,Y] [--tab SPEC]
                                 put the pointer on an element (`:hover`)
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
  help               this text (also `-h` and `--help`), then any plugin
                     actions installed (see `selftest`)

SPEC   a CDP target id prefix (`id:2D4BC76C`), `active` (the tab whose page
       reports itself visible — only one per window), or a title/url
       substring; a spec matching nothing, or several tabs, refuses and names
       them
flags: --browser NAME   the browser to drive (open/close/tab) or to narrow
                        (info/tab list); default: a live managed browser, else
                        the first Chromium-family one on PATH
       --profile DIR    the INSTANCE to drive: a profile directory under the
                        root, which is how two instances of ONE browser are
                        told apart (`open --profile <root>/work`)
       --frame VALUE    the FRAME inside the tab: a URL substring, or an index
                        from `tab frames` (a cross-origin frame is a target of
                        its own, so every verb that acts on a page's CONTENT
                        works inside it — not nav/back/forward/reload/list/
                        frames/info/close/activate, which act on the tab)
       --allow CLASSES  the capability classes this call may use (read, write,
                        code, file, egress, or * for all)
       --deny CLASSES   classes it may not; a verb is refused `not-allowed`
                        when ANY of its classes fails the policy
gate:  --allow/--deny, or BROWSER_CONTROL_ALLOW/BROWSER_CONTROL_DENY for a
       whole session. `selftest` reports the policy in force and is the one
       verb that is never gated — a gate that blocks its own explanation
       would be a trap. No policy set means no gate.
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
            fail(ERR_BAD_ARGS, f"{verb}: unknown flag {arg!r}")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"{verb}: one argument at most, got {len(rest)}")
    if not rest:
        if required:
            fail(ERR_BAD_ARGS,
                 f"{verb}: a TAB spec is required (id:<prefix> or a "
                 "title/url substring)")
        return ""
    if not str(rest[0]).strip():
        # an EMPTY value is not "no value": `tab activate ""` used to fall
        # through to "the only page tab", which is a tab nobody named
        fail(ERR_BAD_ARGS, f"{verb}: an empty argument is not a value")
    return rest[0]


def _none(rest: list[str], verb: str) -> None:
    for arg in rest:
        fail(ERR_BAD_ARGS, f"{verb}: takes no arguments, got {arg!r}")


def _urls(rest: list[str], verb: str) -> list[str]:
    """Every URL this verb was given, in order — none is dropped.

    A flag is refused rather than ignored (the tab is `--tab`, and these verbs
    take no other), and a caller that asks for three sites gets three.
    """
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"{verb}: unknown flag {arg!r}")
    return list(rest)


def _no_browser_flag(verb: str, browser: str) -> None:
    """Refuse `--browser` on a verb that reports every browser it finds."""
    if browser:
        fail(ERR_BAD_ARGS,
             f"{verb}: --browser does not apply — this verb reports every "
             "browser on the machine")


def _int(value: str, what: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        fail(ERR_BAD_ARGS, f"{what} needs a number, got {value!r}")


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
                fail(ERR_BAD_ARGS, f"{verb}: {arg} needs a value")
            key, value = arg[2:], str(rest[index + 1])
            if key in ("port", "pid"):
                out[key] = _int(value, f"{verb}: {arg}")
            else:
                out[key] = value
            index += 2
            continue
        known = ", ".join(["--port N", "--pid N", "--profile DIR"]
                          + [f"--{name}" for name in allow])
        fail(ERR_BAD_ARGS,
             f"{verb}: unknown argument {arg!r} (flags: {known})")
    if out["list"] and (out["all"] or out["port"] or out["pid"]
                        or out["profile"]):
        fail(ERR_BAD_ARGS, f"{verb}: --list takes no other argument")
    if out["all"] and (out["port"] or out["pid"] or out["profile"]):
        fail(ERR_BAD_ARGS, f"{verb}: --all takes no other selector")
    return out


def cmd_attach(rest: list[str], browser: str) -> dict:
    """`attach --port N|--pid N|--profile DIR` / `attach --list`."""
    _no_browser_flag("attach", browser)
    selector = _selector(rest, "attach", ("list",))
    if selector["list"]:
        if browser_lib.scope():
            # a scope that cannot apply is REFUSED, by every verb: this one
            # dropped --profile silently and answered a broader question
            # (a review flagged it)
            fail(ERR_BAD_ARGS,
                 "attach --list: --profile narrows an instance and --list "
                 "answers for every attachment — drop one of the two")
        return attachments()
    return attach(port=selector["port"], pid=selector["pid"],
                  profile=browser_lib.scope())


def cmd_detach(rest: list[str], browser: str) -> dict:
    """`detach ...` / `detach --all` — revoke a tab-write authorization."""
    _no_browser_flag("detach", browser)
    selector = _selector(rest, "detach", ("all",))
    # --all means every record, so a scoped call does not narrow it
    profile = "" if selector["all"] else browser_lib.scope()
    return detach(port=selector["port"], pid=selector["pid"],
                  profile=profile, detach_all=selector["all"])


def cmd_open(rest: list[str], browser: str) -> dict:
    return launch(_urls(rest, "open"), browser=browser)


def cmd_close(rest: list[str], browser: str) -> dict:
    """`close [--force] [--port N | --pid N | --profile DIR]`.

    With no selector: the managed browser this CLI started. Naming one is how a
    browser it did NOT start (another tool's, or one it merely attached to)
    gets stopped on purpose — the name is the consent, and the browser has to
    be a live, answering, VERIFIED Chromium-family process for it to mean
    anything. `--force` says the tabs may go with it.
    """
    rest, force = _switch(rest, "--force")
    selector = _selector(rest, "close", ())
    scoped = browser_lib.scope()
    if scoped and (selector["port"] or selector["pid"]):
        fail(ERR_BAD_ARGS,
             "close: --profile names an instance, so it takes no --port or "
             "--pid — one way to name a browser per call")
    return stop(browser=browser, force=force, port=selector["port"],
                pid=selector["pid"], profile=scoped)


def cmd_list(rest: list[str], browser: str) -> dict:
    """`list`: every browser on the machine — so nothing narrows it.

    `--profile` is the INSTANCE selector, and this verb reports the whole
    machine, so it refuses one rather than ignoring it: a scope that is
    silently dropped is a caller who thinks they asked for something.
    """
    _no_browser_flag("list", browser)
    if browser_lib.scope():
        fail(ERR_BAD_ARGS,
             "list: --profile does not apply — this verb reports every browser "
             "on the machine; `tab list --profile DIR` shows one instance's "
             "tabs and `info --profile DIR` its endpoint")
    _none(rest, "list")
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
        fail(ERR_NO_WEBSOCKETS,
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
             "action_log": audit.LOG.path() or "off",
             "verbs": sorted(HANDLERS),
             "capabilities": {
                 "classes": list(capabilities.CLASSES),
                 "by_class": capabilities.by_class(),
                 # asked from the SAME function the hermetic test uses, so a
                 # verb added without a class shows up in this reply instead of
                 # being quietly missing from a table nobody re-reads
                 "unclassified": capabilities.unclassified(
                     HANDLERS, {"tab": TAB_SUBCOMMANDS,
                                "profile": PROFILE_SUBCOMMANDS})},
             "policy": _POLICY.describe(),
             "browsers": found}
    reply.update(_plugins_report())
    if browser:
        reply["requested"] = {"name": browser,
                              "path": shutil.which(browser) or ""}
    if browser_lib.scope():
        reply["scoped_to"] = browser_lib.scope()
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
                fail(ERR_BAD_ARGS, f"{verb}: {flag} needs a value")
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
    """`--tab SPEC`, or "" — the tab a page verb acts on.

    `--tab ""` is REFUSED rather than read as "no spec": the flag was given, and
    an empty spec reaching `_one_tab` means "the only page tab" — a tab nobody
    named. The library refuses an empty spec, so the CLI must too, or the same
    argv means two things one layer apart (`tab info ""` refused it while
    `tab text --tab ""` quietly picked a tab).
    """
    rest, spec = _pop(rest, "--tab", verb)
    if spec is not None and not str(spec).strip():
        fail(ERR_BAD_ARGS,
             f"{verb}: --tab needs a SPEC (id:<prefix> or a title/url "
             "substring); leave the flag out to act on the only page tab")
    return rest, spec or ""


def _float(value: str, what: str) -> float:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        fail(ERR_BAD_ARGS, f"{what} needs a number, got {value!r}")
    if not math.isfinite(number):
        fail(ERR_BAD_ARGS, f"{what} must be a finite number, got {value!r}")
    return number


def cmd_tab_list(rest: list[str], browser: str) -> dict:
    _none(rest, "tab list")
    return list_tabs(browser=browser)


def cmd_tab_info(rest: list[str], browser: str) -> dict:
    return tab_info(_one(rest, "tab info", required=True), browser=browser)


def _pop_all(rest: list[str], flag: str,
             verb: str) -> tuple[list[str], list[str]]:
    """Remove EVERY `--flag VALUE` (or `--flag=VALUE`), values in order.

    A repeatable flag needs its own reader: `--except a --except b` is two
    exceptions, and `_pop` would leave the second one in argv.
    """
    out: list[str] = []
    values: list[str] = []
    index = 0
    while index < len(rest):
        arg = str(rest[index])
        if arg == flag:
            if index + 1 >= len(rest):
                fail(ERR_BAD_ARGS, f"{verb}: {flag} needs a value")
            values.append(str(rest[index + 1]))
            index += 2
            continue
        if arg.startswith(flag + "="):
            values.append(arg.split("=", 1)[1])
            index += 1
            continue
        out.append(arg)
        index += 1
    return out, values


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
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"tab close: unknown flag {arg!r}")
    return close_tabs(rest, browser=browser, title=title, url=url,
                      all_tabs=every, excepts=excepts, like=likes, dry=dry)


def cmd_tab_nav(rest: list[str], browser: str) -> dict:
    """`tab nav URL [--tab SPEC]` — navigate, then read the address back."""
    rest, spec = _tab_flag(rest, "tab nav")
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"tab nav: unknown flag {arg!r}")
    if not rest:
        fail(ERR_BAD_ARGS,
             "tab nav: a URL is required (http(s) or about:blank)")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS,
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
    rest, at = _pop(rest, "--at", "tab hover")
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"tab hover: unknown flag {arg!r}")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab hover: one TEXT at most, got {len(rest)}")
    needle = rest[0] if rest else None
    if at is not None:
        if needle is not None or selector is not None or index is not None:
            fail(ERR_BAD_ARGS,
                 "tab hover: --at is a POINT — give that or a TEXT/"
                 "--selector (with --index), not both")
        return dom.hover(None, at=at, tab=spec, browser=browser)
    if (needle is None) == (selector is None):
        fail(ERR_BAD_ARGS, "tab hover: give TEXT, --selector CSS, or --at X,Y")
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
            fail(ERR_BAD_ARGS, f"tab check: unknown flag {arg!r}")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab check: one TEXT at most, got {len(rest)}")
    needle = rest[0] if rest else None
    if (needle is None) == (selector is None):
        fail(ERR_BAD_ARGS, "tab check: give TEXT or --selector CSS, not both")
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
            fail(ERR_BAD_ARGS, f"tab select: unknown flag {arg!r}")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab select: one TEXT at most, got {len(rest)}")
    needle = rest[0] if rest else None
    if (needle is None) == (selector is None):
        fail(ERR_BAD_ARGS, "tab select: give TEXT or --selector CSS, not both")
    if value is None:
        fail(ERR_BAD_ARGS,
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
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"tab screenshot: unknown flag {arg!r}")
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
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"tab js: unknown flag {arg!r}")
    if not rest:
        fail(ERR_BAD_ARGS, "tab js: an EXPRESSION is required")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS,
             f"tab js: one expression at most, got {len(rest)} — the tab is "
             "--tab SPEC")
    return dom.js(rest[0], tab=spec, browser=browser)


def cmd_tab_frames(rest: list[str], browser: str) -> dict:
    """`tab frames [--tab SPEC]` — this page's iframes, and which are drivable."""
    rest, spec = _tab_flag(rest, "tab frames")
    _none(rest, "tab frames")
    row, tab_row = dom._resolve(spec, browser, for_write=False)  # noqa: SLF001
    return dom.frames(row, tab_row)


def cmd_tab_find(rest: list[str], browser: str) -> dict:
    """`tab find TEXT | --selector CSS [--cap N] [--tab SPEC]`."""
    rest, spec = _tab_flag(rest, "tab find")
    rest, selector = _pop(rest, "--selector", "tab find")
    rest, cap = _pop(rest, "--cap", "tab find")
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"tab find: unknown flag {arg!r}")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab find: one TEXT at most, got {len(rest)}")
    needle = rest[0] if rest else None
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
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"tab click: unknown flag {arg!r}")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab click: one TEXT at most, got {len(rest)}")
    needle = rest[0] if rest else None
    # `--at X,Y` is a POINT: it replaces the spec instead of joining it
    if at is not None:
        if needle is not None or selector is not None or index is not None:
            fail(ERR_BAD_ARGS,
                 "tab click: --at is a POINT — give that or a TEXT/"
                 "--selector (with --index), not both")
        return dom.click(None, at=at, tab=spec, browser=browser)
    if (needle is None) == (selector is None):
        fail(ERR_BAD_ARGS, "tab click: give TEXT, --selector CSS, or --at X,Y")
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
            fail(ERR_BAD_ARGS, f"tab scroll: unknown flag {arg!r}")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab scroll: one TEXT at most, got {len(rest)}")
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
            fail(ERR_BAD_ARGS, f"tab focus: unknown flag {arg!r}")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab focus: one TEXT at most, got {len(rest)}")
    needle = rest[0] if rest else None
    if (needle is None) == (selector is None):
        fail(ERR_BAD_ARGS, "tab focus: give TEXT or --selector CSS, not both")
    return dom.focus(needle, selector=selector,
                     index=_int(index, "tab focus --index")
                     if index is not None else None,
                     tab=spec, browser=browser)


def cmd_tab_press(rest: list[str], browser: str) -> dict:
    """`tab press KEY [--tab SPEC]` — one key event at the DOM focus."""
    rest, spec = _tab_flag(rest, "tab press")
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"tab press: unknown flag {arg!r}")
    if not rest:
        fail(ERR_BAD_ARGS, "tab press: KEY is required (enter, tab, escape, …)")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab press: one KEY at most, got {len(rest)}")
    return dom.press(rest[0], tab=spec, browser=browser)


def _text_arg(rest: list[str], verb: str, tab: str) -> str:
    """The single TEXT a writing verb takes — nothing else, and no flags."""
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"{verb}: unknown flag {arg!r}")
    if not rest:
        fail(ERR_BAD_ARGS, f"{verb}: TEXT is required")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS,
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
    rest, spec = _tab_flag(rest, "tab upload")
    rest, selector = _pop(rest, "--selector", "tab upload")
    rest, index = _pop(rest, "--index", "tab upload")
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"tab upload: unknown flag {arg!r}")
    if not rest:
        fail(ERR_BAD_ARGS, "tab upload: FILE is required (an absolute path)")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab upload: one FILE at most, got {len(rest)}")
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
            fail(ERR_BAD_ARGS, f"tab media: unknown flag {arg!r}")
    if not rest:
        fail(ERR_BAD_ARGS, "tab media: MODE is required (state, play or pause)")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, f"tab media: one MODE at most, got {len(rest)}")
    return dom.media(rest[0],
                     index=_int(index, "tab media --index")
                     if index is not None else None,
                     tab=spec, browser=browser)


def cmd_profile_info(rest: list[str], browser: str) -> dict:
    """`profile info [--profile DIR]`."""
    _none(rest, "profile info")
    if browser:
        fail(ERR_BAD_ARGS,
             "profile info: --browser does not narrow it — the instance is "
             "named with --profile DIR, and a scope that cannot apply is "
             "refused rather than dropped")
    return profile_lib.info()


def cmd_profile_seed(rest: list[str], browser: str) -> dict:
    """`profile seed --from DIR [--force] [--dry]`."""
    rest, source = _pop(rest, "--from", "profile seed")
    rest, force = _switch(rest, "--force")
    rest, dry = _switch(rest, "--dry")
    _none(rest, "profile seed")
    return profile_lib.seed(source or "", browser=browser, force=force,
                            dry=dry)


def cmd_profile_reset(rest: list[str], browser: str) -> dict:
    """`profile reset [--force]`."""
    rest, force = _switch(rest, "--force")
    _none(rest, "profile reset")
    return profile_lib.reset(browser=browser, force=force)


def cmd_profile(rest: list[str], browser: str) -> dict:
    """`profile info|seed|reset` — the browser-level noun for profiles.

    `tab` owns everything about a page tab, the way this owns the profiles the
    CLI manages: seeing them, giving one a source profile's logins, and wiping
    one. The instance itself is named with the global `--profile DIR`.
    """
    handler = PROFILE_SUBCOMMANDS.get(str(rest[0]) if rest else "")
    if handler is None:
        fail(ERR_BAD_ARGS,
             "profile: a subcommand is required (info, seed, reset)")
    return handler(rest[1:], browser)


# `tab`'s subcommands: a reserved first word, so a URL can never be mistaken
# for one (and vice versa).
TAB_SUBCOMMANDS: dict[str, Handler] = {
    "list": cmd_tab_list,
    "frames": cmd_tab_frames,
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
    "extract": cmd_tab_extract,
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

# `profile`'s subcommands, the way `tab` has its own: a reserved first word.
PROFILE_SUBCOMMANDS: dict[str, Handler] = {
    "info": cmd_profile_info,
    "seed": cmd_profile_seed,
    "reset": cmd_profile_reset,
}

HANDLERS: dict[str, Handler] = {
    "open": cmd_open,
    "close": cmd_close,
    "list": cmd_list,
    "info": cmd_info,
    "attach": cmd_attach,
    "detach": cmd_detach,
    "profile": cmd_profile,
    "tab": cmd_tab,
    "selftest": cmd_selftest,
}


# The GLOBAL flags, and what each one sets: pulled out of argv in one place so
# every verb sees the same globals, and none can quietly not know about one.
FLAG_KEY = {"--browser": "browser", "--profile": "profile",
            "--frame": "frame", "--allow": "allow", "--deny": "deny"}


def _flags(args: list[str]) -> tuple[list[str], dict[str, str | None]]:
    """Pull the GLOBAL flags out of argv, anywhere.

    `--browser NAME` picks the browser, `--profile DIR` the instance,
    `--frame VALUE` the frame inside the tab (a URL substring or an index from
    `tab frames`), `--allow`/`--deny` the capability classes this call may use.
    One place, so every verb sees the same globals. A flag that was NOT given
    comes back as None rather than "", because `--allow` must be able to tell
    the two apart: an empty value is a refusal, and reading it as "absent" is
    how `--allow ""` used to mean "allow everything".
    """
    found: dict[str, str | None] = dict.fromkeys(FLAG_KEY.values(), None)
    rest: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in FLAG_KEY:
            if index + 1 >= len(args):
                fail(ERR_BAD_ARGS, f"{arg} needs a value")
            if arg in ("--allow", "--deny") \
                    and found[FLAG_KEY[arg]] is not None:
                # last-wins DROPPED an earlier class in the unsafe direction:
                # `--deny read --deny write` denied only `write`, allowing the
                # read it was told to deny (a review flagged it). The repeatable
                # per-verb flags have the opposite contract, so a repeat here is
                # refused rather than silently merged.
                fail(ERR_BAD_ARGS,
                     f"{arg}: given twice — name every class once "
                     f"({arg} read,write …)")
            found[FLAG_KEY[arg]] = args[index + 1]
            index += 2
            continue
        named = [key for key in FLAG_KEY
                 if arg.startswith(key + "=")]
        if named:
            if named[0] in ("--allow", "--deny") \
                    and found[FLAG_KEY[named[0]]] is not None:
                fail(ERR_BAD_ARGS,
                     f"{named[0]}: given twice — name every class once "
                     f"({named[0]}=read,write …)")
            found[FLAG_KEY[named[0]]] = arg.split("=", 1)[1]
            index += 1
            continue
        rest.append(arg)
        index += 1
    return rest, found


def _bare_tab_word(word: str) -> None:
    """Refuse a `tab` word that is neither a subcommand nor a URL.

    The URL path would say only "refusing this as a URL", which reads as a
    complaint about a URL when what happened is a typo — so this names both
    lists, and the subcommand the word is closest to. It runs BEFORE the gate,
    so a typo is `bad-args` whether or not a policy is in force (otherwise the
    verb that knows the real message never gets to run).
    """
    try:
        browser_lib.safe_url(word)
    except ControlError:
        near = difflib.get_close_matches(str(word), sorted(TAB_SUBCOMMANDS),
                                         n=1, cutoff=0.6)
        fail(ERR_BAD_ARGS,
             f"tab: {str(word)[:40]!r} is neither a subcommand (have: "
             + ", ".join(sorted(TAB_SUBCOMMANDS))
             + ") nor a URL (http(s) or about:blank only)"
             + (f" — did you mean `tab {near[0]}`?" if near else ""))


def _modes(head: str) -> tuple[str, ...]:
    """The modes a mode-carrying `tab` subcommand accepts.

    Read from the DECLARED surface, so no list is kept twice: `tab dialog
    accept` and `tab media play` are actions, and `--for` takes the values its
    own table holds (`tab wait --for js` is the one that changes class).
    """
    if head == "wait":
        return tuple(dom.WAIT_EXPRS)
    prefix = f"tab {head} "
    return tuple(entry[len(prefix):] for entry in capabilities.ACTIONS
                 if entry.startswith(prefix))


def resolved_mode(verb: str, rest: list[str]) -> str:
    """The mode a call will RUN — read the way its handler will read it.

    The gate authorises a mode-carrying subcommand BY its mode, so it has to
    resolve that mode exactly as the verb does: the LAST `--for` (what `_pop`
    leaves), normalised by `dom.mode_of` (the one normaliser in the codebase),
    and for a positional mode the first positional left once that verb's own
    flags are out of the way. A mode the gate cannot read is one it cannot
    authorise, so an unrecognised one is refused here rather than guessed at —
    a typo has to be `bad-args` whether or not a policy is in force.
    """
    if verb != "tab" or not rest:
        return ""
    head = str(rest[0])
    if head == "wait":
        # `--tab` FIRST, exactly as `cmd_tab_wait` pops it: a `--tab` value that
        # is literally `--for` must not be read as the mode
        args = list(rest[1:])
        args, _spec = _pop(args, "--tab", "tab wait")
        _kept, value = _pop(args, "--for", "tab wait")
        if value is None:
            return ""
        raw = str(value)
    elif head in ("dialog", "media"):
        args = list(rest[1:])
        args, _spec = _pop(args, "--tab", f"tab {head}")
        value_flag = "--text" if head == "dialog" else "--index"
        args, _value = _pop(args, value_flag, f"tab {head}")
        raw = next((str(arg) for arg in args
                    if not str(arg).startswith("-")), "")
        if not raw:
            return ""
    else:
        return ""
    mode = dom.mode_of(raw)
    modes = _modes(head)
    if mode not in modes:
        fail(ERR_BAD_ARGS,
             f"tab {head}: {'--for' if head == 'wait' else 'MODE'} is "
             + "|".join(modes) + f", got {raw!r}")
    return mode


def action(verb: str, rest: list[str]) -> str:
    """Which DECLARED action a call is: verb, subcommand, and its mode.

    Three subcommands answer differently by mode — `tab wait --for js` is code
    while `tab wait` reads, `tab dialog accept` writes while `state` reads,
    `tab media play` writes while `state` reads — so the gate asks for the mode
    on exactly those, resolved by `resolved_mode` (the reading its verb does),
    and for the plain key on everything else. A caller cannot get a write past
    the gate by spelling it as a read: `--for JS`, `--for " js "` and a repeated
    `--for` all resolve to the mode the verb will actually run.

    "" means "this call declares no action" — a subcommand nobody has — and the
    gate then stays out of the way, so the caller sees the verb's own
    `bad-args` rather than `not-allowed` for a typo.
    """
    head = str(rest[0]) if rest else ""
    if verb == "tab":
        if head not in TAB_SUBCOMMANDS:
            return verb              # a URL: the URL path, which is `tab`
        if head == "wait":
            return ("tab wait --for js" if resolved_mode(verb, rest) == "js"
                    else "tab wait")
        if head in ("dialog", "media"):
            return f"tab {head} {resolved_mode(verb, rest) or 'state'}"
        return f"tab {head}"
    if verb == "profile":
        return f"profile {head}" if head in PROFILE_SUBCOMMANDS else ""
    return verb


# The plugins loaded for THIS invocation: replaced at the top of `main`, so
# one call can never inherit another's actions (or leave a ghost behind).
# The policy ONE invocation runs under: `main` replaces its contents from
# the flags and the environment before the gate is asked anything.
_POLICY = policy_lib.Policy()

PLUGINS = plugins_lib.Registry()


def _verb_names() -> list[str]:
    """Every top-level verb a caller can run: built-ins first, then plugins."""
    return [*HANDLERS, *PLUGINS.actions]


def _plugins_report() -> dict:
    return {"plugins": PLUGINS.describe(), "plugin_errors": PLUGINS.errors}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Plugins load once per invocation, BEFORE the help text and the gate:
    # `--help`/`selftest` report them, and the classes they declare have to be
    # in the surface before `allowed()` is asked anything.
    global PLUGINS
    PLUGINS = plugins_lib.load(reserved=set(HANDLERS) | {"help"})
    capabilities.set_plugins({verb: tuple(spec["classes"])
                              for verb, spec in PLUGINS.actions.items()})
    if args and args[0] in ("-h", "--help", "help"):
        print(USAGE)
        for spec in PLUGINS.actions.values():
            print(f"  {spec['usage']}")
        return 0
    verb = ""
    rest: list[str] = []
    ok = False
    code: str | None = None
    try:
        if not args:
            # INSIDE the try, so the `finally` writes the audit line: this
            # refusal used to return before the log existed, while the
            # flags-only refusal below was fixed for exactly this (a review
            # flagged it)
            print(USAGE, file=sys.stderr)
            print("ERR[bad-args]: a verb is required "
                  f"(have: {', '.join(_verb_names())})", file=sys.stderr)
            code = ERR_BAD_ARGS
            return 2
        rest, flags = _flags(args)
        if not rest:
            # every token was a GLOBAL flag, so there is no verb to run: this
            # used to be an uncaught IndexError out of `main` — a traceback and
            # exit 1, which is neither of the two things the contract promises
            print(USAGE, file=sys.stderr)
            given = ", ".join(f"{key}={value!r}"
                              for key, value in flags.items() if value)
            print("ERR[bad-args]: a verb is required "
                  f"(have: {', '.join(_verb_names())})"
                  + (f" — {given} was given, but no verb to run"
                     if given else ""), file=sys.stderr)
            code = ERR_BAD_ARGS           # so the audit line carries the code
            return 2
        verb, rest = rest[0], rest[1:]
        # a flag given an EMPTY value is a MISTAKE, not an absent flag: every
        # other value-carrying flag refuses one (--tab "", --frame "", a
        # policy that names no class), while `--profile ""` silently cleared
        # the instance scope and `--browser ""` fell back to the default
        # (a review flagged the asymmetry)
        for name, value in (("--browser", flags["browser"]),
                            ("--profile", flags["profile"])):
            if value is not None and not str(value).strip():
                fail(ERR_BAD_ARGS,
                     f"{name}: an empty value is not a name — name a browser "
                     "or a profile, or leave the flag off")
        # the globals, in the order they matter: the instance, the frame, the
        # policy. Each is SET OR CLEARED per invocation, so no verb inherits
        # another call's scope.
        browser = flags["browser"] or ""
        browser_lib.scope(flags["profile"] or "")
        dom.frame(flags["frame"] or "")
        # NOT `or None`: a flag given an empty value must reach the policy,
        # which refuses it, instead of reading as "the call named no policy"
        _POLICY.update(policy_lib.Policy.from_sources(flags["allow"],
                                                     flags["deny"]))
        audit.LOG.begin(verb)          # no secret is known yet
        handler = HANDLERS.get(verb)
        if handler is None:
            plugin = PLUGINS.actions.get(verb)
            handler = plugin["run"] if plugin is not None else None
        if handler is None:
            raise ControlError(ERR_UNKNOWN_COMMAND,
                               f"{verb} (have: {', '.join(_verb_names())})")
        head = str(rest[0]) if rest else ""
        if verb == "tab" and head and head not in TAB_SUBCOMMANDS:
            _bare_tab_word(head)
        if flags["frame"] is not None and not str(flags["frame"]).strip():
            fail(ERR_BAD_ARGS,
                 "--frame needs a VALUE — a URL substring or an index from "
                 "`tab frames` (an empty value is not a frame)")
        if flags["frame"] and verb != "selftest" and not (
                verb == "tab" and head in dom.FRAME_VERBS):
            # a scope that cannot apply is REFUSED, by every verb: `list
            # --frame 1` and `open --frame 1 URL` used to accept it and drop it
            fail(ERR_BAD_ARGS,
                 f"{verb}{' ' + head if head else ''}: --frame does not apply "
                 "— it scopes the verbs that act on a page's CONTENT ("
                 + ", ".join(sorted(dom.FRAME_VERBS))
                 + "), and `selftest` reports it; nothing else takes it")
        if verb != "selftest":
            # `selftest` is never gated: it is the verb that REPORTS the policy,
            # and a gate that blocks its own explanation is a trap. Everything
            # else answers to the classes its action declares — and a call whose
            # action is "" declares none, so the verb's own refusal is what the
            # caller sees (`bad-args` for a subcommand nobody has).
            wanted = action(verb, rest)
            if wanted:
                permitted, why = _POLICY.allowed(
                    wanted, capabilities.classes_for(wanted))
                if not permitted:
                    fail(ERR_NOT_ALLOWED, why)
        reply = handler(rest, browser)
        scoped_frame = dom.frame()
        if verb == "tab" and head in dom.FRAME_VERBS and scoped_frame:
            # one place says which frame a scoped call acted in, so no verb has
            # to remember to (and none can forget to) — plus WHICH document that
            # turned out to be, because an index is the page's live frame order
            reply["frame"] = scoped_frame
            resolved = dom.frame_resolved()
            if resolved is not None:
                reply["frame_resolved"] = resolved
        print(json.dumps(reply))
        # flush HERE, where BrokenPipeError is still catchable: a reply under
        # stdio's buffer raised nothing at `print`, and the failure surfaced at
        # interpreter shutdown as "Exception ignored" with exit status 120
        # instead of ERR[broken-pipe]/2 (a review measured it)
        sys.stdout.flush()
        ok = True
        return 0
    except ControlError as e:
        code = e.code
        print(f"ERR[{e.code}]: {e.message}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        # a closed reader (`| head`) is not a crash: the verb already did its
        # work, and the caller gets a code instead of a traceback
        code = ERR_BROKEN_PIPE
        # point stdout at the null device: bytes still buffered in stdio are
        # flushed at shutdown, and a SECOND failure there overrides the `2`
        # returned below with 120 (measured; a review flagged it)
        with contextlib.suppress(OSError, ValueError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        with contextlib.suppress(OSError):
            print("ERR[broken-pipe]: the reader of stdout went away",
                  file=sys.stderr)
        return 2
    except Exception as e:                       # noqa: BLE001
        # ONE JSON object on stdout, or ERR[code] on stderr — an unexpected
        # failure is reported as `internal` with its type and message, never as
        # a traceback, and it still reaches the action log in `finally`
        code = ERR_INTERNAL
        with contextlib.suppress(OSError):
            print(f"ERR[internal]: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    finally:
        # one line per invocation, refusals included. A secret the verb PROVED
        # is written as a length, never as text (lib.audit), and a log that
        # cannot be written never fails a verb.
        audit.LOG.write(action=verb, ok=ok, code=code,
                        args=rest if verb else args)
