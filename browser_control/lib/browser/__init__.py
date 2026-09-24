"""browser — the package facade: every `browser.X` name callers already use.

The implementation is split by responsibility (`lifecycle`, `machine`,
`owners`, `readback`, `tabs`, `nav`), and this module re-exports the whole
surface — public names, the private helpers the suites call by name, and the
leaf re-exports (`norm`, `pid_file`, `lock`, ...) the rest of the tree grew up
importing from here — so no call site changes and no patch target moves.
Cross-module calls inside the package resolve through THIS namespace
(`_pkg.name`), which is what keeps a `browser.X = ...` monkeypatch
intercepting.
"""
from __future__ import annotations

from browser_control.lib import cdp  # noqa: F401
from browser_control.lib.browser.lifecycle import (  # noqa: F401
    BROWSER_BINS,
    DEFAULT_PROFILES,
    STOP_WAIT_S,
    _default_profile,
    _named_browser,
    _open_tabs,
    _opened_entry,
    _page_count,
    _scoped,
    _writable_profile,
    _writable_route,
    attach,
    attachments,
    binary,
    desktop_user_agent,
    detach,
    ensure_up,
    flags,
    instance_dir,
    launch,
    managed_profile,
    profiles,
    safe_url,
    scope,
    stop,
    ua_from_version,
)
from browser_control.lib.browser.machine import (  # noqa: F401
    BrowserRow,
    Endpoint,
    _closeable,
    _drivable,
    _endpoint_details,
    _foreign_row,
    _narrow,
    _readable,
    _row,
    _split,
    _tabs_or_fail,
    _who,
    _writable,
    brief,
    browser_info,
    browsers,
    census_processes,
    driver_port,
    endpoint_of,
    is_attached,
    list_browsers,
    list_tabs,
    may_write,
    profile_pid,
    row_port,
    tabs_of,
)
from browser_control.lib.browser.nav import (  # noqa: F401
    ACTIVATE_TIMEOUT_S,
    HISTORY_TIMEOUT_S,
    NAV_MOVE_S,
    NAV_TIMEOUT_S,
    READY_EXPR,
    RELOAD_TIMEOUT_S,
    _eval,
    _href,
    _no_drive,
    _ready,
    _same_page,
    _time_origin,
    _wait_document,
    _wait_move,
    _wait_new_document,
    _wait_url_change,
    activate,
    history,
    nav,
    reload_page,
)
from browser_control.lib.browser.owners import (  # noqa: F401
    GUARD,
    EndpointGuard,
    _await_owner,
    _drive_refusal,
    endpoint_owner,
    not_local_refusal,
    verify_profile_endpoint,
    verify_ws_owner,
)
from browser_control.lib.browser.readback import (  # noqa: F401
    LAUNCH_WAIT_S,
    PORT_WAIT_S,
    TAB_WAIT_S,
    _fallback_row,
    _host_of,
    _require_tab_list_readable,
    _same_site,
    _wait_ids_gone,
    _wait_own_port,
    _wait_rows,
    _wait_tabs,
    _wait_url,
    browser_call_at,
    browser_ws_at,
    page_eval,
    page_session,
    page_ws,
    rows,
    url_landed,
)
from browser_control.lib.browser.selector import Selector  # noqa: F401
from browser_control.lib.browser.tabs import (  # noqa: F401
    ACTIVE_SPEC,
    _close_and_verify,
    _exact_matches,
    _exact_spec_match,
    _match_spec,
    _resolve_close_set,
    _spec_hits,
    _spec_matches,
    _tab_count,
    _visible,
    close_tabs,
    new_tab,
    one_tab,
    resolve_across,
    resolve_tab,
    tab_info,
)
from browser_control.lib.locks import (  # noqa: F401
    LOCK_WAIT_S,
    LockState,
    profile_lock,
)
from browser_control.lib.paths import (  # noqa: F401
    expand,
    is_managed,
    lock_path,
    norm,
    pid_file,
    profile_dir,
    root,
)
from browser_control.lib.proc import (  # noqa: F401
    BROWSER_EXES,
    cmdline_value,
    is_headless_cmd,
    pid_alive,
    pid_of,
)

# The private spellings the rest of the tree (and the suites) grew up with;
# the implementations live in the submodules and the leaves.
_brief = brief
_one_tab = one_tab
_tabs_of = tabs_of
_rows = rows
_verify_profile_endpoint = verify_profile_endpoint
_resolve_across = resolve_across
lock = profile_lock
_lock = profile_lock
_norm = norm
_pid_file = pid_file
_pid_alive = pid_alive
_cmdline_value = cmdline_value
_pid_of = pid_of
