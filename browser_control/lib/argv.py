"""argv — the token readers, below the CLI adapter.

Every refusal string here is the frozen contract: the messages are the ones the
CLI has always printed, spelled once. The unknown-flag scan, the "one TEXT at
most" rule and the `--flag VALUE`/`--flag=value` readers live here, UNDER the
adapter layer, so the plugin seam (`browser_control.plugin_api`) can hand a
plugin the same readers the built-in verbs use without importing
`browser_control.cli`. `browser_control.cli.argv` re-exports every name, so no
call site moves.
"""
from __future__ import annotations

import math

from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    fail,
)


def _no_flags(rest: list[str], verb: str) -> list[str]:
    """The remaining args, or a refusal on the first flag-looking one.

    The ONE unknown-flag scan: `_one`, `_needle`, `_urls`, `_none` and
    `_text_arg` all report through it, so the rule and its message are spelled
    once and the flag is always reported before any arity refusal.
    """
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"{verb}: unknown flag {arg!r}")
    return rest


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


def _scan(rest: list[str], flags: tuple[str, ...], verb: str, *,
          refuse_repeat: tuple[str, ...] = (),
          ) -> tuple[list[str], dict[str, list[str]]]:
    """Pull every `--flag VALUE` / `--flag=VALUE` for `flags` out of argv.

    The ONE reader under `_pop` (one flag, the last value), `_pop_all` (one
    flag, every value) and `_flags` (the global map): values come back in the
    order they were given, one list per flag. A bare flag takes the NEXT token
    whatever it is — even one that looks like a flag — which is what makes
    `--tab --for` mean the tab NAMED `--for`, the reading the mode resolver
    depends on. A flag named in `refuse_repeat` may be given once: the class
    flags, where last-wins silently dropped the earlier class. `verb` prefixes
    a missing-value refusal; the globals pass "" because `_flags` runs before
    any verb is known.
    """
    values: dict[str, list[str]] = {flag: [] for flag in flags}
    out: list[str] = []
    index = 0
    while index < len(rest):
        arg = str(rest[index])
        if arg in values:
            if index + 1 >= len(rest):
                fail(ERR_BAD_ARGS,
                     f"{verb}: {arg} needs a value" if verb
                     else f"{arg} needs a value")
            if arg in refuse_repeat and values[arg]:
                fail(ERR_BAD_ARGS, _twice(arg))
            values[arg].append(str(rest[index + 1]))
            index += 2
            continue
        named = next((flag for flag in flags
                      if arg.startswith(flag + "=")), "")
        if named:
            if named in refuse_repeat and values[named]:
                fail(ERR_BAD_ARGS, _twice(named, inline=True))
            values[named].append(arg.split("=", 1)[1])
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

def _switch(rest: list[str], flag: str) -> tuple[list[str], bool]:
    """Pull a VALUE-LESS flag (`--uncheck`, `--full`, `--force`) out of argv."""
    given = any(str(arg) == flag for arg in rest)
    return [arg for arg in rest if str(arg) != flag], given

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
    """The single TEXT a writing verb takes — nothing else, and no flags."""
    _no_flags(rest, verb)
    if not rest:
        fail(ERR_BAD_ARGS, f"{verb}: TEXT is required")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, _one_text_at_most(verb, len(rest)))
    return rest[0]
