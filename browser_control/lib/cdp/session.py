"""session — `Session`: ONE connection for a sequence of CDP calls.

A verb that needs several calls (a click is a move, a press, a release and a
read) opens one `Session`, so the sequence shares a connection, a dialog
watch and one id counter.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, NoReturn

from browser_control.lib.cdp import rpc  # pyright: ignore[reportMissingImports]
from browser_control.lib.cdp.rpc import (  # pyright: ignore[reportMissingImports]
    BLOCKED_HINT,
    DIALOG_EVENT,
    DIALOG_GRACE_S,
    PARKED_BUDGET_S,
    _await_reply,
    _checked_ws,
    _page_enable,
    _value_of,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_BLOCKED,
    ERR_CDP_ERROR,
    ERR_EVAL_TIMEOUT,
    ERR_JS_ERROR,
    ERR_NO_WEBSOCKETS,
    ControlError,
    fail,
)
from browser_control.lib.text import foreign  # pyright: ignore[reportMissingImports]


class Session:
    """ONE websocket for a SEQUENCE of calls: a click is a move, a press, a
    release and a read-back; scrolling to an edge is a wheel and a read per
    step; revealing an element is `DOM.getDocument`, `requestNode`,
    `scrollIntoViewIfNeeded` and a check.

    A connection per call turns those into a dozen sockets, and the calls
    cannot be fired blind because each step depends on the last read. Nothing
    here retries: a session carries mutations, and the caller's read-back is
    the recovery.
    """

    def __init__(self, ws_url: str, timeout: float = 15.0,
                 page_domain: bool = True) -> None:
        """`page_domain=False` skips the `Page.enable` every other session
        starts with. Exactly one verb needs that: `tab dialog` answering a
        dialog that is ALREADY up, because enabling the domain is what blocks
        on a parked tab while the handling command answers regardless."""
        if rpc.websockets is None:
            fail(ERR_NO_WEBSOCKETS,
                 "the `websockets` package is required to speak CDP "
                 "(pip install websockets)")
        self._ws_url = _checked_ws(ws_url, "tab")
        self._timeout = timeout
        self._page_domain = page_domain
        self.page_domain_ok = True
        self.parked = False
        self._loop: Any = None
        self._ws: Any = None
        self._rid = 0
        # what the target PUSHED while a call was in flight (a dialog opening
        # mid-verb is the one that matters): a refusal can name it instead of
        # guessing, and it is the only way to know a dialog's own text
        self.events: list[dict] = []

    def _connect(self) -> Any:
        if self._ws is None:
            self._loop = asyncio.new_event_loop()
            try:
                self._ws = self._loop.run_until_complete(
                    # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket -- loopback-only, host-checked by _checked_ws
                    rpc.websockets.connect(self._ws_url, max_size=2 ** 24,
                                       open_timeout=10))
            except Exception as e:                             # noqa: BLE001
                self.close()
                raise ControlError(ERR_CDP_ERROR,
                                   f"cannot open the tab's connection: "
                                   f"{e}") from e
            try:
                if self._page_domain:
                    self._loop.run_until_complete(_page_enable(self._ws, 1))
            except ControlError as e:
                # A parked tab cannot enable the domain — and that is exactly
                # when a BROWSER-side command (`Page.navigate`) is the way out,
                # so the session stays usable and remembers why it is limited.
                # Only a BLOCKED enable means "this tab is parked": recording a
                # protocol refusal as parked made every later hint blame a
                # dialog that was never there (a review flagged it).
                self.page_domain_ok = False
                self.parked = e.code == "blocked"
            except Exception as e:                             # noqa: BLE001
                self.close()
                raise ControlError(ERR_CDP_ERROR,
                                   f"Page.enable: {e}") from e
            self._rid = 1 if self._page_domain else 0    # id 1 was Page.enable
        return self._ws

    def dialog(self) -> dict:
        """The last dialog this session saw OPEN, or {} — the page's own.

        Only a dialog that opened while this session was live can be here:
        connecting later cannot see what was announced to somebody else, which
        is exactly why `tab dialog state` may have to answer `null`.
        """
        for msg in reversed(self.events):
            if msg.get("method") == DIALOG_EVENT:
                return dict(msg.get("params") or {})
        return {}

    def _parked_now(self, parked_at: float) -> bool:
        """Did the reply window close because a dialog parked the renderer?

        Asked as a method because the answer belongs AFTER the wait: computing
        it before would compare against a deadline we have not reached yet,
        and the refusal would claim the full budget was spent.
        """
        return bool(parked_at) and time.monotonic() >= parked_at

    def _blocked_hint(self) -> str:
        """What to add to a refusal when the renderer stopped answering."""
        dialog = self.dialog()
        if dialog:
            kind = str(dialog.get("type") or "dialog")
            message = str(dialog.get("message") or "")[:80]
            return (f" — it opened a {kind} ({message!r}): `tab dialog accept` "
                    "or `tab dialog dismiss` answers it while the browser is "
                    "still showing it, and a tab that stays parked is "
                    "replaced by `tab nav URL` or closed with `tab close`")
        if self.parked:
            return (" — the tab was ALREADY parked when this session opened "
                    "(a suppressed JavaScript dialog, or a script that does "
                    "not yield): `tab nav URL` replaces the document and its "
                    "renderer, `tab close` ends the tab")
        return (" — the renderer is blocked: a JavaScript dialog it opened, "
                "or a script that does not yield (see `tab dialog state`)")

    async def _call(self, method: str, params: dict, budget: float) -> dict:
        ws = self._ws
        self._rid += 1
        rid = self._rid
        parked_at = 0.0

        def saw(msg: dict) -> None:
            """Every frame that is not the reply: keep it, watch for a dialog."""
            nonlocal parked_at
            if not msg.get("method"):
                return
            self.events.append({"method": msg["method"],
                                "params": msg.get("params") or {}})
            del self.events[:-20]
            if not parked_at and msg["method"] == DIALOG_EVENT:
                parked_at = time.monotonic() + DIALOG_GRACE_S

        def wait_hook(wait: float) -> float:
            if parked_at:
                # a dialog this session SAW open parks the renderer: the reply
                # cannot come until somebody answers it, so waiting the whole
                # budget buys nothing but a slower refusal
                return min(wait, max(0.1, parked_at - time.monotonic()))
            return wait

        def timed_out(e: BaseException | None = None) -> NoReturn:
            if self._parked_now(parked_at):
                raise ControlError(ERR_BLOCKED,
                    f"{method}: no reply — the renderer is parked by a "
                    f"JavaScript dialog{self._blocked_hint()}") from e
            code = (ERR_EVAL_TIMEOUT if method.startswith("Runtime.evaluate")
                    else ERR_CDP_ERROR)
            if self.parked:
                # the tab was parked before this session opened: one code
                # for "the renderer is not answering", whatever the call
                code = ERR_BLOCKED
            raise ControlError(
                code, f"{method}: no reply within {budget:g}s"
                f"{self._blocked_hint()}") from e

        def past_deadline() -> NoReturn:
            raise ControlError(ERR_CDP_ERROR,
                               f"{method}: no reply for id {rid} within "
                               f"{budget:g}s{self._blocked_hint()}")

        return await _await_reply(
            ws, rid, method, params, budget,
            error_text=lambda err: (
                f"{method}: {foreign(err.get('message'))} "
                f"(code {foreign(err.get('code'), 40)})"),
            on_timeout=timed_out, on_deadline=past_deadline,
            on_frame=saw, wait_hook=wait_hook)

    def call(self, method: str, params: dict | None = None,
             timeout: float = 0.0) -> dict:
        """One method call on the open connection: the protocol's `result`."""
        # `_connect` INSIDE the try: a connection that dies between the
        # handshake and `Page.enable` — the tab closed, the browser exited mid
        # verb — used to escape as a raw exception out of every page verb
        # instead of a `cdp-error` (a review flagged it).
        try:
            self._connect()
            budget = timeout or self._timeout
            if self.parked:
                # nothing will answer: a browser-side command (`Page.navigate`)
                # still works, so it gets a short budget and its own refusal
                budget = min(budget, PARKED_BUDGET_S)
            return self._loop.run_until_complete(
                self._call(method, params or {}, budget))
        except ControlError:
            raise
        except Exception as e:                                 # noqa: BLE001
            raise ControlError(ERR_CDP_ERROR, f"{method}: {e}") from e

    def evaluate(self, expression: str, timeout: float = 0.0) -> Any:
        """One Runtime.evaluate on the open connection, value semantics."""
        return _value_of(self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True}, timeout))

    def handle(self, expression: str, timeout: float = 0.0) -> str:
        """The objectId of a NON-serialized expression, for `DOM.requestNode`.

        A remote object lives as long as the session: this is how an element
        found by the matcher becomes a `nodeId` that a DOM method can act on
        without a line of scrolling JavaScript.
        """
        result = self.call("Runtime.evaluate",
                           {"expression": expression, "returnByValue": False},
                           timeout)
        details = result.get("exceptionDetails")
        if details:
            exception = details.get("exception") or {}
            text = str(exception.get("description") or details.get("text")
                       or "page JS exception")
            fail(ERR_JS_ERROR, f"Runtime.evaluate: {foreign(text)}")
        return str((result.get("result") or {}).get("objectId") or "")

    def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(self._ws.close())
            self._ws = None
        if self._loop is not None:
            self._loop.close()
            self._loop = None

    def __enter__(self) -> Session:
        return self

    def __exit__(self, exc_type: Any = None, exc: Any = None,
                 traceback: Any = None) -> None:
        self.close()


