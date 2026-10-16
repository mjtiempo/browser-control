"""discovery — where plugins live, and how one file is imported.

`BROWSER_CONTROL_PLUGIN_PATH` is an OVERRIDE: when it is set, only its
directories are searched, so a test (or a deployment that wants a fixed set)
does not pick up whatever the user's plugin directory happens to contain. SET
and UNSET are the two states — a variable set to "" searches NO directory
rather than falling back to the default one, which is what makes "lock plugins
out" a thing a deployment can actually ask for (`_entries` has the reasoning).

A RELATIVE entry is refused, never resolved against the current directory. A
plugin is local code running in this process with the CLI's own credentials and
its CDP access, and an entry like `plugins` in a shell rc would import whatever
`./plugins/*.py` the directory you happen to be standing in holds — executing an
untrusted tree's code on every invocation, before the policy gate exists. So
`dirs()` returns only entries that name an absolute path (or start with `~`),
and `refused()` reports the rest with the reason, which the plugin set carries
as typed errors (`PluginSet.load`). An entry that names NO path at all is
reported the same way, so nothing on the search path is dropped in silence.

`import_module` is the OTHER boundary: it catches `BaseException`, because a
plugin's `sys.exit` or `KeyboardInterrupt` at import is exactly what it exists
for (see its docstring).
"""
from __future__ import annotations

import importlib.util
import os
import re
from typing import Any

from browser_control.lib.paths import expand

PLUGIN_ENV = "BROWSER_CONTROL_PLUGIN_PATH"
DEFAULT_PLUGIN_DIR = "~/.local/share/browser-control/plugins"

__all__ = ["DEFAULT_PLUGIN_DIR", "PLUGIN_ENV", "dirs", "import_module",
           "refused"]


def _entries() -> list[tuple[str, str]]:
    """Every entry the environment names: `(path, why)` — `why` "" = usable.

    One parse for `dirs()` and `refused()`, so the rule is spelled once and
    the two readers cannot disagree about which entries were searched.

    UNSET and EMPTY are different answers, and the module docstring's promise
    ("when it is set, only its directories are searched") needs the difference:
    `os.environ.get(PLUGIN_ENV, "")` read a variable set to "" as unset, so a
    deployment that locked plugins out with an empty value got the user's own
    `~/.local/share/browser-control/plugins` imported instead — the opposite of
    what it asked for, and the asymmetry this tree refuses everywhere else (a
    blank `BROWSER_CONTROL_ALLOW`/`--allow` is a refusal precisely because a
    blank value used to read as "no policy"). An entry that names NO directory
    (`""`, or a stray `:` separator) is REPORTED rather than skipped in
    silence, like every other entry that is not searched.
    """
    if PLUGIN_ENV not in os.environ:
        return [(expand(DEFAULT_PLUGIN_DIR), "")]
    out: list[tuple[str, str]] = []
    for part in os.environ[PLUGIN_ENV].split(os.pathsep):
        text = part.strip()
        if not text:
            out.append((part, (f"{PLUGIN_ENV} names no directory in this "
                               "entry — an empty entry is not a path; unset "
                               "the variable to search the default directory, "
                               "or name an absolute path")))
            continue
        if text.startswith("~") or os.path.isabs(text):
            out.append((expand(text), ""))
            continue
        out.append((text, _relative_reason(text)))
    return out


def _relative_reason(entry: str) -> str:
    """Why a relative plugin entry was skipped, naming the path it would mean."""
    try:
        would_be = os.path.join(os.getcwd(), entry)
    except OSError:                 # a current directory that is gone
        would_be = entry
    return (f"a RELATIVE plugin directory is refused — {entry!r} would import "
            f"from {would_be!r}, whatever the current directory happens to "
            "hold, with this CLI's own credentials and its CDP access; name "
            "an absolute path or `~/…`")


def dirs() -> list[str]:
    """Every directory a plugin may be loaded from, de-duplicated.

    Only absolute (or `~`-rooted) entries are here: see the module docstring.
    `refused()` names what this skipped and why; nothing is dropped silently.
    """
    return list(dict.fromkeys(path for path, why in _entries() if not why))


def refused() -> list[tuple[str, str]]:
    """The entries `dirs()` skipped: `(entry, why)`, for the plugin set's errors."""
    return [(entry, why) for entry, why in _entries() if why]


def import_module(path: str) -> tuple[Any | None, str]:
    """Import one plugin file. `(module, "")` or `(None, reason)` — never raises.

    `BaseException`, not `Exception`: this is the boundary a plugin cannot cross
    by RAISING. A plugin that calls `sys.exit(0)` while it is imported used to
    take the WHOLE invocation down — every call exited 0 with empty stdout and
    no audit line, indistinguishable from success to a JSON consumer — and a
    `KeyboardInterrupt` at import exited 130 with a traceback. Swallowing even
    THAT is deliberate: the plugin is refused and named here (`import failed:
    SystemExit: 0`), and this CLI's own exit status and audit line stay its own.

    What it cannot swallow, and does not claim to: `os._exit()`, which ends the
    process below the interpreter. stdout is a separate question and is handled
    by the CALLER (`plugins.captured_stdout` diverts it around this call), so a
    plugin that prints does not put a second line ahead of the reply.
    """
    stem = os.path.basename(path)[:-3]
    module_name = "browser_control_plugin_" + re.sub(r"[^A-Za-z0-9_]",
                                                     "_", stem)
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None, "cannot be loaded as a Python module"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except BaseException as e:                                 # noqa: BLE001
        return None, f"import failed: {type(e).__name__}: {e}"
    return module, ""
