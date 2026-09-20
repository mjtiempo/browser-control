"""discovery — where plugins live, and how one file is imported.

`BROWSER_CONTROL_PLUGIN_PATH` is an OVERRIDE: when it is set, only its
directories are searched, so a test (or a deployment that wants a fixed set)
does not pick up whatever the user's plugin directory happens to contain.
"""
from __future__ import annotations

import importlib.util
import os
import re
from typing import Any

from browser_control.lib.paths import expand  # pyright: ignore[reportMissingImports]

PLUGIN_ENV = "BROWSER_CONTROL_PLUGIN_PATH"
DEFAULT_PLUGIN_DIR = "~/.local/share/browser-control/plugins"

__all__ = ["DEFAULT_PLUGIN_DIR", "PLUGIN_ENV", "dirs", "import_module"]


def dirs() -> list[str]:
    """Every directory a plugin may be loaded from, de-duplicated."""
    raw = os.environ.get(PLUGIN_ENV, "")
    if raw.strip():
        out = [expand(part.strip())
               for part in raw.split(os.pathsep) if part.strip()]
        return list(dict.fromkeys(out))
    return [expand(DEFAULT_PLUGIN_DIR)]


def import_module(path: str) -> tuple[Any | None, str]:
    """Import one plugin file. `(module, "")` or `(None, reason)` — never raises."""
    stem = os.path.basename(path)[:-3]
    module_name = "browser_control_plugin_" + re.sub(r"[^A-Za-z0-9_]",
                                                     "_", stem)
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None, "cannot be loaded as a Python module"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:                                     # noqa: BLE001
        return None, f"import failed: {type(e).__name__}: {e}"
    return module, ""
