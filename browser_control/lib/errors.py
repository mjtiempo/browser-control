"""errors — the one refusal type every boundary catches, and its vocabulary.

A refusal is data, not a traceback: `code` is the contract a caller branches
on, `message` is one line for the human. `lib` raises it, `cli` prints it.

Every code the tool may speak is a named constant here, and `CODES` is the
registry the hermetic check reads: a raise site that invents a code fails the
check instead of shipping a string nobody can branch on.
"""
from __future__ import annotations

from typing import NoReturn

ERR_ACTIVATE_NOT_VERIFIED = "activate-not-verified"
ERR_AMBIGUOUS_BROWSER = "ambiguous-browser"
ERR_AMBIGUOUS_ELEMENT = "ambiguous-element"
ERR_AMBIGUOUS_OPTION = "ambiguous-option"
ERR_ATTACH_FAILED = "attach-failed"
ERR_BAD_ARGS = "bad-args"
ERR_BLOCKED = "blocked"
ERR_BROKEN_PIPE = "broken-pipe"
ERR_BROWSER_NOT_STOPPED = "browser-not-stopped"
ERR_CDP_ERROR = "cdp-error"
ERR_CDP_NOT_LOCAL = "cdp-not-local"
ERR_CDP_UNREACHABLE = "cdp-unreachable"
ERR_CHECK_NOT_VERIFIED = "check-not-verified"
ERR_CLOSE_TAB_NOT_VERIFIED = "close-tab-not-verified"
ERR_DIALOG_NOT_VERIFIED = "dialog-not-verified"
ERR_EVAL_TIMEOUT = "eval-timeout"
ERR_FILE_EXISTS = "file-exists"
ERR_FOCUS_NOT_VERIFIED = "focus-not-verified"
ERR_FRAME_AMBIGUOUS = "frame-ambiguous"
ERR_FRAME_NOT_SEPARATE = "frame-not-separate"
ERR_FRAME_UNATTRIBUTABLE = "frame-unattributable"
ERR_HOVER_NOT_VERIFIED = "hover-not-verified"
ERR_INSERT_NOT_VERIFIED = "insert-not-verified"
ERR_INTERNAL = "internal"
ERR_JS_ERROR = "js-error"
ERR_LAUNCH_FAILED = "launch-failed"
ERR_MEDIA_BLOCKED = "media-blocked"
ERR_MEDIA_NOT_VERIFIED = "media-not-verified"
ERR_NAV_FAILED = "nav-failed"
ERR_NAV_NOT_VERIFIED = "nav-not-verified"
ERR_NO_BROWSER = "no-browser"
ERR_NO_DIALOG = "no-dialog"
ERR_NO_FILE = "no-file"
ERR_NO_FOCUS = "no-focus"
ERR_NO_FRAME = "no-frame"
ERR_NO_MATCH = "no-match"
ERR_NO_MEDIA = "no-media"
ERR_NO_PAGE_TAB = "no-page-tab"
ERR_NO_VIEWPORT = "no-viewport"
ERR_NO_VIEWPORT_TARGET = "no-viewport-target"
ERR_NO_WEBSOCKETS = "no-websockets"
ERR_NOT_A_SELECT = "not-a-select"
ERR_NOT_ALLOWED = "not-allowed"
ERR_NOT_ATTACHED = "not-attached"
ERR_NOT_CHECKABLE = "not-checkable"
ERR_NOT_MANAGED = "not-managed"
ERR_OCCLUDED = "occluded"
ERR_PROFILE_BUSY = "profile-busy"
ERR_PROFILE_EXISTS = "profile-exists"
ERR_PROFILE_LIVE = "profile-live"
ERR_PROFILE_UNUSABLE = "profile-unusable"
ERR_RELOAD_NOT_VERIFIED = "reload-not-verified"
ERR_RESET_FAILED = "reset-failed"
ERR_RESET_NOT_VERIFIED = "reset-not-verified"
ERR_RESULT_TOO_LARGE = "result-too-large"
ERR_SCREENSHOT_NOT_VERIFIED = "screenshot-not-verified"
ERR_SCROLL_NOT_VERIFIED = "scroll-not-verified"
ERR_SEED_FAILED = "seed-failed"
ERR_SEED_NOT_VERIFIED = "seed-not-verified"
ERR_SELECT_NOT_VERIFIED = "select-not-verified"
ERR_TAB_AMBIGUOUS = "tab-ambiguous"
ERR_TABS_OPEN = "tabs-open"
ERR_TYPE_NOT_VERIFIED = "type-not-verified"
ERR_UNKNOWN_COMMAND = "unknown-command"
ERR_UPLOAD_NOT_VERIFIED = "upload-not-verified"
ERR_WAIT_TIMEOUT = "wait-timeout"
ERR_WRITE_FAILED = "write-failed"

# The registry the hermetic check reads and every `fail` is checked against:
# DERIVED from the constants above, so the vocabulary has ONE spelling. The
# check compares this set with the same `ERR_*` names it scans the tree for, so
# a code that is only ever built at runtime stays invisible here — on purpose.
CODES: frozenset[str] = frozenset(
    value for name, value in globals().items()
    if name.startswith("ERR_") and isinstance(value, str))


class ControlError(Exception):
    """A structured refusal: `ERR[code]: message` at the CLI boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def fail(code: str, message: str) -> NoReturn:
    raise ControlError(code, message)
