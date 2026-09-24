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
served is simply not available. That includes a plugin that tries to END the
process while it is imported (`sys.exit`), or one interrupted with
`KeyboardInterrupt`: `import_module` catches `BaseException` and `PluginSet.load`
never raises, so a plugin can never choose this CLI's exit status, empty its
stdout, or skip its audit line. `PluginSet.load()` is the one loader;
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
    discovery,
)

from .spec import (
    PLUGIN_API,
    PluginAction,
    PluginError,
    PluginInfo,
    register,
)

__all__ = ["PLUGIN_API", "PLUGIN_ENV", "PluginAction", "PluginError",
           "PluginInfo", "PluginSet"]

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
        """Load every plugin under the search path. NEVER raises.

        `reserved` is the set of top-level verbs already taken (the CLI's own
        handlers); a plugin action with one of those names is refused and named.

        This is the plugin BOUNDARY, so the guarantee is unconditional: a
        plugin file is local code, and whatever it does at import — including
        `sys.exit(0)`, which used to make every invocation exit 0 with empty
        stdout and no audit line — comes back as a typed error in `errors`
        instead of an exception. `discovery.import_module` catches
        `BaseException` for the import itself, each file is guarded here as
        well, and the whole body is wrapped so even the loader cannot raise.
        A plugin directory named RELATIVELY is refused by `discovery` and
        reported here: it would import from whatever the current directory
        holds.
        """
        found = cls()
        try:
            found._load(reserved)
        except BaseException as e:                             # noqa: BLE001
            # belt and braces: `_load` guards every step, and this is the
            # promise its callers rely on — a set comes back, always
            found.errors.append(PluginError(
                "<plugin loader>",
                f"the loader itself failed: {type(e).__name__}: {e}",
                "import"))
        return found

    def _load(self, reserved: set[str] | None) -> None:
        """`load`'s body: fill this set from the search path, refusing per file."""
        for entry, why in discovery.refused():
            # a path we would not search is REPORTED, never silently dropped:
            # `selftest` is where the caller finds out it was skipped
            self.errors.append(PluginError(entry, why, "path"))
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
                self.errors.append(
                    PluginError(directory, f"cannot list: {e}", "import"))
                continue
            for name in names:
                if name.startswith("_") or not name.endswith(".py"):
                    continue
                path = os.path.join(directory, name)
                try:
                    module, why = discovery.import_module(path)
                    if module is None:
                        self.errors.append(PluginError(path, why, "import"))
                        continue
                    installed, info = register(module, path, self.errors, taken)
                except BaseException as e:                     # noqa: BLE001
                    # a hostile PLUGIN dict (an object whose iteration exits,
                    # a `classes` list that raises) is a refused plugin too
                    self.errors.append(PluginError(
                        path,
                        f"the plugin declaration failed: "
                        f"{type(e).__name__}: {e}", "schema"))
                    continue
                for action in installed:
                    self.actions[action.verb] = action
                if info is not None:
                    self.plugins.append(info)

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
