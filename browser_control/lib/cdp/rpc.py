"""rpc — one CDP method call: framing, one deadline, the reply's value.

Only this module speaks the websocket protocol. `call` is one method, one
deadline, never retried (a mutation must not be replayed); `evaluate` and
`evaluate_until` are the page-expression forms.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from collections.abc import Callable
from typing import Any, NoReturn

from browser_control.lib.errors import (
    ERR_BLOCKED,
    ERR_CDP_ERROR,
    ERR_CDP_NOT_LOCAL,
    ERR_JS_ERROR,
    ERR_RESULT_TOO_LARGE,
    ControlError,
    fail,
)
from browser_control.lib.text import foreign

# The one third-party dependency. Typed as Any so a missing package is a
# runtime refusal (`no-websockets`), not an import-time traceback — and so the
# type checker does not have to reason about the optional import.
websockets: Any
try:
    import websockets as _websockets
except ImportError:                                          # pragma: no cover
    _websockets = None
websockets = _websockets


EVAL_RESULT_CAP = 64_000

async def _await_reply(ws: Any, rid: int, method: str, params: dict,
                       budget: float, *, error_text: Callable[[dict], str],
                       on_timeout: Callable[[BaseException], None] | None = None,
                       on_deadline: Callable[[], None] | None = None,
                       on_frame: Callable[[dict], None] | None = None,
                       wait_hook: Callable[[float], float] | None = None,
                       bad_frame: Callable[[BaseException], None] | None = None,
                       ) -> dict:
    """One request, one reply: send, receive, demux by id, map the errors.

    The ONE place this package frames a call and reads a reply. Each caller
    supplies its own vocabulary: `error_text(err)` builds the refusal for a
    protocol `error`; `on_timeout(e)` / `on_deadline()` raise the caller's
    timeout refusal (a blocked renderer, a sample that ran out, an eval that
    will not answer); `on_frame(msg)` sees every frame that is not the reply
    (Session's dialog watch); `wait_hook` shortens a wait (a parked renderer);
    `bad_frame(e)` maps a non-JSON frame. A hook that returns lets the
    TimeoutError propagate, which is what the polling sample wants.
    """
    await ws.send(json.dumps({"id": rid, "method": method, "params": params}))
    deadline = time.monotonic() + budget
    while True:
        wait = max(0.1, deadline - time.monotonic())
        if wait_hook is not None:
            wait = wait_hook(wait)
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=wait))
        except TimeoutError as e:
            if on_timeout is not None:
                on_timeout(e)
            raise
        except (ValueError, TypeError) as e:
            if bad_frame is not None:
                bad_frame(e)
            raise ControlError(ERR_CDP_ERROR,
                               f"{method}: a frame that is not JSON "
                               f"({e})") from e
        if msg.get("id") == rid:
            err = msg.get("error")
            if err:
                raise ControlError(ERR_CDP_ERROR, error_text(err))
            return msg.get("result") or {}
        if on_frame is not None:
            on_frame(msg)
        if time.monotonic() >= deadline:
            if on_deadline is not None:
                on_deadline()
            raise TimeoutError(f"no reply for id {rid} within {budget:g}s")


def _value_of(result: dict) -> Any:
    """The value one Runtime.evaluate reply carries, or a refusal.

    A page-level exception is `js-error`: the page ANSWERED, and its answer
    was a failure — a different fact from a transport failure. A result past
    `EVAL_RESULT_CAP` refuses rather than handing back half a value.
    """
    details = result.get("exceptionDetails")
    if details:
        exception = details.get("exception") or {}
        text = str(exception.get("description") or details.get("text")
                   or "page JS exception")
        fail(ERR_JS_ERROR, f"Runtime.evaluate: {foreign(text)}")
    value = (result.get("result") or {}).get("value")
    try:
        # A string's cap is about the TEXT the page produced, not its JSON
        # escaping: `json.dumps` expands each non-ASCII character to a
        # `\uXXXX` escape (6x) or a surrogate pair (12x), so `tab text`
        # refused on a CJK page far below its declared 40 000-char cap (a
        # review flagged it).
        size = len(value) if isinstance(value, str) else len(json.dumps(value))
    except (TypeError, ValueError):
        size = 0
    if size > EVAL_RESULT_CAP:
        fail(ERR_RESULT_TOO_LARGE,
             f"Runtime.evaluate answered {size} chars (cap {EVAL_RESULT_CAP}) "
             "— narrow the expression, or read the page with `tab text`")
    if isinstance(value, str):
        try:
            return json.loads(value)      # a page that answered JSON
        except (ValueError, TypeError):
            return value                  # a plain string
    return value                          # None for undefined: an answer

PAGE_ENABLE_S = 5.0

PARKED_BUDGET_S = 2.0       # a parked tab will not answer; do not wait long

DIALOG_GRACE_S = 1.0        # after a dialog opens, how long the reply gets

DIALOG_EVENT = "Page.javascriptDialogOpening"

BLOCKED_HINT = ("the tab did not answer a command that asks the PAGE "
                "nothing — its renderer is blocked (a JavaScript dialog, or "
                "a script that does not yield); `tab dialog state` reports "
                "what can be proven, and `tab close` ends it")

async def _page_enable(ws: Any, rid: int) -> None:
    """`Page.enable` on a fresh connection, before anything else runs.

    Measured on Chrome 152: a JavaScript dialog a page opens is SUPPRESSED for
    a target whose Page domain was never enabled — the browser shows nothing,
    `Page.handleJavaScriptDialog` reports "No dialog is showing", and the
    renderer stays parked for good. Enabling the domain first makes the page's
    own dialog a real one: it is announced, it can be answered, the renderer
    comes back. Enabling changes no bytes, so every session does it.

    The one thing it cannot fix is a tab that was ALREADY blocked (its dialog
    was suppressed before any client enabled the domain): that refusal says so
    and names the way out.
    """
    def blocked(_e: BaseException | None = None) -> NoReturn:
        raise ControlError(ERR_BLOCKED, BLOCKED_HINT)

    await _await_reply(
        ws, rid, "Page.enable", {}, PAGE_ENABLE_S,
        error_text=lambda err: (f"Page.enable: {err.get('message')} "
                                f"(code {err.get('code')})"),
        on_timeout=blocked, on_deadline=blocked)


def _checked_ws(url: str, where: str = "endpoint") -> str:
    """The websocket URL, with its HOST checked.

    CDP is loopback by design; a `webSocketDebuggerUrl` naming a foreign host
    would receive everything this tool sends while serving fabricated
    answers, so anything but loopback is refused.
    """
    try:
        host = (urllib.parse.urlparse(str(url)).hostname or "").lower()
    except ValueError as e:
        # a malformed endpoint (`ws://[::1/x`) raised a raw ValueError out of
        # every public transport call; it is a refusal like any other (a
        # review flagged it)
        fail(ERR_CDP_NOT_LOCAL, f"{where}: {url!r} is not a usable endpoint ({e})")
    if host not in ("127.0.0.1", "localhost", "::1"):
        fail(ERR_CDP_NOT_LOCAL,
             f"{where}: the endpoint names a websocket on {host!r} — refusing "
             "to send page traffic off the loopback CDP endpoint")
    return str(url)
