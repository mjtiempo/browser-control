"""plugin_api — the named seam between plugins and the core verbs.

A plugin is local code running in this process with the CLI's own access; what it
should reach for is THIS module, not `browser_control.lib` at large. The names
here are the supported surface (`api: 1`): the refusal type and its code
vocabulary, the verbs a READ-ONLY plugin needs (`nav`, `wait`, `extract`), and
the five a plugin that drives a page the way a person does needs — `focus`,
`type_text` (real per-character key events, with a `delay_s` for a human
cadence), `press`, `click`, and `scroll` (one real wheel event — how a lazy or
virtualized list is made to render past its first window). A context object
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
names the verb in its refusal the same way the CLI does. `text_arg`,
`int_arg` and `float_arg` are the positional and numeric readers beside them,
so "one TEXT at most", "TEXT is required" and "needs a number" are the same
refusals a built-in verb gives.

Everything is re-exported from where it already lived, so nothing moves and no
plugin that imports `lib` directly breaks. The readers come from
`browser_control.lib.argv`, below the CLI adapter: this seam does not import
`browser_control.cli`.
"""
from __future__ import annotations

from browser_control.lib import errors
from browser_control.lib.argv import (
    _float as float_arg,
    _int as int_arg,
    _pop as pop,
    _switch as switch,
    _text_arg as text_arg,
)
from browser_control.lib.browser import nav
from browser_control.lib.dom import (
    click,
    extract,
    focus,
    press,
    scroll,
    type_text,
    wait,
)
from browser_control.lib.errors import ControlError, fail
from browser_control.lib.plugins import PLUGIN_API

__all__ = ["ControlError", "PLUGIN_API", "click", "errors", "extract",
           "fail", "float_arg", "focus", "int_arg", "nav", "pop", "press",
           "scroll", "switch", "text_arg", "type_text", "wait"]
