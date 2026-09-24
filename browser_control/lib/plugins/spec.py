"""spec — the `PLUGIN` dict, validated into typed records.

Validation reports a KIND with every error (`api`, `collision`, `import`, ...),
so a report consumer can tell a bad API version from a verb collision without
parsing prose. The rule that matters stays: a plugin action is refused unless it
declares at least one capability class the gate knows.

An action may also declare `"frames": True`: it acts inside the `--frame` scope
the CLI sets for the invocation, which the CLI refuses for every verb that does
not declare it (`cli.main._authorise`). The declaration is the plugin's own,
like its classes — nothing here verifies that the action really reads inside a
frame — but it IS validated like every other field: the value has to be a real
boolean, because `bool()` read `"frames": "false"` (any truthy non-bool,
including the string `"false"`) as an ENABLED scope, the opposite of what the
author declared (a review found it). A declaration that is not a boolean is
refused with `kind="schema"`, so the action never reaches the surface.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from browser_control.lib import capabilities

PLUGIN_API = 1
_ACTION = re.compile(r"^[a-z][a-z0-9_-]*$")

__all__ = ["PLUGIN_API", "PluginAction", "PluginError", "PluginInfo", "register"]


@dataclass(frozen=True)
class PluginAction:
    """One verb a plugin installed."""

    verb: str
    run: Callable
    classes: tuple[str, ...]
    usage: str
    plugin: str
    path: str
    #: does this action act inside the caller's global `--frame` scope?
    #: declared as a real boolean (`"frames": True`), never a truthy string
    frames: bool = False


@dataclass(frozen=True)
class PluginInfo:
    """One plugin that installed at least one action."""

    name: str
    path: str
    description: str
    verbs: tuple[str, ...]

    def as_dict(self) -> dict:
        """The shape `selftest` reports (unchanged)."""
        return {"name": self.name, "path": self.path,
                "description": self.description, "verbs": list(self.verbs)}


@dataclass(frozen=True)
class PluginError:
    """One refused plugin or action, with the kind of refusal."""

    path: str
    message: str
    kind: str = "schema"

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def register(module: object, path: str, errors: list[PluginError],
             taken: set[str]) -> tuple[list[PluginAction], PluginInfo | None]:
    """Validate one imported module's `PLUGIN` dict. Never raises."""
    plugin = getattr(module, "PLUGIN", None)
    if not isinstance(plugin, dict):
        errors.append(PluginError(path, "declares no PLUGIN dict", "schema"))
        return [], None
    if plugin.get("api") != PLUGIN_API:
        errors.append(PluginError(
            path, f"PLUGIN api {plugin.get('api')!r} != {PLUGIN_API}", "api"))
        return [], None
    name = str(plugin.get("name") or "").strip()
    if not name:
        errors.append(PluginError(path, "PLUGIN has no name", "schema"))
        return [], None
    actions = plugin.get("actions")
    if not isinstance(actions, dict) or not actions:
        errors.append(PluginError(path, f"{name}: declares no actions", "schema"))
        return [], None
    installed: list[PluginAction] = []
    for verb, spec in sorted(actions.items()):
        verb = str(verb)
        if not _ACTION.match(verb):
            errors.append(PluginError(
                path, f"{name}: {verb!r} is not a verb name (lowercase "
                      "letters, digits, `_`, `-`)", "verb-name"))
            continue
        if verb in taken:
            errors.append(PluginError(
                path, f"{name}: the verb {verb!r} is already taken — skipped",
                "collision"))
            continue
        if not isinstance(spec, dict) or not callable(spec.get("run")):
            errors.append(PluginError(
                path, f"{name}: {verb!r} has no run(rest, browser)", "run"))
            continue
        classes = tuple(str(c) for c in (spec.get("classes") or ()))
        unknown = [c for c in classes if c not in capabilities.CLASSES]
        if not classes or unknown:
            errors.append(PluginError(
                path, f"{name}: {verb!r} declares classes {classes!r}; every "
                      "action needs at least one of "
                      + ", ".join(capabilities.CLASSES), "classes"))
            continue
        # `frames` opts the action into the caller's `--frame` scope, so it is
        # read as a BOOLEAN and nothing else: `bool(spec.get("frames"))` turned
        # `"frames": "false"` — and every other truthy non-bool — into an
        # ENABLED scope, the opposite of the author's declaration (a review
        # found it). A declaration that is not a real boolean is a schema
        # refusal, reported like every other malformed field, and the action is
        # skipped BEFORE its verb name is claimed.
        frames = spec.get("frames", False)
        if not isinstance(frames, bool):
            errors.append(PluginError(
                path, f"{name}: {verb!r} declares frames {frames!r}; the key "
                      "is a real boolean (`true`/`false`) — it opts the "
                      "action into the caller's --frame scope", "schema"))
            continue
        taken.add(verb)
        installed.append(PluginAction(
            verb=verb, run=spec["run"], classes=classes,
            usage=str(spec.get("usage") or verb), plugin=name, path=path,
            frames=frames))
    info = None
    if installed:
        info = PluginInfo(name=name, path=path,
                          description=str(plugin.get("description") or ""),
                          verbs=tuple(a.verb for a in installed))
    return installed, info
