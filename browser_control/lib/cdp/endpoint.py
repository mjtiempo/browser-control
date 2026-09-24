"""endpoint — where the browser is: the port file, capped HTTP JSON, reachability.

The endpoint is not assumed: the browser is launched with
`--remote-debugging-port=0`, so the OS picks the port and the browser writes
it into its profile's `DevToolsActivePort`. Every call reads that file — a
port file alone is not proof, the port must answer. `listener_of` (the
kernel's answer to "who owns that port") lives in `lib/proc.py` and is
re-exported by the package.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
import stat
import threading
import urllib.request
from typing import Any

from browser_control.lib.errors import (
    ERR_CDP_ERROR,
    ERR_CDP_NOT_LOCAL,
    ERR_CDP_UNREACHABLE,
    ERR_RESULT_TOO_LARGE,
    ControlError,
    fail,
)

PORT_FILE = "DevToolsActivePort"

# The port file is ONE line from a browser this tool started: a few decimal
# digits and the browser's own websocket path. Anything bigger is not that
# answer, and reading it would be reading whatever a profile-writer chose.
PORT_FILE_CAP = 64

GET_CAP = 8 * 1024 * 1024

GET_DEADLINE_S = 5.0

# After the socket is shut down, how long the reader gets to come back before
# the refusal is raised without it: SHUT_RDWR wakes a blocked `recv` at once,
# so this only covers the thread handoff, never a peer's next byte.
CLOSE_GRACE_S = 0.5

def _small_file(path: str, cap: int) -> bytes:
    """At most `cap` bytes of a REGULAR file this process did not write.

    Every guard here is load-bearing, because the PATH belongs to another
    process: a FIFO planted at `<profile>/DevToolsActivePort` blocked the read
    forever, and a symlink to `/dev/zero` made `readline()` grow until it died
    (both reproduced) — a profile directory is writable by whoever planted it.
    `O_NOFOLLOW` refuses the link, `O_NONBLOCK` keeps a FIFO from blocking the
    OPEN (a reader-less FIFO blocks in `open`, before any read), and the
    `fstat` refuses anything that is not a small regular file. `b""` is "no
    answer", which is what the callers report: a typed refusal is not wanted
    for a file that merely is not a port.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return b""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            return b""
        return os.read(fd, cap)
    except OSError:
        return b""
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)

def port_of(profile: str) -> int:
    """The port the browser wrote into its profile, 0 when there is none.

    0 is "no answer": a stale file from a dead run, a FIFO, a symlink, a
    device and a browser that never started all look the same here, and the
    caller decides. The read itself is `_small_file`'s — the file is chosen by
    another process, so it is read the way any such file must be.
    """
    raw = _small_file(os.path.join(profile, PORT_FILE), PORT_FILE_CAP)
    try:
        value = int(raw.split(b"\n", 1)[0].strip())
    except ValueError:
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
        fail(ERR_CDP_NOT_LOCAL,
             f"{getattr(req, 'full_url', '?')}: refused a redirect "
             f"({code}) to {newurl}")

def _read_body(r: Any, chunks: list[bytes],
               errors: list[BaseException]) -> None:
    """One body read whose OUTCOME is recorded instead of raised.

    This is a thread target, so an exception has nowhere to go but
    `threading.excepthook`: a raw traceback on stderr, which breaks the CLI's
    contract that a run prints `ERR[code]: message` and nothing else. The
    bytes and the exception both come back through these two lists.

    A body that STOPPED early is recorded here too. The response knows how much
    of its `Content-Length` never arrived — `r.length` counts down as bytes do
    — and a leftover is proof the answer was cut off, which the parser cannot
    see: what did arrive may be perfectly good JSON up to the cut, so a
    truncated body used to be reported `cdp-error` ("answered no JSON") for
    what is a failure to answer. Chunked bodies are exempt: `http.client`
    raises for a missing terminal chunk by itself.

    An answer that ended before its BODY began is the same class. A peer that
    stops after the status line, or inside the header block, leaves a response
    with no `Content-Length`, no chunking and an empty body: `http.client`'s
    header parser treats EOF as the end of the block, so nothing is raised and
    the parser below saw a body of `b""` — "answered no JSON" again, for an
    endpoint that had stopped answering (a review measured it). Unframed AND
    bodyless is therefore recorded here. A COMPLETE body is not: an empty body
    the response itself framed (`Content-Length: 0`) and a body that arrived
    but is not JSON both stay `cdp-error`.

    A read that hit its OWN ceiling is not that case either: `GET_CAP + 1`
    bytes came back, so whatever `length` still says is the cap talking, and
    `_get_bytes` refuses it as `result-too-large` — not as a peer that stopped
    early.
    """
    try:
        data = r.read(GET_CAP + 1)
    except BaseException as e:                                 # noqa: BLE001
        errors.append(e)
        return
    chunks.append(data)
    if len(data) > GET_CAP:
        return
    if getattr(r, "chunked", False):
        return
    left = getattr(r, "length", None)
    if isinstance(left, int):
        if left > 0:
            errors.append(http.client.IncompleteRead(data, left))
        return
    if not data:
        errors.append(http.client.RemoteDisconnected(
            "the endpoint stopped answering: its reply ended before a byte "
            "of body arrived"))

