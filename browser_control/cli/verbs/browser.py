"""verbs.browser — the top-level verb handlers: open, close, list, info, attach, detach, selftest."""  # noqa: E501
from __future__ import annotations

import platform
import shutil
import sys

from browser_control import __version__
from browser_control.cli import registry
from browser_control.cli.argv import (
    _no_browser_flag,
    _none,
    _selector,
    _switch,
    _urls,
)
from browser_control.lib import audit, capabilities
from browser_control.lib import browser as browser_lib
from browser_control.lib.cdp import (
    rpc as cdp_rpc,
    session as cdp_session,
)
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    fail,
)


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
        return browser_lib.attachments()
    return browser_lib.attach(port=selector["port"], pid=selector["pid"],
                  profile=browser_lib.scope())

def cmd_detach(rest: list[str], browser: str) -> dict:
    """`detach ...` / `detach --all` — revoke a tab-write authorization."""
    _no_browser_flag("detach", browser)
    selector = _selector(rest, "detach", ("all",))
    # --all means every record, so a scoped call does not narrow it
    profile = "" if selector["all"] else browser_lib.scope()
    return browser_lib.detach(port=selector["port"], pid=selector["pid"],
                  profile=profile, detach_all=selector["all"])

def cmd_open(rest: list[str], browser: str) -> dict:
    """`open [URL...] [--headless]` — start (or adopt) the managed browser.

    `--headless` is a property of the PROCESS, so `launch` owns what it means
    when one is already up; this adapter only pulls the switch out before
    `_urls`, which refuses anything flag-shaped. The switch always rides
    along, given or not: `launch` answers the same thing for `False` that it
    always did, and the mode is never guessed at one layer above the process.
    """
    rest, headless = _switch(rest, "--headless")
    return browser_lib.launch(_urls(rest, "open"), browser=browser,
                  headless=headless)

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
    return browser_lib.stop(browser=browser, force=force, port=selector["port"],
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
    return browser_lib.list_browsers()

def cmd_info(rest: list[str], browser: str) -> dict:
    _none(rest, "info")
    return browser_lib.browser_info(browser=browser)

def cmd_selftest(rest: list[str], browser: str) -> dict:
    """Prove the install without a browser: interpreter, dependency, verbs.

    The one thing that FAILS here is a missing `websockets`: without it no
    verb can speak CDP, and an install that cannot reach a browser should say
    so at once rather than at the first `tabs`. A machine with no browser is
    reported, not failed — the command is installed either way.
    """
    _none(rest, "selftest")
    cdp_session.require_websockets()
    # the tables, the policy and the plugin set live on `cli.registry`, which
    # this module imports at module level: no deferred import, no reach into
    # `cli.main`'s privates
    found = []
    for name in browser_lib.BROWSER_BINS:
        path = shutil.which(name)
        if path:
            found.append({"name": name, "path": path})
    reply = {"ok": True, "command": "browser-control-cli",
             "version": __version__,
             "python": sys.executable,
             "python_version": platform.python_version(),
             "websockets": getattr(cdp_rpc.websockets, "__version__", "unknown"),
             "profile_root": browser_lib.root(),
             "action_log": audit.LOG.path() or "off",
             # the exposure a caller cannot otherwise see: CDP has no auth,
             # and loopback is not a per-user boundary — so a managed browser
             # on a shared machine is reachable by any local process (a
             # review flagged that this was written only in a code comment)
             "cdp_exposure": ("loopback tcp, unauthenticated — any local "
                              "process (any uid) can reach the CDP port"),
             "verbs": sorted(registry.HANDLERS),
             "capabilities": {
                 "classes": list(capabilities.CLASSES),
                 "by_class": capabilities.by_class(),
                 # the plugin verbs whose classes the PLUGIN declared: the
                 # gate enforces them, but nothing verified them, and saying
                 # so here keeps the report from reading as if it had (a
                 # review flagged it)
                 "plugin_declared": capabilities.plugin_declared(),
                 # asked from the SAME function the hermetic test uses, so a
                 # verb added without a class shows up in this reply instead of
                 # being quietly missing from a table nobody re-reads
                 "unclassified": capabilities.unclassified(
                     registry.HANDLERS, {"tab": registry.TAB_SUBCOMMANDS,
                                "profile": registry.PROFILE_SUBCOMMANDS})},
             "policy": registry.POLICY.describe(),
             "browsers": found}
    reply.update(registry.plugins_report())
    if browser:
        reply["requested"] = {"name": browser,
                              "path": shutil.which(browser) or ""}
    if browser_lib.scope():
        reply["scoped_to"] = browser_lib.scope()
    if not found:
        reply["warning"] = ("no Chromium-family browser on PATH — `open` "
                            "will refuse until one is installed")
    return reply
