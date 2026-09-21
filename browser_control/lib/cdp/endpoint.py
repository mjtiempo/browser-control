"""endpoint — where the browser is: the port file, capped HTTP JSON, reachability.

The endpoint is not assumed: the browser is launched with
`--remote-debugging-port=0`, so the OS picks the port and the browser writes
it into its profile's `DevToolsActivePort`. Every call reads that file — a
port file alone is not proof, the port must answer. `listener_of` (the
kernel's answer to "who owns that port") lives in `lib/proc.py` and is
re-exported by the package.
"""
from __future__ import annotations

import json
import os
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

GET_CAP = 8 * 1024 * 1024

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
        fail(ERR_CDP_NOT_LOCAL,
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
                fail(ERR_CDP_UNREACHABLE,
                     f"{url}: the endpoint did not finish answering within "
                     f"{GET_DEADLINE_S:g}s")
            body = chunks[0] if chunks else b""
    except ControlError:
        raise
    except Exception as e:                                     # noqa: BLE001
        fail(ERR_CDP_UNREACHABLE, f"{url}: {e}")
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
