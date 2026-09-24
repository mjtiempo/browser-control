"""rpc — one CDP method call: framing, one deadline, the reply's value.

Only this module speaks the websocket protocol. `call` is one method, one
deadline, never retried (a mutation must not be replayed); `evaluate` and
`evaluate_until` are the page-expression forms.
"""
from __future__ import annotations

import asyncio
import json
import math
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

# One websocket FRAME may not exceed this (`Session` connects with it as
# `max_size`). It bounds the TRANSFER, and only the transfer: a page answer
# between `EVAL_RESULT_CAP` and this is refused by `_value_of` after it lands,
# and one past it cannot land at all.
TRANSPORT_CAP = 2 ** 24

# The page's own `undefined`, as `_value_of` returns it. CDP sends NO `value`
# for it — the same absence a peer sends for nothing at all — so the page's
# `undefined` and its `null` (a real `value` of None) used to arrive as one
# answer. This sentinel is what lets a caller tell them apart, and it is a
# plain object: no JSON reply can produce one, and `json.dumps` refuses it
# rather than printing a guess.
UNDEFINED: Any = object()

def _too_big(e: BaseException) -> bool:
    """Did that receive fail because a frame was past the transport cap?

    Two spellings, because websockets has two layers: the Sans-I/O layer raises
    `PayloadTooBig`, and the I/O layer turns it into a 1009 close — RFC 6455
    "message too big" — which arrives as a `ConnectionClosed`. So a
    `ConnectionClosed` is asked about the close frames it SENT and it RECEIVED
    (never the deprecated `.code`, which warns), and the class is read off the
    live `websockets` binding: a suite replaces that binding, and a missing
    name must not become an AttributeError in the middle of a refusal.
    """
    kind = getattr(websockets, "PayloadTooBig", None)
    if isinstance(kind, type) and isinstance(e, kind):
        return True
    return any(getattr(frame, "code", None) == 1009
               for frame in (getattr(e, "sent", None),
                             getattr(e, "rcvd", None)))

def _error_text(method: str, err: Any) -> str:
    """The refusal for one protocol `error`, whatever SHAPE the peer sent.

    CDP defines `error` as an object (`{"code":…,"message":…}`), and both
    callers read `.get` off it — so a peer answering `{"error": "boom"}` raised
    a raw AttributeError, and the refusal the caller finally printed named
    Python's own fault (`'str' object has no attribute 'get'`) instead of the
    peer's (a review flagged it). An object keeps its message and code; a
    non-object is reported by its own bounded text and its type, because "the
    peer refused the call, and this is what it sent instead" is the fact.
    """
    if isinstance(err, dict):
        return (f"{method}: {foreign(err.get('message'))} "
                f"(code {foreign(err.get('code'), 40)})")
    return (f"{method}: the peer refused the call with `error` = "
            f"{type(err).__name__} {foreign(err)!r}, not the CDP error object")


