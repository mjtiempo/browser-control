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
from typing import Any

from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_BLOCKED,
    ERR_CDP_ERROR,
    ERR_CDP_NOT_LOCAL,
    ERR_EVAL_TIMEOUT,
    ERR_JS_ERROR,
    ERR_NO_WEBSOCKETS,
    ERR_RESULT_TOO_LARGE,
    ControlError,
    fail,
)
from browser_control.lib.text import foreign  # pyright: ignore[reportMissingImports]

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

async def _call(ws_url: str, method: str, params: dict,
                timeout: float) -> dict:
    try:
        # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket -- loopback-only, host-checked by _checked_ws
        async with websockets.connect(ws_url, max_size=2 ** 24,
                                      open_timeout=10) as ws:
            # one request, one reply: a mutation must not be replayed, so
            # there is no retry loop here — the caller's read-back is the
            # recovery
            await ws.send(json.dumps({"id": 1, "method": method,
                                      "params": params}))
            deadline = time.time() + timeout
            while True:
                msg = json.loads(await asyncio.wait_for(
                    ws.recv(), timeout=max(0.1, deadline - time.time())))
                if msg.get("id") == 1:
                    err = msg.get("error")
                    if err:
                        fail(ERR_CDP_ERROR,
                             f"{method}: {foreign(err.get('message'))} "
                             f"(code {foreign(err.get('code'), 40)})")
                    return msg.get("result") or {}
                if time.time() >= deadline:
                    fail(ERR_CDP_ERROR,
                         f"{method}: no reply for id 1 within {timeout:g}s")
    except ControlError:
        raise                       # the protocol answered; not a transport hiccup
    except Exception as e:                                     # noqa: BLE001
        raise ControlError(ERR_CDP_ERROR, f"{method}: {e}") from e
    raise ControlError(ERR_CDP_ERROR,
                       f"{method}: did not answer within {timeout:g}s")

def call(ws_url: str, method: str, params: dict | None = None,
         timeout: float = 15.0) -> dict:
    """One CDP method call, returning the protocol's own `result`."""
    if websockets is None:
        fail(ERR_NO_WEBSOCKETS,
             "the `websockets` package is required to speak CDP "
             "(pip install websockets)")
    return asyncio.run(_call(ws_url, method, params or {}, timeout))

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
    await ws.send(json.dumps({"id": rid, "method": "Page.enable",
                              "params": {}}))
    deadline = time.time() + PAGE_ENABLE_S
    while True:
        try:
            msg = json.loads(await asyncio.wait_for(
                ws.recv(), timeout=max(0.1, deadline - time.time())))
        except TimeoutError as e:
            raise ControlError(ERR_BLOCKED, BLOCKED_HINT) from e
        except (ValueError, TypeError) as e:
            raise ControlError(ERR_CDP_ERROR,
                f"Page.enable: a frame that is not JSON ({e})") from e
        if msg.get("id") == rid:
            err = msg.get("error")
            if err:
                raise ControlError(ERR_CDP_ERROR,
                    f"Page.enable: {err.get('message')} "
                    f"(code {err.get('code')})")
            return
        if time.time() >= deadline:
            raise ControlError(ERR_BLOCKED, BLOCKED_HINT)

async def _sample(ws, rid: int, expression: str, budget: float) -> Any:
    """One evaluate on an ALREADY open connection, or TimeoutError."""
    await ws.send(json.dumps({"id": rid, "method": "Runtime.evaluate",
                              "params": {"expression": expression,
                                         "returnByValue": True}}))
    deadline = time.time() + budget
    while True:
        try:
            msg = json.loads(await asyncio.wait_for(
                ws.recv(), timeout=max(0.1, deadline - time.time())))
        except TimeoutError:
            raise                       # the caller decides: poll again, or
        except (ValueError, TypeError) as e:   # refuse `eval-timeout`
            raise ControlError(ERR_CDP_ERROR,
                f"Runtime.evaluate: a frame that is not JSON ({e})") from e
        if msg.get("id") == rid:
            err = msg.get("error")
            if err:
                fail(ERR_CDP_ERROR,
                     f"Runtime.evaluate: {err.get('message')} "
                     f"(code {err.get('code')})")
            return _value_of(msg.get("result") or {})
        if time.time() >= deadline:
            raise TimeoutError(f"no reply for id {rid} within {budget:g}s")

