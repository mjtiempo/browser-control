"""errors — the one refusal type every boundary catches.

A refusal is data, not a traceback: `code` is the contract a caller branches
on, `message` is one line for the human. `lib` raises it, `cli` prints it.
"""
from __future__ import annotations

from typing import NoReturn


class ControlError(Exception):
    """A structured refusal: `ERR[code]: message` at the CLI boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def fail(code: str, message: str) -> NoReturn:
    raise ControlError(code, message)
