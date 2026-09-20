"""coerce — page-supplied values turned into the type a verb needs.

Every value here crossed `Runtime.evaluate` or a `/json` reply, so the page —
not this tool — chose it. A value that is not the shape a verb expected yields
the caller's default or an empty container, never a `TypeError` out of a verb:
the page owns the value, and a refusal the caller can name is the only honest
answer.
"""
from __future__ import annotations

__all__ = ["as_float", "as_int", "as_ints", "as_list"]


def as_int(value: object, default: int = 0) -> int:
    """An integer the page reported, or `default` when it is not one."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_float(value: object, default: float = 0.0) -> float:
    """A float the page reported, or `default` when it is not one."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_ints(value: object, count: int = 0) -> list[int]:
    """A numeric list from the page, or [] — never a TypeError out of a verb.

    A value that is not a list at all is []: iterating a dict would have
    produced its KEYS as numbers, which is worse than nothing.
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
