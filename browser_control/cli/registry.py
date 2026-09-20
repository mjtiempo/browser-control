"""registry — the verb surface as records, and the ONE action resolver.

The same verb used to be declared four times: names→callables, names→classes,
prose in `USAGE`, and frame eligibility. A `Verb` record carries all of it, and
the gate and the handler read the SAME `action_of`, which is the seam that once
shipped the `--for JS` hole (the gate read the raw token while the verb ran
caller code).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
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

__all__ = ["Handler", "Verb", "VerbTable", "action_of", "modes_of"]


@dataclass(frozen=True)
class Verb:
    """One verb (or subcommand) as a record."""

    name: str
    handler: Handler
    classes: tuple[str, ...] = ()
    usage: str = ""
    takes_scope: bool = True
    takes_browser: bool = True
    frame_ok: bool = False
    action_of: Callable[[list[str]], str] | None = None


@dataclass
class VerbTable:
    """The verb surface: top-level verbs, and each noun's subcommands."""

    verbs: dict[str, Verb] = field(default_factory=dict)
    subcommands: dict[str, dict[str, Verb]] = field(default_factory=dict)

    def handlers(self) -> dict[str, Handler]:
        """The top-level name→callable map the CLI dispatches through."""
        return {name: verb.handler for name, verb in self.verbs.items()}

    def sub(self, noun: str) -> dict[str, Handler]:
        """One noun's subcommand table (name→callable)."""
        return {name: verb.handler
                for name, verb in self.subcommands.get(noun, {}).items()}

    def lookup(self, verb: str, rest: list[str]) -> Verb | None:
        """The record a call resolves to, or None for a name nobody has."""
        record = self.verbs.get(verb)
        if record is None:
            return None
        head = str(rest[0]) if rest else ""
        if head and head in self.subcommands.get(verb, {}):
            return self.subcommands[verb][head]
        return record

    def action_of(self, verb: str, rest: list[str]) -> str:
        """Which DECLARED action a call is: verb, subcommand, and its mode."""
        record = self.lookup(verb, rest)
        if record is None or record.action_of is None:
            return ""
        return record.action_of(rest)


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
