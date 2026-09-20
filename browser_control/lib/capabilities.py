"""capabilities — what every verb can do, declared once and checked.

The plan's *Kind* column made machine-readable. A policy gate, an agent's own
guardrails or a reviewer should be able to ask "may this run?" without
hardcoding a verb list in another codebase — and the answer should not drift
the first time a verb is added, so this table is checked against the CLI's own
handler tables (`cli.main.HANDLERS` and `TAB_SUBCOMMANDS`) by a hermetic test
AND at runtime: `selftest` reports what is unclassified instead of quietly
pretending the surface is complete.

One entry per RESOLVED ACTION, because a subcommand can change the class:
`tab dialog state` reads while `tab dialog accept` writes, `tab media state`
reads while `tab media play` writes, and `tab wait --for load` reads while
`tab wait --for js` runs caller code.

The vocabulary is closed and small:

* ``read``   — reads state; nothing is changed but /proc and loopback CDP
* ``write``  — changes the page, the browser, or this CLI's own authorization
* ``code``   — runs code the caller supplied (the declared escape hatch)
* ``file``   — touches a path the CALLER named (a screenshot, an upload)
* ``egress`` — would reach the network (nothing today: the plugin tier will)

`file` is about the caller's data, not about infrastructure: every verb may
append to the action log, and `open` writes a profile, which is the tool's own
business rather than a capability a caller needs to know about.

This is a DECLARATION, not a sandbox: it says what a verb can reach, and
enforcement (if it is ever wanted) belongs to whoever reads it.
"""
from __future__ import annotations

import os

from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_BAD_ARGS,
    fail,
)

CLASSES = ("egress", "file", "code", "read", "write")

#: action -> the classes it holds. Keys are what a caller actually invokes:
#: a top-level verb (`open`), a tab subcommand (`tab nav`), or a subcommand
#: whose MODE changes the answer (`tab wait --for js`).
ACTIONS: dict[str, tuple[str, ...]] = {
    # browser level
    "open": ("write",),
    "close": ("write",),
    "list": ("read",),
    "info": ("read",),
    "attach": ("write",),
    "detach": ("write",),
    "selftest": ("read",),
    # tabs
    "tab": ("write",),
    "tab list": ("read",),
    "tab frames": ("read",),
    "tab info": ("read",),
    "tab close": ("write",),
    "tab nav": ("write",),
    "tab back": ("write",),
    "tab forward": ("write",),
    "tab reload": ("write",),
    "tab activate": ("write",),
    "tab hover": ("write",),
    "tab check": ("write",),
    "tab select": ("write",),
    "tab click": ("write",),
    "tab scroll": ("write",),
    "tab focus": ("write",),
    "tab press": ("write",),
    "tab insert": ("write",),
    "tab type": ("write",),
    "tab upload": ("write", "file"),
    "tab screenshot": ("read", "file"),
    # the parts that are not simply a write: a read is a read, caller code is
    # code, and a mode can decide between them
    "tab dialog state": ("read",),
    "tab dialog accept": ("write",),
    "tab dialog dismiss": ("write",),
    "tab media state": ("read",),
    "tab media play": ("write",),
    "tab media pause": ("write",),
    "tab find": ("read",),
    "tab text": ("read",),
    "tab extract": ("read",),
    "tab wait": ("read",),
    "tab wait --for js": ("code",),
    "tab js": ("code", "write"),
    # profiles
    "profile info": ("read",),
    "profile seed": ("write", "file"),
    "profile reset": ("write",),
}


def unclassified(handlers: dict,
                 subcommands: dict[str, dict] | None = None) -> list[str]:
    """The verbs with NO entry — a top-level verb, or a subcommand of one.

    `subcommands` maps a noun to its subcommand table, so one call covers every
    noun the CLI has:

        unclassified(HANDLERS, {"tab": TAB_SUBCOMMANDS,
                               "profile": PROFILE_SUBCOMMANDS})

    Asked at runtime (`selftest` prints the answer) and by the hermetic test,
    from this one function, so a new verb cannot exist in one place and be
    missing in the other without both saying so.
    """
    missing = [verb for verb in handlers
               if verb not in ACTIONS
               and not any(key.startswith(f"{verb} ") for key in ACTIONS)]
    for noun, table in (subcommands or {}).items():
        for sub in table:
            key = f"{noun} {sub}"
            if not any(action == key or action.startswith(key + " ")
                       for action in ACTIONS):
                missing.append(key)
    return sorted(missing)


