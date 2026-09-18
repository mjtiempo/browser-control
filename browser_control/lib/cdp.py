"""cdp — where the endpoint is, JSON reads, and one websocket call.

The endpoint is not assumed: the browser is launched with
`--remote-debugging-port=0`, so the OS picks the port and the browser writes
it into its profile's `DevToolsActivePort`. Every call reads that file — a
port file alone is not proof, the port must answer.

Only two things speak CDP here: `get_json` (HTTP, capped) and `call` (one
websocket call, one deadline, never retried). `websockets` is the one
third-party dependency and it is imported lazily enough that `open`, `close`
and `tabs` still work without it.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ControlError,
    fail,
)

# The one third-party dependency. Typed as Any so a missing package is a
# runtime refusal (`no-websockets`), not an import-time traceback — and so the
# type checker does not have to reason about the optional import.
websockets: Any
try:
    import websockets as _websockets
except ImportError:                                          # pragma: no cover
    _websockets = None
websockets = _websockets

PORT_FILE = "DevToolsActivePort"
# The most a CDP HTTP reply may be. `/json` for a browser with hundreds of
# tabs is tens of KB; a body past this is not a tab list.
GET_CAP = 8 * 1024 * 1024
# The most a page's ANSWER may be. An expression can return a whole document;
# past this the caller should narrow the expression rather than reason about
# half a value — `tab text` truncates IN the page, so its cap is separate.
EVAL_RESULT_CAP = 64_000


def port_of(profile: str) -> int:
    """The port the browser wrote into its profile, 0 when there is none.

    0 is "no answer": a stale file from a dead run and a browser that never
    started look the same here, and the caller decides.
    """
    try:
        with open(os.path.join(profile, PORT_FILE)) as f:
            value = int(f.readline().strip())
    except (OSError, ValueError):
        return 0
    return value if 0 < value < 65536 else 0


def _get_bytes(url: str) -> bytes:
    """The body at `url`, capped, or a refusal — never a bare exception."""
    body = b""
    try:
        # loopback by construction: the URL is built from the port file, and
        # the port is the browser's own (semgrep: ignore)
        with urllib.request.urlopen(url, timeout=5) as r:   # noqa: S310
            body = r.read(GET_CAP + 1)
    except Exception as e:                                     # noqa: BLE001
        fail("cdp-unreachable", f"{url}: {e}")
    if len(body) > GET_CAP:
        fail("result-too-large",
             f"{url}: the endpoint answered more than {GET_CAP} bytes — "
             "that is not a CDP reply")
    return body


def _get_port(port: int, path: str) -> Any:
    """GET one CDP JSON endpoint on an explicit loopback port."""
    body = _get_bytes(f"http://127.0.0.1:{port}{path}")
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError as e:
        fail("cdp-error", f"{path}: the endpoint answered no JSON: {e}")


def get_json(profile: str, path: str) -> Any:
    """GET one CDP JSON endpoint on the profile's own loopback port."""
    port = port_of(profile)
    if not port:
        fail("cdp-unreachable",
             f"no DevTools port in {profile}: the browser is not running "
             "(or was started without --remote-debugging-port=0)")
    return _get_port(port, path)


def answers(port: int) -> bool:
    """Is a CDP endpoint answering on this loopback port right now?

    For a caller that has a PORT rather than a managed profile: a browser
    started by hand with `--remote-debugging-port=N`, or one whose profile is
    not ours to read a port file from.
    """
    if not port:
        return False
    try:
        return isinstance(_get_port(port, "/json/version"), dict)
    except ControlError:
        return False


def version_at(port: int) -> dict:
    """What `/json/version` says about the browser on `port`, or {}.

    The product and protocol versions come from the BROWSER, not from the
    binary's name, which is what makes them worth reporting.
    """
    if not port:
        return {}
    try:
        info = _get_port(port, "/json/version")
    except ControlError:
        return {}
    return info if isinstance(info, dict) else {}


def reachable(profile: str) -> bool:
    """Is a browser answering CDP on this profile right now?"""
    return answers(port_of(profile))


def page_rows(profile: str) -> list[dict]:
    """The page targets of the browser on this profile."""
    return _pages(get_json(profile, "/json"))


def page_rows_at(port: int) -> list[dict]:
    """The page targets of the browser answering on this loopback port."""
    return _pages(_get_port(port, "/json"))


def _pages(rows: Any) -> list[dict]:
    """A `/json` reply -> its PAGE targets, id-sorted.

    Sorted by target id locally: CDP does not document `/json` order, so "the
    tabs" would otherwise follow an external array's order.
    """
    if not isinstance(rows, list):
        fail("cdp-error", "/json answered a shape this tool cannot read")
    pages = [r for r in rows
             if isinstance(r, dict) and r.get("type") == "page" and r.get("id")]
    return sorted(pages, key=lambda r: str(r["id"]))


def target_ws(port: int, target_id: str) -> str:
    """The websocket of ONE page target on `port`, host-checked.

    Read from `/json` — whose rows carry it — so the connection is that tab's
    own, not a re-resolution of a spec that may have moved since.
    """
    row = next((r for r in _pages(_get_port(port, "/json"))
                if str(r.get("id")) == str(target_id)), None)
    if row is None:
        fail("no-page-tab", f"tab {str(target_id)[:10]}… left the tab list")
    ws = str(row.get("webSocketDebuggerUrl") or "")
    if not ws:
        fail("no-page-tab",
             f"tab {str(target_id)[:10]}… has no websocket endpoint")
    return _checked_ws(ws, "tab")


