"""frames — `--frame`: which document a content verb acts on.

A cross-origin frame is a target of its own; the census names them and the
resolution records WHICH one a session actually attached to.
"""
from __future__ import annotations

from browser_control.lib import (
    browser as browser_lib,
)
from browser_control.lib import (
    cdp,
)
from browser_control.lib import dom as _pkg
from browser_control.lib import (
    scope as scope_state,
)
from browser_control.lib.browser.machine import (
    row_port,
)
from browser_control.lib.browser.owners import (
    verify_port_owner,
)
from browser_control.lib.coerce import (
    as_int,
    as_ints,
)
from browser_control.lib.dom.scripts import (
    FRAME_CENSUS,
    FRAME_DOC,
    PAGE_ROOT,
    fill,
)
from browser_control.lib.errors import (
    ERR_FRAME_AMBIGUOUS,
    ERR_FRAME_NOT_SEPARATE,
    ERR_FRAME_UNATTRIBUTABLE,
    ERR_NO_FRAME,
    ControlError,
    fail,
)
from browser_control.lib.text import foreign


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
      flagged it). Both halves of "two frames" are counted: two TARGETS with
      that URL, and two ROWS of this census that show it (a page with one
      `src` in two iframes and one target bound the first row silently, and row
      order is not evidence — a review found that half missing);
    * a browser that does not say WHO owns an iframe target: nothing is
      attributed (`attribution: "unattributable"`), and `_frame_target` refuses
      — a URL that appears once is not proof, because a cross-origin frame in
      this page's own process has no target while another tab's single target
      with the same URL looks unique (a review constructed exactly that).

    A row says which of those happened: `matched: true` is a URL match,
    `matched: "elimination"` is the one pair left when exactly ONE separate
    frame is unbound and exactly ONE of this tab's targets is unclaimed, and
    `matched: false` with `target: null` is a SEPARATE frame this census could
    not bind (its `src` attribute is not what its target COMMITTED — a
    redirect, a script rewrite — so `""` would have presented "no target" as a
    fact while `--frame` refused). A same-process frame keeps `target: ""`,
    which is the fact: it has no target of its own.

    `census` lets a caller that already evaluated it (a read that wants the
    counts) skip a second evaluation. `committed` is the URL the browser
    actually committed, which can differ from the `src` attribute the page
    shows. The census PIERCES open shadow roots (a frame inside one is a frame
    the page can see, and walking only the document omitted it silently — a
    review found that after the matcher had learned to pierce them).

    The port's HOLDER is re-judged immediately before the connections this
    opens (`verify_port_owner`): the census and the target list are two more
    websockets built from a port the caller resolved a moment earlier, and a
    rebound port would otherwise answer with somebody else's frames.
    """
    verify_port_owner(port)
    if census is None:
        census = cdp.evaluate(cdp.target_ws(port, page_target),
                              fill(FRAME_CENSUS)) or []
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
    # A URL that more than one ROW of this census shows is ambiguous by itself,
    # and counting it only among the browser's TARGETS was not enough: two rows
    # with one `src` and ONE listed target bound the FIRST row silently
    # (`candidates: 0`, `matched: true`) and left the second `target: null`, so
    # pairing by row order delivered `--frame 0` into the sibling's document —
    # the one thing this module refuses to do (a review found it). The count is
    # over the rows that can TAKE a target: a same-process row has none of its
    # own and takes no part in the pairing.
    claimed: dict[str, int] = {}
    for entry in census if isinstance(census, list) else []:
        if not isinstance(entry, dict) or entry.get("same_process"):
            continue
        key = str(entry.get("url") or "")
        if key:
            claimed[key] = claimed.get(key, 0) + 1
    used: set[str] = set()
    rows: list[dict] = []
    for entry in census if isinstance(census, list) else []:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "")
        same_process = bool(entry.get("same_process"))
        # how many things ANSWER to this URL: rows of this page that show it
        # and targets of this tab that committed it are two views of the same
        # set, so the larger view is the count — a sum would count one frame
        # twice, and the larger view is still a LOWER bound (a target can
        # belong to a nested frame this top-level census does not list). Below
        # two there is nothing to confuse.
        rivals = max(claimed.get(url, 0), shared.get(url, 0))
        candidates = rivals if rivals > 1 else 0
        match = None
        if url and not same_process and not candidates:
            match = next((r for r in pool if str(r.get("url")) == url
                          and str(r.get("id")) not in used), None)
        if match is not None:
            used.add(str(match["id"]))
        rows.append({"index": as_int(entry.get("index")), "url": url,
                     "committed": str((match or {}).get("url") or ""),
                     "name": str(entry.get("name") or ""),
                     "box": as_ints(entry.get("box")),
                     "visible": bool(entry.get("visible")),
                     "same_process": same_process,
                     # `target` is a FACT or it is nothing: a same-process frame
                     # really has no target of its own (`""`), while a SEPARATE
                     # frame this census could not bind gets `None` — it may own
                     # a target whose COMMITTED url differs from the `src` the
                     # page shows (a redirect, a script rewrite), and `""` said
                     # "no target" while `--frame` refused claiming there was
                     # nothing to attach to (a review found exactly that)
                     "target": (str(match["id"]) if match is not None
                                else ("" if same_process else None)),
                     "matched": match is not None,
                     "attribution": attribution,
                     "candidates": candidates})
    # BIND BY ELIMINATION, and only when it is a proof: exactly ONE separate
    # frame is unbound and exactly ONE of this tab's targets is unclaimed, so
    # the pair is forced. Anything else stays unbound and refuses — driving the
    # wrong frame is worse than saying "this one is ambiguous".
    unmatched = [r for r in rows
                 if not r["same_process"] and r["target"] is None
                 and not r["attribution"] and not r["candidates"]]
    remaining = [t for t in pool if str(t.get("id")) not in used]
    if len(unmatched) == 1 and len(remaining) == 1:
        bound = unmatched[0]
        bound["target"] = str(remaining[0].get("id") or "")
        bound["committed"] = str(remaining[0].get("url") or "")
        bound["matched"] = "elimination"
    return rows

def frames(tab: str = "", browser: str = "") -> dict:
    """`tab frames`: the page's own TOP-LEVEL frames, and which can be driven.

    Resolves its own tab, like every other verb: the CLI used to reach the
    private `_resolve` for this one call (a review flagged it).
    """
    row, tab_row = _pkg._resolve(tab, browser, for_write=False)
    return frames_of_rows(row, tab_row)


def _separate_count(rows: list[dict]) -> int:
    """How many of these frames are SEPARATE — a fact of the CENSUS.

    A separate frame is one whose document the page cannot read
    (`same_process: false`), whether or not this CLI managed to bind its
    target. Counting only BOUND targets reported frames the census had already
    proven separate as "not separate", so two of them gave `separate: 0` and a
    note that offered `--frame` on exactly the rows where it refuses (a review
    found it). A bound target counts too: it is the same fact, seen from the
    browser's side.
    """
    return sum(1 for r in rows if r.get("target") or not r.get("same_process"))

def _bound_count(rows: list[dict]) -> int:
    """How many of these frames this CLI can actually ATTACH to."""
    return sum(1 for r in rows if r.get("target"))

def _browser_is_silent(rows: list[dict]) -> bool:
    """Is the browser's silence about ownership what blocks a frame of this page?

    Only when at least one frame IS separate (the census proves it) and no
    target could be attributed to it. A page whose frames all share its process
    is fully explained without the browser saying anything about iframe targets
    — claiming "this browser does not report which tab owns an iframe target"
    there said nothing about the page and hid the proof that `separate` is 0 (a
    review found it).
    """
    return (any(r.get("attribution") for r in rows)
            and any(not r.get("same_process") and not r.get("target")
                    for r in rows))

def _frames_note_text(total: int, separate: int | None, bound: int | None,
                      silent: bool = False) -> str:
    """The note a frame reply carries: what `--frame` can and cannot reach.

    ONE builder, so `tab frames` and the note a refusal carries cannot tell a
    caller two different things. It never advertises `--frame` for a row where
    it refuses: a page of same-process frames reads with
    `tab text`/`tab extract --frame N`, and a page whose separate frames could
    not be bound (the browser is silent, or a redirect moved the committed URL)
    says so and points at the coordinate route instead.
    """
    if silent:
        return ("this browser does not report which tab owns an iframe target, "
                "so the CLI cannot attribute one to this tab and `--frame` "
                "cannot reach a SEPARATE frame of it — a same-process frame "
                "still reads (`tab text --frame N`, `tab extract --frame N`), "
                "and `tab click --at X,Y` hits one by coordinate")
    if separate is None:
        return (f"this page has {total} frame(s): `tab frames` lists them, and "
                "this browser does not say which can be driven directly")
    if not separate:
        return (f"this page has {total} frame(s), none of them separate (they "
                "all share the page's process): `tab text --frame N` and `tab "
                "extract --frame N` read one, and input there needs `tab click "
                "--at X,Y` by coordinate")
    if not bound:
        return (f"this page has {total} frame(s), {separate} of them separate, "
                "and not one could be bound to a target this CLI may attach "
                "to: `tab frames` says why, `tab click --at X,Y` reaches one "
                "by coordinate, or drive the tab that owns it")
    return (f"this page has {total} frame(s), {separate} of them separate: "
            f"`tab frames` lists them and `--frame` reaches the {bound} it "
            "bound")

def frames_of_rows(row: dict, tab_row: dict) -> dict:
    """`frames` for an already-resolved (browser row, tab row)."""
    port = row_port(row) or as_int(cdp.port_of(str(row["profile"])))
    rows = _pkg.frames_of(port, str(tab_row["id"]))
    silent = _browser_is_silent(rows)
    reply = {"ok": True, "count": len(rows), "frames": rows,
             # `separate` is the CENSUS fact — a frame whose document the page
             # cannot read is separate whether or not this CLI bound its target
             # — and never NULL for a page whose frames all share its process:
             # `separate: 0` is then PROVEN, and the browser's silence about
             # iframe targets has nothing to do with it (a review found the
             # reply saying 0 and NULL in the two cases where the opposite was
             # true). `bound` rides along so the note cannot promise `--frame`
             # for a frame it refuses.
             "separate": _separate_count(rows),
             "bound": _bound_count(rows),
             "note": _frames_note_text(len(rows), _separate_count(rows),
                                       _bound_count(rows), silent)}
    if not rows:
        reply["note"] = ("this page has no iframes — nothing for `--frame` to "
                          "name")
    reply["tab"] = f"id:{tab_row['id']}"
    reply["browser"] = browser_lib.brief(row)
    return reply

def _frame_scope(row: dict, tab_row: dict, *,
                 allow_same_process: bool = False) -> dict | None:
    """The `--frame` a verb is scoped to, resolved — or None when it has none.

    A frame can be reached in two ways, and the difference is not cosmetic:

    * a SEPARATE frame (cross-origin) is a CDP target: the verb attaches to it
      and every content verb works inside, exactly as the manual says;
    * a SAME-PROCESS frame has no target at all — its document is one the PAGE
      can read (`contentDocument`), so a READ is rooted there instead of
      attaching. That is what `allow_same_process` grants, and it is the only
      way to read such a frame WITHOUT running caller code: `tab js` used to be
      the sole door, and `--deny code` closes it.

    `allow_same_process=False` is the answer for the verbs that cannot be
    rooted — input needs a target, and `find`'s hit test asks the PAGE what is
    under a point, which inside a frame is the frame element itself.
    """
    wanted = scope_state.current().frame_wanted
    if not wanted:
        return None
    port = row_port(row) or as_int(cdp.port_of(str(row["profile"])))
    return _frame_scope_of(_pkg.frames_of(port, str(tab_row["id"])), wanted,
                           allow_same_process=allow_same_process, record=True)


def _frame_scope_of(rows: list[dict], wanted: str, *,
                    allow_same_process: bool,
                    record: bool = False) -> dict:
    """The frame `wanted` names, resolved to a document scope — or a refusal.

    The ONE reader of what a frame can and cannot be, so `_frame_target` (which
    needs an attachable target) and a rooted read cannot drift apart.
    """
    found = dict(_frame_row(rows, wanted))
    index, url = as_int(found["index"]), str(found.get("url") or "")
    if found.get("same_process") and allow_same_process:
        if record:
            scope_state.current().resolve_frame(index, url, "",
                                                same_process=True)
        found["kind"] = "same_process"
        return found
    if found.get("attribution"):
        fail(ERR_FRAME_UNATTRIBUTABLE,
             f"frame [{found['index']}] "
             f"{foreign(found['url'], 60) or 'srcdoc'} — this browser does "
             "not report which tab owns an iframe target, so the CLI cannot "
             "tell this frame from another tab's with the same URL. A "
             "same-process frame reads with `tab text --frame N`/`tab extract "
             "--frame N` (they root the read in the page, which needs no "
             "target), `tab click --at X,Y` hits one by coordinate, or drive "
             "the tab that owns it")
    if found.get("candidates"):
        # the remedy has to be one that can WORK: `tab frames` prints this row
        # with `candidates` > 0, so naming it by that index refuses for the
        # same reason — the refusal used to send the caller back to the verb
        # that had just refused (a review found the circle)
        fail(ERR_FRAME_AMBIGUOUS,
             f"frame [{found['index']}] {foreign(found['url'], 60)} — "
             f"at least {found['candidates']} frames or targets answer to that "
             "URL and the CLI cannot tell them apart, so naming this one by "
             "index (or by URL) cannot either: these frames cannot be told "
             "apart from here. Input can still reach this one by COORDINATE "
             "(`tab click --at X,Y`), or drive it from the tab that owns the "
             "frame")
    if not found["target"]:
        if found.get("same_process"):
            fail(ERR_FRAME_NOT_SEPARATE,
                 f"frame [{found['index']}] "
                 f"{foreign(found['url'], 60) or 'srcdoc'} shares the page's "
                 "process (a same-origin or srcdoc frame), so it has no "
                 "target of its own to attach to: a READ takes its text and "
                 "records with `--frame` (`tab text`/`tab extract`), and "
                 "input needs `tab click --at X,Y` by coordinate")
        fail(ERR_FRAME_NOT_SEPARATE,
             f"frame [{found['index']}] "
             f"{foreign(found['url'], 60) or 'srcdoc'} shows that `src`, but "
             "no iframe target of this tab has COMMITTED it — a redirect or a "
             "script rewrite changes the committed URL — and what is left "
             "cannot be told apart, so this verb will not guess which target "
             "is this frame. `tab frames` lists the ones that WERE bound, and "
             "`tab click --at X,Y` hits this one by coordinate")
    if record:
        scope_state.current().resolve_frame(index, url, str(found["target"]))
    found["kind"] = "target"
    return found



def frame_root(frame: dict | None) -> str:
    """The JS document a READ is rooted at, given its resolved `--frame`.

    The page itself, unless that frame shares the page's PROCESS: then there is
    no session to attach to, and the read runs in the page rooted at that
    frame's own `contentDocument` (`FRAME_DOC`) instead. Splice it into a read
    expression as its `__ROOT__`.
    """
    if frame is None or frame.get("kind") != "same_process":
        return PAGE_ROOT
    return fill(FRAME_DOC, index=str(frame["index"]))


def _frame_row(rows: list[dict], wanted: str) -> dict:
    """The one frame a `--frame` value NAMES, or a refusal.

    An integer is the index `tab frames` prints; anything else is a
    case-insensitive substring of the frame's URL. Several matches refuse
    `frame-ambiguous` and name the index to use instead — the rule every other
    selector follows.
    """
    if not rows:
        fail(ERR_NO_FRAME, "this page has no iframes — `tab frames` lists them")
    text_ = str(wanted or "").strip()
    if text_.isdigit():
        hits = [r for r in rows if as_int(r["index"]) == as_int(text_)]
    else:
        hits = [r for r in rows
                if text_.lower() in str(r.get("url") or "").lower()]
    if not hits:
        have = "; ".join(
            f"[{r['index']}] {foreign(r['url'], 52) or 'srcdoc'}"
            for r in rows[:4])
        fail(ERR_NO_FRAME, f"no frame matches {wanted!r} (have: {have})")
    if len(hits) > 1:
        where = "; ".join(f"[{r['index']}] {foreign(r['url'], 52)}"
                          for r in hits[:4])
        fail(ERR_FRAME_AMBIGUOUS,
             f"{len(hits)} frames match {wanted!r} — pick one by index "
             f"(`--frame 0` … `--frame {len(rows) - 1}`): {where}")
    return hits[0]


def _frame_target(port: int, page_target: str, wanted: str) -> dict:
    """The frame TARGET a `--frame` value names, or a refusal.

    The form a verb that must ATTACH uses (input, hit testing, `tab js`). A
    same-process frame has no target of its own and refuses
    `frame-not-separate`; a READ asks `_frame_scope(... allow_same_process=True)`
    instead, which roots the expression at that frame's own document.
    """
    return _frame_scope_of(_pkg.frames_of(port, page_target), wanted,
                           allow_same_process=False)

def _frame_summary(row: dict, tab_row: dict,
                   session: cdp.Session | None = None) -> dict:
    """What a read is NOT showing: the page's TOP-LEVEL frames, by kind.

    A `tab text` that silently omits everything inside an iframe is the one
    place this tool could read as "there is nothing there". The census makes it
    say so instead — and it is taken over the PAGE, never over a `--frame`
    scoped session, so a scoped read still reports the page it is a frame of.

    `separate` counts the frames that ARE separate documents — the census proves
    it (`same_process: false`) or this CLI bound a target of their own — and
    `bound` how many of those it can actually ATTACH to; a frame the census
    proves separate but could not be bound is not "not separate" (a review
    found `separate: 0` next to a note offering `--frame`). `cross_origin`
    counts frames whose document the page cannot read, which for the rows
    `frames_of` builds is the same set: a separate frame is exactly one the
    page cannot read. An UNREADABLE census is an error, not an empty page:
    `{}` means "no frames", and a page that breaks the census expression must
    not be able to look like a page without frames.

    A caller already holding a session hands it in: then a page with NO frames
    costs one evaluation on that connection, and the second connection (the
    browser target list) is only opened when there is something to attribute —
    every `find`/`text` used to pay for both, framed or not (a review measured
    it). `separate` is null when the target list could not be read — unless the
    census itself proves every frame shares the page's process, which is 0
    whatever the browser says.
    """
    page = str(tab_row["id"])
    port = row_port(row) or as_int(cdp.port_of(str(row["profile"])))
    try:
        if session is not None and not scope_state.current().frame_wanted:
            census = session.evaluate(fill(FRAME_CENSUS))
        else:
            # the one connection this helper opens on its own: judged first,
            # like every other websocket the DOM tier builds
            verify_port_owner(port)
            census = cdp.evaluate(cdp.target_ws(port, page), fill(FRAME_CENSUS))
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
        # known and leave `separate` NULL rather than claiming "no targets" —
        # unless the census itself proves there are none, which is what a
        # browser that reports no iframe target at all produces when every
        # frame shares the page's process
        counted = [r for r in raw if isinstance(r, dict)]
        return {"total": len(raw),
                "separate": (0 if counted and all(r.get("same_process")
                                                  for r in counted) else None),
                "bound": None,
                "cross_origin": sum(1 for r in counted
                                    if not r.get("same_process")),
                "same_process": sum(1 for r in counted
                                    if r.get("same_process")),
                "visible": sum(1 for r in counted if r.get("visible"))}
    return {"total": len(rows),
            # `separate` is the CENSUS's own fact (see `_separate_count`) and
            # `bound` is how many of them this CLI can attach to: the note
            # reads both, so it can neither report a proven-separate frame as
            # "not separate" nor offer `--frame` for one it refuses
            "separate": _separate_count(rows),
            "bound": _bound_count(rows),
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
    return " — " + _frames_note_text(census["total"], census.get("separate"),
                                     census.get("bound"))
