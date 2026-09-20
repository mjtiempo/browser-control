"""policy — the capability gate as a value object.

A DECLARED surface is only half of a policy; this is the other half — the
check that a call is allowed to do what it is about to do, before it does it.
Two rules make it worth having:

* **Fail CLOSED.** An action with no classes is refused as unclassified: a
  policy that lets an unknown verb through is not a policy.
* **The refusal explains itself.** It names the classes the action holds and
  the rule that blocked them, so a blocked caller learns what to ask for.

No policy set means no gate: every call behaves as it did before one existed.
`cli.main` builds one `Policy` per invocation from the flags and the
environment; `lib.capabilities` answers which classes an action holds.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

from browser_control.lib.capabilities import CLASSES
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_BAD_ARGS,
    fail,
)

ALLOW_ENV = "BROWSER_CONTROL_ALLOW"
DENY_ENV = "BROWSER_CONTROL_DENY"
EVERY = ("*", "all")

__all__ = ["ALLOW_ENV", "DENY_ENV", "EVERY", "Policy"]


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


class Policy:
    """The rule in force for ONE invocation: allow, deny, and their source."""

    def __init__(self, *, allow: tuple[str, ...] = (),
                 deny: tuple[str, ...] = (), allow_set: bool = False,
                 deny_set: bool = False, source: str = "",
                 enforced: bool = False) -> None:
        self.allow = tuple(allow)
        self.deny = tuple(deny)
        self.allow_set = bool(allow_set)
        self.deny_set = bool(deny_set)
        self.source = source
        self.enforced = bool(enforced)

    @classmethod
    def from_sources(cls, allow: str | None = None, deny: str | None = None,
                     env: Mapping[str, str] | None = None) -> Policy:
        """Build the policy from flags AND the environment.

        A flag may NARROW what the environment set for the session, never
        widen or replace it. Measured by a review, and the reason this is not
        "flags win": with `BROWSER_CONTROL_DENY=code`, a call passing
        `--deny egress` replaced the host's deny list and `tab js` ran; with
        `BROWSER_CONTROL_ALLOW=read`, a call passing `--allow code` did the
        same. So both sets are kept — the allow side INTERSECTS (a class must
        be allowed by both) and the deny side UNIONS (a denial by either
        holds). A flag given with an empty value, or one that names no class,
        is refused.
        """
        env = os.environ if env is None else env
        env_allow, env_deny = env.get(ALLOW_ENV), env.get(DENY_ENV)
        # a value that names NO class is refused on the flag AND on the
        # environment: a blank one used to read as "no policy", so
        # `BROWSER_CONTROL_ALLOW=" "` switched the gate off while the same
        # value on `--allow` was refused (a review flagged the asymmetry).
        # Presence is what tells "unset" from "blank", and a blank value is a
        # mistake in either place.
        for value, what, spell in ((allow, "--allow", "--deny *"),
                                   (deny, "--deny", "--deny *"),
                                   (env_allow, ALLOW_ENV, "--deny *"),
                                   (env_deny, DENY_ENV, "--deny *")):
            if value is not None and not str(value).strip():
                fail(ERR_BAD_ARGS,
                     f"{what}: names no class — name them, use * for every "
                     f"class, or `{spell}` to allow nothing (unset it for no "
                     "policy)")
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
        return cls(allow=allow_classes, deny=deny_classes, allow_set=allow_set,
                   deny_set=deny_set, source=" + ".join(sources),
                   # a policy is in force when one was ASKED for, even if the
                   # resolved sets are empty: an empty allow-list means nothing
                   # is allowed, which is not the same as no policy at all
                   enforced=bool(allow_set or deny_set))

    def update(self, other: Policy) -> None:
        """Become `other` — the CLI's holder for one invocation's policy.

        Attribute mutation, not rebinding: the holder is one object and every
        reader of it (the gate, `selftest`) sees the invocation's rule without
        a module-level `global`.
        """
        self.allow = other.allow
        self.deny = other.deny
        self.allow_set = other.allow_set
        self.deny_set = other.deny_set
        self.source = other.source
        self.enforced = other.enforced

    def describe(self) -> dict:
        """The policy in force, for `selftest` to report."""
        return {"allow": list(self.allow), "deny": list(self.deny),
                "allow_set": self.allow_set, "deny_set": self.deny_set,
                "source": self.source, "enforced": self.enforced}

    def allowed(self, action: str, classes: tuple[str, ...]) -> tuple[bool, str]:
        """May that action run under this policy? (yes, or why not)."""
        if not self.enforced:
            return True, ""
        if not classes:
            return False, (f"{action} is not in the declared surface (see "
                           "`selftest`), and an unclassified verb is refused")
        blocked = [name for name in classes if name in self.deny]
        if blocked:
            return False, (f"{action} is {'+'.join(classes)}, and "
                           f"{blocked[0]!r} is denied by {self.source}")
        if self.allow_set:
            # an ALLOW-SET with no class in it allows NOTHING: `if allow:`
            # would read that as "no allow-list", which is how an empty policy
            # turned the gate off (measured)
            missing = [name for name in classes if name not in self.allow]
            if missing:
                return False, (f"{action} is {'+'.join(classes)}, and "
                               f"{missing[0]!r} is not allowed by "
                               f"{self.source}")
        return True, ""
