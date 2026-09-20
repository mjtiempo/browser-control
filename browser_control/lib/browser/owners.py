"""owners — the endpoint identity guard.

The port comes from a FILE; this asks the kernel who holds it and whether
that process is the browser the profile says it is. Every write path asks
before it sends anything.
"""
from __future__ import annotations

from browser_control.lib import browser as _pkg  # pyright: ignore[reportMissingImports]
from browser_control.lib import (
    cdp,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib.browser.constants import (  # pyright: ignore[reportMissingImports]
    LAUNCH_WAIT_S,
)
from browser_control.lib.coerce import (  # pyright: ignore[reportMissingImports]
    as_int,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_CDP_NOT_LOCAL,
    ERR_CDP_UNREACHABLE,
    fail,
)
from browser_control.lib.poll import (  # pyright: ignore[reportMissingImports]
    POLL_SLOW,
    poll,
)
from browser_control.lib.proc import (  # pyright: ignore[reportMissingImports]
    BROWSER_EXES,
    find_pid,
    pid_on_marker,
)

_OWNER_CACHE: dict[tuple[str, int], dict] = {}

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
    key = (profile, port)
    cached = _OWNER_CACHE.get(key)
    if cached is not None:
        return cached
    owner = cdp.listener_of(port)
    pid = as_int(owner.get("pid"))
    exe = str(owner.get("exe") or "")
    cmd = str(owner.get("cmd") or "")
    profile_pid = find_pid(profile) if profile else 0
    if not owner:
        verdict = {"verified": False, "pid": 0, "exe": "",
                   "profile_pid": profile_pid,
                   "reason": "no process holds the port (the socket is gone)"}
    elif exe not in BROWSER_EXES:
        verdict = {"verified": False, "pid": pid, "exe": exe,
                   "profile_pid": profile_pid,
                   "reason": f"pid {pid} holds the port and is not a "
                             f"Chromium-family browser ({exe or 'unknown'})"}
    elif profile and not pid_on_marker(pid, cmd, profile):
        verdict = {"verified": False, "pid": pid, "exe": exe,
                   "profile_pid": profile_pid,
                   "reason": f"pid {pid} holds the port, but its own command "
                             f"line does not run {profile}"}
    else:
        verdict = {"verified": True, "pid": pid, "exe": exe,
                   "profile_pid": profile_pid, "reason": ""}
    _OWNER_CACHE[key] = verdict
    return verdict

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
    suspects = _pkg._narrow([r for r in rows if r["cdp"].get("reachable")
                        and not r["cdp"].get("verified")], browser)
    if suspects:
        row = suspects[0]
        not_local_refusal(str(row["profile"]), as_int(row["cdp"]["port"]),
                          str(row["cdp"].get("reason") or "unknown"))

def verify_profile_endpoint(profile: str) -> None:
    """Refuse when the endpoint on that profile is not the browser we think.

    `ensure_up` proves only that something ANSWERS; the port comes from a FILE,
    so it is the kernel that says who owns it (`endpoint_owner`). The drive path
    resolves its rows through `_drivable`, which requires a VERIFIED endpoint —
    and the paths that call the browser directly (`_open_tabs` for
    `open`/`tab URL`, `close_tabs` for `Target.closeTarget`) ask here, because
    they re-read the port file at call time (a stale one sent a tab-creating
    call wherever it pointed).
    """
    port = as_int(cdp.port_of(profile))
    if not port:
        fail(ERR_CDP_UNREACHABLE,
             f"no port file in {profile} — nothing to drive there")
    owner = _pkg.endpoint_owner(profile, port)
    if not owner.get("verified"):
        not_local_refusal(profile, port, str(owner.get("reason") or "unknown"))

def _await_owner(profile: str, port: int) -> dict:
    """The endpoint's owner, waiting out a browser that is still STARTING.

    A port file appears before the listener answers — Chrome writes it and
    binds a moment later, the same gap `_wait_port` covers — so a second
    caller that takes the lock inside that window must not read it as a
    stranger. It waits, and only an endpoint that still does not verify while a
    process on that profile IS running is treated as suspicious; if the port
    answers and nothing on the profile runs, the file is stale and the caller
    starts a browser anyway (`warning`).

    The memo is EVICTED before every re-check: an unverified verdict from a
    moment ago must not be allowed to outlive the listener it was about.
    """
    owner = _pkg.endpoint_owner(profile, port) if cdp.reachable(profile) else {}
    if not port:
        # no port at all is not an endpoint to judge: whoever asked may have
        # read the port file before the browser wrote it (measured in the
        # concurrent `open` case, where that stale 0 refused a whole call)
        return {}
    if not owner or owner.get("verified") or not owner.get("profile_pid"):
        return owner
    def probe() -> dict:
        _OWNER_CACHE.pop((profile, port), None)
        return _pkg.endpoint_owner(profile, port) if cdp.reachable(profile) else {}

    return poll(probe, timeout=LAUNCH_WAIT_S, interval=POLL_SLOW,
                accept=lambda o: bool(o.get("verified")))[1]