async def _evaluate(ws_url: str, expression: str, timeout: float) -> Any:
    try:
        # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket -- loopback-only, host-checked by _checked_ws
        async with websockets.connect(ws_url, max_size=2 ** 24,
                                      open_timeout=10) as ws:
            await _page_enable(ws, 1)      # dialogs must be real, not wedges
            # one send: an expression may WRITE (a nav assignment, a form
            # submit), so there is no retry — the caller's read-back is the
            # recovery
            try:
                return await _sample(ws, 2, expression, timeout)
            except TimeoutError as e:
                raise ControlError(ERR_EVAL_TIMEOUT,
                                   f"Runtime.evaluate: {e}") from e
    except ControlError:
        raise
    except Exception as e:                                     # noqa: BLE001
        raise ControlError(ERR_CDP_ERROR, f"Runtime.evaluate: {e}") from e
    raise ControlError(ERR_CDP_ERROR, "Runtime.evaluate: no answer")

async def _evaluate_until(ws_url: str, expression: str, accept: Any,
                          timeout: float, interval: float) -> tuple[Any, int]:
    """ONE connection, many samples, until `accept(value)` or the deadline.

    A sample the page does not answer is what a poll loop is FOR, so it is
    not an error here: the budget belongs to the loop, not to each sample.
    Only the connection itself can fail, and that is a refusal.
    """
    value: Any = None
    samples = 0
    try:
        # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket -- loopback-only, host-checked by _checked_ws
        async with websockets.connect(ws_url, max_size=2 ** 24,
                                      open_timeout=10) as ws:
            # a dialog the samples outlive is ANNOUNCED once the domain is on
            await _page_enable(ws, 1)
            deadline = time.time() + timeout
            while True:
                samples += 1
                try:
                    value = await _sample(ws, samples + 1, expression,
                                          max(0.5, deadline - time.time()))
                    if accept(value):
                        return value, samples
                except TimeoutError:
                    pass               # this sample went unanswered
                if time.time() >= deadline:
                    return value, samples
                await asyncio.sleep(interval)
    except ControlError:
        raise
    except Exception as e:                                     # noqa: BLE001
        raise ControlError(ERR_CDP_ERROR, f"Runtime.evaluate: {e}") from e
    raise ControlError(ERR_CDP_ERROR, "Runtime.evaluate: no answer")

def evaluate(ws_url: str, expression: str, timeout: float = 15.0) -> Any:
    """Evaluate an expression on ONE tab's own connection, returning its value.

    `returnByValue`, so a page that answers JSON is decoded to the value it
    produced. Three failures stay apart, because a caller branches on them:
    the page THREW (`js-error`), the page did not answer within the budget
    (`eval-timeout`), and the transport itself failed (`cdp-error`).
    """
    if websockets is None:
        fail(ERR_NO_WEBSOCKETS,
             "the `websockets` package is required to speak CDP "
             "(pip install websockets)")
    return asyncio.run(_evaluate(_checked_ws(ws_url, "tab"), expression,
                                 timeout))

def evaluate_until(ws_url: str, expression: str, accept: Any, timeout: float,
                   interval: float = 0.4) -> tuple[Any, int]:
    """Sample `expression` on ONE connection until `accept(value)` is true.

    Returns (the last value, how many samples were taken). Built for the verbs
    that poll: a websocket per sample turns a 30-sample wait into 30
    connections.
    """
    if websockets is None:
        fail(ERR_NO_WEBSOCKETS,
             "the `websockets` package is required to speak CDP "
             "(pip install websockets)")
    return asyncio.run(_evaluate_until(_checked_ws(ws_url, "tab"), expression,
                                       accept, timeout, interval))


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
