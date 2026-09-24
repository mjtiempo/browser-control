"""page-reader — the rendered TEXT of one or more URLs, in ONE call.

The loop this replaces is three CLI processes per page (`tab nav` → `tab wait`
→ `tab text`) plus the shell glue that carries the URL between them, paid once
per page for a caller reading a LIST of them (a SERP, a queue, a set of docs).
This is that loop as one verb: every URL is navigated, waited for and read on
the one browser the CLI already drives.

It is a READER. The text is the page's own rendered text through
`plugin_api.text` — no caller code, no interpreter — and `length` still reports
what the page held before `--chars`, so `truncated` is the page's own answer to
"was there more", exactly as `tab text` gives it. The navigation is why the
declared classes include `write`, and the URLs are why they include `egress`: a
`--allow read` gate must not authorise a verb that browses.

A page that fails does not take the call down — each URL's refusal is listed in
`errors` beside the pages that answered, because a caller reading a list wants
the rest of it. When NOTHING could be read the verb refuses with the first
error's own code: a call that did nothing must not look like a call that did.

An empty `text` with a `frames` census is the one reading that looks like
"there is nothing there", so the census rides along when the page has frames,
and when exactly ONE same-process frame holds the words this verb reads that
frame's document itself and NAMES it (`read_frame`) — no second call, and no
caller code. The global `--frame` also works here: this action declares
`"frames": True`, so the CLI accepts the scope for it and every read the verb
makes happens inside the document the caller named. `--tab` picks the tab when
several are open.

Install: copy this file into ``~/.local/share/browser-control/plugins/`` (or
any directory in ``BROWSER_CONTROL_PLUGIN_PATH``).

    browser-control-cli page read https://example.com https://example.org \
        --chars 4000
"""
from __future__ import annotations

from browser_control import plugin_api
from browser_control.plugin_api import (
    ControlError,
    errors,
    fail,
    float_arg,
    int_arg,
    pop,
    tab_arg,
)

#: What a page comes back as when the caller names no budget: enough for an
#: article, small enough that a list of them stays a reply and not a transcript.
DEFAULT_CHARS = 4000
#: A page that has not settled by now is read AS IT IS (see `_one`): the wait is
#: a courtesy to slow pages, not a verdict on them.
DEFAULT_TIMEOUT_S = 20.0
#: Reading a list is a loop, not a crawl: past this the caller wants a script.
MAX_URLS = 20


def _urls(rest: list[str], verb: str) -> list[str]:
    """Every positional URL, in order — NONE is dropped.

    `--chars`, `--timeout` and `--tab` are taken out before this, so anything
    left that starts with `-` is a flag nobody reads — reading the page anyway
    would be the tool guessing what the caller meant. A bare `--` ends the
    options (the marker is consumed, as the core's readers do): every token
    after it is a URL, so a URL starting with `-` is reachable.

    An EMPTY word is kept, not filtered out: it is a URL the caller gave, and
    the url/`nav` path refuses it by name (`refusing '' as a URL`), which is
    what every other reader in this tool does with an empty value. Dropping it
    silently turned `page read ""` into "needs at least one URL" and hid it
    inside a list of good ones.
    """
    out: list[str] = []
    for index, word in enumerate(rest):
        text = str(word)
        if text == "--":
            out.extend(str(later) for later in rest[index + 1:])
            break
        if text.startswith("-"):
            fail(errors.ERR_BAD_ARGS,
                 f"{verb}: unknown option {text!r} — this verb takes URLs, "
                 "--chars N, --timeout S and --tab SPEC")
        out.append(text)
    if not out:
        fail(errors.ERR_BAD_ARGS, f"{verb}: needs at least one URL")
    if len(out) > MAX_URLS:
        fail(errors.ERR_BAD_ARGS,
             f"{verb}: {len(out)} URLs is over the {MAX_URLS} this verb reads "
             "in one call — split the list")
    return out


def _frame_words(data: dict, chars: int, tab: str, browser: str) -> dict:
    """The words of the ONE same-process frame, or {} when there is no ONE.

    A page can render its text inside a same-process frame and answer this read
    with an empty `text` — the census then says there is exactly one such
    frame, and that frame's document is readable with no target to attach to
    (the scope `tab text --frame N` sets). Completing the record from it, and
    NAMING it, is not a guess: the page's own census said where the words are.
    A caller reading a list should not need a second call to learn that a page
    that plainly has words is not empty.

    The scope is SAVED and restored, never simply cleared: the caller may have
    set the global `--frame` for this whole call (this action declares
    `frames`), and clearing it here would drop their scope for every URL after
    this one. Anything else — no frames, several of them, or one this browser
    cannot attribute — returns nothing and leaves the honest empty answer alone.
    """
    frames = data.get("frames") or {}
    if frames.get("total") != 1 or frames.get("same_process") != 1:
        return {}
    previous = plugin_api.frame()
    plugin_api.frame("0")
    try:
        inner = plugin_api.text(chars=chars, tab=tab, browser=browser)
    finally:
        plugin_api.frame(previous)
    if not str(inner.get("text") or "").strip():
        return {}
    return {"text": inner.get("text"), "length": inner.get("length"),
            "truncated": inner.get("truncated"), "read_frame": 0,
            "frame_url": inner.get("url"),
            "note": ("the page rendered no text of its own: this is frame 0's "
                     "document, named so the record cannot be read as the "
                     "page's own words")}


