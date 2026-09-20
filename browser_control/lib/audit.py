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
The file's DIRECTORY is created on the first write (mode 0700) and the file is
opened 0600 — its lines carry the argv of a call — with an existing wider file
narrowed. When the configured path cannot be written, OR a wider file cannot be
narrowed to 0600, the line lands in a scratch directory built for the purpose
(`scratch_dir`, unique and 0700 via `mkdtemp`): a record is lost only when
neither the configured file nor scratch can be written, never silently
(`selftest` reports the path the log would use).
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from typing import Any

LOG_ENV = "BROWSER_CONTROL_LOG"
DEFAULT_LOG = "~/.local/state/browser-control/actions.jsonl"
SRC = "browser-control-cli"
SCRATCH_PREFIX = "browser-control"
ARG_CAP = 4096          # chars kept of ONE argv entry (a `tab js` is a program)
ARGS_CAP = 16_384       # chars kept of the whole argument list
_SCRATCH = ""


def scratch_dir() -> str:
    """A PRIVATE scratch directory for this process, created on first use.

    `tempfile.mkdtemp`: the name is unique, the mode is 0700, and the directory
    is ours. The timestamped name this used to build could be pre-created by
    another local user — or be a symlink — and `exist_ok=True` accepted it, so
    the fallback log could land in a directory they owned, or fail against all
    eight names and drop the record (a review flagged it). Two runs in the same
    second also shared one directory before, which the docstring claimed they
    never would. "" when even /tmp cannot be written, which every caller has to
    read as "no scratch".
    """
    global _SCRATCH
    if _SCRATCH:
        return _SCRATCH
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        _SCRATCH = tempfile.mkdtemp(prefix=f"{SCRATCH_PREFIX}-{stamp}-")
    except OSError:
        return ""
    return _SCRATCH


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
                     # CENSOR FIRST, truncate after: `_censor` only replaces an
                     # argument when the WHOLE secret is present, so truncating
                     # first wrote the first 4096 characters of any longer
                     # secret in cleartext (a review flagged it). Redacting to
                     # `<redacted: N chars>` first also keeps the budget math
                     # honest.
                     "args": _bounded([_brief(self._censor(str(a)))
                                       for a in (args or [])])}
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
        """One line to one file, making its directory first. False on failure.

        Opened 0600, and an existing wider file is narrowed with `fchmod`: the
        lines carry the argv (`tab js <program>`, a file path), so a
        world-readable action log is the second half of the promise that only a
        PROVEN secret is redacted (a review flagged the mode).
        """
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                             0o600)
            try:
                if stat.S_IMODE(os.fstat(handle).st_mode) & 0o077:
                    try:
                        os.fchmod(handle, 0o600)
                    except OSError:
                        # a file we cannot narrow may be readable by others, and
                        # this line carries the argv: refuse THIS file and let
                        # the caller fall back to the private scratch log rather
                        # than write it where it cannot be protected (a review
                        # flagged that the chmod could lose the record)
                        return False
                payload = line.encode("utf-8")
                offset = 0
                while offset < len(payload):
                    # `os.write` may write fewer bytes than asked (a full
                    # filesystem, RLIMIT_FSIZE); the return value was dropped,
                    # so a short write silently truncated the line while
                    # `_append` still said it succeeded — no scratch fallback,
                    # no record (a review flagged it).
                    written = os.write(handle, payload[offset:])
                    if written <= 0:
                        return False
                    offset += written
            finally:
                os.close(handle)
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
