"""media — `tab media state|play|pause`: the page's own playback state.

There is no CDP method for playback; the oracle is the media element's own
`paused`/`time`, polled until it followed or the budget ran out.
"""
from __future__ import annotations

import json

from browser_control.lib import (
    browser as browser_lib,
)
from browser_control.lib import (
    cdp,
)
from browser_control.lib import dom as _pkg
from browser_control.lib.coerce import (
    as_float,
    as_int,
)
from browser_control.lib.dom.result import (
    Verdict,
)
from browser_control.lib.dom.scripts import (
    MEDIA_ACTION_EXPR,
    MEDIA_STATE_EXPR,
    fill,
)
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    ERR_MEDIA_BLOCKED,
    ERR_MEDIA_NOT_VERIFIED,
    ERR_NO_MEDIA,
    fail,
)
from browser_control.lib.poll import (
    POLL_NORMAL,
    poll,
)
from browser_control.lib.text import foreign

MEDIA_TIMEOUT_S = 5.0       # how long a play/pause is given to take effect

def _element_at(index: int) -> str:
    """How a refusal names the element the verb is about.

    `index` is the CONCRETE index a verb drives; `-1` is a read that could not
    resolve one (the page did not report it), which every message still has to
    be able to name.
    """
    return (f"the element at index {index}" if index >= 0
            else "the element the read picked")

def _identity_change(before: dict, now: dict, index: int) -> str:
    """How the element a media verb is about CHANGED under it, or "".

    The index a verb drives is the page's LIVE order: a page that re-sorts,
    inserts or replaces its players mid-verb (an ad slot, a re-rendered
    `<video>`) leaves the index pointing at a DIFFERENT element, and the new
    element's clock then "verified" an action aimed at the one before it — a
    review measured `play --index 0` certified by an ad that arrived during the
    poll. So the identity that has to survive the verb is the element's tag,
    its `src`, and the size of the list the index was taken from.
    """
    was_count, now_count = as_int(before.get("count")), as_int(now.get("count"))
    if was_count != now_count:
        return (f"the page's media list changed under the verb ({was_count} → "
                f"{now_count} element(s)), so {_element_at(index)} is no longer "
                "proven to be the one that was driven")
    was_tag = str(before.get("element") or "")
    now_tag = str(now.get("element") or "")
    if was_tag != now_tag:
        return (f"{_element_at(index)} is now "
                f"{foreign(now_tag, 20) or 'nothing'} (it was "
                f"{foreign(was_tag, 20)})")
    was_src, now_src = str(before.get("src") or ""), str(now.get("src") or "")
    # an element ACQUIRING its source (`<source>` children, `preload=none`) is
    # the SAME element loading, not a replacement: only a src that was already
    # committed and now reads differently is evidence that this is another
    # element — which is exactly the ad the review watched certify a `play`
    if was_src and was_src != now_src:
        return (f"{_element_at(index)} now plays "
                f"{foreign(now_src, 60) or 'nothing'} (it was "
                f"{foreign(was_src, 60)})")
    return ""

def _playback_verdict(mode: str, before: dict, after: dict) -> Verdict:
    """Did the play/pause take effect? Judged ONLY from the read-back.

    `play` is verified when the CLOCK MOVED — not merely when the element
    stopped reporting `paused`: measured, a media element with no source
    reports `paused: false` the moment `play()` is called and never plays a
    frame, which is exactly the overclaim this check exists to catch. `pause`
    is verified by the state the page reports.
    """
    if mode == "play":
        if as_float(after.get("time")) > as_float(before.get("time")):
            return Verdict(True, "the clock advanced")
        if as_int(after.get("ready_state")) == 0:
            return Verdict(False, ("the element has nothing to play "
                                  "(readyState 0: no supported source)"))
        return Verdict(False, "the clock did not advance")
    if after.get("paused"):
        return Verdict(True, "the element reports paused")
    return Verdict(False, "the element is still playing")

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
                index: int = -1, timeout: float = MEDIA_TIMEOUT_S) -> dict:
    """Poll the page's own playback state until it followed, or the deadline.

    A play() the browser REJECTED stops the poll at once: the reason (the
    autoplay policy, no supported source) is the answer, and waiting would
    only make the caller wait for it.

    The probe reads the SAME element the action drove: `index` is the CONCRETE
    index the before-read settled on, never the `-1` preferred rule — a probe
    that re-picked its own element certified a play by an unrelated
    already-playing player's clock (a review found it).
    """
    def probe() -> dict:
        # bound the probe by the budget the verb ADVERTISES: the session's
        # 15s default otherwise let `tab media play` take ~20s against its
        # own 5s deadline (a review flagged it)
        got = session.evaluate(_media_state_expr(index), timeout=timeout)
        return got if isinstance(got, dict) else {}

    def done(state: dict) -> bool:
        if not state.get("found"):
            return True
        verified, _why = _playback_verdict(mode, before, state)
        return verified or bool(state.get("error"))

    _attempts, state = poll(probe, timeout=timeout, interval=POLL_NORMAL,
                            accept=done)
    return state

