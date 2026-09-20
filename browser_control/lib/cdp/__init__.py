"""cdp — the package facade: every `cdp.X` name callers already use.

The implementation is split by responsibility (`endpoint`, `targets`, `rpc`,
`session`), and this module re-exports the whole surface — public names, the
constants (`PORT_FILE`, `websockets`) and the private helpers the suites call
by name — so no call site changes and no patch target moves.

`listener_of` is the kernel's answer to "who owns that port"; it lives in
`lib/proc.py` and is re-exported here because `browser.py` and the checks
call `cdp.listener_of`.
"""
from __future__ import annotations

from browser_control.lib.cdp.endpoint import (  # noqa: F401
    GET_CAP,
    GET_DEADLINE_S,
    PORT_FILE,
    _get_bytes,
    _get_port,
    _LoopbackOnly,
    answers,
    get_json,
    port_of,
    reachable,
    version_at,
)
from browser_control.lib.cdp.rpc import (  # noqa: F401
    BLOCKED_HINT,
    DIALOG_EVENT,
    DIALOG_GRACE_S,
    EVAL_RESULT_CAP,
    PAGE_ENABLE_S,
    PARKED_BUDGET_S,
    _call,
    _evaluate,
    _evaluate_until,
    _page_enable,
    _sample,
    _value_of,
    call,
    evaluate,
    evaluate_until,
    websockets,
)
from browser_control.lib.cdp.session import Session  # noqa: F401
from browser_control.lib.cdp.targets import (  # noqa: F401
    _checked_ws,
    _of_kind,
    _pages,
    browser_call,
    browser_ws,
    frame_rows,
    frame_targets,
    page_rows,
    page_rows_at,
    rows_to_tabs,
    target_ws,
)
from browser_control.lib.proc import listener_of  # noqa: F401
