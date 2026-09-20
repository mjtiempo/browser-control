"""keys — the key triples CDP takes, in one table.

A wrong triple SILENTLY does nothing in the page, which is why `press`,
`insert`, `type` and `select` all read from here.
"""
from __future__ import annotations

KEYS = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, ""),
    "escape": ("Escape", "Escape", 27, ""),
    "backspace": ("Backspace", "Backspace", 8, ""),
    "delete": ("Delete", "Delete", 46, ""),
    "space": (" ", "Space", 32, " "),
    "arrowup": ("ArrowUp", "ArrowUp", 38, ""),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, ""),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, ""),
    "arrowright": ("ArrowRight", "ArrowRight", 39, ""),
    "home": ("Home", "Home", 36, ""),
    "end": ("End", "End", 35, ""),
    "pageup": ("PageUp", "PageUp", 33, ""),
    "pagedown": ("PageDown", "PageDown", 34, ""),
}


def key_event(name: str, kind: str = "keyDown", *,
              with_text: bool = True) -> dict:
    """One key event as CDP takes it, from the ONE key table.

    `with_text=False` is a deliberate variant, not an oversight: a `keyDown`
    without `text` does NOT submit a form (measured), so `type`'s newline and
    `select`'s arrows ask for it explicitly while `press` takes the text the
    table declares.
    """
    key, code, vk, text = KEYS[name]
    event = {"type": kind, "key": key, "code": code,
             "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
    if with_text and text and kind in ("keyDown", "char"):
        event["text"] = text
        event["unmodifiedText"] = text
    return event


def typed_events(char: str) -> list[dict]:
    """The three events one typed CHARACTER sends (keyDown, char, keyUp).

    A character outside the Latin-1 range still rides the `char` event even
    if its virtual key code means nothing — the browser does the same.
    """
    vk = ord(char)
    base = {"key": char, "code": char, "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk}
    return [{"type": "keyDown", **base},
            {"type": "char", "text": char, **base},
            {"type": "keyUp", **base}]
