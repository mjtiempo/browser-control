"""result — the page snapshot a verdict compares, and the verdict itself.

Three verbs used to hand-write "did the page change" against two different dict
shapes (one built from `STATE_EXPR`, one from the matcher's payload), and two
verdict producers returned bare tuples. One `PageState` and one `Verdict`, so the
comparison is spelled once and a caller cannot compare the wrong two fields.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

__all__ = ["PageState", "Verdict"]


@dataclass(frozen=True)
class PageState:
    """The page facts a change verdict compares.

    `scroll` is `[x, y]` from `STATE_EXPR`; a matcher payload carries the same
    pair under `scroll`, so both shapes become one here.
    """

    url: str = ""
    title: str = ""
    active: str = ""
    scroll: tuple = ()

    @classmethod
    def from_dict(cls, data: dict) -> PageState:
        scroll = data.get("scroll")
        if not isinstance(scroll, (list, tuple)):
            scroll = [data.get("x"), data.get("y")]
        return cls(url=str(data.get("url") or ""),
                   title=str(data.get("title") or ""),
                   active=str(data.get("active") or ""),
                   scroll=tuple(scroll))

    def changed(self, other: PageState) -> bool:
        """Did anything this state holds differ from `other`?"""
        return (self.url != other.url or self.title != other.title
                or self.active != other.active or self.scroll != other.scroll)


class Verdict(NamedTuple):
    """A read-back verdict: what was proven, and the sentence saying why.

    A NamedTuple on purpose: `verified, why = ...` and `verdict[0]` both keep
    working, so the shape changed without any caller changing.
    """

    verified: bool | None
    why: str = ""
