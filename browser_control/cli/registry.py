"""registry — the ONE action resolver for the verb surface, and its tables.

A mode-carrying subcommand answers differently by mode, and the gate and the
handler must read that mode the SAME way: `action_of` is that one reader (with
`resolved_mode` underneath it), which is the seam that once shipped the
`--for JS` hole (the gate read the raw token while the verb ran caller code).

The tables live HERE — `HANDLERS`, `TAB_SUBCOMMANDS`, `PROFILE_SUBCOMMANDS`,
the gate's `POLICY` and the plugin set `PLUGINS` — because `selftest` reports
them and `capabilities.unclassified` validates them, and a verb reaching into
`cli.main` for them was a cycle kept honest only by a deferred import. They are
filled by `cli.main` through `register()` at import: the adapters need the verb
modules, and importing those from the tables' home would put the cycle back.
This module imports no verb module, so the graph stays a DAG (main -> verbs ->
registry, main -> registry). The tables are empty until `cli.main` is imported;
their only reader that could observe that — `cmd_selftest` — is reachable
solely through main's dispatch (or a test that imports main).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from browser_control.lib import (
    capabilities,
    dom,
    plugins as plugins_lib,
    policy as policy_lib,
)
from browser_control.lib.argv import _head_of
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    fail,
)

Handler = Callable[[list[str], str], dict]

__all__ = ["Handler", "HANDLERS", "PLUGINS", "POLICY", "PROFILE_SUBCOMMANDS",
           "TAB_SUBCOMMANDS", "action_of", "modes_of", "plugins_report",
           "register", "resolved_mode", "verb_names"]

#: Every top-level verb a caller can run, built-ins first; filled by `cli.main`.
HANDLERS: dict[str, Handler] = {}
#: The `tab` subcommands, by the word that names them.
TAB_SUBCOMMANDS: dict[str, Handler] = {}
#: The `profile` subcommands, by the word that names them.
PROFILE_SUBCOMMANDS: dict[str, Handler] = {}

#: The gate's policy for the invocation in flight (`cli.main` sets it).
POLICY = policy_lib.Policy()
#: The plugin set loaded for the invocation in flight (`cli.main` sets it).
PLUGINS = plugins_lib.PluginSet()


def register(*, handlers: dict[str, Handler], tab: dict[str, Handler],
             profile: dict[str, Handler]) -> None:
    """Fill the tables. Called ONCE, by `cli.main` at import.

    Registration, not definition: the adapters live in `cli.main` and the verb
    modules, and importing those from the tables' home would put the cycle
    back. `update`, so the caller hands its literals over without this module
    and the caller sharing one object.
    """
    HANDLERS.update(handlers)
    TAB_SUBCOMMANDS.update(tab)
    PROFILE_SUBCOMMANDS.update(profile)


def verb_names() -> list[str]:
    """Every top-level verb a caller can run: built-ins first, then plugins."""
    return [*HANDLERS, *PLUGINS.actions]


def plugins_report() -> dict:
    """What the loaded plugin set has to say (`selftest` prints it)."""
    return PLUGINS.report()


def modes_of(head: str) -> tuple[str, ...]:
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


def resolved_mode(verb: str, rest: list[str], *,
                  pop: Callable[[list[str], str, str], tuple[list[str], Any]],
                  ) -> str:
    """The mode a call will RUN — read the way its handler will read it.

    The gate authorises a mode-carrying subcommand BY its mode, so it has to
    resolve that mode exactly as the verb does: the LAST `--for` (what the
    reader leaves), normalised by `dom.mode_of` (the one normaliser in the
    codebase), and for a positional mode the first positional left once that
    verb's own flags are out of the way. A mode the gate cannot read is one it
    cannot authorise, so an unrecognised one is refused here rather than guessed
    at — a typo has to be `bad-args` whether or not a policy is in force.
    """
    if verb != "tab" or not rest:
        return ""
    head = str(rest[0])
    if head == "wait":
        # `--tab` FIRST, exactly as `cmd_tab_wait` pops it: a `--tab` value that
        # is literally `--for` must not be read as the mode
        args = list(rest[1:])
        args, _spec = pop(args, "--tab", "tab wait")
        _kept, value = pop(args, "--for", "tab wait")
        if value is None:
            return ""
        raw = str(value)
    elif head in ("dialog", "media"):
        args = list(rest[1:])
        args, _spec = pop(args, "--tab", f"tab {head}")
        value_flag = "--text" if head == "dialog" else "--index"
        args, _value = pop(args, value_flag, f"tab {head}")
        raw = next((str(arg) for arg in args
                    if not str(arg).startswith("-")), "")
        if not raw:
            return ""
    else:
        return ""
    mode = dom.mode_of(raw)
    modes = modes_of(head)
    if mode not in modes:
        fail(ERR_BAD_ARGS,
             f"tab {head}: {'--for' if head == 'wait' else 'MODE'} is "
             + "|".join(modes) + f", got {raw!r}")
    return mode


def _js_writes_a_file(rest: list[str], *,
                      pop: Callable[[list[str], str, str], tuple[list[str], Any]],
                      ) -> bool:
    """Does this `tab js` call write a file?

    `--out FILE` is the one `tab js` flag that changes what the call can DO —
    it writes a file the caller names — and the gate authorises a call by its
    action, from the argv alone, before the handler runs. `--tab` is popped
    FIRST, exactly as `cmd_tab_js` pops it, so a `--tab` value that is
    literally `--out` is not read as the flag.
    """
    args = list(rest[1:])
    args, _spec = pop(args, "--tab", "tab js")
    _kept, out = pop(args, "--out", "tab js")
    return out is not None


def action_of(verb: str, rest: list[str], *,
              tab_subcommands: dict[str, Handler],
              profile_subcommands: dict[str, Handler],
              pop: Callable[[list[str], str, str], tuple[list[str], Any]],
              ) -> str:
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
    `bad-args` rather than `not-allowed` for a typo. The head is read by
    `lib.argv._head_of`, the same reader the dispatcher uses: a leading bare
    `--` means everything after it is positional, so `tab -- list` is the URL
    path (`tab`), not the `tab list` subcommand.
    """
    head = _head_of(rest)
    if verb == "tab":
        if head not in tab_subcommands:
            return verb              # a URL: the URL path, which is `tab`
        if head == "wait":
            mode = resolved_mode(verb, rest, pop=pop)
            return "tab wait --for js" if mode == "js" else "tab wait"
        if head == "js":
            # the file class must be reachable from the argv ALONE: the gate
            # runs before the handler, so `--out` is read here the same way
            return "tab js --out" if _js_writes_a_file(rest, pop=pop) \
                else "tab js"
        if head in ("dialog", "media"):
            return f"tab {head} {resolved_mode(verb, rest, pop=pop) or 'state'}"
        return f"tab {head}"
    if verb == "profile":
        return f"profile {head}" if head in profile_subcommands else ""
    return verb
