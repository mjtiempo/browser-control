"""argv — the flag readers every verb shares.

Every refusal string here is the frozen contract: the messages are the ones the
CLI has always printed, spelled once. The unknown-flag scan, the "one TEXT at
most" rule and the `--flag VALUE`/`--flag=value` readers live here, so a handler
names a rule instead of retyping it.
"""
from __future__ import annotations

import math

from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    fail,
)


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

def _one_text_at_most(verb: str, count: int) -> str:
    """The one spelling of the "one TEXT" refusal (the literal lives here)."""
    return (f"{verb}: one TEXT at most, got {count} — quote it if it has "
            "spaces")


def _no_flags(rest: list[str], verb: str) -> list[str]:
    """The remaining args, or a refusal on the first flag-looking one."""
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"{verb}: unknown flag {arg!r}")
    return rest


def _needle(rest: list[str], verb: str) -> str | None:
    """The optional single TEXT a matcher verb takes, or a refusal.

    The unknown-flag scan and the "one TEXT at most" rule in ONE place — six
    verbs used to spell both out (RF-31).
    """
    _no_flags(rest, verb)
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, _one_text_at_most(verb, len(rest)))
    return rest[0] if rest else None


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

def _opt_int(value: str | None, what: str) -> int | None:
    """A flag's number, or None when the flag was not given at all.

    The ``… if value is not None else None`` a dozen verbs spelled out: the
    difference between an absent flag and a bad value is the whole point of
    the check, so it stays one call.
    """
    return None if value is None else _int(value, what)

def _selector(rest: list[str], verb: str, allow: tuple[str, ...]) -> dict:
    """`--port N | --pid N | --profile DIR`, plus `--list`/`--all` where a
    verb allows them. Pure argv work: an unknown flag is refused, and the
    combinations that mean two different things are refused too."""
    out = {"port": 0, "pid": 0, "list": False, "all": False}
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
        if arg in ("--port", "--pid"):
            if index + 1 >= len(rest):
                fail(ERR_BAD_ARGS, f"{verb}: {arg} needs a value")
            key, value = arg[2:], str(rest[index + 1])
            out[key] = _int(value, f"{verb}: {arg}")
            index += 2
            continue
        known = ", ".join(["--port N", "--pid N", "--profile DIR"]
                          + [f"--{name}" for name in allow])
        fail(ERR_BAD_ARGS,
             f"{verb}: unknown argument {arg!r} (flags: {known})")
    if out["list"] and (out["all"] or out["port"] or out["pid"]):
        fail(ERR_BAD_ARGS, f"{verb}: --list takes no other argument")
    if out["all"] and (out["port"] or out["pid"]):
        fail(ERR_BAD_ARGS, f"{verb}: --all takes no other selector")
    return out

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

def _pop_all(rest: list[str], flag: str,
             verb: str) -> tuple[list[str], list[str]]:
    """Remove EVERY `--flag VALUE` (or `--flag=VALUE`), values in order.

    A repeatable flag needs its own return shape: `--except a --except b` is
    two exceptions, and `_pop` would answer only the last one.
    """
    rest, values = _scan(rest, (flag,), verb)
    return rest, values[flag]

def _text_arg(rest: list[str], verb: str) -> str:
    """The single TEXT a writing verb takes — nothing else, and no flags."""
    for arg in rest:
        if str(arg).startswith("-"):
            fail(ERR_BAD_ARGS, f"{verb}: unknown flag {arg!r}")
    if not rest:
        fail(ERR_BAD_ARGS, f"{verb}: TEXT is required")
    if len(rest) > 1:
        fail(ERR_BAD_ARGS, _one_text_at_most(verb, len(rest)))
    return rest[0]

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

    The pulling is the shared `_scan` — the same reader `_pop`/`_pop_all` use —
    in ONE pass, because the value of a bare flag is the next token whatever it
    looks like. `--allow`/`--deny` are the two the reader refuses to see twice
    (`refuse_repeat`): the per-verb repeatable flags have the opposite contract,
    last-wins, which for a capability class is the unsafe direction.
    """
    rest, values = _scan(args, tuple(FLAG_KEY), "",
                         refuse_repeat=("--allow", "--deny"))
    return rest, {key: (values[flag][-1] if values[flag] else None)
                  for flag, key in FLAG_KEY.items()}
