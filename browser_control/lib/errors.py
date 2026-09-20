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

CODES: frozenset[str] = frozenset({
    ERR_ACTIVATE_NOT_VERIFIED,
    ERR_AMBIGUOUS_BROWSER,
    ERR_AMBIGUOUS_ELEMENT,
    ERR_AMBIGUOUS_OPTION,
    ERR_ATTACH_FAILED,
    ERR_BAD_ARGS,
    ERR_BLOCKED,
    ERR_BROKEN_PIPE,
    ERR_BROWSER_NOT_STOPPED,
    ERR_CDP_ERROR,
    ERR_CDP_NOT_LOCAL,
    ERR_CDP_UNREACHABLE,
    ERR_CHECK_NOT_VERIFIED,
    ERR_CLOSE_TAB_NOT_VERIFIED,
    ERR_DIALOG_NOT_VERIFIED,
    ERR_EVAL_TIMEOUT,
    ERR_FILE_EXISTS,
    ERR_FOCUS_NOT_VERIFIED,
    ERR_FRAME_AMBIGUOUS,
    ERR_FRAME_NOT_SEPARATE,
    ERR_FRAME_UNATTRIBUTABLE,
    ERR_HOVER_NOT_VERIFIED,
    ERR_INSERT_NOT_VERIFIED,
    ERR_INTERNAL,
    ERR_JS_ERROR,
    ERR_LAUNCH_FAILED,
    ERR_MEDIA_BLOCKED,
    ERR_MEDIA_NOT_VERIFIED,
    ERR_NAV_FAILED,
    ERR_NAV_NOT_VERIFIED,
    ERR_NO_BROWSER,
    ERR_NO_DIALOG,
    ERR_NO_FILE,
    ERR_NO_FOCUS,
    ERR_NO_FRAME,
    ERR_NO_MATCH,
    ERR_NO_MEDIA,
    ERR_NO_PAGE_TAB,
    ERR_NO_VIEWPORT,
    ERR_NO_VIEWPORT_TARGET,
    ERR_NO_WEBSOCKETS,
    ERR_NOT_A_SELECT,
    ERR_NOT_ALLOWED,
    ERR_NOT_ATTACHED,
    ERR_NOT_CHECKABLE,
    ERR_NOT_MANAGED,
    ERR_OCCLUDED,
    ERR_PROFILE_BUSY,
    ERR_PROFILE_EXISTS,
    ERR_PROFILE_LIVE,
    ERR_PROFILE_UNUSABLE,
    ERR_RELOAD_NOT_VERIFIED,
    ERR_RESET_FAILED,
    ERR_RESET_NOT_VERIFIED,
    ERR_RESULT_TOO_LARGE,
    ERR_SCREENSHOT_NOT_VERIFIED,
    ERR_SCROLL_NOT_VERIFIED,
    ERR_SEED_FAILED,
    ERR_SEED_NOT_VERIFIED,
    ERR_SELECT_NOT_VERIFIED,
    ERR_TAB_AMBIGUOUS,
    ERR_TABS_OPEN,
    ERR_TYPE_NOT_VERIFIED,
    ERR_UNKNOWN_COMMAND,
    ERR_UPLOAD_NOT_VERIFIED,
    ERR_WAIT_TIMEOUT,
    ERR_WRITE_FAILED,
})


class ControlError(Exception):
    """A structured refusal: `ERR[code]: message` at the CLI boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def fail(code: str, message: str) -> NoReturn:
    raise ControlError(code, message)
