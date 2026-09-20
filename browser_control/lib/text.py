"""text — one flattener for text THIS TOOL did not write.

A page, a browser or an argument list can put newlines (which would forge a
line in stderr or in the audit log), terminal control sequences (OSC 52, CSI)
and megabytes into a refusal message, and the CLI prints refusal messages
verbatim. Untrusted text is flattened and capped here rather than reaching the
terminal raw; C0, DEL and C1 controls are dropped, ordinary Unicode text is
kept (a review flagged the injection and the unbounded size).

One order, everywhere: split whitespace, slice to the cap, then drop control
characters. The order is `cdp._foreign`'s, and it matters — filtering before
slicing gives a different apology for the same page text depending on which
helper a caller reached for.
"""
from __future__ import annotations

__all__ = ["foreign", "flat"]


def foreign(text: object, cap: int = 200) -> str:
    """One bounded, escape-free, single-line piece of text we did not write."""
    line = " ".join(str(text or "").split())[:cap]
    return "".join(ch for ch in line
                   if ch >= " " and not "\x7f" <= ch <= "\x9f")


def flat(text: object, cap: int = 60) -> str:
    """`foreign` with the short cap page titles use in refusals."""
    return foreign(text, cap=cap)
