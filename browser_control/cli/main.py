"""browser-control-cli — argv in, service calls out, JSON printed.

Adapters for the verbs, registered in `registry.HANDLERS`; `main` parses
`--browser`, dispatches, prints the reply as one JSON object on stdout, and
maps a refusal to `ERR[code]: message` on stderr with exit 2. The reply is the
only thing on stdout — the no-verb path prints the usage text to stderr — and
no caller-producible input can produce a traceback.
"""
from __future__ import annotations

import contextlib
import difflib
import json
import os
import sys

from browser_control.cli import registry
from browser_control.cli.argv import (
    _flags,
    _pop,
    _urls,
)
from browser_control.cli.verbs.browser import (
    cmd_attach,
    cmd_close,
    cmd_detach,
    cmd_info,
    cmd_list,
    cmd_open,
    cmd_selftest,
)
from browser_control.cli.verbs.profile import (
    cmd_profile_info,
    cmd_profile_logins,
    cmd_profile_reset,
    cmd_profile_seed,
)
from browser_control.cli.verbs.tab import (
    cmd_tab_activate,
    cmd_tab_back,
    cmd_tab_check,
    cmd_tab_click,
    cmd_tab_close,
    cmd_tab_dialog,
    cmd_tab_extract,
    cmd_tab_find,
    cmd_tab_focus,
    cmd_tab_forward,
    cmd_tab_frames,
    cmd_tab_hover,
    cmd_tab_info,
    cmd_tab_insert,
    cmd_tab_js,
    cmd_tab_list,
    cmd_tab_media,
    cmd_tab_nav,
    cmd_tab_press,
    cmd_tab_reload,
    cmd_tab_screenshot,
    cmd_tab_scroll,
    cmd_tab_select,
    cmd_tab_text,
    cmd_tab_type,
    cmd_tab_upload,
    cmd_tab_wait,
)
from browser_control.lib import audit, capabilities, dom
from browser_control.lib import browser as browser_lib
from browser_control.lib import plugins as plugins_lib
from browser_control.lib import policy as policy_lib
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    ERR_BROKEN_PIPE,
    ERR_INTERNAL,
    ERR_NOT_ALLOWED,
    ERR_UNKNOWN_COMMAND,
    ControlError,
    fail,
)