# ---------------------------------------------------------------- the wrappers
# One-shot calls are a `Session` that opens and closes around one exchange:
# exactly one `websockets.connect` site exists in this package, and it is
# `Session._connect`.
def call(ws_url: str, method: str, params: dict | None = None,
         timeout: float = 15.0) -> dict:
    """One CDP method call, returning the protocol's own `result`."""
    if rpc.websockets is None:
        fail(ERR_NO_WEBSOCKETS,
             "the `websockets` package is required to speak CDP "
             "(pip install websockets)")
    with Session(_checked_ws(ws_url), page_domain=False) as session:
        return session.call(method, params or {}, timeout)


def evaluate(ws_url: str, expression: str, timeout: float = 15.0) -> Any:
    """Evaluate an expression on ONE tab's own connection, returning its value.

    `returnByValue`, so a page that answers JSON is decoded to the value it
    produced. Three failures stay apart, because a caller branches on them:
    the page THREW (`js-error`), the page did not answer within the budget
    (`eval-timeout`), and the transport itself failed (`cdp-error`).
    """
    if rpc.websockets is None:
        fail(ERR_NO_WEBSOCKETS,
             "the `websockets` package is required to speak CDP "
             "(pip install websockets)")
    with Session(_checked_ws(ws_url, "tab")) as session:
        return session.evaluate(expression, timeout)


