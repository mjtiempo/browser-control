"""argv — the flag readers every verb shares.

Every refusal string here is the frozen contract: the messages are the ones the
CLI has always printed, spelled once. The unknown-flag scan, the "one TEXT at
most" rule and the `--flag VALUE`/`--flag=value` readers live in
`browser_control.lib.argv` — UNDER this adapter layer, so `plugin_api` can hand
a plugin the same readers without importing the CLI — and are re-exported here,
so no call site moves. This module keeps the readers whose shape is a VERB's:
the single positional, the optional needle, the no-argument and URL rules, and
the `--port`/`--pid`/`--tab`/global-flag grammar.
"""
from __future__ import annotations

from browser_control.lib.argv import (  # noqa: F401
    _float,
    _int,
    _no_flags,
    _one_text_at_most,
    _pop,
    _scan,
    _switch,
    _tab_arg,
    _text_arg,
    _twice,
)
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    fail,
)


def _one(rest: list[str], verb: str, required: bool = False) -> str:
    """The verb's single positional argument, or a refusal."""
    _no_flags(rest, verb)
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
    for arg in _no_flags(rest, verb):
        fail(ERR_BAD_ARGS, f"{verb}: takes no arguments, got {arg!r}")

def _urls(rest: list[str], verb: str) -> list[str]:
    """Every URL this verb was given, in order — none is dropped.

    A flag is refused rather than ignored (the tab is `--tab`, and these verbs
    take no other), and a caller that asks for three sites gets three.
    """
    return list(_no_flags(rest, verb))

def _no_browser_flag(verb: str, browser: str) -> None:
    """Refuse `--browser` on a verb that reports every browser it finds."""
    if browser:
        fail(ERR_BAD_ARGS,
             f"{verb}: --browser does not apply — this verb reports every "
             "browser on the machine")

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
            if out[key] < 1:
                # 0 is a number the SELECTOR grammar does not have: it used
                # to read as "no selector" (`Selector.given` filters falsy),
                # so `close --port 0` stopped the MANAGED browser and
                # `--profile DIR --port 0` silently dropped the --port
                fail(ERR_BAD_ARGS,
                     f"{verb}: {arg} must be 1 or more, got {value!r}")
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

def _tab_flag(rest: list[str], verb: str) -> tuple[list[str], str]:
    """`--tab SPEC`, or "" — the tab a page verb acts on.

    The rule lives in `lib.argv._tab_arg` so the plugin seam hands plugins the
    SAME reader: `--tab ""` is refused there, never read as "no spec" — the
    same argv must not mean two things one layer apart (`tab info ""` refused
    it while `tab text --tab ""` quietly picked a tab).
    """
    return _tab_arg(rest, verb)

def _pop_all(rest: list[str], flag: str,
             verb: str) -> tuple[list[str], list[str]]:
    """Remove EVERY `--flag VALUE` (or `--flag=VALUE`), values in order.

    A repeatable flag needs its own return shape: `--except a --except b` is
    two exceptions, and `_pop` would answer only the last one.
    """
    rest, values = _scan(rest, (flag,), verb)
    return rest, values[flag]

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