def _json_object(value: Any, method: str) -> dict:
    """A reply's `result` as the JSON OBJECT CDP defines, or a refusal.

    Callers read keys out of it (`Session.handle`, `_value_of`), so a peer that
    answers `{"id":N,"result":"notadict"}` used to raise an AttributeError out
    of a `lib` call — `ERR[internal]` at the CLI, the one shape a refusal
    contract must never take (a review measured it). An ABSENT key is the
    protocol's own "nothing to say" and stays `{}`; anything else present is
    refused, naming the method and the shape that arrived.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    fail(ERR_CDP_ERROR,
         f"{method}: the reply's `result` is {type(value).__name__}, not the "
         "JSON object CDP defines")

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

    A frame past the transport cap is refused `result-too-large`, not
    `cdp-error`: the page DID answer, its answer was too big to move, and that
    is the same class as `_value_of`'s post-transfer cap — every other
    transport failure stays `cdp-error`, where the caller looks for one. The
    reply itself must be a JSON object (`_json_object`), so no caller can be
    handed something to call `.get()` on.
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
        except Exception as e:                                 # noqa: BLE001
            if _too_big(e):
                fail(ERR_RESULT_TOO_LARGE,
                     f"{method}: the page's answer was too big to transfer — "
                     f"the connection's frame limit is {TRANSPORT_CAP} bytes "
                     "(16 MiB), so the reply was cut off at the socket; ask "
                     "the page for LESS (slice it in the page, or narrow the "
                     "expression)")
            raise
        if not isinstance(msg, dict):
            raise ControlError(ERR_CDP_ERROR,
                               f"{method}: a frame that is not a JSON object "
                               f"({type(msg).__name__})")
        if msg.get("id") == rid:
            err = msg.get("error")
            if err:
                raise ControlError(ERR_CDP_ERROR, error_text(err))
            return _json_object(msg.get("result"), method)
        if on_frame is not None:
            on_frame(msg)
        if time.monotonic() >= deadline:
            if on_deadline is not None:
                on_deadline()
            raise TimeoutError(f"no reply for id {rid} within {budget:g}s")


def _value_of(result: dict, raw: bool = False,
              cap: int | None = EVAL_RESULT_CAP) -> Any:
    """The value one Runtime.evaluate reply carries, or a refusal.

    A page-level exception is `js-error`: the page ANSWERED, and its answer
    was a failure — a different fact from a transport failure. A result past
    `cap` refuses rather than handing back half a value.

    The cap is applied to the value AFTER it has crossed the wire, not instead
    of the wire's own bound: the socket refuses a reply past `TRANSPORT_CAP`
    (16 MiB) as `result-too-large` too, so this 64 k default is the tighter,
    post-transfer rule and the frame limit is the backstop under it.
    `cap=None` is the ONLY spelling of "no cap" — a verb whose expression
    already sliced its answer IN THE PAGE passes it, because JSON escaping
    inflates the wire value far past the text the page produced (`tab text` on
    a CJK page). Any int IS the cap, so a zero or negative one refuses
    everything rather than quietly disabling the rule.

    `raw=True` returns a string answer as THAT string, without the JSON
    decode below. The decode exists for this CLI's own probes, which
    `JSON.stringify` their findings on purpose and read the value back; a
    caller's own expression (`tab js`) is not one of those, and a page string
    that merely parses as JSON was reported as the value it looked like —
    `localStorage.getItem('k')` holding "null" answered null (a review
    measured it). The page's OWN value is what the escape hatch promised.

    The page's `undefined` is `UNDEFINED`, not None: `null` is a real `value`
    of None, while `undefined` carries no `value` at all — the shape a reply
    with nothing to say has too. A missing/absent `type` still answers None,
    so a bare `{}` keeps meaning what it always meant.
    """
    details = result.get("exceptionDetails")
    if isinstance(details, dict) and details:
        exception = details.get("exception")
        if not isinstance(exception, dict):
            # `exception` is an object in the protocol, but a peer that answers
            # a string or a list is still reporting a page-level failure —
            # reading `.get` off it raised a raw AttributeError out of a `lib`
            # call, and `evaluate_until` (which catches `ControlError` only)
            # let it kill a whole `tab wait` poll (a review measured it). A
            # non-object is treated as ABSENT, so the refusal keeps its code
            # and falls back to the details' own text.
            exception = {}
        text = str(exception.get("description") or details.get("text")
                   or "page JS exception")
        fail(ERR_JS_ERROR, f"Runtime.evaluate: {foreign(text)}")
    inner = _json_object(result.get("result"), "Runtime.evaluate")
    value = inner.get("value")
    if value is None:
        # CDP carries NaN/±Infinity/-0 in `unserializableValue`, not `value`.
        # Reading only `value` reported them as null — indistinguishable from
        # `undefined` — from the one verb whose promise is the page's OWN
        # value (a review flagged it). The four documented spellings parse
        # with float(); a non-finite one stays the protocol's own token so
        # stdout is still valid JSON (json.dumps would print a bare NaN).
        special = inner.get("unserializableValue")
        if isinstance(special, str):
            try:
                parsed = float(special)
            except ValueError:
                value = special
            else:
                if math.isfinite(parsed):
                    value = parsed
                else:
                    # the protocol's own token, returned BEFORE the JSON
                    # decode below: `json.loads("NaN")` would accept it back
                    # as a float, which is the one thing this must not do
                    return special
        elif special is None and inner.get("type") == "undefined":
            # no `value`, no `unserializableValue`, and the type says the page's
            # answer IS undefined: the sentinel keeps that apart from `null`
            return UNDEFINED
    try:
        # A string's cap is about the TEXT the page produced, not its JSON
        # escaping: `json.dumps` expands each non-ASCII character to a
        # `\uXXXX` escape (6x) or a surrogate pair (12x), so `tab text`
        # refused on a CJK page far below its declared 40 000-char cap (a
        # review flagged it).
        size = len(value) if isinstance(value, str) else len(json.dumps(value))
    except (TypeError, ValueError):
        size = 0
    if cap is not None and size > cap:
        fail(ERR_RESULT_TOO_LARGE,
             f"Runtime.evaluate answered {size} chars (cap {cap}) "
             "— narrow the expression, or read the page with `tab text`")
    if isinstance(value, str) and not raw:
        try:
            return json.loads(value)      # a page that answered JSON
        except (ValueError, TypeError):
            return value                  # a plain string
    return value                          # None for a JSON null; UNDEFINED above

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
        error_text=lambda err: _error_text("Page.enable", err),
        on_timeout=blocked, on_deadline=blocked)


def _checked_ws(url: str, where: str = "endpoint") -> str:
    """The websocket URL, with its HOST checked.

    CDP is loopback by design; a `webSocketDebuggerUrl` naming a foreign host
    would receive everything this tool sends while serving fabricated
    answers, so anything but loopback is refused. Loopback is not a user
    boundary, though — a co-tenant user on this machine can reach the port —
    so this guard is against remote hosts and wrong-process takeover, not
    against another local user.
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
