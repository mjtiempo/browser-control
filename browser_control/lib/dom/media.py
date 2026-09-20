"""media — `tab media state|play|pause`: the page's own playback state.

There is no CDP method for playback; the oracle is the media element's own
`paused`/`time`, polled until it followed or the budget ran out.
"""
from __future__ import annotations

import json

from browser_control.lib import (
    browser as browser_lib,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib import (
    cdp,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib import dom as _pkg  # pyright: ignore[reportMissingImports]
from browser_control.lib.coerce import (  # pyright: ignore[reportMissingImports]
    as_float,
    as_int,
)
from browser_control.lib.dom.scripts import (  # pyright: ignore[reportMissingImports]
    MEDIA_ACTION_EXPR,
    MEDIA_STATE_EXPR,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_BAD_ARGS,
    ERR_MEDIA_BLOCKED,
    ERR_MEDIA_NOT_VERIFIED,
    ERR_NO_MEDIA,
    fail,
)
from browser_control.lib.poll import (  # pyright: ignore[reportMissingImports]
    POLL_NORMAL,
    poll,
)

MEDIA_TIMEOUT_S = 5.0       # how long a play/pause is given to take effect

def _playback_verdict(mode: str, before: dict, after: dict) -> tuple[bool, str]:
    """Did the play/pause take effect? Judged ONLY from the read-back.

    `play` is verified when the CLOCK MOVED — not merely when the element
    stopped reporting `paused`: measured, a media element with no source
    reports `paused: false` the moment `play()` is called and never plays a
    frame, which is exactly the overclaim this check exists to catch. `pause`
    is verified by the state the page reports.
    """
    if mode == "play":
        if as_float(after.get("time")) > as_float(before.get("time")):
            return True, "the clock advanced"
        if as_int(after.get("ready_state")) == 0:
            return False, ("the element has nothing to play (readyState 0: no "
                           "supported source)")
        return False, "the clock did not advance"
    if after.get("paused"):
        return True, "the element reports paused"
    return False, "the element is still playing"

def _media_reply(row: dict, tab_row: dict, state: dict, mode: str,
                 before: dict | None = None) -> dict:
    """The media reply: what the PAGE reports, plus the clock as evidence."""
    reply = {"ok": True, "mode": mode,
             "element": state.get("element"),
             "count": as_int(state.get("count")),
             "playing": bool(state.get("playing")),
             "paused": bool(state.get("paused")),
             "ended": bool(state.get("ended")),
             "muted": bool(state.get("muted")),
             "volume": as_float(state.get("volume")),
             "rate": as_float(state.get("rate")),
             "time": as_float(state.get("time")),
             "duration": as_float(state.get("duration")),
             "ready_state": as_int(state.get("ready_state")),
             "src": str(state.get("src") or ""),
             "tab": f"id:{tab_row['id']}",
             "browser": browser_lib.brief(row)}
    if before is not None:
        reply["time_before"] = as_float(before.get("time"))
        reply["advanced"] = as_float(state.get("time")) > as_float(before.get("time"))
    return reply

def _poll_media(session: cdp.Session, mode: str, before: dict,
                timeout: float = MEDIA_TIMEOUT_S) -> dict:
    """Poll the page's own playback state until it followed, or the deadline.

    A play() the browser REJECTED stops the poll at once: the reason (the
    autoplay policy, no supported source) is the answer, and waiting would
    only make the caller wait for it.
    """
    def probe() -> dict:
        got = session.evaluate(MEDIA_STATE_EXPR)
        return got if isinstance(got, dict) else {}

    def done(state: dict) -> bool:
        if not state.get("found"):
            return True
        verified, _why = _playback_verdict(mode, before, state)
        return verified or bool(state.get("error"))

    _attempts, state = poll(probe, timeout=timeout, interval=POLL_NORMAL,
                            accept=done)
    return state

def media(mode: str, index: int | None = None, tab: str = "",
          browser: str = "") -> dict:
    """`tab media state|play|pause`: read, start or stop the page's media.

    There is no CDP method for playback — the `Media` domain is experimental
    and event-only, and a media KEY is a toggle aimed at whichever session has
    focus (a background tab's audio, or nothing) — so this verb drives the
    ELEMENT and then VERIFIES what the page reports: a play that leaves
    `paused: true` refuses `media-not-verified`, a `play()` the browser rejects
    (the autoplay policy, no supported source) refuses `media-blocked` with the
    reason, and the state read is `tab media state`. `--index` picks among
    several players; without it the playing one — else the largest — is used,
    and `count` says how many the page has.
    """
    name = _pkg.mode_of(mode)
    if name not in ("state", "play", "pause"):
        fail(ERR_BAD_ARGS, f"tab media: MODE is state|play|pause, got {mode!r}")
    if index is not None and name == "state":
        fail(ERR_BAD_ARGS, "tab media state: --index is for play/pause")
    row, tab_row = _pkg._resolve(tab, browser, for_write=(name != "state"))
    with _pkg._session(row, tab_row) as session:
        before = session.evaluate(MEDIA_STATE_EXPR)
        before = before if isinstance(before, dict) else {}
        if not before.get("found"):
            fail(ERR_NO_MEDIA,
                 f"tab media: the page has no video or audio element "
                 f"({as_int(before.get('count'))} found)")
        if name == "state":
            return _media_reply(row, tab_row, before, name)
        # `__MODE__` here is play|pause, NOT the matcher's text|selector, so
        # this expression is filled directly (and -1 means "the preferred one")
        session.evaluate(
            MEDIA_ACTION_EXPR.replace("__MODE__", json.dumps(name))
            .replace("__INDEX__",
                     str(-1 if index is None else as_int(index))))
        after = _poll_media(session, name, before)
    if not after.get("found"):
        fail(ERR_NO_MEDIA, f"tab media {name}: the element left the page")
    verified, why = _playback_verdict(name, before, after)
    if not verified:
        if after.get("error"):
            fail(ERR_MEDIA_BLOCKED,
                 f"tab media {name}: the page refused: {after['error']}" + (
                     " — if that is the autoplay policy, a real gesture is "
                     "needed first (`tab click` the player)"
                     if "interact" in str(after.get("error")) else ""))
        fail(ERR_MEDIA_NOT_VERIFIED, f"tab media {name}: {why}")
    return _media_reply(row, tab_row, after, name, before)
