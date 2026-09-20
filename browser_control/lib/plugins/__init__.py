"""plugins — site actions that ride on the core's generic verbs.

The core knows how to open a browser, drive a tab, and read the page; it does
not know what any SITE means. A plugin is a Python file that teaches it one:
which URL, which selectors, what to call the fields — and which capability
classes the action holds, so the gate can still refuse it.

    PLUGIN = {
        "api": 1,
        "name": "x-reader",
        "description": "read-only X (Twitter) search results",
        "actions": {
            "x": {
                "run": run,                 # (rest, browser) -> reply dict
                "classes": ("read",),
                "usage": "x search QUERY [--latest] [--cap N]",
            },
        },
    }

`run(rest, browser)` gets the argv after the GLOBAL flags were stripped and the
value of `--browser`; it returns the same one-JSON-object reply every built-in
handler returns, or refuses with `errors.fail(...)`. The supported seam is
`browser_control.plugin_api` (api: 1).

Loading is FAIL-OPEN for the CLI: a plugin that does not import, declares the
wrong API, or collides with a verb already taken is recorded as a typed
`PluginError` and skipped — the CLI keeps working, and the action it would have
served is simply not available. `PluginSet.load()` is the one loader;
`lib/plugins/discovery.py` owns the search path and `lib/plugins/spec.py` owns
validation.

TRUST: a plugin is local code running in this process with the CLI's own access.
The capability classes it declares are DECLARATIONS (exactly like
`lib.capabilities` itself), not a sandbox. Install plugins the way you install
the tool.
"""
from __future__ import annotations

import os

from browser_control.lib.plugins import (
    discovery,  # pyright: ignore[reportMissingImports]
)

from .spec import (
    PLUGIN_API,
    PluginAction,
    PluginError,
    PluginInfo,
    register,
)

__all__ = ["PLUGIN_API", "PLUGIN_ENV", "PluginAction", "PluginError",
           "PluginInfo", "PluginSet", "Registry", "load"]

PLUGIN_ENV = discovery.PLUGIN_ENV
DEFAULT_PLUGIN_DIR = discovery.DEFAULT_PLUGIN_DIR


class PluginSet:
    """What one load found: usable actions, plugin metadata, and typed errors."""

    def __init__(self) -> None:
        self.actions: dict[str, PluginAction] = {}
        self.plugins: list[PluginInfo] = []
        self.errors: list[PluginError] = []

    @classmethod
    def load(cls, reserved: set[str] | None = None) -> PluginSet:
        """Load every plugin under the search path. Never raises.

        `reserved` is the set of top-level verbs already taken (the CLI's own
        handlers); a plugin action with one of those names is refused and named.
        """
        found = cls()
        taken = set(reserved or ())
        for directory in discovery.dirs():
            if not os.path.isdir(directory):
                continue
            try:
                names = sorted(os.listdir(directory))
            except OSError as e:
                # a plugin directory that cannot be read is REPORTED, not fatal:
                # loading is fail-open, and `selftest` is where the caller finds
                # out that something on the search path did not load.
                found.errors.append(
                    PluginError(directory, f"cannot list: {e}", "import"))
                continue
            for name in names:
                if name.startswith("_") or not name.endswith(".py"):
                    continue
                path = os.path.join(directory, name)
                module, why = discovery.import_module(path)
                if module is None:
                    found.errors.append(PluginError(path, why, "import"))
                    continue
                installed, info = register(module, path, found.errors, taken)
                for action in installed:
                    found.actions[action.verb] = action
                if info is not None:
                    found.plugins.append(info)
        return found

    def reset(self, other: PluginSet) -> None:
        """Become `other` — the CLI's holder for one invocation's plugins.

        Attribute mutation, not rebinding, so `main` needs no `global`.
        """
        self.actions = other.actions
        self.plugins = other.plugins
        self.errors = other.errors

    def classes(self) -> dict[str, tuple[str, ...]]:
        """The capability classes of every installed action, by verb."""
        return {verb: action.classes for verb, action in self.actions.items()}

    def verb_names(self) -> list[str]:
        """Every installed plugin verb."""
        return list(self.actions)

    def usages(self) -> list[str]:
        """The usage line of every installed action."""
        return [action.usage for action in self.actions.values()]

    def describe(self) -> list[dict]:
        """The plugin metadata `selftest` reports (shape unchanged)."""
        return [plugin.as_dict() for plugin in self.plugins]

    def report(self) -> dict:
        """`selftest`'s `plugins`/`plugin_errors` keys, ready for JSON."""
        return {"plugins": self.describe(),
                "plugin_errors": [str(e) for e in self.errors]}


#: The old name, for one release: the class grew a construction policy but the
#: shape is the same.
Registry = PluginSet


def load(reserved: set[str] | None = None) -> PluginSet:
    """`PluginSet.load`, kept as a module function for existing callers."""
    return PluginSet.load(reserved)
