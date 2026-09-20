"""frames — `--frame`: which document a content verb acts on.

A cross-origin frame is a target of its own; the census names them and the
resolution records WHICH one a session actually attached to.
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
    as_ints,
)
from browser_control.lib.dom.scripts import (  # pyright: ignore[reportMissingImports]
    FRAME_CENSUS,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_FRAME_AMBIGUOUS,
    ERR_FRAME_NOT_SEPARATE,
    ERR_FRAME_UNATTRIBUTABLE,
    ERR_NO_FRAME,
    ControlError,
    fail,
)


def frame(wanted: str | None = None) -> str:
    """Set, clear or read the frame this process's verbs are about.

    `None` READS it; `""` clears it (the CLI clears a call that passes no
    `--frame`, so no verb inherits another's scope); anything else sets it.
    The read form matters: making "no argument" clear the scope as well meant
    every lookup wiped what it was looking at (which is exactly the bug this
    docstring exists to prevent).
    """
    return scope_state.current().set_frame(wanted)

def frame_resolved() -> dict | None:
    """Which frame the last session actually attached to, or None.

    An index is the page's live iframe order, so "which document did that act
    in" is not something a caller can infer from their own argument. The
    session records the resolution here and the CLI puts it in the reply, in
    ONE place, so no verb has to remember to and none can forget to.
    """
    return scope_state.current().resolved_frame()

FRAME_VERBS = frozenset({
    "js", "wait", "find", "text", "click", "hover", "check", "select",
    "scroll", "focus", "press", "insert", "type", "upload", "media",
    "screenshot", "extract",
})

def frames_of(port: int, page_target: str,
              census: list | None = None) -> list[dict]:
    """Every TOP-LEVEL frame of THAT page: the DOM's view, plus its own target.

    The DOM knows the geometry and whether a frame shares the page's process
    (`contentDocument` readable — a same-process frame has no target of its
    own). The browser's target list knows which frames are separate targets and
    WHICH TAB each belongs to: `Target.getTargets` gives an iframe target a
    `parentId`, and matching on it is what keeps `--frame` inside the tab it was
    asked about. Matching by URL alone — what this used to do — sent input into
    another tab's frame: measured with two tabs embedding the same widget, both
    resolved to ONE target.

    Two facts a URL cannot settle are REFUSED rather than guessed:

    * two frames in this tab with the same URL (`candidates` > 0) — target ids
      are random, so pairing them by id order can bind the sibling (a review
      flagged it);
    * a browser that does not say WHO owns an iframe target: nothing is
      attributed (`attribution: "unattributable"`), and `_frame_target` refuses
      — a URL that appears once is not proof, because a cross-origin frame in
      this page's own process has no target while another tab's single target
      with the same URL looks unique (a review constructed exactly that).

    `census` lets a caller that already evaluated it (a read that wants the
    counts) skip a second evaluation. `committed` is the URL the browser
    actually committed, which can differ from the `src` attribute the page
    shows. The census PIERCES open shadow roots (a frame inside one is a frame
    the page can see, and walking only the document omitted it silently — a
    review found that after the matcher had learned to pierce them).
    """
    if census is None:
        census = cdp.evaluate(cdp.target_ws(port, page_target), FRAME_CENSUS) \
            or []
    owned = cdp.frame_targets(port)
    shared: dict[str, int] = {}
    attribution = ""
    if any(str(row.get("parent")) for row in owned):
        pool = [r for r in owned if r["parent"] == page_target]
        counted: dict[str, int] = {}
        for row in pool:
            key = str(row.get("url"))
            counted[key] = counted.get(key, 0) + 1
        shared = {url: count for url, count in counted.items() if count > 1}
    else:
        # This browser does not say which tab owns an iframe target, and a URL
        # that appears ONCE is not proof of ownership: a cross-origin frame that
        # stays in this page's PROCESS (a sandboxed one) has no target of its
        # own, while another tab's single target with the same URL looks unique
        # — so `--frame` would drive that other tab. A review constructed
        # exactly that, so nothing is attributed here and `_frame_target`
        # refuses rather than guessing.
        pool = []
        attribution = "unattributable"
    used: set[str] = set()
    rows: list[dict] = []
    for entry in census if isinstance(census, list) else []:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "")
        match = None
        if url and not entry.get("same_process") and not shared.get(url):
            match = next((r for r in pool if str(r.get("url")) == url
                          and str(r.get("id")) not in used), None)
        if match is not None:
            used.add(str(match["id"]))
        rows.append({"index": as_int(entry.get("index")), "url": url,
                     "committed": str((match or {}).get("url") or ""),
                     "name": str(entry.get("name") or ""),
                     "box": as_ints(entry.get("box")),
                     "visible": bool(entry.get("visible")),
                     "same_process": bool(entry.get("same_process")),
                     "target": str((match or {}).get("id") or ""),
                     "attribution": attribution,
                     "candidates": shared.get(url, 0)})
    return rows

def frames(tab: str = "", browser: str = "") -> dict:
    """`tab frames`: the page's own TOP-LEVEL frames, and which can be driven.

    Resolves its own tab, like every other verb: the CLI used to reach the
    private `_resolve` for this one call (a review flagged it).
    """
    row, tab_row = _pkg._resolve(tab, browser, for_write=False)
    return frames_of_rows(row, tab_row)


def frames_of_rows(row: dict, tab_row: dict) -> dict:
    """`frames` for an already-resolved (browser row, tab row)."""
    port = cdp.port_of(str(row["profile"]))
    rows = _pkg.frames_of(port, str(tab_row["id"]))
    unattributed = any(r.get("attribution") for r in rows)
    reply = {"ok": True, "count": len(rows), "frames": rows,
             # NULL, never 0, when the browser attributes nothing: "none of
             # them can be driven" is a claim this cannot support, and the note
             # below used to promise `--frame` while `--frame` refused
             # `frame-unattributable` (a review caught the contradiction)
             "separate": None if unattributed
             else sum(1 for r in rows if r["target"]),
             "note": ("this browser does not report which tab owns an iframe "
                      "target, so `--frame` cannot attribute one to this tab "
                      "— `tab js` reads a same-process frame, and "
                      "`tab click --at X,Y` hits one by coordinate"
                      if unattributed else
                      "a cross-origin frame is a target of its own: name it "
                      "with `--frame <url substring|index>` and every verb "
                      "that acts on a page's content works inside it")}
    if not rows:
        reply["note"] = ("this page has no iframes — nothing for `--frame` to "
                          "name")
    reply["tab"] = f"id:{tab_row['id']}"
    reply["browser"] = browser_lib.brief(row)
    return reply

def _frame_target(port: int, page_target: str, wanted: str) -> dict:
    """The frame a `--frame` value names, or a refusal.

    An integer is the index `tab frames` prints; anything else is a
    case-insensitive substring of the frame's URL. Several matches refuse
    `frame-ambiguous` and name the index to use instead — the rule every other
    selector follows. A frame that shares the page's PROCESS has no target to
    drive, and refuses `frame-not-separate` with what to do instead.
    """
    rows = _pkg.frames_of(port, page_target)
    if not rows:
        fail(ERR_NO_FRAME, "this page has no iframes — `tab frames` lists them")
    text_ = str(wanted or "").strip()
    if text_.isdigit():
        hits = [r for r in rows if as_int(r["index"]) == as_int(text_)]
    else:
        hits = [r for r in rows
                if text_.lower() in str(r.get("url") or "").lower()]
    if not hits:
        have = "; ".join(f"[{r['index']}] {str(r['url'])[:52] or 'srcdoc'}"
                         for r in rows[:4])
        fail(ERR_NO_FRAME, f"no frame matches {wanted!r} (have: {have})")
    if len(hits) > 1:
        where = "; ".join(f"[{r['index']}] {str(r['url'])[:52]}" for r in hits[:4])
        fail(ERR_FRAME_AMBIGUOUS,
             f"{len(hits)} frames match {wanted!r} — pick one by index "
             f"(`--frame 0` … `--frame {len(rows) - 1}`): {where}")
    found = hits[0]
    if found.get("attribution"):
        fail(ERR_FRAME_UNATTRIBUTABLE,
             f"frame [{found['index']}] "
             f"{str(found['url'])[:60] or 'srcdoc'} — this browser does not "
             "report which tab owns an iframe target, so the CLI cannot tell "
             "this frame from another tab's with the same URL. `tab js` reads "
             "a same-process frame, `tab click --at X,Y` hits one by "
             "coordinate, or drive the tab that owns it")
    if found.get("candidates"):
        fail(ERR_FRAME_AMBIGUOUS,
             f"frame [{found['index']}] {str(found['url'])[:60]} matches "
             f"{found['candidates']} targets in this browser and it does not "
             "say which tab owns them — run `tab frames` in the tab you mean "
             "and name the frame by its index there")
    if not found["target"]:
        fail(ERR_FRAME_NOT_SEPARATE,
             f"frame [{found['index']}] "
             f"{str(found['url'])[:60] or 'srcdoc'} has no target of its OWN in "
             "this tab: either it shares the page's process (a same-origin or "
             "srcdoc frame), or its committed URL differs from the `src` the "
             "page shows (a redirect). `tab js` reads a same-process frame, "
             "and `tab click --at X,Y` hits either one by coordinate")
    return found

def _frame_summary(row: dict, tab_row: dict,
                   session: cdp.Session | None = None) -> dict:
    """What a read is NOT showing: the page's TOP-LEVEL frames, by kind.

    A `tab text` that silently omits everything inside an iframe is the one
    place this tool could read as "there is nothing there". The census makes it
    say so instead — and it is taken over the PAGE, never over a `--frame`
    scoped session, so a scoped read still reports the page it is a frame of.

    `separate` counts frames with a CDP target of their own (`--frame` can drive
    those), `cross_origin` counts frames whose document the page cannot read —
    two different facts that used to share one number. An UNREADABLE census is
    an error, not an empty page: `{}` means "no frames", and a page that breaks
    the census expression must not be able to look like a page without frames.

    A caller already holding a session hands it in: then a page with NO frames
    costs one evaluation on that connection, and the second connection (the
    browser target list) is only opened when there is something to attribute —
    every `find`/`text` used to pay for both, framed or not (a review measured
    it). `separate` is null, never 0, when the target list could not be read.
    """
    page = str(tab_row["id"])
    port = cdp.port_of(str(row["profile"]))
    try:
        if session is not None and not scope_state.current().frame_wanted:
            census = session.evaluate(FRAME_CENSUS)
        else:
            census = cdp.evaluate(cdp.target_ws(port, page), FRAME_CENSUS)
    except ControlError as e:
        return {"error": f"{e.code}: {e.message}", "total": None,
                "separate": None, "cross_origin": None, "visible": None,
                "note": ("the frame census could not be read, so framed "
                         "content may exist that this read does not show — "
                         "that is NOT the same as a page without frames")}
    raw = census if isinstance(census, list) else []
    if not raw:
        return {}
    try:
        rows = _pkg.frames_of(port, page, census=raw)
    except ControlError:
        rows = []
    if not rows:
        # the DOM census answered but the target list did not: report what is
        # known and leave `separate` NULL rather than claiming "no targets"
        return {"total": len(raw), "separate": None,
                "cross_origin": sum(1 for r in raw if isinstance(r, dict)
                                    and not r.get("same_process")),
                "same_process": sum(1 for r in raw if isinstance(r, dict)
                                    and r.get("same_process")),
                "visible": sum(1 for r in raw if isinstance(r, dict)
                               and r.get("visible"))}
    unattributed = any(r.get("attribution") for r in rows)
    return {"total": len(rows),
            # `separate` counts frames with a target of their own — NULL, never
            # 0, when the browser does not attribute targets to tabs: "none of
            # them can be driven" would be a claim this cannot support
            "separate": None if unattributed else sum(1 for r in rows
                                                      if r["target"]),
            "cross_origin": sum(1 for r in rows if not r["same_process"]),
            "same_process": sum(1 for r in rows if r["same_process"]),
            "visible": sum(1 for r in rows if r["visible"])}

def _frames_note(row: dict | None, tab_row: dict | None) -> str:
    """The sentence a REFUSAL carries when the page has frames.

    A `no-match` that says "nothing matches" while the page has a frame this
    read cannot see inside is the false-absence the whole tier is careful about,
    so the refusal says how many frames there are and how to reach them. Empty
    when there are none (nothing to explain), and computed only on the failure
    path — a verb that succeeds never pays for it.
    """
    if not row or not tab_row:
        return ""
    try:
        census = _frame_summary(row, tab_row)
    except ControlError:
        return ""
    if not census:
        return ""
    if census.get("error"):
        return (" — and the frame census could not be read either, so framed "
                "content may exist that nothing here can see")
    if census.get("total") in (None, 0):
        return ""
    if census.get("separate") is None:
        return (f" — this page has {census['total']} frame(s): `tab frames` "
                "lists them, and this browser does not say which can be "
                "driven directly")
    return (f" — this page has {census['total']} frame(s), "
            f"{census['separate']} of them separate: `tab frames` lists them "
            "and `--frame` reaches inside")
