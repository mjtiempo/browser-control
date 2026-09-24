"""argv — the token readers, below the CLI adapter.

Every refusal string here is the frozen contract: the messages are the ones the
CLI has always printed, spelled once. The unknown-flag scan, the "one TEXT at
most" rule and the `--flag VALUE`/`--flag=value` readers live here, UNDER the
adapter layer, so the plugin seam (`browser_control.plugin_api`) can hand a
plugin the same readers the built-in verbs use without importing
`browser_control.cli`. `browser_control.cli.argv` re-exports every name, so no
call site moves.

A bare `--` is the standard END-OF-FLAGS marker, and it is the ONE spelling
that makes a positional starting with `-` reachable (`tab type -- -hello`,
`tab js -- -1`). Everything after it is POSITIONAL and the marker itself is
never a positional; a flag-shaped token BEFORE it is refused exactly as it
always was. The marker is carried through the intermediate outputs of `_scan`
(so a second reader — `_pop` after `_flags` — still knows which tokens are
positional) and is consumed by the reader that turns tokens into positionals
(`_no_flags`).
"""
from __future__ import annotations

import math

from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    fail,
)

MARKER = "--"


def _head_of(rest: list[str]) -> str:
    """The token that names a subcommand, or "" when none does.

    A leading bare `--` is the end-of-flags marker: everything after it is
    positional, so none of it can name a subcommand — `tab -- list` is the URL
    `list`, not the `tab list` verb.
    """
    if rest and str(rest[0]) != MARKER:
        return str(rest[0])
    return ""


def _no_flags(rest: list[str], verb: str) -> list[str]:
    """The POSITIONAL args, or a refusal on the first flag-looking one.

    The ONE unknown-flag scan: `_one`, `_needle`, `_urls`, `_none` and
    `_text_arg` all report through it, so the rule and its message are spelled
    once and the flag is always reported before any arity refusal.

    A bare `--` ends the flags: it is CONSUMED (never a positional) and every
    token after it is positional, so a TEXT or a URL that starts with `-` is
    reachable at last.
    """
    out: list[str] = []
    for index, arg in enumerate(rest):
        text = str(arg)
        if text == MARKER:
            out.extend(str(later) for later in rest[index + 1:])
            return out
        if text.startswith("-"):
            fail(ERR_BAD_ARGS, f"{verb}: unknown flag {text!r}")
        out.append(text)
    return out


def _one_text_at_most(verb: str, count: int) -> str:
    """The one spelling of the "one TEXT" refusal (the literal lives here)."""
    return (f"{verb}: one TEXT at most, got {count} — quote it if it has "
            "spaces")


def _twice(flag: str, inline: bool = False) -> str:
    """The refusal a CLASS flag repeats with: naming a class twice used to
    drop the earlier one in the unsafe direction. `inline` is the
    `--flag=value` spelling the offending token used, quoted back because the
    literal is the contract, not a paraphrase of it."""
    spelled = f"{flag}=" if inline else f"{flag} "
    return (f"{flag}: given twice — name every class once "
            f"({spelled}read,write …)")


def _refuse_flag_value(flag: str, value: str, flags: tuple[str, ...]) -> None:
    """Refuse a value that is a spelling of a flag in the SAME table.

    `--frame --deny=egress` used to set `frame="--deny=egress"` and leave
    `deny` unset: the caller's policy vanished with no refusal and the gate was
    never consulted, so an egress action ran under argv that spelled a deny (a
    review measured it). The rule is keyed on the flag TUPLE being scanned, so
    a flag OUTSIDE that table is still a legal value — `--tab --for` stays the
    tab NAMED `--for`, the decoy `registry.resolved_mode` depends on — while
    the bare `--` marker, which would eat the rest of the line as one value, is
    refused here too.
    """
    if value == MARKER:
        fail(ERR_BAD_ARGS,
             f"{flag}: {value!r} is the end-of-flags MARKER, not a value")
    if value in flags or any(value.startswith(other + "=") for other in flags):
        fail(ERR_BAD_ARGS, f"{flag}: {value!r} is a FLAG, not a value")

