"""owners — the endpoint identity guard.

The port comes from a FILE, the row or the caller; this asks the kernel who
holds it and whether that process is the browser the profile says it is.
Every write path asks before it sends anything, and the page paths ask AGAIN
immediately before they connect (`verify_ws_owner`): a verdict is about a
listener, and a listener can be gone by the time the connection is made.
"""
from __future__ import annotations

from browser_control.lib import browser as _pkg
from browser_control.lib import (
    cdp,
)
from browser_control.lib.browser.constants import (
    LAUNCH_WAIT_S,
)
from browser_control.lib.browser.machine import (
    driver_port,
    endpoint_of,
    profile_pid,
)
from browser_control.lib.coerce import (
    as_int,
)
from browser_control.lib.errors import (
    ERR_CDP_NOT_LOCAL,
    ERR_CDP_UNREACHABLE,
    fail,
)
from browser_control.lib.poll import (
    POLL_SLOW,
    poll,
)
from browser_control.lib.proc import (
    BROWSER_EXES,
    pid_on_marker,
)


class EndpointGuard:
    """The kernel memo: one /proc walk per (profile, port) per process.

    A verdict is about a LISTENER, and a listener can change: `forget()` is
    how a caller that just started or stopped one says so, and `refresh=True`
    is the same thing for one read. Keeping the eviction API on the object
    replaced two hand-written pops in two modules — the failure mode the
    docstring on the old cache described in prose.

    It also REMEMBERS, per port, the profile whose browser last VERIFIED on
    it. That is the only identity a caller holding nothing but a port has:
    `dom.tab._document_ws` is handed a port and a target id, and `tab wait`
    hands the websocket it builds to `cdp.evaluate_until`, which EXECUTES the
    caller's expression. `verify_port_owner` re-judges the holder against that
    remembered profile, so the connection is authorised by a listener that is
    there NOW rather than by a verdict taken a moment ago.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int], dict] = {}
        self._verified: dict[int, str] = {}

    def verdict(self, profile: str, port: int, *,
                refresh: bool = False) -> dict:
        """The verdict for that endpoint, from the memo or a fresh walk."""
        key = (profile, port)
        if refresh:
            self._cache.pop(key, None)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        verdict = self._judge(profile, port)
        self._cache[key] = verdict
        if verdict.get("verified"):
            self._verified[port] = profile
        return verdict

    def forget(self, profile: str, port: int) -> None:
        """Evict one (profile, port) verdict.

        What a port was last VERIFIED for is NOT evicted: it is the identity a
        port-only caller connects under (`verify_port_owner`), and the
        connection itself re-judges the holder against the kernel — so
        dropping the memo must not drop the only profile that port can be
        judged by.
        """
        self._cache.pop((profile, port), None)

    def verified_profile(self, port: int) -> str:
        """The profile whose browser last VERIFIED on that port, "" if none."""
        return self._verified.get(as_int(port), "")

    @staticmethod
    def _judge(profile: str, port: int) -> dict:
        """Judge that port NOW: is its holder the browser of this profile?

        `held` says whether ANY process holds the port at all. It is what
        separates the two refusals a stale endpoint earns — the socket is gone
        (`cdp-unreachable`) from a different process holding it
        (`cdp-not-local`) — and it is read out of the SAME kernel answer the
        rest of the verdict is built from, never guessed from a reason string.

        The profile's pid comes from `machine.profile_pid`, which answers from
        the process list the census in progress already read: one `list` walks
        /proc once however many rows it verifies.
        """
        owner = cdp.listener_of(port)
        pid = as_int(owner.get("pid"))
        exe = str(owner.get("exe") or "")
        cmd = str(owner.get("cmd") or "")
        running_pid = profile_pid(profile) if profile else 0
        if not owner:
            return {"verified": False, "pid": 0, "exe": "", "held": False,
                    "profile_pid": running_pid,
                    "reason": ("no process holds the port "
                               "(the socket is gone)")}
        if owner.get("exposed"):
            # a listener bound to 0.0.0.0 or a LAN address is reachable off
            # this machine: it is not the loopback endpoint this profile's
            # browser owns, and the reason names the bind (a review flagged
            # that only the port was checked, so a wide bind verified)
            return {"verified": False, "pid": 0, "exe": "", "held": True,
                    "profile_pid": running_pid,
                    "reason": f"the endpoint on port {port} is bound to "
                              f"{owner['exposed']}, not loopback — reachable "
                              "off this machine, so it is not the endpoint "
                              "this profile's browser owns"}
        if exe not in BROWSER_EXES:
            return {"verified": False, "pid": pid, "exe": exe, "held": True,
                    "profile_pid": running_pid,
                    "reason": f"pid {pid} holds the port and is not a "
                              f"Chromium-family browser ({exe or 'unknown'})"}
        if not profile:
            # fail CLOSED: with no profile to compare against, the cmdline
            # check below would be SKIPPED and a bare exe name would verify —
            # which is how a browser started without `--user-data-dir` (no
            # known default either) could be attached and granted tab writes
            # on identity it never proved (a review found the check's `if
            # profile and ...` guard). Nothing names the browser, so nothing
            # can verify it.
            return {"verified": False, "pid": pid, "exe": exe, "held": True,
                    "profile_pid": running_pid,
                    "reason": ("the browser names no profile (it was started "
                                "without --user-data-dir, and its executable "
                                "has no known default), so its endpoint "
                                "cannot be verified")}
        if not pid_on_marker(pid, cmd, profile):
            return {"verified": False, "pid": pid, "exe": exe, "held": True,
                    "profile_pid": running_pid,
                    "reason": f"pid {pid} holds the port, but its own command "
                              f"line does not run {profile}"}
        return {"verified": True, "pid": pid, "exe": exe, "held": True,
                "profile_pid": running_pid, "reason": ""}


GUARD = EndpointGuard()

def endpoint_owner(profile: str, port: int) -> dict:
    """Is the process holding that port the browser this profile says it is?

    {"verified": bool, "pid": int, "exe": str, "profile_pid": int,
    "reason": str}. The port comes from a FILE, so it is checked against the
    kernel (`cdp.listener_of`): the process that owns the listening socket.

    The rule, and why it stops there:

    * the holder passes when its OWN cmdline names this profile
      (`--user-data-dir=<profile>`, compared as a normalised path) — Chrome's own
      helpers inherit that flag, so one rule covers them too;
    * a Chromium-family process that does NOT name it is UNVERIFIED, however many
      other processes are running the profile: `open` sees a process running on
      the profile as a stranger and refuses to adopt its endpoint (a review
      measured the looser "some process runs the profile" rule this replaced);
    * anything else does not pass, and every caller that would DRIVE that
      endpoint refuses instead (`cdp-not-local`). A local process that names
      itself `chrome` cannot be told apart, and docs/progress.md says so.
    """
    return GUARD.verdict(profile, port)


def not_local_refusal(profile: str, port: int, reason: str) -> None:
    """Refuse to drive an endpoint that is not the browser we think it is.

    The advice is the mundane one, because the mundane cause is the common
    one: a stale port file, or a port that was taken after the browser died.
    """
    fail(ERR_CDP_NOT_LOCAL,
         f"port {port} on {profile} answers, but it is not that profile's "
         f"browser: {reason}. Nothing was sent to it. Either the port file is "
         "stale or the port was taken — run `browser-control-cli close --force` "
         "(it finds the profile's own process by its cmdline) and then "
         "`open` again — or the browser was started WITHOUT "
         "`--user-data-dir`, so its command line does not name a profile and "
         "this CLI cannot verify it: start it with "
         "`--user-data-dir=<the profile>` to attach to it")

def _drive_refusal(browser: str, rows: list[dict]) -> None:
    """Refuse a drive when the only candidates answered but did not verify."""
    suspects = _pkg._narrow([r for r in rows if endpoint_of(r).get("reachable")
                        and not endpoint_of(r).get("verified")], browser)
    if suspects:
        row = suspects[0]
        not_local_refusal(str(row["profile"]), as_int(endpoint_of(row)["port"]),
                          str(endpoint_of(row).get("reason") or "unknown"))

def verify_profile_endpoint(profile: str, port: int = 0) -> None:
    """Refuse when the endpoint on that profile is not the browser we think.

    `ensure_up` proves only that something ANSWERS; this asks the kernel who
    owns the port (`endpoint_owner`). The drive path resolves its rows through
    `_drivable`, which requires a VERIFIED endpoint — and the paths that call
    the browser directly (`_open_tabs` for `open`/`tab URL`, `close_tabs` for
    `Target.closeTarget`) ask here, because they re-read the port at call time
    (a stale one sent a tab-creating call wherever it pointed).

    `port` is the endpoint the CALLER already resolved — the row's own port,
    which is the only oracle for a browser started with an explicit
    `--remote-debugging-port=N` (such a browser writes no port file). Without
    one the port is resolved the way the census resolves it (`driver_port`:
    the file when it answers AND verifies, else the port the PROCESS itself
    names), so a verb that has no row still cannot be answered by a stale
    file.

    The two failures are two refusals, exactly as in `verify_ws_owner`: a port
    NOBODY holds any more is `cdp-unreachable` (the endpoint went away) and a
    port held by something that is not this profile's browser is
    `cdp-not-local`. Reading both as `cdp-not-local` produced the
    self-contradictory "…answers, but it is not that profile's browser: no
    process holds the port (the socket is gone)" (a review found it): the
    port had stopped answering, so nothing was refused for being a stranger.
    A verdict that does not say whether the port is held at all keeps the
    `cdp-not-local` refusal — an unclear oracle is not evidence that the
    endpoint went away.
    """
    endpoint = as_int(port) or driver_port(profile)
    if not endpoint:
        fail(ERR_CDP_UNREACHABLE,
             f"no CDP port is known for {profile} — nothing to drive there "
             "(no `DevToolsActivePort` file, and no running browser on that "
             "profile names one)")
    owner = _pkg.endpoint_owner(profile, endpoint)
    if owner.get("verified"):
        return
    reason = str(owner.get("reason") or "unknown")
    if owner.get("held") is False:
        fail(ERR_CDP_UNREACHABLE,
             f"nothing holds the CDP port {endpoint} on {profile} any more "
             f"({reason}) — the endpoint went away, so nothing was sent")
    not_local_refusal(profile, endpoint, reason)

def verify_port_owner(port: int) -> None:
    """`verify_ws_owner` for a caller that holds the PORT and nothing else.

    The DOM tier's `_document_ws` is handed a port and a target id: `tab wait`
    builds its websocket there and gives it to `cdp.evaluate_until`, which
    EXECUTES the caller's expression — and the frame census opens its own
    connection the same way. Neither call has a profile, so the holder is
    re-judged against the only identity the port has: the profile it last
    VERIFIED for (`EndpointGuard.verified_profile`, recorded by the census
    verdict that authorised the verb). A port no verdict ever covered
    authorises nothing — a refusal, never a connection — and a port whose
    holder changed since that verdict fails the same `verify_ws_owner` a
    profile-carrying caller would get.
    """
    if not port:
        fail(ERR_CDP_UNREACHABLE,
             "no CDP port is known for this call — nothing to connect to")
    profile = GUARD.verified_profile(port)
    if not profile:
        fail(ERR_CDP_NOT_LOCAL,
             f"port {port} is not an endpoint this call has verified for any "
             "profile — nothing was sent to it")
    verify_ws_owner(profile, port)

def _await_owner(profile: str, port: int) -> dict:
    """The endpoint's owner, waiting out a browser that is still STARTING.

    Two windows of one start, and the wait covers both:

    * the port ANSWERS but does not verify yet — the listener is up and is not
      this profile's browser (another process took the port the file names,
      and the browser's own listener is a moment behind);
    * NOTHING answers yet while a process on this profile IS running: the
      browser is up and its endpoint is not. Returning `{}` there — what this
      did whenever nothing answered — read as "nothing is running": `launch`
      computed `already: false` and spawned a SECOND browser on a profile
      whose own browser was a moment from answering, then reported
      `started: true` (a review measured it). The wait is the same
      `LAUNCH_WAIT_S` budget, and the fast path (`{}`, no wait at all) is
      kept for a profile with NO process on it — the stale-file case, where
      there is nothing coming and the caller starts a browser anyway.

    The port is the CALLER's (`driver_port`, resolved once for the verb): this
    never re-reads the file the caller just resolved, and it probes THAT port
    — a browser started with an explicit `--remote-debugging-port=N` has no
    file to re-read and would otherwise look like nothing at all.

    The memo is EVICTED before every re-check: an unverified verdict from a
    moment ago must not be allowed to outlive the listener it was about.
    """
    if not port:
        # no port at all is not an endpoint to judge: whoever asked may have
        # read the port file before the browser wrote it (measured in the
        # concurrent `open` case, where that stale 0 refused a whole call)
        return {}
    owner = _pkg.endpoint_owner(profile, port) if cdp.answers(port) else {}
    if owner.get("verified"):
        return owner
    if owner and not owner.get("profile_pid"):
        # it ANSWERS and nothing runs on the profile: a stale file, and the
        # caller warns about it. No wait — there is no browser coming.
        return owner
    if not owner and not profile_pid(profile):
        # nothing answers and nothing runs: the fast path, unchanged
        return owner
    def probe() -> dict:
        GUARD.forget(profile, port)
        return _pkg.endpoint_owner(profile, port) if cdp.answers(port) else {}

    return poll(probe, timeout=LAUNCH_WAIT_S, interval=POLL_SLOW,
                accept=lambda o: bool(o.get("verified")))[1]

def verify_ws_owner(profile: str, port: int) -> None:
    """Re-judge the port a websocket is about to be opened on — NOW.

    A verdict is about a LISTENER, and the page paths build their websocket
    from the same port a moment later: between the check and the connection
    the port can be REBOUND, and then keystrokes and caller JS go to the new
    holder while the reply still says `verified: true` (a review flagged the
    verify-then-use gap; `cdp._checked_ws` checks the HOST and ignores the
    port). The memo is evicted (`refresh=True`) so what authorises the traffic
    is the listener that is there when the connection is made.

    The two failures are two refusals: a port nobody holds any more is
    `cdp-unreachable` (the endpoint went away), and a port held by something
    that is not this profile's browser is `cdp-not-local`.
    """
    if not port:
        fail(ERR_CDP_UNREACHABLE,
             f"no CDP port is known for {profile} — nothing to connect to")
    owner = GUARD.verdict(profile, port, refresh=True)
    if owner.get("verified"):
        return
    reason = str(owner.get("reason") or "unknown")
    if not owner.get("held"):
        fail(ERR_CDP_UNREACHABLE,
             f"nothing holds the CDP port {port} on {profile} any more "
             f"({reason}) — the endpoint went away between the check and the "
             "connection, so nothing was sent")
    not_local_refusal(profile, port, reason)
