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
enforcement belongs to `lib/policy.py`, which reads this table.
"""
from __future__ import annotations

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
    "profile logins": ("read",),
    "profile seed": ("write", "file"),
    "profile reset": ("write",),
}


class Surface:
    """The declared verb surface for ONE invocation.

    The built-in table is fixed; the plugin-declared actions arrive when the
    plugins load, and are REPLACED (not added to) so a process that loads
    plugins for one call cannot leave a ghost action behind for the next.
    """

    def __init__(self) -> None:
        self.plugin_actions: dict[str, tuple[str, ...]] = {}

    def set_plugins(self, actions: dict[str, tuple[str, ...]]) -> None:
        """Replace the plugin-declared actions."""
        self.plugin_actions = {action: tuple(classes)
                               for action, classes in actions.items()}

    def classes_for(self, action: str) -> tuple[str, ...]:
        """The classes an action holds — built-in or plugin-declared."""
        return ACTIONS.get(action) or self.plugin_actions.get(action) or ()

    def unclassified(self, handlers: dict,
                     subcommands: dict[str, dict] | None = None) -> list[str]:
        """The verbs with NO entry — a top-level verb, or a subcommand.

        `subcommands` maps a noun to its subcommand table, so one call covers
        every noun the CLI has:

            unclassified(HANDLERS, {"tab": TAB_SUBCOMMANDS,
                                   "profile": PROFILE_SUBCOMMANDS})

        Asked at runtime (`selftest` prints the answer) and by the hermetic
        test, from this one function, so a new verb cannot exist in one place
        and be missing in the other without both saying so. Plugin verbs are
        NOT handlers, so they are not part of this question.
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

    def by_class(self) -> dict[str, list[str]]:
        """The surface the other way round: which actions hold each class.

        Plugin-declared actions are included: a caller asking "what can run
        under `--deny code`" should see them too.
        """
        grouped: dict[str, list[str]] = {name: [] for name in CLASSES}
        for source in (ACTIONS, self.plugin_actions):
            for action, classes in sorted(source.items()):
                for name in classes:
                    grouped.setdefault(name, []).append(action)
        return grouped


#: The process-wide surface the CLI drives: `main` replaces the plugin side
#: once per invocation. `lib/policy.py` is the gate that reads it.
SURFACE = Surface()


def set_plugins(actions: dict[str, tuple[str, ...]]) -> None:
    """Replace the plugin-declared actions on the process-wide surface."""
    SURFACE.set_plugins(actions)


def classes_for(action: str) -> tuple[str, ...]:
    """The classes an action holds, from the process-wide surface."""
    return SURFACE.classes_for(action)


def unclassified(handlers: dict,
                 subcommands: dict[str, dict] | None = None) -> list[str]:
    """`Surface.unclassified`, on the process-wide surface."""
    return SURFACE.unclassified(handlers, subcommands)


def by_class() -> dict[str, list[str]]:
    """`Surface.by_class`, on the process-wide surface."""
    return SURFACE.by_class()