def _read_outcome(url: str, chunks: list[bytes], errors: list[BaseException],
                  timed_out: bool, grace: float = 0.0) -> bytes:
    """What a read's outcome means: the body, or the refusal it earns.

    One place decides for all three, because deciding separately got one of
    them wrong: a reader that died mid-body (a reset, a peer that hangs up
    early) left `chunks` empty, `b""` was parsed as a body, and the endpoint
    was reported `cdp-error` — "answered no JSON" — for what is a failure to
    ANSWER at all. That is the wrong CLASS, the one callers branch on
    (`readback._require_tab_list_readable` separates it from a malformed
    body). A reader still running at the deadline, a reader that died, and a
    body that stopped short of its own length are all `cdp-unreachable`, with
    the recorded reason.

    `grace` is the extra time the call RESERVED past `GET_DEADLINE_S` to
    abandon the blocked reader ("" when it reserved none), and the timeout
    refusal states it: the deadline is what the caller budgeted, so the bound
    the call actually enforced is named rather than implied.
    """
    if timed_out:
        fail(ERR_CDP_UNREACHABLE,
             f"{url}: the endpoint did not finish answering within "
             f"{GET_DEADLINE_S:g}s"
             + (f" (plus up to {grace:g}s to abandon the blocked reader)"
                if grace else ""))
    if errors:
        fail(ERR_CDP_UNREACHABLE, f"{url}: {errors[0]}")
    return chunks[0] if chunks else b""

def _watched_opener(sockets: list[Any]) -> Any:
    """An opener whose HTTP connection RECORDS its socket as it dials.

    The deadline has to reach a peer that dribbles the STATUS LINE, and at that
    moment there is no response to ask for a socket: `urllib` builds the
    connection inside its own `do_open`. Subclassing the connection is the one
    hook that sees it — the socket lands in `sockets` the moment `connect()`
    returns, so the caller can shut it down while the headers are still coming
    in. Nothing else about the request changes.
    """
    class _Watched(http.client.HTTPConnection):
        def connect(self) -> None:
            super().connect()
            sockets.append(self.sock)

    class _Handler(urllib.request.HTTPHandler):
        def http_open(self, req: Any) -> Any:
            return self.do_open(_Watched, req)

    return _Handler()

def _sockets_of(r: Any, sockets: list[Any]) -> list[Any]:
    """Every socket the read may be blocked ON.

    The recorded connection socket is the same object the response wraps
    (`r.fp` is a `BufferedReader` over a `SocketIO` holding it), so the
    recorded ones are used as they are; the response-side probes are the
    fallback for a transport that built its response some other way, and a
    fake response in a suite simply yields none — the deadline then has
    nothing to shut down, which is not an error.
    """
    if sockets:
        return list(sockets)
    sock = getattr(getattr(getattr(r, "fp", None), "raw", None), "_sock", None)
    if sock is None:
        sock = getattr(r, "sock", None)
    return [] if sock is None else [sock]

def _shutdown(sockets: list[Any]) -> None:
    """Half-close what a blocked reader is waiting on, so it can let go.

    This is the fix for the deadline that did not hold: `shutdown(SHUT_RDWR)`
    makes the pending `recv` return instead of waiting for a peer's next byte,
    which releases the `BufferedReader` lock the reader holds — and it is that
    lock that made `response.close()` on the CALLER's thread wait for a peer
    that drips (measured: hung past 40 s under a 5 s budget). A socket that is
    already gone, or a fake transport with none, is not a failure here: the
    deadline is the answer either way.
    """
    for sock in sockets:
        with contextlib.suppress(OSError, AttributeError, ValueError):
            sock.shutdown(socket.SHUT_RDWR)

