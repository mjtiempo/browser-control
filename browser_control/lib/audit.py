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
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

LOG_ENV = "BROWSER_CONTROL_LOG"
DEFAULT_LOG = "~/.local/state/browser-control/actions.jsonl"
SRC = "browser-control-cli"


def _oneline(text: object) -> str:
    """One line, always: a message that carries a newline would forge a line
    in a log a reader parses."""
    return " ".join(str(text or "").split())


class ActionLog:
    """The log itself: one instance, one line per command invocation."""

    def __init__(self) -> None:
        self._secret = ""

    def begin(self, action: str = "") -> None:
        """Start an invocation: no secret is known yet."""
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
        return os.path.abspath(os.path.expanduser(raw or DEFAULT_LOG))

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
                     "src": SRC, "action": _oneline(action), "ok": bool(ok),
                     "args": [self._censor(str(a)) for a in (args or [])]}
        if code:
            row["code"] = _oneline(code)
        if detail:
            row["detail"] = _oneline(detail)[:200]
        if self._secret:
            row["redacted"] = True
        try:
            with open(path, "a") as handle:
                handle.write(json.dumps(row) + "\n")
        except OSError:
            return          # a log that cannot be written is not a failure


# One instance: the command marks a secret, the command writes the line.
LOG = ActionLog()


def reset_redaction() -> None:
    """Clear the per-invocation secret (the CLI does this before dispatch)."""
    LOG.begin("")
