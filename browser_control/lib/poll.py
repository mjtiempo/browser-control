"""poll — one bounded sample-until-accept loop for every read-back.

Every verb that waits reads its own effect back, and the shape is always the
same: sample, ask whether that IS the answer, stop at the deadline, sleep.
The shape was written out two dozen times, each with its own bookkeeping; the
budget belongs at the call site, the loop belongs here.

`on_error` is for the waits that deliberately swallow a refusal and decide
what it means for THEM (a tab list that cannot be read mid-startup is no rows
yet, not an absence); it must re-raise what the wait does not swallow.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

__all__ = ["POLL_FAST", "POLL_LOAD", "POLL_NORMAL", "POLL_SLOW", "deadline",
           "poll"]

# The four intervals the verbs measured for themselves, named so a reader can
# tell "one paint" from "a browser starting".
POLL_FAST = 0.15        # a page read-back: one frame
POLL_NORMAL = 0.2       # a process or an endpoint disappearing
POLL_SLOW = 0.25        # a browser starting, or a document moving
POLL_LOAD = 0.3         # a document load


def deadline(timeout: float) -> float:
    """The instant a budget ends."""
    return time.time() + timeout


def poll(probe: Callable[[], Any], *, timeout: float, interval: float,
         accept: Callable[[Any], bool] = bool,
         on_error: Callable[[Exception], Any] | None = None) -> tuple[int, Any]:
    """Sample `probe()` until `accept(value)` or the budget runs out.

    Returns ``(attempts, last)`` — the last sample even on a timeout, because
    every caller reports what it saw. `on_error` turns a refusal into the
    value that refusal means for THIS wait; it must re-raise what the wait
    does not swallow. The probe runs at least once.
    """
    end = deadline(timeout)
    attempts = 0
    last: Any = None
    while True:
        attempts += 1
        try:
            last = probe()
        except Exception as e:                                 # noqa: BLE001
            if on_error is None:
                raise
            last = on_error(e)
        if accept(last):
            return attempts, last
        if time.time() >= end:
            return attempts, last
        time.sleep(interval)