def by_class() -> dict[str, list[str]]:
    """The surface the other way round: which actions hold each class.

    Plugin-declared actions are included (see `set_plugins`): a caller asking
    "what can run under `--deny code`" should see them too.
    """
    grouped: dict[str, list[str]] = {name: [] for name in CLASSES}
    for source in (ACTIONS, PLUGIN_ACTIONS):
        for action, classes in sorted(source.items()):
            for name in classes:
                grouped.setdefault(name, []).append(action)
    return grouped


#: Actions a PLUGIN declared when it was loaded. Kept apart from `ACTIONS`
#: because the built-in table is checked against the handler tables (a plugin
#: verb is not a handler), but it is the same closed vocabulary and the same
#: gate.
PLUGIN_ACTIONS: dict[str, tuple[str, ...]] = {}


def set_plugins(actions: dict[str, tuple[str, ...]]) -> None:
    """Replace the plugin-declared actions — called once per CLI invocation.

    Replacing (rather than adding) means a process that loads plugins for one
    call cannot leave a ghost action behind for the next one.
    """
    PLUGIN_ACTIONS.clear()
    for action, classes in actions.items():
        PLUGIN_ACTIONS[action] = tuple(classes)


# ---------------------------------------------------------------- the gate
# A DECLARED surface is only half of a policy; this is the other half — the
# check that a call is allowed to do what it is about to do, before it does it.
# Two rules make it worth having:
#
# * **Fail CLOSED.** An action with no classes is refused as unclassified: a
#   policy that lets an unknown verb through is not a policy.
# * **The refusal explains itself.** It names the classes the action holds and
#   the rule that blocked them, so a blocked caller learns what to ask for.
#
# No policy set means no gate: every call behaves as it did before one existed.
ALLOW_ENV = "BROWSER_CONTROL_ALLOW"
DENY_ENV = "BROWSER_CONTROL_DENY"
EVERY = ("*", "all")

POLICY: dict = {"allow": (), "deny": (), "allow_set": False,
                "deny_set": False, "source": "", "enforced": False}


def _classes(text: str, what: str) -> tuple[str, ...]:
    """The classes a policy string names, or a refusal.

    An unknown class is REFUSED rather than ignored: a typo in a policy must
    not quietly allow what it was written to stop.
    """
    out: list[str] = []
    for part in str(text or "").replace(" ", "").split(","):
        if not part:
            continue
        if part in EVERY:
            return tuple(CLASSES)
        if part not in CLASSES:
            fail(ERR_BAD_ARGS,
                 f"{what}: {part!r} is not a capability class (have: "
                 + ", ".join(CLASSES) + ", or * for all)")
        if part not in out:
            out.append(part)
    return tuple(out)


def _classes_of(value: str, what: str) -> tuple[str, ...]:
    """The classes a policy value names — refusing one that names NONE.

    `--allow ,` names nothing, and a policy that names nothing used to read as
    "no policy", turning the gate off (measured by a review: `--allow ,` let
    `tab js` run while `BROWSER_CONTROL_ALLOW=read` was set). "Nothing is
    allowed" has a spelling — `--deny '*'` — and a value that says nothing is
    a mistake, not a policy.
    """
    text = str(value or "").strip()
    if not text:
        return ()
    if not [part for part in text.replace(" ", "").split(",") if part]:
        fail(ERR_BAD_ARGS,
             f"{what}: names no class — name them, use * for every class, or "
             "`--deny *` to allow nothing")
    return _classes(text, what)


