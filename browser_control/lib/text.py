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

import unicodedata

__all__ = ["foreign", "flat"]


def _invisible(ch: str) -> bool:
    """Is this character one that renders as nothing, or reorders a line?

    `Cf` — Unicode's "format" category — is the whole family: the bidi marks
    and embeddings/overrides (U+061C, U+200B–U+200F, U+202A–U+202E,
    U+2066–U+2069), the word joiner and the invisible operators (U+2060–U+2064),
    the deprecated format run (U+206A–U+206F), the interlinear annotations
    (U+FFF9–U+FFFB), the soft hyphen (U+00AD), the BOM (U+FEFF) and the
    Mongolian vowel separator (U+180E). A `Cf` check replaces the hand-listed
    ranges because the list was always one family behind: a review measured
    U+00AD, U+2060, U+2062, U+FEFF and U+180E still reaching the terminal.
    Beside it, two things `Cf` does NOT cover: the TAG block
    (U+E0000–U+E007F), which spells an invisible second string over the visible
    one, and the two Hangul fillers (U+3164, U+FFA0), which are `Lo` blanks
    that render as nothing at all.
    """
    return (unicodedata.category(ch) == "Cf"
            or "\U000e0000" <= ch <= "\U000e007f"
            or ch == "\u3164" or ch == "\uffa0")


def foreign(text: object, cap: int = 200) -> str:
    """One bounded, escape-free, single-line piece of text we did not write.

    C0/DEL/C1 are dropped, and so is every invisible formatting character that
    cannot forge a line or a CSI/OSC sequence but CAN reorder or disguise a URL,
    a host or a filename rendered to a human reading a refusal (`_invisible`
    names the families). The rule is the character's Unicode CATEGORY, not a
    hand-kept list of code points: normal text is untouched, because no `Cf`
    character, tag or Hangul filler is text a reader wanted to see.
    """
    line = " ".join(str(text or "").split())[:cap]
    return "".join(ch for ch in line
                   if ch >= " " and not "\x7f" <= ch <= "\x9f"
                   and not _invisible(ch))


def flat(text: object, cap: int = 60) -> str:
    """`foreign` with the short cap page titles use in refusals."""
    return foreign(text, cap=cap)
