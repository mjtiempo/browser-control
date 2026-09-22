"""coerce — page-supplied values turned into the type a verb needs.

Every value here crossed `Runtime.evaluate` or a `/json` reply, so the page —
not this tool — chose it. A value that is not the shape a verb expected yields
the caller's default or an empty container, never a `TypeError` out of a verb:
the page owns the value, and a refusal the caller can name is the only honest
answer.
"""
from __future__ import annotations

import math

__all__ = ["as_float", "as_int", "as_ints", "as_list"]


def as_int(value: object, default: int = 0) -> int:
    """An integer the page reported, or `default` when it is not a number.

    A number is a SPELLING, not a type: `window.innerWidth` is `1280.5` on a
    page whose zoom is not 100%, and `int(str(1280.5))` raises — so the whole
    value fell to the caller's default, and a page with a perfectly good
    viewport reported `[0, 0]`. `tab find`/`scroll`/`click`/`hover` then
    refused it as a windowless browser, `tab text` reported the false fact and
    `tab screenshot` refused on 0 px. Any FINITE numeric spelling is read as
    the integer it names instead; a value that is not a number at all — or a
    non-finite one, which `int()` cannot place — is still the caller's default.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        # not an integral spelling — `float` is the second reading, and only a
        # FINITE result is a number `int()` can place
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return default
        return int(number) if math.isfinite(number) else default


def as_float(value: object, default: float = 0.0) -> float:
    """A float the page reported, or `default` when it is not one.

    Python's own spelling of a float: `nan` and `inf` parse, and are handed on
    as they are — a caller that a size depends on checks `math.isfinite`
    (`screenshot`) rather than being handed a default that would read as fact.
    """
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_ints(value: object, count: int | None = None) -> list[int]:
    """A numeric list from the page, or [] — never a TypeError out of a verb.

    A value that is not a list at all is []: iterating a dict would have
    produced its KEYS as numbers, which is worse than nothing. `count` caps the
    list; None — the default — keeps every element, so "no limit" reads as a
    limit nobody set rather than a limit of zero.
    """
    if not isinstance(value, (list, tuple)):
        return []
    out = [as_int(item) for item in value]
    return out[:count] if count else out


def as_list(value: object) -> list:
    """A page-supplied list, or [] — never a TypeError out of a verb.

    For sequences of NAMES. A dict is not a list here: its keys would be read
    as labels.
    """
    return list(value) if isinstance(value, (list, tuple)) else []