def rows_to_tabs(rows: list[dict]) -> list[dict]:
    """One page row, as every verb reports it."""
    return [{"id": str(r.get("id") or ""),
             "title": str(r.get("title") or ""),
             "url": str(r.get("url") or "")} for r in rows]


def _checked_ws(url: str, where: str = "endpoint") -> str:
    """The websocket URL, with its HOST checked.

    CDP is loopback by design; a `webSocketDebuggerUrl` naming a foreign host
    would receive everything this tool sends while serving fabricated
    answers, so anything but loopback is refused.
    """
    host = (urllib.parse.urlparse(str(url)).hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        fail("cdp-not-local",
             f"{where}: the endpoint names a websocket on {host!r} — refusing "
             "to send page traffic off the loopback CDP endpoint")
    return str(url)


def browser_ws(profile: str) -> str:
    """The browser endpoint's websocket URL, shape- and host-checked."""
    info = get_json(profile, "/json/version")
    if not isinstance(info, dict):
        fail("cdp-error", "/json/version answered a shape this tool cannot read")
    ws = str(info.get("webSocketDebuggerUrl") or "")
    if not ws:
        fail("cdp-error", "/json/version names no browser websocket endpoint")
    return _checked_ws(ws)


def browser_call(profile: str, method: str, params: dict) -> dict:
    """One CDP call on the BROWSER endpoint (`Target.*`)."""
    return call(browser_ws(profile), method, params)


async def _call(ws_url: str, method: str, params: dict,
                timeout: float) -> dict:
    try:
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
                        fail("cdp-error",
                             f"{method}: {err.get('message')} "
                             f"(code {err.get('code')})")
                    return msg.get("result") or {}
                if time.time() >= deadline:
                    fail("cdp-error",
                         f"{method}: no reply for id 1 within {timeout:g}s")
    except ControlError:
        raise                       # the protocol answered; not a transport hiccup
    except Exception as e:                                     # noqa: BLE001
        raise ControlError("cdp-error", f"{method}: {e}") from e
    raise ControlError("cdp-error",
                       f"{method}: did not answer within {timeout:g}s")


def call(ws_url: str, method: str, params: dict | None = None,
         timeout: float = 15.0) -> dict:
    """One CDP method call, returning the protocol's own `result`."""
    if websockets is None:
        fail("no-websockets",
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
        fail("js-error", f"Runtime.evaluate: {text.splitlines()[0]}")
    value = (result.get("result") or {}).get("value")
    try:
        size = len(json.dumps(value))
    except (TypeError, ValueError):
        size = 0
    if size > EVAL_RESULT_CAP:
        fail("result-too-large",
             f"Runtime.evaluate answered {size} chars (cap {EVAL_RESULT_CAP}) "
             "— narrow the expression, or read the page with `tab text`")
    if isinstance(value, str):
        try:
            return json.loads(value)      # a page that answered JSON
        except (ValueError, TypeError):
            return value                  # a plain string
    return value                          # None for undefined: an answer


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
            raise ControlError(
                "cdp-error",
                f"Runtime.evaluate: a frame that is not JSON ({e})") from e
        if msg.get("id") == rid:
            err = msg.get("error")
            if err:
                fail("cdp-error",
                     f"Runtime.evaluate: {err.get('message')} "
                     f"(code {err.get('code')})")
            return _value_of(msg.get("result") or {})
        if time.time() >= deadline:
            raise TimeoutError(f"no reply for id {rid} within {budget:g}s")


async def _evaluate(ws_url: str, expression: str, timeout: float) -> Any:
    try:
        async with websockets.connect(ws_url, max_size=2 ** 24,
                                      open_timeout=10) as ws:
            # one send: an expression may WRITE (a nav assignment, a form
            # submit), so there is no retry — the caller's read-back is the
            # recovery
            try:
                return await _sample(ws, 1, expression, timeout)
            except TimeoutError as e:
                raise ControlError("eval-timeout",
                                   f"Runtime.evaluate: {e}") from e
    except ControlError:
        raise
    except Exception as e:                                     # noqa: BLE001
        raise ControlError("cdp-error", f"Runtime.evaluate: {e}") from e
    raise ControlError("cdp-error", "Runtime.evaluate: no answer")


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
        async with websockets.connect(ws_url, max_size=2 ** 24,
                                      open_timeout=10) as ws:
            deadline = time.time() + timeout
            while True:
                samples += 1
                try:
                    value = await _sample(ws, samples, expression,
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
        raise ControlError("cdp-error", f"Runtime.evaluate: {e}") from e
    raise ControlError("cdp-error", "Runtime.evaluate: no answer")


def evaluate(ws_url: str, expression: str, timeout: float = 15.0) -> Any:
    """Evaluate an expression on ONE tab's own connection, returning its value.

    `returnByValue`, so a page that answers JSON is decoded to the value it
    produced. Three failures stay apart, because a caller branches on them:
    the page THREW (`js-error`), the page did not answer within the budget
    (`eval-timeout`), and the transport itself failed (`cdp-error`).
    """
    if websockets is None:
        fail("no-websockets",
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
        fail("no-websockets",
             "the `websockets` package is required to speak CDP "
             "(pip install websockets)")
    return asyncio.run(_evaluate_until(_checked_ws(ws_url, "tab"), expression,
                                       accept, timeout, interval))
