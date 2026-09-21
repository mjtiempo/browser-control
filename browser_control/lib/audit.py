"""audit — one JSONL line per action, with the secret redacted.

The COMMAND writes the line after the verb returns (`cli.main`), so a refusal
is recorded exactly like a success. Two rules make it safe to keep:

* **Fail-open.** A log that cannot be written never turns a working verb into
  a failure; nothing here raises.
* **A proven secret is never written.** A verb that *proves* the text it
  handled is a secret marks that exact text (`mark_secret`), and the writer
  replaces it wherever it appears in the argv with a length. The rest of the
  line — the verb, the tab spec, the refusal code — is kept, because a log who
  redacts everything says nothing.

`BROWSER_CONTROL_LOG` names the file, or `off` disables the log entirely.
Where a line GOES — the 0600/0700 modes, the fchmod narrowing, the short-write
check and the scratch fallback — is `lib/logfile.py::FileSink`; this module
builds the record and hands it to a sink (injectable, so a test can watch what
was written).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from browser_control.lib.logfile import (
    FileSink,
)
from browser_control.lib.paths import expand
from browser_control.lib.text import foreign

LOG_ENV = "BROWSER_CONTROL_LOG"
DEFAULT_LOG = "~/.local/state/browser-control/actions.jsonl"
SRC = "browser-control-cli"
ARG_CAP = 4096          # chars kept of ONE argv entry (a `tab js` is a program)
ARGS_CAP = 16_384       # chars kept of the whole argument list


def _brief(text: str) -> str:
    """One argv entry, bounded.

    `tab js` takes a whole program and an argument list can be long: one
    invocation used to write a 5 MB line into the log, which is not a record of
    anything (a review flagged it). What was cut is NAMED, so the line never
    looks complete when it is not.
    """
    if len(text) <= ARG_CAP:
        return text
    return f"{text[:ARG_CAP]}<truncated: {len(text)} chars>"


def _bounded(parts: list[str]) -> list[str]:
    """An argument list with a total budget, and the cut named."""
    out: list[str] = []
    used = 0
    for index, part in enumerate(parts):
        if used + len(part) > ARGS_CAP:
            out.append(f"<{len(parts) - index} more argument(s)>")
            break
        out.append(part)
        used += len(part)
    return out


class ActionLog:
    """The log itself: one instance, one line per command invocation."""

    def __init__(self, sink: FileSink | None = None) -> None:
        self._secret = ""
        self._sink = sink if sink is not None else FileSink()

    def begin(self) -> None:
        """Start an invocation: no secret is known yet.

        Called at the TOP of `main`, before any early refusal: a call that
        refuses before it reaches the verb must not be stamped with the
        PREVIOUS call's `redacted` (fail-closed, but a lie about this call).
        """
        self._secret = ""

    def mark_secret(self, text: str) -> None:
        """This EXACT text is a secret: write its length, never its content.

        Called by a verb that proved it (a password field, or a focus whose
        type cannot be read — fail closed), with the text it is about to send.
        """
        self._secret = str(text)

    @property
    def redacted(self) -> bool:
        return bool(self._secret)

    def path(self) -> str:
        """Where the next line goes, or "" when the log is off.

        An unset or empty variable means the default file; `off` (or `0`/`none`)
        means no log at all — which is how the hermetic tests stay hermetic.
        """
        raw = os.environ.get(LOG_ENV, "").strip()
        if raw.lower() in ("off", "0", "none"):
            return ""
        return expand(raw or DEFAULT_LOG)

    def _censor(self, argument: str) -> str:
        if self._secret and self._secret in argument:
            return f"<redacted: {len(self._secret)} chars>"
        return argument

    def write(self, action: str, ok: bool = True, code: str | None = None,
              detail: str | None = None, args: Any = None) -> None:
        """Append one line. NEVER raises: a log is not a gate."""
        path = self.path()
        if not path:
            return
        row: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                     "src": SRC, "action": foreign(action), "ok": bool(ok),
                     # CENSOR FIRST, truncate after: `_censor` only replaces an
                     # argument when the WHOLE secret is present, so truncating
                     # first wrote the first 4096 characters of any longer
                     # secret in cleartext (a review flagged it). Redacting to
                     # `<redacted: N chars>` first also keeps the budget math
                     # honest.
                     "args": _bounded([_brief(self._censor(str(a)))
                                       for a in (args or [])])}
        if code:
            row["code"] = foreign(code)
        if detail:
            row["detail"] = foreign(detail)[:200]
        if self._secret:
            row["redacted"] = True
        line = json.dumps(row) + "\n"
        # the configured file first; only when THAT fails is the scratch
        # directory built — a fallback is a second chance, not a first move
        # (building it eagerly made one empty directory per CLI call)
        if self._sink.write(path, line):
            return
        fallback = self._sink.fallback(path)
        if fallback:
            self._sink.write(fallback, line)

# One instance: the command marks a secret, the command writes the line.
SINK = FileSink()
LOG = ActionLog(SINK)


def scratch_dir() -> str:
    """The process's private scratch directory, from the default sink.

    Kept as a module function because the suites and `selftest` ask for it by
    name; the policy lives in `FileSink.scratch_dir`.
    """
    return SINK.scratch_dir()
