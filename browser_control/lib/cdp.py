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
import contextlib
import json
import os
import threading
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
# One WALL-CLOCK budget for a CDP HTTP read, not one per socket operation: a
# peer that drips a byte every four seconds kept the per-operation timeout from
# ever firing (a review flagged it).
GET_DEADLINE_S = 5.0


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


class _LoopbackOnly(urllib.request.HTTPRedirectHandler):
    """A CDP endpoint answers where it answers: a redirect is REFUSED.

    The URL is built from the port file, so it is loopback by construction —
    but `urlopen` follows a 3xx by default, and a local process that owns that
    port could send this request (whose answer is parsed as CDP JSON) anywhere.
    The websocket has been host-checked from the start; the HTTP read was not
    (a review flagged it).
    """

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        fail("cdp-not-local",
             f"{getattr(req, 'full_url', '?')}: refused a redirect "
             f"({code}) to {newurl}")


def _get_bytes(url: str) -> bytes:
    """The body at `url`, capped, or a refusal — never a bare exception.

    Two hardening rules live here. The opener carries an EMPTY ProxyHandler:
    `build_opener` keeps urllib's environment-driven default otherwise, so
    `http_proxy` diverted this request — whose answer is parsed as CDP JSON —
    off loopback, to whoever answered the proxy (a review flagged it; measured
    with a fake proxy that answered for a dead port). And the read runs under
    a wall-clock deadline: `timeout=5` bounds one socket operation, not a peer
    that drips a byte every four seconds.
    """
    body = b""
    chunks: list[bytes] = []
    try:
        # loopback by construction; redirects and proxies are refused rather
        # than followed or consulted (semgrep: ignore)
        opener = urllib.request.build_opener(   # noqa: S310
            _LoopbackOnly, urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=5) as r:
            # a reader thread plus a join bounds the TOTAL time: reading in the
            # caller cannot be interrupted, and `http.client` loops internally
            # under the per-operation timeout, so a drip-feeding endpoint held
            # a verb open indefinitely (a review flagged it)
            reader = threading.Thread(
                target=lambda: chunks.append(r.read(GET_CAP + 1)),
                daemon=True)
            reader.start()
            reader.join(GET_DEADLINE_S)
            if reader.is_alive():
                fail("cdp-unreachable",
                     f"{url}: the endpoint did not finish answering within "
                     f"{GET_DEADLINE_S:g}s")
            body = chunks[0] if chunks else b""
    except ControlError:
        raise
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


def _proc_text(pid: str, name: str) -> str:
    """One /proc file of a pid as text, or "" — argv NULs PRESERVED.

    Chrome rewrites a child's cmdline in place (spaces between entries, one
    trailing NUL) while the main process keeps real argv boundaries; callers
    handle both forms. Flattening here made `--user-data-dir` containing a
    space lose its tail in the ownership check (a review flagged it).
    """
    try:
        with open(f"/proc/{pid}/{name}", "rb") as handle:
            return handle.read().decode("utf-8", "replace").rstrip("\0\n")
    except OSError:
        return ""


def _exe_basename(pid: str) -> str:
    """The executable a pid is running, by name, or ""."""
    try:
        return os.path.basename(os.path.realpath(f"/proc/{pid}/exe"))
    except OSError:
        return ""


def _listening_inodes(port: int) -> set[str]:
    """The socket inodes LISTENING on that port, tcp4 and tcp6.

    `/proc/net/tcp` is a table: `sl local_address rem_address st … inode`, with
    the port in HEX and `0A` meaning LISTEN.
    """
    wanted = f"{port:04X}"
    inodes: set[str] = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(name) as handle:
                next(handle, "")            # the header line
                for line in handle:
                    fields = line.split()
                    if len(fields) < 10 or fields[3] != "0A":
                        continue
                    if fields[1].rpartition(":")[2] != wanted:
                        continue
                    inodes.add(fields[9])
        except OSError:
            continue
    return inodes