def evaluate_until(ws_url: str, expression: str, accept: Any, timeout: float,
                   interval: float = 0.4) -> tuple[Any, int]:
    """Sample `expression` on ONE connection until `accept(value)` is true.

    Returns (the last value, how many samples were taken). Built for the verbs
    that poll: a websocket per sample turns a 30-sample wait into 30
    connections. A sample the page does not answer is what a poll loop is FOR:
    it is caught and the loop continues; only the connection itself failing,
    or a tab that was ALREADY parked when the session opened, refuses.
    """
    if rpc.websockets is None:
        fail(ERR_NO_WEBSOCKETS,
             "the `websockets` package is required to speak CDP "
             "(pip install websockets)")
    deadline = time.monotonic() + timeout
    value: Any = None
    samples = 0
    with Session(_checked_ws(ws_url, "tab")) as session:
        session._connect()                    # Page.enable happens here
        if session.parked:
            # the tab was parked before this session opened: polling cannot
            # answer, and the old path refused `blocked` right here
            raise ControlError(ERR_BLOCKED, BLOCKED_HINT)
        while True:
            samples += 1
            try:
                value = session.evaluate(expression,
                                         max(0.5, deadline - time.monotonic()))
                if accept(value):
                    return value, samples
            except ControlError as e:
                # a sample that did not answer (or a renderer parked mid-poll)
                # is what the loop is for; anything else is a real failure
                if e.code not in (ERR_EVAL_TIMEOUT, ERR_BLOCKED):
                    raise
            if time.monotonic() >= deadline:
                return value, samples
            time.sleep(interval)
