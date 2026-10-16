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
process from PYTHON while it is imported (`sys.exit(0)`, which used to make
every invocation exit 0 with empty stdout), or one interrupted with
`KeyboardInterrupt`: `import_module` catches `BaseException` and `PluginSet.load`
never raises. A plugin also cannot write AHEAD of the reply any more: the import
and the handler both run with stdout diverted (`captured_stdout`), and anything
they printed is reported instead — as a typed `stdout` error naming the plugin,
or as a `warning` on the reply.

Where that boundary ENDS, said out loud rather than implied: `os._exit()` and a
raw `os.write(1, …)` go around the interpreter, so NO in-process loader can
contain them — a plugin that calls `os._exit(7)` still chooses this process's
exit status, and one that writes to fd 1 directly still reaches the caller's
stdout. A plugin is not sandboxed (see TRUST below); these guards keep an
ORDINARY plugin's mistakes — a stray `print`, a `sys.exit` in a helper — from
breaking the CLI's contract, and nothing more. `PluginSet.load()` is the one
loader; `lib/plugins/discovery.py` owns the search path and `lib/plugins/spec.py`
owns validation.

TRUST: a plugin is local code running in this process with the CLI's own access.
The capability classes it declares are DECLARATIONS (exactly like
`lib.capabilities` itself), not a sandbox. Install plugins the way you install
the tool.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys

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
           "PluginInfo", "PluginSet", "captured_stdout"]

PLUGIN_ENV = discovery.PLUGIN_ENV
DEFAULT_PLUGIN_DIR = discovery.DEFAULT_PLUGIN_DIR


@contextlib.contextmanager
def captured_stdout():  # noqa: ANN201
    """Run plugin code with its `print`s diverted, and answer what it said.

    A plugin is local code, and local code prints: one stray `print()` — or a
    library the plugin imports that prints — put an extra line on stdout AHEAD
    of the reply, so `list` answered two lines, the first not JSON, and every
    JSON consumer of a SUCCESSFUL invocation broke with no refusal to branch on
    (a review measured it). stdout IS the reply, so plugin code runs with
    `sys.stdout` pointed at a buffer; the caller reads what landed there and
    decides — the loader records it as a typed error so `selftest` names the
    plugin, and `cli.main` carries it as a `warning` on the reply.

    What this reaches: `print`, and anything writing through `sys.stdout`. What
    it does NOT reach: a raw `os.write(1, …)`, and `os._exit`, which ends the
    process below the interpreter. The module docstring's TRUST note says how
    far this boundary goes rather than implying it is airtight.
    """
    real = sys.stdout
    buffer = io.StringIO()
    sys.stdout = buffer
    try:
        yield buffer
    finally:
        sys.stdout = real


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
                installed: list[PluginAction] = []
                info: PluginInfo | None = None
                try:
                    # the IMPORT and the declaration both run with stdout
                    # diverted: a plugin that prints while it loads would
                    # otherwise write ahead of the reply on the one stream the
                    # contract reserves for it (see `captured_stdout`)
                    chatter = ""
                    with captured_stdout() as said:
                        module, why = discovery.import_module(path)
                        if module is not None:
                            installed, info = register(module, path,
                                                       self.errors, taken)
                        chatter = said.getvalue()
                except BaseException as e:                     # noqa: BLE001
                    # a hostile PLUGIN dict (an object whose iteration exits,
                    # a `classes` list that raises) is a refused plugin too
                    self.errors.append(PluginError(
                        path,
                        f"the plugin declaration failed: "
                        f"{type(e).__name__}: {e}", "schema"))
                    continue
                if module is None:
                    self.errors.append(PluginError(path, why, "import"))
                    continue
                if chatter:
                    # reported, never fatal: the output is already discarded,
                    # and the CLI keeps working — but `selftest` names the
                    # plugin that did it instead of hiding the habit
                    self.errors.append(PluginError(
                        path,
                        f"{len(chatter)} character(s) were written to stdout "
                        "while this plugin loaded — stdout IS this CLI's "
                        "reply, so they were discarded; a plugin's own output "
                        "belongs on stderr or in the action log", "stdout"))
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
