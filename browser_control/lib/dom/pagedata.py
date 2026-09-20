"""pagedata — page-supplied values turned into shapes a verb can use.

Everything here crossed `Runtime.evaluate`: a malformed row is dropped, never
raised out of a verb.
"""
from __future__ import annotations

import re
from typing import Any

from browser_control.lib.coerce import (  # pyright: ignore[reportMissingImports]
    as_ints,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_NO_VIEWPORT,
    fail,
)
from browser_control.lib.text import (  # pyright: ignore[reportMissingImports]
    foreign,
)

_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

_ATTR_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:.-]*$")

def _well_formed(rows: Any, required: tuple) -> list[dict]:
    """The dict rows a page-supplied extraction must be, filtered to shape.

    Everything on this list crossed `Runtime.evaluate`, and the page — not this
    tool — answered it: a row that is not the object the verb indexes is
    dropped, not fatal."""
    return [row for row in rows
            if isinstance(row, dict) and all(key in row for key in required)]

def _viewport(data: dict, target_id: str) -> list[int]:
    """The page's viewport, or a refusal when it has none."""
    viewport = as_ints(data.get("viewport"))
    if len(viewport) < 2 or viewport[0] <= 0 or viewport[1] <= 0:
        fail(ERR_NO_VIEWPORT,
             f"tab {target_id[:10]}… reports no viewport ({viewport}) — a box "
             "in no viewport is not a target: this is a windowless or "
             "never-shown browser, so nothing can be measured in it")
    return viewport[:2]

def _describe(row: dict) -> str:
    """One match, in a few words, for a refusal message (page text, sanitised)."""
    return (f"{foreign(row.get('tag'), 20)} "
            f"{foreign(row.get('name') or row.get('text'), 40)}").strip()

def _element(row: dict) -> dict:
    """The element fields a reply carries (the geometry, not the page's)."""
    return {key: row.get(key) for key in
            ("tag", "role", "name", "type", "href", "text", "in_viewport",
             "clipped", "box", "center", "viewport", "point", "hit",
             "hit_element")}