def _get_bytes(url: str) -> bytes:
    """The body at `url`, capped, or a refusal — never a bare exception.

    Three hardening rules live here. The opener carries an EMPTY ProxyHandler:
    `build_opener` keeps urllib's environment-driven default otherwise, so
    `http_proxy` diverted this request — whose answer is parsed as CDP JSON —
    off loopback, to whoever answered the proxy (a review flagged it; measured
    with a fake proxy that answered for a dead port). The opener also records
    the connection's socket, because the wall-clock deadline has to be able to
    reach a peer that dribbles the HEADERS as well as the body.

    And the WHOLE request runs on one worker thread that this thread waits for
    with `GET_DEADLINE_S`: `timeout=5` bounds one socket operation, not a peer
    that drips a byte every four seconds, and reading in the caller cannot be
    interrupted. At the deadline the recorded socket is shut down — which is
    what makes the worker's blocked read return, so the worker closes the
    response itself and no fd is left behind — and the refusal is built from
    the worker's recorded outcome. That split is also what keeps `close()` off
    this thread: the caller never touches the response, so it can never wait
    for a `BufferedReader` lock a reader is still holding. The reader's own
    exception is recorded, not raised (`_read_body`), so nothing reaches
    `threading.excepthook`.

    The deadline is what it says it is. `CLOSE_GRACE_S` is spent ONLY when
    there was a socket to shut down and the worker had not come back after it:
    waiting it out unconditionally charged every deadline a grace nobody
    could use — a worker blocked inside `connect` has recorded no socket, so
    nothing wakes it and the wait was pure overshoot (measured: 1.00 s for a
    0.5 s deadline). A socket shut down here wakes its blocked read at once,
    so the join returns as soon as the worker unwinds; only a worker that
    STILL has not come back reserves the grace, and `_read_outcome` then names
    that bound in the refusal.
    """
    chunks: list[bytes] = []
    errors: list[BaseException] = []
    refusals: list[ControlError] = []
    sockets: list[Any] = []
    opened: list[Any] = []
    timed_out = False
    grace = 0.0

    def fetch() -> None:
        """One request and its whole body, off the caller's thread."""
        try:
            # loopback by construction; redirects and proxies are refused
            # rather than followed or consulted (semgrep: ignore)
            opener = urllib.request.build_opener(   # noqa: S310
                _LoopbackOnly, urllib.request.ProxyHandler({}),
                _watched_opener(sockets))
            with opener.open(url, timeout=5) as r:
                opened.append(r)
                _read_body(r, chunks, errors)
        except ControlError as e:
            # A refusal raised INSIDE the request — `_LoopbackOnly` refusing a
            # redirect is the one that happens here — is this library's own
            # answer ABOUT the endpoint, and it keeps its code: carrying it as
            # a transport error reported `cdp-unreachable` for a peer that
            # answered perfectly well (the redirect check caught exactly that).
            refusals.append(e)
        except BaseException as e:                             # noqa: BLE001
            errors.append(e)

    try:
        worker = threading.Thread(target=fetch, daemon=True)
        try:
            worker.start()
        except RuntimeError as e:
            fail(ERR_CDP_UNREACHABLE,
                 f"{url}: cannot start a reader thread ({e})")
        worker.join(GET_DEADLINE_S)
        timed_out = worker.is_alive()
        if timed_out:
            # the worker's own response is the fallback when the connection
            # never recorded itself; a list read from here is safe either way
            targets = _sockets_of(opened[0] if opened else None, sockets)
            if targets:
                # something to wake: the shutdown releases the blocked read, so
                # the grace below is only for a worker that has not unwound yet
                _shutdown(targets)
                if worker.is_alive():
                    worker.join(CLOSE_GRACE_S)
                    grace = CLOSE_GRACE_S
    except ControlError:
        raise
    except Exception as e:                                     # noqa: BLE001
        fail(ERR_CDP_UNREACHABLE, f"{url}: {e}")
    if refusals:
        # a decision the request itself reached outranks the deadline reading
        # above: `timed_out` is sampled while the worker still runs, so a
        # recorded refusal means it finished with an answer of its own
        raise refusals[0]
    body = _read_outcome(url, chunks, errors, timed_out, grace)
    if len(body) > GET_CAP:
        fail(ERR_RESULT_TOO_LARGE,
             f"{url}: the endpoint answered more than {GET_CAP} bytes — "
             "that is not a CDP reply")
    return body

def _get_port(port: int, path: str) -> Any:
    """GET one CDP JSON endpoint on an explicit loopback port."""
    body = _get_bytes(f"http://127.0.0.1:{port}{path}")
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError as e:
        fail(ERR_CDP_ERROR, f"{path}: the endpoint answered no JSON: {e}")

def get_json(profile: str, path: str) -> Any:
    """GET one CDP JSON endpoint on the profile's own loopback port."""
    port = port_of(profile)
    if not port:
        fail(ERR_CDP_UNREACHABLE,
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