def _scan(rest: list[str], flags: tuple[str, ...], verb: str, *,
          refuse_repeat: tuple[str, ...] = (),
          ) -> tuple[list[str], dict[str, list[str]]]:
    """Pull every `--flag VALUE` / `--flag=VALUE` for `flags` out of argv.

    The ONE reader under `_pop` (one flag, the last value), `_pop_all` (one
    flag, every value) and `_flags` (the global map): values come back in the
    order they were given, one list per flag. A bare flag takes the NEXT token
    as its value — a VERB flag that merely looks like one included, which is
    what makes `--tab --for` mean the tab NAMED `--for`, the reading the mode
    resolver depends on — but never a spelling of a flag in `flags` itself nor
    the bare `--` marker (`_refuse_flag_value`). A flag named in
    `refuse_repeat` may be given once: the class flags, where last-wins
    silently dropped the earlier class. `verb` prefixes a missing-value
    refusal; the globals pass "" because `_flags` runs before any verb is
    known.

    A bare `--` ends the flags: scanning stops there, and the marker plus
    everything after it is KEPT in the output, because the reader that turns
    tokens into positionals (`_no_flags`) is the one that consumes it — a
    second pass over the same argv must still know which tokens are positional.
    A value-carrying flag before the marker still refuses a missing value.
    """
    values: dict[str, list[str]] = {flag: [] for flag in flags}
    out: list[str] = []
    index = 0
    while index < len(rest):
        arg = str(rest[index])
        if arg == MARKER:
            out.extend(str(later) for later in rest[index:])
            break
        if arg in values:
            if index + 1 >= len(rest):
                fail(ERR_BAD_ARGS,
                     f"{verb}: {arg} needs a value" if verb
                     else f"{arg} needs a value")
            value = str(rest[index + 1])
            _refuse_flag_value(arg, value, flags)
            if arg in refuse_repeat and values[arg]:
                fail(ERR_BAD_ARGS, _twice(arg))
            values[arg].append(value)
            index += 2
            continue
        named = next((flag for flag in flags
                      if arg.startswith(flag + "=")), "")
        if named:
            value = arg.split("=", 1)[1]
            _refuse_flag_value(named, value, flags)
            if named in refuse_repeat and values[named]:
                fail(ERR_BAD_ARGS, _twice(named, inline=True))
            values[named].append(value)
            index += 1
            continue
        out.append(arg)
        index += 1
    return out, values

def _pop(rest: list[str], flag: str, verb: str) -> tuple[list[str], str | None]:
    """Remove `--flag VALUE` (or `--flag=VALUE`) from argv, once.

    None means the flag was NOT given, which a verb must be able to tell apart
    from an empty value. Given twice, the LAST value wins (the `--for` reading
    the mode resolver relies on); `_pop_all` is the repeatable form.
    """
    rest, values = _scan(rest, (flag,), verb)
    return rest, values[flag][-1] if values[flag] else None

def _tab_arg(rest: list[str], verb: str) -> tuple[list[str], str]:
    """`--tab SPEC`, or "" — the ONE reader of a tab spec.

    `--tab ""` is REFUSED rather than read as "no spec": the flag was given,
    and an empty spec reaching `_one_tab` means "the only page tab" — a tab
    nobody named. The library refuses an empty spec, so the CLI and every
    plugin must too, or the same argv means two things one layer apart
    (`tab info ""` refused it while `tab text --tab ""` quietly picked a tab).
    """
    rest, spec = _pop(rest, "--tab", verb)
    if spec is not None and not str(spec).strip():
        fail(ERR_BAD_ARGS,
             f"{verb}: --tab needs a SPEC (id:<prefix> or a title/url "
             "substring); leave the flag out to act on the only page tab")
    return rest, spec or ""


def _switch(rest: list[str], flag: str) -> tuple[list[str], bool]:
    """Pull a VALUE-LESS flag (`--uncheck`, `--full`, `--force`) out of argv.

    Only a flag BEFORE a bare `--` counts: the marker and everything after it
    stay in the output (the positional readers consume the marker), so a token
    that merely spells the switch after it is a positional, not a switch.
    """
    out: list[str] = []
    given = False
    stopped = False
    for arg in rest:
        text = str(arg)
        if not stopped and text == MARKER:
            stopped = True
        elif not stopped and text == flag:
            given = True
            continue
        out.append(text)
    return out, given

def _int(value: str, what: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        fail(ERR_BAD_ARGS, f"{what} needs a number, got {value!r}")

def _float(value: str, what: str) -> float:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        fail(ERR_BAD_ARGS, f"{what} needs a number, got {value!r}")
    if not math.isfinite(number):
        fail(ERR_BAD_ARGS, f"{what} must be a finite number, got {value!r}")
    return number

def _text_arg(rest: list[str], verb: str) -> str:
    """The single TEXT a writing verb takes — nothing else, and no flags.

    The `--` rule lives in `_no_flags`, so `tab type -- -hello` types
    `-hello`: the marker is consumed and the token after it is the TEXT.
    """
    rest = _no_flags(rest, verb)
    if not rest:
        fail(ERR_BAD_ARGS, f"{verb}: TEXT is required")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, _one_text_at_most(verb, len(rest)))
    return rest[0]
