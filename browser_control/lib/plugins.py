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

`run(rest, browser)` gets the argv after the GLOBAL flags were stripped
(`--browser`, `--profile`, `--frame`, `--allow`, `--deny`) and the value of
`--browser`; it returns the same one-JSON-object reply every built-in handler
returns, or refuses with `errors.fail(...)`. A plugin action may use anything
in `browser_control.lib` — including `tab extract`, the generic extraction
engine this tier is built on.

Discovery, in order:

* each directory in ``BROWSER_CONTROL_PLUGIN_PATH`` (colon-separated); when
  that variable is SET it REPLACES the default below, so a test or a
deployment can point at exactly one place;
* otherwise ``~/.local/share/browser-control/plugins``, when it exists.

Only top-level ``*.py`` files are imported, in sorted order; a file whose name
starts with ``_`` is skipped, and the default directory is never created (an
install with no plugins has no side effects). Loading is FAIL-OPEN for the
CLI: a plugin that does not import, declares the wrong API, or collides with a
verb already taken is recorded in ``Registry.errors`` (reported by `selftest`)
and skipped — the CLI keeps working, and the action it would have served is
simply not available.

TRUST: a plugin is local code running in this process with the CLI's own
access. The capability classes it declares are DECLARATIONS (exactly like
`lib.capabilities` itself), not a sandbox. Install plugins the way you install
the tool.
"""
from __future__ import annotations

import importlib.util
import os
import re
from typing import Any

from browser_control.lib import capabilities  # noqa: E402
from browser_control.lib.paths import expand  # pyright: ignore[reportMissingImports]

PLUGIN_API = 1
PLUGIN_ENV = "BROWSER_CONTROL_PLUGIN_PATH"
DEFAULT_PLUGIN_DIR = "~/.local/share/browser-control/plugins"
_ACTION = re.compile(r"^[a-z][a-z0-9_-]*$")


class Registry:
    """What one load found: usable actions, plugin metadata, and load errors."""

    def __init__(self) -> None:
        self.actions: dict[str, dict] = {}
        self.plugins: list[dict] = []
        self.errors: list[str] = []

    def describe(self) -> list[dict]:
        return [dict(plugin) for plugin in self.plugins]

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")


def _dirs() -> list[str]:
    """Every directory a plugin may be loaded from, de-duplicated.

    `BROWSER_CONTROL_PLUGIN_PATH` is an OVERRIDE: when it is set, only its
    directories are searched. That keeps a test (or a deployment that wants a
    fixed set) from picking up whatever the user's plugin directory happens to
    contain.
    """
    raw = os.environ.get(PLUGIN_ENV, "")
    if raw.strip():
        out = [expand(part.strip())
               for part in raw.split(os.pathsep) if part.strip()]
        return list(dict.fromkeys(out))
    return [expand(DEFAULT_PLUGIN_DIR)]


def load(reserved: set[str] | None = None) -> Registry:
    """Load every plugin under the search path. Never raises.

    `reserved` is the set of top-level verbs already taken (the CLI's own
    handlers); a plugin action with one of those names is refused and named.
    """
    registry = Registry()
    taken = set(reserved or ())
    for directory in _dirs():
        if not os.path.isdir(directory):
            continue
        try:
            names = sorted(os.listdir(directory))
        except OSError as e:
            # a plugin directory that cannot be read is REPORTED, not fatal:
            # loading is fail-open, and `selftest` is where the caller finds
            # out that something on the search path did not load.
            registry.error(directory, f"cannot list: {e}")
            continue
        for name in names:
            if name.startswith("_") or not name.endswith(".py"):
                continue
            path = os.path.join(directory, name)
            module = _import(path, registry)
            if module is not None:
                _register(module, path, registry, taken)
    return registry


def _import(path: str, registry: Registry) -> Any | None:
    stem = os.path.basename(path)[:-3]
    module_name = "browser_control_plugin_" + re.sub(r"[^A-Za-z0-9_]",
                                                     "_", stem)
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            registry.error(path, "cannot be loaded as a Python module")
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:                                     # noqa: BLE001
        registry.error(path, f"import failed: {type(e).__name__}: {e}")
        return None
    return module


def _register(module: Any, path: str, registry: Registry,
              taken: set[str]) -> None:
    plugin = getattr(module, "PLUGIN", None)
    if not isinstance(plugin, dict):
        registry.error(path, "declares no PLUGIN dict")
        return
    if plugin.get("api") != PLUGIN_API:
        registry.error(path,
                       f"PLUGIN api {plugin.get('api')!r} != {PLUGIN_API}")
        return
    name = str(plugin.get("name") or "").strip()
    if not name:
        registry.error(path, "PLUGIN has no name")
        return
    actions = plugin.get("actions")
    if not isinstance(actions, dict) or not actions:
        registry.error(path, f"{name}: declares no actions")
        return
    installed: list[str] = []
    for verb, spec in sorted(actions.items()):
        verb = str(verb)
        if not _ACTION.match(verb):
            registry.error(path,
                           f"{name}: {verb!r} is not a verb name (lowercase "
                           "letters, digits, `_`, `-`)")
            continue
        if verb in taken:
            registry.error(path, f"{name}: the verb {verb!r} is already taken "
                                 "— skipped")
            continue
        if not isinstance(spec, dict) or not callable(spec.get("run")):
            registry.error(path,
                           f"{name}: {verb!r} has no run(rest, browser)")
            continue
        classes = tuple(str(c) for c in (spec.get("classes") or ()))
        unknown = [c for c in classes if c not in capabilities.CLASSES]
        if not classes or unknown:
            registry.error(
                path,
                f"{name}: {verb!r} declares classes {classes!r}; every action "
                "needs at least one of " + ", ".join(capabilities.CLASSES))
            continue
        taken.add(verb)
        registry.actions[verb] = {
            "run": spec["run"], "classes": classes,
            "usage": str(spec.get("usage") or verb),
            "plugin": name, "path": path,
        }
        installed.append(verb)
    if installed:
        registry.plugins.append({
            "name": name,
            "path": path,
            "description": str(plugin.get("description") or ""),
            "verbs": installed,
        })
