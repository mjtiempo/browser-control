"""text_input — press, insert, type: real key events into a page.

The secret rule lives here: a text a verb PROVES is a secret is marked for
the audit log, and a focus whose type cannot be read fails closed.
"""
from __future__ import annotations

import math
import time

from browser_control.lib import (
    audit,
    cdp,
)
from browser_control.lib import (
    browser as browser_lib,
)
from browser_control.lib import dom as _pkg
from browser_control.lib.dom.keys import (
    KEYS,
    key_event,
    typed_events,
)
from browser_control.lib.dom.result import (
    Verdict,
)
from browser_control.lib.dom.scripts import (
    TEXT_TARGET_EXPR,
    fill,
)
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    ERR_INSERT_NOT_VERIFIED,
    ERR_NO_FOCUS,
    ERR_TYPE_NOT_VERIFIED,
    fail,
)
from browser_control.lib.text import (
    foreign,
)

TYPE_PAUSE_S = 0.008        # between keystrokes: the page's handlers need air

def _text_target(session: cdp.Session) -> dict:
    """What the DOM focus is right now (see `TEXT_TARGET_EXPR`).

    A probe that cannot answer is reported as an UNREADABLE focus, which the
    caller treats as a secret (fail closed) and as an oracle that cannot
    judge.
    """
    try:
        data = session.evaluate(fill(TEXT_TARGET_EXPR))
    except Exception:                                          # noqa: BLE001
        return {"focused": True, "frame": True, "editable": True,
                "secret": True, "active": None, "length": None,
                "unreadable": True}
    return data if isinstance(data, dict) else {
        "focused": True, "frame": True, "editable": True, "secret": True,
        "active": None, "length": None, "unreadable": True}

def _is_secret(probe: dict) -> bool:
    """Is the focused field a password (or one this page cannot tell us about)?

    Pure, so the policy is testable without a browser: unreadable, a frame, or
    a `type=password` attribute all mean YES — and so does any focus the probe
    reports as `secret` (it sets that for a value it cannot read, the same
    fail-closed rule).
    """
    return bool(probe.get("secret") or probe.get("frame")
                or probe.get("unreadable"))

def _length_delta(before: dict, after: dict) -> int | None:
    """How many characters the focused field gained, or None if unreadable."""
    first, second = before.get("length"), after.get("length")
    if isinstance(first, int) and isinstance(second, int):
        return second - first
    return None

def _text_verdict(before: dict, after: dict, chars: int) -> Verdict:
    """(verified, why) for a text verb.

    True  — the field is readable and its text grew by EVERY character asked
            for: the text landed.
    False — the probe PROVES there was nowhere for the text to go: this focus
            takes no text at all (nothing, the body, or the document element
            with nothing editable in it, is focused).
    None  — the oracle cannot judge: a frame, a canvas, an unreadable value, a
            focus that moved, a PARTIAL landing, a SHRINK that is not a
            shortfall, or a length that did not move at all. An unclear oracle
            is not proof of absence, and it is not proof of arrival either: a
            field that took 2 of 10 characters (a `maxlength`, an input filter)
            is exactly the overclaim this tri-state exists to prevent, so it is
            reported as unclear and NAMED rather than certified (a review
            measured `verified: true, chars: 10` on a `<input maxlength=4>`).

    A length that did NOT change is not proof the text missed: `Input.insertText`
    and per-character typing REPLACE the current selection, so a masked or
    fixed-length field can take every character and end the same length, and a
    SHRINK is text that landed OVER a selection — the old branch refused both
    while saying "did not change" about a field that went from 5 to 3 (a review
    measured it).

    The ORDER is part of the answer. A focus that MOVED is judged first: a write
    into a real field that then blurred to an empty focus may have landed, and
    "the focus takes no text" would be a claim the probe cannot support. What
    `False` means is narrower — the probe measured a focus that takes no text —
    so it stays the one PROVEN no-op, which is why the refusal codes that name
    it are still raised.
    """
    if after.get("target") != before.get("target"):
        return Verdict(None, "the focus moved while the text was being written")
    if after.get("editable") is False or before.get("editable") is False:
        return Verdict(False, "the focus takes no text — nothing, the body, or "
                              "the document element with nothing editable in "
                              "it is focused")
    if after.get("frame") or before.get("frame") \
            or after.get("length") is None:
        return Verdict(None, "the focused element is not readable from this "
                             "document (a frame, a canvas, or an element with "
                             "no readable value)")
    grew = _length_delta(before, after)
    if grew is None:
        return Verdict(None, "the focused element's text is not readable")
    if grew >= chars:
        return Verdict(True, f"the field grew by {grew} character(s) for {chars}")
    if grew > 0:
        return Verdict(None,
                       f"only {grew} of {chars} character(s) landed — the "
                       "field may be capped (maxlength) or filtering input")
    if grew < 0:
        return Verdict(None,
                       f"the field went from {before.get('length')} to "
                       f"{after.get('length')} characters — the text may have "
                       "REPLACED a selection; the oracle cannot tell")
    return Verdict(None, "the length did not change")