def policy(allow: str | None = None, deny: str | None = None) -> dict:
    """Replace the policy in force, from flags AND the environment.

    A flag may NARROW what the environment set for the session, never widen or
    replace it. Measured by a review, and the reason this is not "flags win":
    with `BROWSER_CONTROL_DENY=code`, a call passing `--deny egress` replaced
    the host's deny list and `tab js` ran; with `BROWSER_CONTROL_ALLOW=read`, a
    call passing `--allow code` did the same. So both sets are kept — the allow
    side INTERSECTS (a class must be allowed by both) and the deny side UNIONS
    (a denial by either holds). A flag given with an empty value, or one that
    names no class, is refused.
    """
    env_allow, env_deny = os.environ.get(ALLOW_ENV), os.environ.get(DENY_ENV)
    # a value that names NO class is refused on the flag AND on the environment:
    # a blank one used to read as "no policy", so `BROWSER_CONTROL_ALLOW=" "`
    # switched the gate off while the same value on `--allow` was refused (a
    # review flagged the asymmetry). Presence is what tells "unset" from
    # "blank", and a blank value is a mistake in either place.
    for value, what, spell in ((allow, "--allow", "--deny *"),
                               (deny, "--deny", "--deny *"),
                               (env_allow, ALLOW_ENV, "--deny *"),
                               (env_deny, DENY_ENV, "--deny *")):
        if value is not None and not str(value).strip():
            fail(ERR_BAD_ARGS,
                 f"{what}: names no class — name them, use * for every class, "
                 f"or `{spell}` to allow nothing (unset it for no policy)")
    env_allow = env_allow or ""
    env_deny = env_deny or ""
    flag_allow = _classes_of(allow, "--allow") if allow is not None else ()
    flag_deny = _classes_of(deny, "--deny") if deny is not None else ()
    env_allow_classes = _classes_of(env_allow, ALLOW_ENV)
    env_deny_classes = _classes_of(env_deny, DENY_ENV)
    if allow is None:
        allow_classes = env_allow_classes
    elif env_allow.strip():
        allow_classes = tuple(name for name in flag_allow
                              if name in env_allow_classes)
    else:
        allow_classes = flag_allow
    deny_classes = tuple(dict.fromkeys(flag_deny + env_deny_classes))
    allow_set = allow is not None or bool(env_allow.strip())
    deny_set = deny is not None or bool(env_deny.strip())
    sources = []
    if allow is not None:
        sources.append("--allow")
    if env_allow.strip():
        sources.append(ALLOW_ENV)
    if deny is not None:
        sources.append("--deny")
    if env_deny.strip():
        sources.append(DENY_ENV)
    POLICY.update({"allow": allow_classes, "deny": deny_classes,
                   "allow_set": allow_set, "deny_set": deny_set,
                   "source": " + ".join(sources),
                   # a policy is in force when one was ASKED for, even if the
                   # resolved sets are empty: an empty allow-list means nothing
                   # is allowed, which is not the same as no policy at all
                   "enforced": bool(allow_set or deny_set)})
    return dict(POLICY)


def describe() -> dict:
    """The policy in force, for `selftest` to report."""
    return {"allow": list(POLICY["allow"]), "deny": list(POLICY["deny"]),
            "allow_set": POLICY["allow_set"], "deny_set": POLICY["deny_set"],
            "source": POLICY["source"],
            "enforced": bool(POLICY["enforced"])}


def allowed(action: str) -> tuple[bool, str]:
    """May that action run under the policy in force? (yes, or why not)."""
    if not POLICY["enforced"]:
        return True, ""
    classes = ACTIONS.get(action) or PLUGIN_ACTIONS.get(action)
    if not classes:
        return False, (f"{action} is not in the declared surface (see "
                       "`selftest`), and an unclassified verb is refused")
    blocked = [name for name in classes if name in POLICY["deny"]]
    if blocked:
        return False, (f"{action} is {'+'.join(classes)}, and "
                       f"{blocked[0]!r} is denied by {POLICY['source']}")
    if POLICY["allow_set"]:
        # an ALLOW-SET with no class in it allows NOTHING: `if allow:` would
        # read that as "no allow-list", which is how an empty policy turned the
        # gate off (measured)
        missing = [name for name in classes if name not in POLICY["allow"]]
        if missing:
            return False, (f"{action} is {'+'.join(classes)}, and "
                           f"{missing[0]!r} is not allowed by "
                           f"{POLICY['source']}")
    return True, ""
