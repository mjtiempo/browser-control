"""plugin_api — the named seam between plugins and the core verbs.

A plugin is local code running in this process with the CLI's own access; what it
should reach for is THIS module, not `browser_control.lib` at large. The names
here are the supported surface (`api: 1`): the refusal type, and the three verbs a
read-only plugin needs. A context object carrying tab/browser/frame is a separate
`api: 2` proposal — the `run(rest, browser)` signature does not change here.

Everything is re-exported from where it already lived, so nothing moves and no
plugin that imports `lib` directly breaks.
"""
from __future__ import annotations

from browser_control.lib.browser import nav
from browser_control.lib.dom import extract, wait
from browser_control.lib.errors import ControlError, fail
from browser_control.lib.plugins import PLUGIN_API

__all__ = ["ControlError", "PLUGIN_API", "extract", "fail", "nav", "wait"]
