"""selector — the browser a caller NAMED: `--port`, `--pid` or `--profile`.

Three verbs take the same selector, with two arities: `attach` and `detach`
require exactly one, while `close` allows none (the managed browser) and
refuses two. One value object, one validator, one row lookup — so the
consent gate reads the same in all three, and a fourth verb cannot spell it
differently.
"""
from __future__ import annotations

from dataclasses import dataclass

from browser_control.lib.coerce import as_int
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    fail,
)
from browser_control.lib.paths import norm

__all__ = ["Selector"]


@dataclass(frozen=True)
class Selector:
    """Which browser the caller named — at most one of port/pid/profile."""

    port: int = 0
    pid: int = 0
    profile: str = ""

    def given(self) -> list[str]:
        """The flags the caller actually gave, in refusal-message order."""
        return [name for name, value in (("--port", self.port),
                                         ("--pid", self.pid),
                                         ("--profile", self.profile))
                if value]

    def named(self) -> bool:
        """Did the caller name a browser at all?"""
        return bool(self.given())

    def what(self) -> str:
        """The name as the caller spelled it, for a refusal message."""
        if self.pid:
            return f"--pid {as_int(self.pid)}"
        if self.profile:
            return f"--profile {self.profile}"
        return f"--port {as_int(self.port)}"

    def require_one(self, verb: str, *, allow_none: bool = False,
                    extra: str = "") -> None:
        """Refuse anything but the arity this verb allows.

        `allow_none` is `close`'s arity: no selector means the managed
        browser. `extra` carries a verb-specific tail (detach's `--all`).
        The texts are the frozen contract, spelled exactly once.
        """
        given = self.given()
        if allow_none:
            if len(given) > 1:
                fail(ERR_BAD_ARGS,
                     f"{verb}: name ONE browser — --port N, --pid N or "
                     f"--profile DIR{extra}")
            return
        if len(given) != 1:
            fail(ERR_BAD_ARGS,
                 f"{verb}: name ONE browser — --port N, --pid N or "
                 f"--profile DIR{extra}")

    def find(self, rows: list[dict]) -> dict | None:
        """The census row this selector names, or None."""
        if self.pid:
            return next((r for r in rows
                         if r["pid"] == as_int(self.pid)), None)
        if self.profile:
            want = norm(self.profile)
            return next((r for r in rows
                         if norm(str(r["profile"])) == want), None)
        want_port = as_int(self.port)
        return next((r for r in rows
                     if as_int((r.get("cdp") or {}).get("port")) == want_port), None)