def _media_state_expr(index: int) -> str:
    """The state read about ONE element: `index` >= 0 is `all[index]`.

    `-1` keeps the preferred rule (the playing element, else the largest) and
    is what `tab media state` reads, because that verb drives nothing. Every
    verb that ACTS passes the concrete index the before-read settled on, so the
    read-back cannot be about a different element than the action — see
    `media()`.
    """
    return fill(MEDIA_STATE_EXPR, index=str(index))

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
    and `count` says how many the page has. The read-back is ALWAYS about the
    element the action drove: WHICH element that is, is fixed BEFORE the action
    — the caller's `--index`, or the concrete index the preferred read settled
    on — and carried through the action and every probe, and the element there
    must still be the same one (tag, `src`, and the size of the list the index
    came from) or the verb refuses rather than certifying from another
    element's clock. An `--index` the page cannot satisfy refuses `bad-args`
    naming the count rather than quietly driving nothing.
    """
    name = _pkg.mode_of(mode)
    if name not in ("state", "play", "pause"):
        fail(ERR_BAD_ARGS, f"tab media: MODE is state|play|pause, got {mode!r}")
    if index is not None and name == "state":
        fail(ERR_BAD_ARGS, "tab media state: --index is for play/pause")
    picked = -1 if index is None else as_int(index)
    if index is not None and picked < 0:
        fail(ERR_BAD_ARGS,
             f"tab media {name}: --index names one of the page's players, so "
             f"it must be 0 or more, got {index!r}")
    page = _pkg.Tab.open(tab, browser, for_write=(name != "state"))
    row, tab_row = page.row, page.tab_row
    with page.session() as session:
        before = session.evaluate(_media_state_expr(picked))
        before = before if isinstance(before, dict) else {}
        # `--index N` the page cannot satisfy is a REFUSAL, not "the preferred
        # one": the action used to be a silent no-op (`found: false`, ignored)
        if index is not None and picked >= as_int(before.get("count")):
            fail(ERR_BAD_ARGS,
                 f"tab media {name}: --index {picked} is out of range: the "
                 f"page has {as_int(before.get('count'))} video/audio "
                 "element(s)")
        if not before.get("found"):
            fail(ERR_NO_MEDIA,
                 f"tab media: the page has no video or audio element "
                 f"({as_int(before.get('count'))} found)")
        # WHICH element this verb is about, fixed HERE from the read that
        # picked it: the caller's `--index`, or the CONCRETE index the
        # preferred rule settled on in this read. Threading the rule itself
        # (`-1`) let every probe re-pick, so the action and its read-back could
        # be about two different players — a `pause` the page really applied
        # was refused, a reply described `b.mp4` while `a.mp4` was the one
        # paused, and a `play` aimed at a dead element was certified by an
        # unrelated ad's clock (a review measured all three).
        held = picked if index is not None else as_int(before.get("index"), -1)
        if held < 0:
            # a page whose read did not say WHICH element it picked (our own
            # expression always does): keep the caller's value and lean on the
            # identity comparison below, which is what the refusal is judged on
            held = picked
        if name == "state":
            return _media_reply(row, tab_row, before, name)
        # `__MODE__` here is play|pause, NOT the matcher's text|selector, so
        # this expression is filled directly; it drives the SAME index the
        # read-back above and below reads
        action = session.evaluate(
            fill(MEDIA_ACTION_EXPR, action=json.dumps(name),
                 index=str(held)))
        action = action if isinstance(action, dict) else {}
        if not action.get("found"):
            # the element the read-back is about left between the two calls:
            # say so instead of polling an element that is no longer there
            fail(ERR_NO_MEDIA,
                 f"tab media {name}: {_element_at(held)} left the page before "
                 f"it could be driven ({as_int(action.get('count'))} "
                 "video/audio element(s) now)")
        changed = _identity_change(before, action, held)
        if changed:
            fail(ERR_MEDIA_NOT_VERIFIED, f"tab media {name}: {changed}")
        after = _poll_media(session, name, before, held)
    if not after.get("found"):
        fail(ERR_NO_MEDIA,
             f"tab media {name}: {_element_at(held)} left the page")
    # the element the ACTION drove is the element the read-back is about: a
    # page that put another one at that index cannot certify this verb
    changed = _identity_change(before, after, held)
    if changed:
        fail(ERR_MEDIA_NOT_VERIFIED, f"tab media {name}: {changed}")
    verified, why = _playback_verdict(name, before, after)
    if not verified:
        if after.get("error"):
            fail(ERR_MEDIA_BLOCKED,
                 f"tab media {name}: the page refused: "
                 f"{foreign(after['error'], 120)}" + (
                     " — if that is the autoplay policy, a real gesture is "
                     "needed first (`tab click` the player)"
                     if "interact" in str(after.get("error")) else ""))
        fail(ERR_MEDIA_NOT_VERIFIED, f"tab media {name}: {why}")
    return _media_reply(row, tab_row, after, name, before)
