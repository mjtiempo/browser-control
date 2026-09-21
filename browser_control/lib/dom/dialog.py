"""dialog — `tab dialog state|accept|dismiss`.

A page's dialog PARKS its renderer; this verb names what can be proven and
answers with `Page.handleJavaScriptDialog`.
"""
from __future__ import annotations

from browser_control.lib import (
    browser as browser_lib,
)
from browser_control.lib import dom as _pkg
from browser_control.lib.dom.scripts import (
    DIALOG_AWAKE,
)
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    ERR_DIALOG_NOT_VERIFIED,
    ERR_NO_DIALOG,
    ControlError,
    fail,
)
from browser_control.lib.poll import (
    POLL_NORMAL,
    poll,
)

DIALOG_PROBE_S = 1.5        # how long the tab is given to prove it is awake

DIALOG_CLEAR_S = 3.0        # how long the renderer is given to come back

NO_DIALOG_NOTE = (
    "the browser is not showing a JavaScript dialog for this tab. Two ways "
    "that happens: there is none, or the dialog was SUPPRESSED because no "
    "client had the Page domain enabled when it opened — there is nothing "
    "left to answer then, and the renderer stays parked: `tab nav URL` "
    "replaces the document (and its renderer), `tab close` ends the tab")

NO_DIALOG_MARK = "No dialog is showing"

def _no_dialog(error: ControlError) -> bool:
    """Did the browser refuse because it is showing no dialog of ours?"""
    return NO_DIALOG_MARK in str(error.message)

def dialog(mode: str = "state", text: str | None = None, tab: str = "",
           browser: str = "") -> dict:
    """`tab dialog`: read whether a JavaScript dialog is up, or answer it.

    Measured (Chrome 152, headed), and the reason this verb exists:

    * a page's own dialog PARKS its renderer — every read on that tab times
      out until the dialog is answered;
    * Chrome SUPPRESSES dialogs for a target whose Page domain was never
      enabled: the browser shows nothing, `handleJavaScriptDialog` reports
      "No dialog is showing", and the tab never comes back;
    * with the domain enabled first, the dialog is announced, answered, and
      the renderer returns — which is why every session this CLI opens enables
      it (`cdp.Session`).

    So `state` reports only what can be PROVEN: the tab answering proves no
    dialog is blocking it (`open: false`), and a tab that cannot answer is
    reported as `open: null, verified: false` rather than as a claim of
    absence. `accept`/`dismiss` is the definitive answer — the browser errors
    for a dialog it is not showing — and its read-back is the renderer
    answering again.
    """
    name = _pkg.mode_of(mode) or "state"
    if name not in ("state", "accept", "dismiss"):
        fail(ERR_BAD_ARGS,
             f"tab dialog: MODE is state, accept or dismiss, got {mode!r}")
    if name == "state":
        page = _pkg.Tab.open(tab, browser, for_write=False)
        row, tab_row = page.row, page.tab_row
        opened: object = None
        verified = False
        note = ""
        try:
            with page.session() as session:
                try:
                    session.evaluate(DIALOG_AWAKE, timeout=DIALOG_PROBE_S)
                    opened, verified = False, True
                    note = ("the tab answers, so no dialog is blocking it "
                            "(a dialog parks the renderer)")
                except ControlError as e:
                    if e.code != "eval-timeout":
                        raise
                    note = ("the tab does not answer: a JavaScript dialog it "
                            "opened, or a script that does not yield — "
                            "`tab dialog accept|dismiss` tells them apart, "
                            "because the browser errors for a dialog it is "
                            "not showing")
        except ControlError as e:
            if e.code != "blocked":
                raise
            note = ("the tab was already blocked before the Page domain "
                    "could be enabled — if a dialog did it, Chromium "
                    "suppressed it and there is nothing left to answer: "
                    "`tab dialog accept|dismiss` says so for certain, and "
                    "`tab close` ends it")
        return {"ok": True, "open": opened, "verified": verified,
                "blocked": True if opened is None else None, "note": note,
                "tab": f"id:{tab_row['id']}",
                "browser": browser_lib.brief(row)}
    # accept | dismiss: NO Page.enable first — a dialog that is already up
    # cannot be announced any more, and enabling is exactly what blocks on a
    # parked tab, while the handling command answers regardless
    params: dict = {"accept": name == "accept"}
    if text is not None:
        params["promptText"] = str(text)
    row, tab_row = _pkg._resolve(tab, browser, for_write=True)
    with _pkg.page_session(row, tab_row, page_domain=False) as session:
        try:
            session.call("Page.handleJavaScriptDialog", params)
        except ControlError as e:
            if _no_dialog(e):
                fail(ERR_NO_DIALOG, NO_DIALOG_NOTE)
            raise
        def probe() -> bool:
            try:
                session.evaluate(DIALOG_AWAKE, timeout=0.8)
                return True
            except ControlError as e:
                if e.code not in ("eval-timeout", "cdp-error", "blocked"):
                    raise
                return False

        _attempts, answered = poll(probe, timeout=DIALOG_CLEAR_S,
                                   interval=POLL_NORMAL)
    if not answered:
        fail(ERR_DIALOG_NOT_VERIFIED,
             f"the {name} was sent, but the tab still does not answer "
             f"within {DIALOG_CLEAR_S:g}s — a page can open another dialog "
             "immediately (see `tab dialog state`), or a script is spinning")
    return {"ok": True, "handled": True, "accepted": name == "accept",
            "verified": True, "prompt_text": text if text is not None else None,
            "note": ("the browser answered the dialog and the tab answers "
                     "again; the dialog's own text is not in this reply — it "
                     "opened before this connection, and Chromium announces a "
                     "dialog only once"),
            "tab": f"id:{tab_row['id']}",
            "browser": browser_lib.brief(row)}
