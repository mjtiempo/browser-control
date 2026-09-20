"""tab — the per-verb prelude: resolve a tab, open one session, reply.

The shared plumbing every DOM verb reads through, kept in one place so the
frame scope and the reply envelope cannot drift between verbs.
"""
from __future__ import annotations

from browser_control.lib import (
    browser as browser_lib,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib import (
    cdp,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib import dom as _pkg  # pyright: ignore[reportMissingImports]
from browser_control.lib import (
    scope as scope_state,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib.coerce import (  # pyright: ignore[reportMissingImports]
    as_int,
)


def mode_of(mode: str | None) -> str:
    """The mode a mode-carrying subcommand will run, normalised in ONE place.

    Three calls answer differently by mode — `tab wait --for`, `tab dialog MODE`
    and `tab media MODE` — and the capability gate authorises a call BY its
    mode, so the gate and the verb that runs must read it the same way.
    Measured, and the reason this function exists: the gate used to compare the
    raw token (`--for JS` was not `js`, and `tab media PLAY` was not `play`),
    so `--allow read` authorised a call that then ran caller code. Normalising
    here — the verb normalises here too — means one spelling cannot be a read to
    the gate and a write to the browser.
    """
    return str(mode or "").strip().lower()

def _resolve(tab: str, browser: str, for_write: bool) -> tuple[dict, dict]:
    """(browser row, tab row) — the resolution every other verb uses."""
    return browser_lib.one_tab(tab, browser, for_write)

def _document_ws(port: int, page_target: str) -> str:
    """The websocket of the DOCUMENT a content verb acts on.

    The page itself, or the FRAME `--frame` names — and the scope is applied
    HERE, in one place, so a verb that drives its own connection cannot be the
    one that forgets it. That gap was real: `tab wait` opens its own connection
    for many samples, and `tab wait --frame 1` evaluated the predicate in the
    top document while its reply said `frame: 1`.
    """
    current = scope_state.current()
    if current.frame_wanted:
        target = _pkg._frame_target(port, page_target, current.frame_wanted)
        # WHICH frame that was: an index is the page's live iframe order, so the
        # reply says what was resolved, not only what was asked for
        current.resolve_frame(as_int(target["index"]), str(target["url"]),
                              str(target["target"]))
        return cdp.target_ws(port, str(target["target"]), "iframe")
    return cdp.target_ws(port, page_target)

def page_session(row: dict, tab_row: dict, *,
                   page_domain: bool = True) -> cdp.Session:
    """ONE connection for the whole verb — or for the FRAME it is scoped to.

    `--frame` attaches to that frame's OWN target. A cross-origin frame is a
    target with its own coordinate space, so every verb below works unchanged
    inside it — measured: a real click dispatched on that session fires the
    frame's own handler and the frame reports the new state.
    `page_domain=False` is the parked-dialog case: a dialog already up cannot be
    announced, and enabling the domain is what blocks on a parked tab.
    """
    port = cdp.port_of(str(row["profile"]))
    return cdp.Session(_document_ws(port, str(tab_row["id"])),
                       page_domain=page_domain)


def _session(row: dict, tab_row: dict) -> cdp.Session:
    """The name the verbs call (and the suites patch): the factory above."""
    return page_session(row, tab_row)

def _reply(row: dict, tab_row: dict, data: dict) -> dict:
    """The page-level facts every DOM read carries."""
    return {"tab": f"id:{tab_row['id']}", "url": data.get("url"),
            "title": data.get("title"), "visibility": data.get("visibility"),
            "browser": browser_lib.brief(row)}

def _with_frame(reply: dict) -> dict:
    """Say which frame the verb acted in, when it was scoped to one.

    The RESOLVED frame as well as the value that was asked for: an index is the
    page's live iframe order, so a caller cannot infer from their own argument
    which document the verb actually changed.
    """
    current = scope_state.current()
    if current.frame_wanted:
        reply["frame"] = current.frame_wanted
        resolved = current.resolved_frame()
        if resolved:
            reply["frame_resolved"] = resolved
    return reply
