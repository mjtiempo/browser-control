"""text_input — press, insert, type: real key events into a page.

The secret rule lives here: a text a verb PROVES is a secret is marked for
the audit log, and a focus whose type cannot be read fails closed.
"""
from __future__ import annotations

import time

from browser_control.lib import (
    audit,  # pyright: ignore[reportMissingImports]
    cdp,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib import (
    browser as browser_lib,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib import dom as _pkg  # pyright: ignore[reportMissingImports]
from browser_control.lib.coerce import (  # pyright: ignore[reportMissingImports]
    as_int,
)
from browser_control.lib.dom.keys import (  # pyright: ignore[reportMissingImports]
    KEYS,
    key_event,
    typed_events,
)
from browser_control.lib.dom.result import (
    Verdict,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib.dom.scripts import (  # pyright: ignore[reportMissingImports]
    TEXT_TARGET_EXPR,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_BAD_ARGS,
    ERR_NO_FOCUS,
    fail,
)

TYPE_PAUSE_S = 0.008        # between keystrokes: the page's handlers need air

def _text_target(session: cdp.Session) -> dict:
    """What the DOM focus is right now (see `TEXT_TARGET_EXPR`).

    A probe that cannot answer is reported as an UNREADABLE focus, which the
    caller treats as a secret (fail closed) and as an oracle that cannot
    judge.
    """
    try:
        data = session.evaluate(TEXT_TARGET_EXPR)
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
    a `type=password` attribute all mean YES.
    """
    return bool(probe.get("secret") or probe.get("frame")
                or probe.get("unreadable"))

def _text_verdict(before: dict, after: dict, chars: int) -> Verdict:
    """(verified, why) for a text verb.

    True  — the field is readable and its text grew: the text landed.
    False — the field is readable and did NOT change: it did not land.
    None  — the oracle cannot judge (a frame, a canvas, an unreadable value,
            or a focus that moved): an unclear oracle is not proof of absence.
    """
    if after.get("frame") or before.get("frame") \
            or after.get("length") is None:
        return Verdict(None, "the focused field is not readable from this document")
    if after.get("target") != before.get("target"):
        return Verdict(None, "the focus moved while the text was being written")
    grew = as_int(after.get("length")) - as_int(before.get("length"))
    if grew > 0:
        return Verdict(True, f"the field grew by {grew} character(s) for {chars}")
    return Verdict(False, "the focused field did not change")

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
    row, tab_row = _pkg._resolve(tab, browser, for_write=True)
    with _pkg._session(row, tab_row) as session:
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
    """Refuse a text write with nowhere to go; mark a PROVEN secret.

    `Input.insertText` into no focus is a silent no-op, and a click target that
    takes no text is the same trap one step further — both are refusals that
    name the fix rather than writes nobody can see.
    """
    before = _text_target(session)
    if not before.get("focused"):
        fail(ERR_NO_FOCUS,
             f"{verb}: nothing is focused, so there is nowhere to put the text "
             "— run `tab focus TEXT` first")
    if not before.get("editable"):
        fail(ERR_NO_FOCUS,
             f"{verb}: the focus is on {before.get('active')!r}, which takes "
             "no text — run `tab focus TEXT` first")
    if _is_secret(before):
        audit.LOG.mark_secret(text)   # fail closed: a secret, or unreadable
    return before

def _text_reply(row: dict, tab_row: dict, before: dict, after: dict,
                text: str, rung: str) -> dict:
    """The reply `insert` and `type` share, from the read-back verdict."""
    verified, why = _text_verdict(before, after, len(text))
    if verified is not None and not verified:
        fail(f"{rung}-not-verified",
             f"{why}: {before.get('active')!r} did not take the "
             f"{len(text)} character(s)")
    reply = {"ok": True, "verb": rung, "chars": len(text),
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
    row, tab_row = _pkg._resolve(tab, browser, for_write=True)
    with _pkg._session(row, tab_row) as session:
        before = _preflight(session, value, "tab insert")
        session.call("Input.insertText", {"text": value})
        after = _text_target(session)
    return _text_reply(row, tab_row, before, after, value, "insert")

def type_text(text: str, tab: str = "", browser: str = "") -> dict:
    """`tab type`: type TEXT as REAL per-character key events.

    Three events per character (keyDown, char, keyUp) on ONE connection, for a
    page whose handlers listen per key (autocomplete, validation, masked
    inputs). `tab insert` is the rung to try first — this one is slower and
    exists for the pages where it is the only thing that works. A newline in
    TEXT becomes an Enter key press; a character outside the Latin-1 range
    still rides the `char` event even if its virtual key code means nothing.
    """
    value = str(text or "")
    if not value:
        fail(ERR_BAD_ARGS, "tab type: TEXT is required")
    row, tab_row = _pkg._resolve(tab, browser, for_write=True)
    with _pkg._session(row, tab_row) as session:
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
            time.sleep(TYPE_PAUSE_S)      # the page's handlers need air
        after = _text_target(session)
    return _text_reply(row, tab_row, before, after, value, "type")