def press(key: str, tab: str = "", browser: str = "") -> dict:
    """`tab press`: one key event at the DOM focus (CDP `Input`).

    Enter submits a focused form, Tab moves on, Escape closes a widget — the
    page's own handlers run, exactly as if a finger pressed it. The DISPATCH is
    verified (an error envelope refuses); the effect belongs to the page, so the
    reply says `verified: false` and the caller reads the outcome with `tab
    text`, `tab info` or `tab js`.
    """
    name = str(key or "").strip().lower()
    if name not in KEYS:
        fail(ERR_BAD_ARGS, f"tab press: unknown key {key!r} "
                         f"(have: {', '.join(sorted(KEYS))})")
    key_name = KEYS[name][0]
    page = _pkg.Tab.open(tab, browser, for_write=True)
    row, tab_row = page.row, page.tab_row
    with page.session() as session:
        # a key WITH text goes as `keyDown` (the browser composes it); one
        # without goes as `rawKeyDown` — the same split the table implies
        down = "keyDown" if KEYS[name][3] else "rawKeyDown"
        session.call("Input.dispatchKeyEvent", key_event(name, down))
        session.call("Input.dispatchKeyEvent",
                     key_event(name, "keyUp", with_text=False))
    return {"ok": True, "key": key_name, "target": "page",
            "verified": False,
            "note": ("the key event was dispatched; read the effect with "
                     "`tab text`, `tab info` or `tab js`"),
            "tab": f"id:{tab_row['id']}", "browser": browser_lib.brief(row)}

def _preflight(session: cdp.Session, text: str, verb: str) -> dict:
    """Refuse a text write with NOWHERE to go; mark a secret, or a focus we
    cannot vouch for.

    An EMPTY focus (nothing, the body, the document element) is the one place
    the input provably lands nowhere, and that is what refuses. Every other
    real focus takes it: a frame's active element, a canvas, or an element the
    page drives with its own key handlers holds no readable `value`, and the
    read-back says `verified: false` for exactly that (`length: null`) rather
    than calling it "takes no text" — what cannot be read is the focus's TYPE,
    and the secret rule below fails closed on it.

    The probe's own `editable` is the second way in, and it is the PROBE's
    answer, not this verb's: a document left editable as a whole (`designMode`,
    or an editable `<body>`) focuses the body, so a probe that measured that
    focus as editable is accepted even though its active element is the body.
    Refusing it here would re-add the `no-focus` the probe is the authority on
    (a review found the probe measuring an editable body and this verb refusing
    it anyway).
    """
    before = _text_target(session)
    if not before.get("focused") and not before.get("editable"):
        fail(ERR_NO_FOCUS,
             f"{verb}: nothing is focused, so there is nowhere to put the text "
             "— run `tab focus TEXT` first")
    if _is_secret(before):
        audit.LOG.mark_secret(text)   # fail closed: a secret, or unreadable
    return before

