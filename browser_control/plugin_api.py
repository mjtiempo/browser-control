"""plugin_api — the named seam between plugins and the core verbs.

A plugin is local code running in this process with the CLI's own access; what it
should reach for is THIS module, not `browser_control.lib` at large. The names
here are the supported surface (`api: 1`): the refusal type and its code
vocabulary, and the three verbs a read-only plugin needs. A context object
carrying tab/browser/frame is a separate `api: 2` proposal — the
`run(rest, browser)` signature does not change here.

`errors` is re-exported WHOLE so a plugin names a refusal code
(`errors.ERR_BAD_ARGS`) instead of typing the string: a raw string that is not
registered ships a code no caller can branch on, which the hermetic vocabulary
check refuses — and since plugins are scanned too, a plugin's typo fails the
suite rather than a user's call.

`pop`/`switch` are the argv readers the core's own verbs use — `--flag VALUE`
and `--flag=VALUE`, and the value-less flag — so a plugin parses its own
arguments the way the built-ins do instead of hand-rolling the loop. `pop`
names the verb in its refusal the same way the CLI does.

Everything is re-exported from where it already lived, so nothing moves and no
plugin that imports `lib` directly breaks.
"""
from __future__ import annotations

from browser_control.cli.argv import (
    _pop as pop,
    _switch as switch,
)
from browser_control.lib import errors
from browser_control.lib.browser import nav
from browser_control.lib.dom import extract, wait
from browser_control.lib.errors import ControlError, fail
from browser_control.lib.plugins import PLUGIN_API

__all__ = ["ControlError", "PLUGIN_API", "errors", "extract", "fail", "nav",
           "pop", "switch", "wait"]