def _one(url: str, chars: int, timeout: float, tab: str,
         browser: str) -> dict:
    """One URL: navigate, wait, read — and say what could not be done.

    A wait that runs out is NOT an error here. The page is still read, because
    the words already rendered are the answer to "what does this page show" —
    throwing them away because a spinner never stopped would be the tool
    deciding the page was broken. The refusal is kept in `wait` instead, so the
    caller can see that the read happened on a page that had not settled.
    """
    page: dict = {"url": url}
    moved = plugin_api.nav(url, tab=tab, browser=browser)
    page["moved"] = bool((moved or {}).get("moved"))
    try:
        plugin_api.wait("load", timeout=timeout, tab=tab, browser=browser)
    except ControlError as e:
        page["wait"] = f"{e.code}: {e.message}"
    data = plugin_api.text(chars=chars, tab=tab, browser=browser)
    page.update({"title": data.get("title"),
                 "landed_on": data.get("url"),
                 "length": data.get("length"),
                 "truncated": data.get("truncated"),
                 "text": data.get("text")})
    if data.get("frames"):
        # the page's own census: `total`/`same_process` say whether the words
        # are in a frame this read did not show
        page["frames"] = data["frames"]
        if not str(data.get("text") or "").strip():
            page.update(_frame_words(data, chars, tab, browser))
    return page


def run(rest: list[str], browser: str) -> dict:
    """`page read URL... [--chars N] [--timeout S] [--tab SPEC]`."""
    args = [str(arg) for arg in rest]
    if not args or args[0] != "read":
        fail(errors.ERR_BAD_ARGS,
             "page: the subcommand is required — `page read URL...` "
             "(have: read)")
    args = args[1:]
    args, chars_flag = pop(args, "--chars", "page read")
    args, timeout_flag = pop(args, "--timeout", "page read")
    args, tab = tab_arg(args, "page read")
    urls = _urls(args, "page read")
    # the readers and their "needs a number" refusals are the CORE's (`int_arg`,
    # `float_arg`); only the defaults this verb alone knows stay here. The test
    # is `is not None`, never truthiness: `--chars ""` is a flag the caller
    # GAVE with an empty value, and the rest of the tool refuses one (`_tab_arg`,
    # `_parse_globals`, `_one`, `_classes_of` all do) — reading it as "absent"
    # silently used the default instead
    chars = int_arg(chars_flag, "page read --chars") if chars_flag is not None \
        else DEFAULT_CHARS
    if chars < 1:
        fail(errors.ERR_BAD_ARGS,
             f"page read: --chars must be at least 1, got {chars}")
    timeout = float_arg(timeout_flag,
                        "page read --timeout") if timeout_flag is not None \
        else DEFAULT_TIMEOUT_S
    if not 0 < timeout < 3600:
        fail(errors.ERR_BAD_ARGS,
             "page read: --timeout must be between 0 and 3600 seconds, "
             f"got {timeout_flag!r}")
    pages: list[dict] = []
    failures: list[dict] = []
    for url in urls:
        try:
            pages.append(_one(url, chars, timeout, tab, browser))
        except ControlError as e:
            failures.append({"url": url, "code": e.code, "message": e.message})
    if not pages:
        first = failures[0]
        raise ControlError(first["code"],
                           f"page read: no URL could be read — "
                           f"{first['url']} failed first: {first['message']}")
    return {"ok": True, "count": len(pages), "pages": pages,
            "errors": failures, "chars": chars, "timeout_s": timeout,
            "note": ("each page is this CLI's own `tab text` answer for one "
                     "URL: `length` is what the page held before `--chars`, "
                     "and text that is empty beside a `frames` census means "
                     "the words are in a frame — the ONE same-process frame "
                     "is read automatically and named in `read_frame`, and "
                     "the global `--frame` scopes every read this verb makes")}


PLUGIN = {
    "api": 1,
    "name": "page-reader",
    "description": "read the rendered text of one or more URLs in one call",
    "actions": {
        "page": {
            "run": run,
            # it navigates (a write) and reads, and the browser reaches the
            # network for each URL: declaring only `read` would let `--allow
            # read` authorise a verb that browses
            "classes": ("read", "write", "egress"),
            # this action acts on a page's CONTENT, so the global `--frame`
            # applies to it: `_authorise` accepts the scope because of this
            # declaration, and the reply names the scope it ran under
            "frames": True,
            "usage": ("page read URL... [--chars N] [--timeout S] [--tab SPEC]"
                      " — each URL's rendered text as a record, with `length`/"
                      "`truncated` from the page's own answer; one URL's "
                      "failure is reported in `errors` rather than losing the "
                      "rest of the list"),
        },
    },
}