def _text_reply(row: dict, tab_row: dict, before: dict, after: dict,
                text: str, rung: str) -> dict:
    """The reply `insert` and `type` share, from the read-back verdict.

    `inserted` is the raw DELTA the field reported (None when the oracle could
    not read a length at all): a PARTIAL landing, a SHRINK over a selection and
    a length that did not move are all UNCLEAR verdicts, so this is how a caller
    still sees what the field actually did. Only a verdict the probe PROVED
    fails (`_text_verdict` says which), and it names the rung's own code.
    """
    verified, why = _text_verdict(before, after, len(text))
    if verified is not None and not verified:
        # the rung names the code: both are REGISTERED in errors.CODES, so a
        # caller can branch on them (a built string would be invisible to the
        # vocabulary check)
        code = (ERR_INSERT_NOT_VERIFIED if rung == "insert"
                else ERR_TYPE_NOT_VERIFIED)
        fail(code, f"{why}: {foreign(before.get('active'), 60)!r} did not take "
                   f"the {len(text)} character(s)")
    reply = {"ok": True, "verb": rung, "chars": len(text),
             "inserted": _length_delta(before, after),
             "active": after.get("active") or before.get("active"),
             "verified": bool(verified),
             "length_before": before.get("length"),
             "length_after": after.get("length"),
             "tab": f"id:{tab_row['id']}",
             "browser": browser_lib.brief(row)}
    if verified is None:
        reply["note"] = why
    return reply

def insert(text: str, tab: str = "", browser: str = "") -> dict:
    """`tab insert`: insert TEXT at the DOM focus — ONE atomic input event.

    The durable rung: `Input.insertText` puts the whole string in in one call,
    the way an IME does, and the read-back judges whether the focused field
    grew. The text itself is NEVER echoed — a password's value would ride home
    in the reply — and a password (or an unreadable focus) marks the action log
    so the secret is written as a length.
    """
    value = str(text or "")
    if not value:
        fail(ERR_BAD_ARGS, "tab insert: TEXT is required")
    page = _pkg.Tab.open(tab, browser, for_write=True)
    row, tab_row = page.row, page.tab_row
    with page.session() as session:
        before = _preflight(session, value, "tab insert")
        session.call("Input.insertText", {"text": value})
        after = _text_target(session)
    return _text_reply(row, tab_row, before, after, value, "insert")

def type_text(text: str, tab: str = "", browser: str = "",
              delay_s: float | None = None) -> dict:
    """`tab type`: type TEXT as REAL per-character key events.

    Three events per character (keyDown, char, keyUp) on ONE connection, for a
    page whose handlers listen per key (autocomplete, validation, masked
    inputs). `tab insert` is the rung to try first — this one is slower and
    exists for the pages where it is the only thing that works. A newline in
    TEXT becomes an Enter key press; a character outside the Latin-1 range
    still rides the `char` event even if its virtual key code means nothing.

    `delay_s` is the pause BETWEEN keystrokes. The default (`TYPE_PAUSE_S`) is
    as fast as a page's handlers can take it, which is right for filling a
    field; a caller that wants a HUMAN cadence asks for one — a plugin typing
    the way a person does passes `12 / WPM` seconds (90 WPM is 0.133 s).
    """
    value = str(text or "")
    if not value:
        fail(ERR_BAD_ARGS, "tab type: TEXT is required")
    pause = TYPE_PAUSE_S
    if delay_s is not None:
        try:
            pause = float(delay_s)
        except (TypeError, ValueError):
            fail(ERR_BAD_ARGS,
                 f"tab type: delay_s must be seconds as a number, got "
                 f"{delay_s!r}")
    if not math.isfinite(pause) or pause < 0:
        fail(ERR_BAD_ARGS,
             f"tab type: delay_s must be a finite pause in seconds at least "
             f"0, got {delay_s!r}")
    page = _pkg.Tab.open(tab, browser, for_write=True)
    row, tab_row = page.row, page.tab_row
    with page.session() as session:
        before = _preflight(session, value, "tab type")
        for char in value:
            if char == "\n":
                # NO text on this keyDown, deliberately: that variant does not
                # submit a form, which is what `type "a\n"` promises
                session.call("Input.dispatchKeyEvent",
                             key_event("enter", "keyDown", with_text=False))
                session.call("Input.dispatchKeyEvent",
                             key_event("enter", "keyUp", with_text=False))
            else:
                for event in typed_events(char):
                    session.call("Input.dispatchKeyEvent", event)
            time.sleep(pause)             # the page's handlers need air
        after = _text_target(session)
    return _text_reply(row, tab_row, before, after, value, "type")