USAGE = """usage: browser-control-cli VERB [ARGS]

  open [URL...] [--headless]
                     start (or adopt) the managed browser; each URL opens;
                     --headless starts it with NO WINDOW (`--headless=new`)
                     and the same verbs drive it
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
  profile logins [--profile DIR] [--site HOST] [--cap N]
                     what logins are IN a managed profile: the hosts its
                     Cookies store names (with expiry) and how many saved
                     logins — read from a COPY of the profile's own stores;
                     evidence, not a session check
  profile seed --from DIR [--force] [--dry]
                     copy a source profile's LOGINS into a managed one (no
                     caches, no lock files, read back; --force WIPES the
                     target first, so what is left is the source and never a
                     mix of the two; --dry counts first); DIR is one Chrome
                     profile (.../Default, Profile 1) or a whole user-data
                     directory — either lands where Chrome reads it (a
                     profile directory under the instance's Default/)
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
                                 — the page's OWN value: a string that parses
                                 as JSON is still that string
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
  selftest [--classes]
                     prove the install: interpreter, websockets, verbs;
                     `--classes` answers the policy question alone (every
                     action and the classes it may reach)
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
def cmd_tab(rest: list[str], browser: str) -> dict:
    """`tab [URL...]` opens tabs; `tab list|info|close` are subcommands.

    A subcommand is a reserved word, so `tab list` can never mean "open the
    site `list`" — a URL carries a scheme (`https://…`, `about:blank`), which
    is what the URL policy enforces anyway.
    """
    handler = registry.TAB_SUBCOMMANDS.get(str(rest[0]) if rest else "")
    if handler is not None:
        return handler(rest[1:], browser)
    return browser_lib.new_tab(_urls(rest, "tab"), browser=browser)
def cmd_profile(rest: list[str], browser: str) -> dict:
    """`profile info|seed|reset` — the browser-level noun for profiles.

    `tab` owns everything about a page tab, the way this owns the profiles the
    CLI manages: seeing them, giving one a source profile's logins, and wiping
    one. The instance itself is named with the global `--profile DIR`.
    """
    handler = registry.PROFILE_SUBCOMMANDS.get(str(rest[0]) if rest else "")
    if handler is None:
        fail(ERR_BAD_ARGS,
             "profile: a subcommand is required (info, logins, seed, reset)")
    return handler(rest[1:], browser)
# the tables' HOME is `cli.registry`, so a verb reads them without reaching
# into this module's privates; the adapters are defined here, where the verb
# imports are, and handed over once
registry.register(
    tab={
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
    },
    profile={
        "info": cmd_profile_info,
        "logins": cmd_profile_logins,
        "seed": cmd_profile_seed,
        "reset": cmd_profile_reset,
    },
    handlers={
        "open": cmd_open,
        "close": cmd_close,
        "list": cmd_list,
        "info": cmd_info,
        "attach": cmd_attach,
        "detach": cmd_detach,
        "profile": cmd_profile,
        "tab": cmd_tab,
        "selftest": cmd_selftest,
    })
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
        near = difflib.get_close_matches(str(word),
                                         sorted(registry.TAB_SUBCOMMANDS),
                                         n=1, cutoff=0.6)
        fail(ERR_BAD_ARGS,
             f"tab: {str(word)[:40]!r} is neither a subcommand (have: "
             + ", ".join(sorted(registry.TAB_SUBCOMMANDS))
             + ") nor a URL (http(s) or about:blank only)"
             + (f" — did you mean `tab {near[0]}`?" if near else ""))
def action(verb: str, rest: list[str]) -> str:
    """Which DECLARED action a call is: verb, subcommand, and its mode.

    The reading itself lives in `cli.registry.action_of` — the SAME function the
    gate and the verb use, so a mode-carrying subcommand cannot be a read to the
    gate and a write to the browser (`--for JS` is refused because both read it
    as `js`).
    """
    return registry.action_of(verb, rest,
                              tab_subcommands=registry.TAB_SUBCOMMANDS,
                              profile_subcommands=registry.PROFILE_SUBCOMMANDS,
                              pop=_pop)
def _parse_globals(flags: dict[str, str | None]) -> dict[str, str]:
    """The name-valued globals a call runs with — pure, and refused when empty.

    A flag given an EMPTY value is a MISTAKE, not an absent flag: every other
    value-carrying flag refuses one (`--tab ""`, `--frame ""`, a policy that
    names no class), while `--profile ""` silently cleared the instance scope
    and `--browser ""` fell back to the default (a review flagged the
    asymmetry). Nothing here touches the process-global state: `_run_invocation`
    sets scope, frame and policy in the ORDER that is contract.
    """
    for name, value in (("--browser", flags["browser"]),
                        ("--profile", flags["profile"])):
        if value is not None and not str(value).strip():
            fail(ERR_BAD_ARGS,
                 f"{name}: an empty value is not a name — name a browser "
                 "or a profile, or leave the flag off")
    return {"browser": flags["browser"] or "",
            "profile": flags["profile"] or "",
            "frame": flags["frame"] or ""}
def _authorise(verb: str, rest: list[str],
               flags: dict[str, str | None]) -> None:
    """Refuse a call the surface or the policy will not run, before dispatch.

    The action (with its MODE) is read by the SAME reader the handler runs
    through (`action`), so what the gate allows and what the verb does cannot
    disagree. `selftest` is never gated — it is the verb that REPORTS the
    policy, and a gate that blocks its own explanation is a trap — while every
    other verb answers to the classes its action declares. A `--frame` scope
    that cannot apply is refused here too, so `list --frame 1` and
    `open --frame 1 URL` cannot accept a scope and silently drop it.
    """
    head = str(rest[0]) if rest else ""
    if verb == "tab" and head and head not in registry.TAB_SUBCOMMANDS:
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
            permitted, why = registry.POLICY.allowed(
                wanted, capabilities.classes_for(wanted))
            if not permitted:
                fail(ERR_NOT_ALLOWED, why)
def _run_invocation(args: list[str]) -> int:
    """One call end to end: plugins, globals, the gate, dispatch, emit.

    Kept as one body because every step's ORDER is contract: plugins load before
    the help text and the gate, the scope/frame/policy are set or cleared before
    the handler runs, and the audit line is written in `finally` on every path
    (refusals included). The two PURE decisions are called IN PLACE —
    `_parse_globals` where the globals are read, `_authorise` after the handler
    is known — so the order they run in is still this body's. `main` is the
    facade the console script calls.
    """
    # Plugins load once per invocation, BEFORE the help text and the gate:
    # `--help`/`selftest` report them, and the classes they declare have to be
    # in the surface before `allowed()` is asked anything.
    registry.PLUGINS.reset(plugins_lib.PluginSet.load(
        reserved=set(registry.HANDLERS) | {"help"}))
    capabilities.set_plugins(registry.PLUGINS.classes())
    # help is a VERB and also `-h`/`--help`, and the global flags may appear
    # anywhere — so the token is looked for after they are stripped too. The
    # argv[0]-only test made `--browser chrome --help` an `unknown-command`,
    # contradicting the flag grammar the usage text states (a review flagged
    # it). A bad global flag is left for the real parse below to refuse.
    rest_for_help: list[str] = []
    with contextlib.suppress(ControlError):
        rest_for_help = _flags(args)[0]
    if (args and args[0] in ("-h", "--help", "help")) or \
            (rest_for_help and rest_for_help[0] in ("-h", "--help", "help")):
        print(USAGE)
        for usage in registry.PLUGINS.usages():
            print(f"  {usage}")
        return 0
    # reset the per-invocation secret HERE, before any early refusal: a call
    # that refuses before the verb must not be stamped with the PREVIOUS
    # call's `redacted` (a review flagged the stale stamp)
    audit.LOG.begin()
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
                  f"(have: {', '.join(registry.verb_names())})",
                  file=sys.stderr)
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
                  f"(have: {', '.join(registry.verb_names())})"
                  + (f" — {given} was given, but no verb to run"
                     if given else ""), file=sys.stderr)
            code = ERR_BAD_ARGS           # so the audit line carries the code
            return 2
        verb, rest = rest[0], rest[1:]
        globals_ = _parse_globals(flags)
        # the globals, in the order they matter: the instance, the frame, the
        # policy. Each is SET OR CLEARED per invocation, so no verb inherits
        # another call's scope.
        browser = globals_["browser"]
        browser_lib.scope(globals_["profile"])
        dom.frame(globals_["frame"])
        # NOT `or None`: a flag given an empty value must reach the policy,
        # which refuses it, instead of reading as "the call named no policy"
        registry.POLICY.update(policy_lib.Policy.from_sources(flags["allow"],
                                                             flags["deny"]))
        handler = registry.HANDLERS.get(verb)
        if handler is None:
            plugin = registry.PLUGINS.actions.get(verb)
            handler = plugin.run if plugin is not None else None
        if handler is None:
            raise ControlError(ERR_UNKNOWN_COMMAND,
                               f"{verb} (have: "
                               + ", ".join(registry.verb_names()) + ")")
        head = str(rest[0]) if rest else ""
        _authorise(verb, rest, flags)
        reply = handler(rest, browser)
        # ONE JSON object on stdout is the contract the usage text states and
        # every plugin is told to keep: a handler that returns a list, a string
        # or None used to print it with exit status 0, breaking every consumer
        # of stdout with no refusal to branch on — so the shape is checked
        # here, where the handler's own name is still in hand
        if not isinstance(reply, dict):
            fail(ERR_INTERNAL,
                 f"{verb}: the handler returned {type(reply).__name__}, "
                 "not a JSON object")
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
def main(argv: list[str] | None = None) -> int:
    """The console entry point: argv in, one call, one exit code."""
    return _run_invocation(list(sys.argv[1:] if argv is None else argv))
