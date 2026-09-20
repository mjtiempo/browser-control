"""scope — the ONE profile and frame an invocation is about.

The CLI is one process per call, and two module globals used to carry what
that call was about: `browser.SCOPE` (`--profile`) and `dom.FRAME`
(`--frame`). They are one fact — which document is this call about — so they
are one object, and the shims (`browser.scope()`, `dom.frame()`) delegate to
it. A verb that needs the scope asks the object; nobody re-spells the lookup.

The instance is process-wide because the CLI builds one invocation per
process and clears the scope at the start of each (`browser.scope("")`,
`dom.frame("")`). `current()` is the handle the shims use; a future embedder
that runs two calls in one process threads its own `Scope` instead.
"""
from __future__ import annotations

from browser_control.lib.paths import norm  # pyright: ignore[reportMissingImports]


class Scope:
    """The profile and frame ONE invocation is about."""

    def __init__(self) -> None:
        self.profile = ""
        self.frame_wanted = ""
        self.frame_resolved: dict | None = None

    def set_profile(self, profile: str | None) -> str:
        """Set, clear or read the profile this call is about.

        `None` reads it, `""` clears it (the CLI does that on every invocation
        that does not pass `--profile`, so one call never inherits another's),
        and a path sets it (normalised once, here).
        """
        if profile is not None:
            text = str(profile).strip()
            self.profile = norm(text) if text else ""
        return self.profile

    def set_frame(self, wanted: str | None) -> str:
        """Set, clear or read the frame this call is about.

        `None` READS it; `""` clears it (the CLI clears a call that passes no
        `--frame`, so no verb inherits another's scope); anything else sets it.
        A resolution belongs to the call that made it: nothing may inherit the
        last call's frame in a reply.
        """
        if wanted is not None:
            self.frame_wanted = str(wanted).strip()
            self.frame_resolved = None
        return self.frame_wanted

    def resolve_frame(self, index: int, url: str, target: str) -> None:
        """Record which frame a session actually attached to.

        An index is the page's live iframe order, so "which document did that
        act in" is not something a caller can infer from their own argument.
        """
        self.frame_resolved = {"index": index, "url": url, "target": target}

    def resolved_frame(self) -> dict | None:
        """The frame the last session attached to, as a COPY, or None."""
        resolved = self.frame_resolved
        return dict(resolved) if isinstance(resolved, dict) else None


_SCOPE = Scope()


def current() -> Scope:
    """The process-wide scope the CLI drives through the shims."""
    return _SCOPE