def listener_of(port: int) -> dict:
    """Which process LISTENS on that port: {pid, exe, cmd} — or {}.

    The port came from a FILE (`DevToolsActivePort`), and a file can be stale
    or its port can be taken by something else. This asks the KERNEL instead:
    the listening socket's inode from `/proc/net/tcp{,6}`, then the process
    holding that inode through `/proc/<pid>/fd`. A few milliseconds, which is
    why the caller memoises.

    A pid whose fd table cannot be read is skipped rather than guessed at: the
    answer is either the process holding the socket or nothing.
    """
    if not port:
        return {}
    marks = {f"socket:[{inode}]" for inode in _listening_inodes(port)}
    if not marks:
        return {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return {}
    for entry in entries:
        if not entry.isdigit():
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            handles = os.listdir(fd_dir)
        except OSError:
            continue
        for handle in handles:
            try:
                if os.readlink(f"{fd_dir}/{handle}") in marks:
                    return {"pid": int(entry), "exe": _exe_basename(entry),
                            "cmd": _proc_text(entry, "cmdline")}
            except OSError:
                continue
    return {}


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


def frame_rows(port: int) -> list[dict]:
    """The IFRAME targets of the browser on this port, id-sorted.

    A CROSS-ORIGIN frame is a target of its own: its own websocket, its own
    coordinate space, its own execution. That is what makes every page verb
    work inside it unchanged (measured: a real click dispatched on that session
    fires the frame's own handler). A same-process frame has no such target, and
    the DOM layer says so rather than pretending (`tab frames`).
    """
    return _of_kind(_get_port(port, "/json"), "iframe")


def frame_targets(port: int) -> list[dict]:
    """The IFRAME targets of this browser, EACH WITH THE TAB THAT OWNS IT.

    `/json` lists iframe targets without their parent, so a frame matched by
    URL alone can belong to ANOTHER tab — measured: two tabs embedding the same
    widget both resolved to ONE target, and `--frame` then typed into the tab
    nobody asked about. The browser endpoint's `Target.getTargets` carries
    `parentId` for an iframe: the owning tab's target id, which is the proof
    this returns. `[]` means the browser did not say (or would not answer), and
    the caller then trusts only a URL that is unique in the whole browser.
    """
    version = _get_port(port, "/json/version")
    url = str((version or {}).get("webSocketDebuggerUrl") or "") \
        if isinstance(version, dict) else ""
    if not url:
        return []
    try:
        result = call(_checked_ws(url), "Target.getTargets")
    except ControlError:
        return []
    infos = result.get("targetInfos") if isinstance(result, dict) else None
    if not isinstance(infos, list):
        return []
    out: list[dict] = []
    for info in infos:
        if not isinstance(info, dict) or str(info.get("type")) != "iframe":
            continue
        out.append({"id": str(info.get("targetId") or ""),
                    "url": str(info.get("url") or ""),
                    "title": str(info.get("title") or ""),
                    "parent": str(info.get("parentId") or ""),
                    "parent_frame": str(info.get("parentFrameId") or "")})
    return sorted(out, key=lambda row: row["id"])


def _of_kind(rows: Any, kind: str) -> list[dict]:
    """A `/json` reply -> the targets of one kind, id-sorted.

    Sorted by target id locally: CDP does not document `/json` order, so "the
    tabs" would otherwise follow an external array's order.
    """
    if not isinstance(rows, list):
        fail("cdp-error", "/json answered a shape this tool cannot read")
    found = [r for r in rows
             if isinstance(r, dict) and r.get("type") == kind and r.get("id")]
    return sorted(found, key=lambda r: str(r["id"]))


def _pages(rows: Any) -> list[dict]:
    """A `/json` reply -> its PAGE targets, id-sorted."""
    return _of_kind(rows, "page")


def target_ws(port: int, target_id: str, kind: str = "page") -> str:
    """The websocket of ONE target on `port`, host-checked.

    `kind="page"` is a tab; `kind="iframe"` is a cross-origin frame, which is
    a target of its own. Read from `/json` — whose rows carry it — so the
    connection is that target's own, not a re-resolution of a spec that may
    have moved since.
    """
    rows = _of_kind(_get_port(port, "/json"), kind)
    row = next((r for r in rows if str(r.get("id")) == str(target_id)), None)
    what = "tab" if kind == "page" else "frame"
    code = "no-page-tab" if kind == "page" else "no-frame"
    if row is None:
        fail(code, f"{what} {str(target_id)[:10]}… left the {what} list")
    ws = str(row.get("webSocketDebuggerUrl") or "")
    if not ws:
        fail(code, f"{what} {str(target_id)[:10]}… has no websocket endpoint")
    return _checked_ws(ws, what)


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
    try:
        host = (urllib.parse.urlparse(str(url)).hostname or "").lower()
    except ValueError as e:
        # a malformed endpoint (`ws://[::1/x`) raised a raw ValueError out of
        # every public transport call; it is a refusal like any other (a
        # review flagged it)
        fail("cdp-not-local", f"{where}: {url!r} is not a usable endpoint ({e})")
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
                        fail("cdp-error",
                             f"{method}: {_foreign(err.get('message'))} "
                             f"(code {_foreign(err.get('code'), 40)})")
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


def _foreign(text: object, cap: int = 200) -> str:
    """One bounded, escape-free line of text THIS TOOL did not write.

    A page or a browser can put newlines, terminal control sequences (OSC 52,
    CSI) or megabytes into an exception description, and the CLI prints
    refusal messages verbatim. Untrusted text is flattened and capped here
    rather than reaching the terminal raw; C0, DEL and C1 controls are
    dropped, ordinary Unicode text is kept (a review flagged the injection
    and the unbounded size).
    """
    line = " ".join(str(text or "").split())[:cap]
    return "".join(ch for ch in line
                   if ch >= " " and not "\x7f" <= ch <= "\x9f")


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
        fail("js-error", f"Runtime.evaluate: {_foreign(text)}")
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
        fail("result-too-large",
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
            raise ControlError("blocked", BLOCKED_HINT) from e
        except (ValueError, TypeError) as e:
            raise ControlError(
                "cdp-error",
                f"Page.enable: a frame that is not JSON ({e})") from e
        if msg.get("id") == rid:
            err = msg.get("error")
            if err:
                raise ControlError(
                    "cdp-error",
                    f"Page.enable: {err.get('message')} "
                    f"(code {err.get('code')})")
            return
        if time.time() >= deadline:
            raise ControlError("blocked", BLOCKED_HINT)


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
        if websockets is None:
            fail("no-websockets",
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
                    websockets.connect(self._ws_url, max_size=2 ** 24,
                                       open_timeout=10))
            except Exception as e:                             # noqa: BLE001
                self.close()
                raise ControlError("cdp-error",
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
                raise ControlError("cdp-error",
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
        return bool(parked_at) and time.time() >= parked_at

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
        await ws.send(json.dumps({"id": rid, "method": method,
                                  "params": params}))
        deadline = time.time() + budget
        parked_at = 0.0
        while True:
            wait = max(0.1, deadline - time.time())
            if parked_at:
                # a dialog this session SAW open parks the renderer: the reply
                # cannot come until somebody answers it, so waiting the whole
                # budget buys nothing but a slower refusal
                wait = min(wait, max(0.1, parked_at - time.time()))
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(),
                                                        timeout=wait))
            except TimeoutError as e:
                if self._parked_now(parked_at):
                    raise ControlError(
                        "blocked",
                        f"{method}: no reply — the renderer is parked by a "
                        f"JavaScript dialog{self._blocked_hint()}") from e
                code = ("eval-timeout" if method.startswith("Runtime.evaluate")
                        else "cdp-error")
                if self.parked:
                    # the tab was parked before this session opened: one code
                    # for "the renderer is not answering", whatever the call
                    code = "blocked"
                raise ControlError(
                    code, f"{method}: no reply within {budget:g}s"
                    f"{self._blocked_hint()}") from e
            except (ValueError, TypeError) as e:
                raise ControlError("cdp-error",
                                   f"{method}: a frame that is not JSON "
                                   f"({e})") from e
            if msg.get("id") == rid:
                err = msg.get("error")
                if err:
                    raise ControlError(
                        "cdp-error", f"{method}: {_foreign(err.get('message'))} "
                        f"(code {_foreign(err.get('code'), 40)})")
                return msg.get("result") or {}
            if msg.get("method"):
                self.events.append({"method": msg["method"],
                                    "params": msg.get("params") or {}})
                del self.events[:-20]
                if not parked_at and msg["method"] == DIALOG_EVENT:
                    parked_at = time.time() + DIALOG_GRACE_S
            if time.time() >= deadline:
                raise ControlError("cdp-error",
                                   f"{method}: no reply for id {rid} within "
                                   f"{budget:g}s{self._blocked_hint()}")

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
            raise ControlError("cdp-error", f"{method}: {e}") from e

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
            fail("js-error", f"Runtime.evaluate: {_foreign(text)}")
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
