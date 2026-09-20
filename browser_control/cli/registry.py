"""registry — the ONE action resolver for the verb surface.

A mode-carrying subcommand answers differently by mode, and the gate and the
handler must read that mode the SAME way: `action_of` is that one reader (with
`resolved_mode` underneath it), which is the seam that once shipped the
`--for JS` hole (the gate read the raw token while the verb ran caller code).

The tables themselves stay on `cli.main` (`HANDLERS`, `TAB_SUBCOMMANDS`,
`PROFILE_SUBCOMMANDS`), which is what `capabilities.unclassified` validates.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from browser_control.lib import (
    capabilities,  # pyright: ignore[reportMissingImports]
    dom,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_BAD_ARGS,
    fail,
)

Handler = Callable[[list[str], str], dict]

__all__ = ["Handler", "action_of", "modes_of", "resolved_mode"]


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
    `bad-args` rather than `not-allowed` for a typo.
    """
    head = str(rest[0]) if rest else ""
    if verb == "tab":
        if head not in tab_subcommands:
            return verb              # a URL: the URL path, which is `tab`
        if head == "wait":
            mode = resolved_mode(verb, rest, pop=pop)
            return "tab wait --for js" if mode == "js" else "tab wait"
        if head in ("dialog", "media"):
            return f"tab {head} {resolved_mode(verb, rest, pop=pop) or 'state'}"
        return f"tab {head}"
    if verb == "profile":
        return f"profile {head}" if head in profile_subcommands else ""
    return verb
