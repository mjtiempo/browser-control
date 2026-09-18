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
The file's DIRECTORY is created on the first write (mode 0700), and when it
cannot be written the line lands in a scratch directory built for the purpose
— `/tmp/browser-control-<timestamp>` (`scratch_dir`) — so a record is lost
only when nothing at all can be written, never silently. `selftest` reports
the path the log would use.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

LOG_ENV = "BROWSER_CONTROL_LOG"
DEFAULT_LOG = "~/.local/state/browser-control/actions.jsonl"
SRC = "browser-control-cli"
SCRATCH_PREFIX = "browser-control"
_SCRATCH = ""


def scratch_dir() -> str:
    """`/tmp/browser-control-<timestamp>`, created on first use (mode 0700).

    One per process, and the name carries WHEN it was made, so two runs never
    share a directory and nothing has to be cleaned up by hand — /tmp is the
    OS's business. This is where the action log goes when the configured path
    cannot be written, and it is what a test run (or any caller) uses for logs
    and artifacts. "" when even /tmp cannot be written, which every caller has
    to read as "no scratch".
    """
    global _SCRATCH
    if _SCRATCH:
        return _SCRATCH
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(tempfile.gettempdir(), f"{SCRATCH_PREFIX}-{stamp}")
    for attempt in range(8):
        candidate = base if not attempt else f"{base}-{attempt}"
        try:
            os.makedirs(candidate, mode=0o700, exist_ok=True)
            _SCRATCH = candidate
            return candidate
        except OSError:
            continue
    return ""


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
        line = json.dumps(row) + "\n"
        # the configured file first; only when THAT fails is the scratch
        # directory built — a fallback is a second chance, not a first move
        # (building it eagerly made one empty directory per CLI call)
        if self._append(path, line):
            return
        fallback = self._scratch_copy(path)
        if fallback:
            self._append(fallback, line)

    @staticmethod
    def _append(path: str, line: str) -> bool:
        """One line to one file, making its directory first. False on failure."""
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
            return True
        except OSError:
            return False

    def _scratch_copy(self, path: str) -> str:
        """Where the record goes when the configured file cannot be written."""
        scratch = scratch_dir()
        if not scratch or os.path.dirname(os.path.abspath(path)) == scratch:
            return ""
        return os.path.join(scratch, "actions.jsonl")


# One instance: the command marks a secret, the command writes the line.
LOG = ActionLog()


def reset_redaction() -> None:
    """Clear the per-invocation secret (the CLI does this before dispatch)."""
    LOG.begin("")
