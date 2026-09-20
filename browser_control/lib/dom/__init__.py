"""dom — the package facade: every `dom.X` name callers already use.

The implementation is split by responsibility (`scripts`, `pagedata`,
`frames`, `tab`, `queries`, `actions`, `scroll`, `text_input`, `keys`,
`media`, `dialog`, `screenshot`), and this module re-exports the whole
surface — the 18 verbs, the constants the CLI reads, and the private helpers
the suites call or patch by name — so no call site changes and no patch
target moves. Cross-module calls inside the package resolve through THIS
namespace (`_pkg.name`), which is what keeps a `dom.X = ...` monkeypatch
intercepting.
"""
from __future__ import annotations

from browser_control.lib.coerce import (  # noqa: F401
    as_float,
    as_int,
    as_ints,
    as_list,
)
from browser_control.lib.dom.actions import (  # noqa: F401
    CHECK_TIMEOUT_S,
    _at_point_click,
    _check_state,
    _checkable,
    _click_at,
    _hover_at,
    _select_probe,
    _under_point,
    check,
    click,
    focus,
    hover,
    select,
    upload,
)
from browser_control.lib.dom.dialog import (  # noqa: F401
    DIALOG_CLEAR_S,
    DIALOG_PROBE_S,
    NO_DIALOG_MARK,
    NO_DIALOG_NOTE,
    _no_dialog,
    dialog,
)
from browser_control.lib.dom.frames import (  # noqa: F401
    FRAME_VERBS,
    _frame_summary,
    _frame_target,
    _frames_note,
    frame,
    frame_resolved,
    frames,
    frames_of,
    frames_of_rows,
)
from browser_control.lib.dom.keys import (  # noqa: F401
    KEYS,
)
from browser_control.lib.dom.media import (  # noqa: F401
    MEDIA_TIMEOUT_S,
    _media_reply,
    _playback_verdict,
    _poll_media,
    media,
)
from browser_control.lib.dom.pagedata import (  # noqa: F401
    _ATTR_NAME,
    _FIELD_NAME,
    _describe,
    _element,
    _viewport,
    _well_formed,
)
from browser_control.lib.dom.queries import (  # noqa: F401
    EVAL_TIMEOUT_S,
    EXTRACT_CAP,
    EXTRACT_FIELD_CHARS,
    EXTRACT_FIELD_MAX,
    EXTRACT_MAX_MATCHES,
    EXTRACT_TOTAL_CHARS,
    FIND_CAP,
    IDLE_DEFAULT_MS,
    TEXT_CAP,
    WAIT_DEFAULT_S,
    WAIT_POLL_S,
    _extract_field,
    _extract_records,
    _extract_schema,
    _match_args,
    _matches_in,
    _node_of,
    _pick,
    _query_args,
    extract,
    find,
    js,
    text,
    wait,
)
from browser_control.lib.dom.result import (  # noqa: F401
    PageState,
    Verdict,
)
from browser_control.lib.dom.screenshot import (  # noqa: F401
    screenshot,
)
from browser_control.lib.dom.scripts import (  # noqa: F401
    CANDIDATES_EXPR,
    CHECK_READ,
    DIALOG_AWAKE,
    ELEMENT_EXPR,
    EXTRACT_EXPR,
    FILES_EXPR,
    FIND_EXPR,
    FOCUS_PROBE,
    FRAME_CENSUS,
    HOVER_PROBE,
    MEDIA_ACTION_EXPR,
    MEDIA_STATE_EXPR,
    POINT_HOVER_PROBE,
    POINT_PROBE,
    PRELUDE,
    SCROLL_PROBE,
    SELECT_PROBE,
    SHOT_METRICS,
    STATE_EXPR,
    TEXT_EXPR,
    TEXT_TARGET_EXPR,
    UPLOAD_DEFAULT_SELECTOR,
    WAIT_EXPRS,
    fill,
)
from browser_control.lib.dom.scroll import (  # noqa: F401
    SCROLL_EDGE_STEP,
    SCROLL_EDGE_STEPS,
    SCROLL_MOVE_S,
    _at_point,
    _point,
    _probe,
    _reveal,
    _settle,
    _wheel,
    scroll,
)
from browser_control.lib.dom.tab import (  # noqa: F401
    _document_ws,
    _reply,
    _resolve,
    _session,
    _with_frame,
    mode_of,
    page_session,
)
from browser_control.lib.dom.text_input import (  # noqa: F401
    TYPE_PAUSE_S,
    _is_secret,
    _preflight,
    _text_reply,
    _text_target,
    _text_verdict,
    insert,
    press,
    type_text,
)
from browser_control.lib.images import (  # noqa: F401
    expected_pixels,
    output_path,
    png_size,
    write_atomic,
)

# The private spellings the rest of the tree (and the suites) grew up with.
_int = as_int
_num = as_float
_ints = as_ints
_list = as_list
_png_size = png_size
_pixels = expected_pixels
_shot_target = output_path
_write_shot = write_atomic
