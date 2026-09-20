"""lifecycle — the managed browser: start it, adopt it, stop it.

One resolver for the binary and profile (`binary`, `instance_dir`), the
launch flags, the check-and-act under the profile lock, and the stop that
proves both the process and the endpoint are gone.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import time
from pathlib import Path

from browser_control.lib import (
    attachments as attachments_lib,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib import browser as _pkg  # pyright: ignore[reportMissingImports]
from browser_control.lib import (
    cdp,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib import (
    scope as scope_state,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib.browser.constants import (  # pyright: ignore[reportMissingImports]
    BROWSER_BINS,
    DEFAULT_PROFILES,
    LAUNCH_WAIT_S,
    PORT_WAIT_S,
    STOP_WAIT_S,
)
from browser_control.lib.browser.machine import (  # pyright: ignore[reportMissingImports]
    endpoint_of,
)
from browser_control.lib.browser.selector import (
    Selector,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib.coerce import (  # pyright: ignore[reportMissingImports]
    as_int,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_AMBIGUOUS_BROWSER,
    ERR_BAD_ARGS,
    ERR_BROWSER_NOT_STOPPED,
    ERR_CDP_NOT_LOCAL,
    ERR_CDP_UNREACHABLE,
    ERR_LAUNCH_FAILED,
    ERR_NO_BROWSER,
    ERR_NO_PAGE_TAB,
    ERR_NOT_ATTACHED,
    ERR_NOT_MANAGED,
    ERR_PROFILE_UNUSABLE,
    ERR_TABS_OPEN,
    ControlError,
    fail,
)
from browser_control.lib.paths import (  # pyright: ignore[reportMissingImports]
    expand,
    is_managed,
    lock_path,
    norm,
    pid_file,
    profile_dir,
    root,
)
from browser_control.lib.poll import (  # pyright: ignore[reportMissingImports]
    POLL_NORMAL,
    poll,
)
from browser_control.lib.proc import (  # pyright: ignore[reportMissingImports]
    pid_alive,
    pid_of,
    pid_on_profile,
    record_pid,
    spawn,
)


def binary(name: str = "") -> str:
    """The browser to drive: the one named, else the first on PATH.

    This is the ONLY resolver — `profile_dir` keys off the path it returns.
    """
    if name:
        found = shutil.which(name)
        if not found:
            raise ControlError(ERR_NO_BROWSER, f"{name!r} is not on PATH")
        return found
    for candidate in BROWSER_BINS:
        found = shutil.which(candidate)
        if found:
            return found
    raise ControlError(ERR_NO_BROWSER,
        "no Chromium-family browser on PATH (looked for: "
        + ", ".join(BROWSER_BINS) + ")")

def scope(profile: str | None = None) -> str:
    """Set, clear or read the profile this process's calls are about.

    `None` reads it, `""` clears it (the CLI does that on every invocation that
    does not pass `--profile`, so one call never inherits another's), and a path
    sets it. A path outside this CLI's root is refused when `open` acts on it
    (`instance_dir`), because only the profiles under that root are the CLI's.
    """
    return scope_state.current().set_profile(profile)

def _scoped() -> str:
    """The profile this invocation is about (the `--profile` scope)."""
    return scope_state.current().profile

def instance_dir(binary_path: str) -> str:
    """The profile `open` is about: the SCOPED instance, else the default one.

    The default stays one profile per browser binary (what `profile_dir`
    builds); `--profile DIR` names a second instance of the same browser
    instead, and it has to live under this CLI's root: outside it we would be
    starting a browser we then refuse to write to.
    """
    scoped = _scoped()
    if not scoped:
        return profile_dir(binary_path)
    if not is_managed(scoped):
        fail(ERR_BAD_ARGS,
             f"--profile {scoped} is not under {root()} — this CLI manages the "
             "profiles in its own root (set BROWSER_CONTROL_ROOT to move it, "
             "or `attach` a browser started elsewhere)")
    return scoped

def profiles() -> list[str]:
    """Every managed profile directory, sorted."""
    base = root()
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [os.path.join(base, name) for name in names
            if os.path.isdir(os.path.join(base, name))]

def ensure_up(profile: str) -> None:
    """Refuse now when the verb needs a browser and none is drivable."""
    if not cdp.reachable(profile):
        fail(ERR_CDP_UNREACHABLE,
             f"no drivable browser on {profile} — run "
             "`browser-control-cli open`, or attach a running one with "
             "`browser-control-cli attach --port N`")

def flags(profile: str) -> list[str]:
    """The launch flags that make a browser drivable ON its managed profile.

    `--no-first-run`/`--no-default-browser-check` are load-bearing: on a
    profile that has never run a browser, Chrome's first-run flow keeps the
    DevTools endpoint from coming up at all (measured: no port in 30 s
    without them, 2 s with).
    """
    return [f"--user-data-dir={profile}", "--remote-debugging-port=0",
            "--no-first-run", "--no-default-browser-check"]

def safe_url(url: str) -> str:
    """A URL we are willing to hand to the browser: http(s) or about:blank."""
    text = str(url).strip()
    if text == "about:blank":
        return text
    if not re.fullmatch(r"https?://\S+", text):
        fail(ERR_BAD_ARGS,
             f"refusing {text[:60]!r} as a URL (http(s) or about:blank only)")
    return text

def _default_profile(exe: str) -> str:
    """The default data directory of a browser executable, when it exists."""
    raw = DEFAULT_PROFILES.get(exe, "")
    if not raw:
        return ""
    path = expand(raw)
    return path if os.path.isdir(path) else ""

def _writable_profile(browser: str = "") -> str:
    """The profile a tab write goes to: the named browser's, else the ONE
    writable browser that is up, else this CLI's own (which `open` starts).

    Two writable browsers refuse — the endpoint belongs to a browser identity,
    and picking one silently is how a tab lands in the browser nobody asked
    about. `attach`/`detach` decide which browsers are candidates; `--browser`
    picks among them, and `--profile DIR` (the process scope) names ONE instance
    outright — the answer to two instances of the same browser.

    A browser that is merely DRIVABLE is not a candidate either: with only a
    stranger's browser up, this returns the profile this CLI starts itself,
    because "write into whatever answered" is the bug this rule exists for.
    """
    if _scoped():
        # the scope is a write target only when it is one this CLI manages or
        # was handed: `--profile /tmp/stranger` used to come straight back and
        # `tab about:blank` then ran `Target.createTarget` in a stranger's
        # browser (a review measured it). `instance_dir` refuses that path for
        # `open`; a write has to refuse it too — the two halves of the gate
        # disagreed about the same argv.
        if not is_managed(_scoped()) \
                and not _pkg.is_attached(_scoped()):
            fail(ERR_NOT_MANAGED,
                 f"--profile {_scoped()} is not a profile this CLI "
                 "manages or has attached — `open --profile DIR` starts one "
                 "under the root, or `attach --port N` hands one over")
        return _scoped()
    rows = _pkg._writable(browser)
    if len(rows) > 1:
        names = ", ".join(os.path.basename(str(r["profile"]))
                          + (" (attached)" if r["attached"] else "")
                          for r in rows)
        fail(ERR_AMBIGUOUS_BROWSER,
             f"{len(rows)} writable browsers are up ({names}) — pass "
             "--profile DIR to name the instance, --browser NAME, or "
             "`detach` one")
    if rows:
        return str(rows[0]["profile"])
    return profile_dir(binary(browser)) if browser else profile_dir(binary())

def managed_profile(browser: str = "") -> str:
    """The profile a LIFECYCLE verb (stop) is about: ours, or the one `open`
    would start. An attached browser is never a candidate — attaching grants
    tab writes, not the right to stop somebody else's browser."""
    rows = _pkg._narrow([r for r in _pkg.browsers()
                    if r["managed"] and endpoint_of(r)["reachable"]], browser)
    if not rows and _scoped():
        # a scoped call is about THAT instance, running or not: `open` starts
        # it, and nothing here may silently fall back to some other profile
        return _scoped()
    if len(rows) > 1:
        # pid AND the whole profile path: two browsers can share an executable
        # name, and "(google-chrome-stable, google-chrome-stable)" is a message
        # nobody can act on (observed)
        names = "; ".join(f'{os.path.basename(str(r["profile"]))} '
                          f'(pid {r["pid"]}, {r["profile"]})' for r in rows)
        fail(ERR_AMBIGUOUS_BROWSER,
             f"{len(rows)} managed browsers are up: {names} — pass "
             "--browser NAME")
    if rows:
        return str(rows[0]["profile"])
    return profile_dir(binary(browser)) if browser else profile_dir(binary())

def attachments() -> dict:
    """`attach --list`: what is attached, and whether it is still there."""
    records = attachments_lib.STORE.records()
    live = {norm(r["profile"]): r for r in _pkg.browsers()}
    rows: list[dict] = []
    for key in sorted(records):
        record = dict(records[key])
        row = live.get(key)
        record["running"] = row is not None
        record["reachable"] = bool(row and endpoint_of(row)["reachable"])
        record["managed"] = bool(row and row["managed"])
        rows.append(record)
    return {"ok": True, "count": len(rows), "attached": rows}

def attach(port: int = 0, pid: int = 0, profile: str = "") -> dict:
    """`attach`: allow TAB WRITES to a browser this CLI did not start.

    The browser keeps its own lifecycle: `close` never stops an attached
    browser (`detach` is how the authorization goes away), and the profile
    must belong to a running, answering Chromium-family process of this
    machine — which is where the identity comes from, not from the caller.
    """
    selector = Selector(port, pid, profile)
    selector.require_one("attach")
    given = selector.given()
    row = selector.find(_pkg.browsers())
    if row is None:
        fail(ERR_NO_BROWSER,
             f"no running Chromium-family browser matches {given[0]}")
    if not endpoint_of(row)["reachable"]:
        fail(ERR_CDP_UNREACHABLE,
             f"pid {row['pid']} does not answer CDP on port "
             f"{row['cdp']['port']} — attach needs a live endpoint")
    if not endpoint_of(row).get("verified"):
        # an endpoint that ANSWERS is not proof of WHOSE it is, and this record
        # is what opens the tab-write gate: `_named_browser` refuses an
        # unverified endpoint, and so must this (a review measured the gap —
        # a stale port was enough to authorise a write path)
        _pkg.not_local_refusal(str(row["profile"]), as_int(endpoint_of(row)["port"]),
                          str(endpoint_of(row).get("reason") or "unknown"))
    record = {"profile": norm(row["profile"]), "pid": row["pid"],
              "port": as_int(endpoint_of(row)["port"]), "exe": row["exe"],
              "managed": row["managed"],
              "attached_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    # the read-modify-write of the records runs under the root's lock (the
    # store owns it): two `attach`s at once used to overwrite each other's line
    already, lock = attachments_lib.STORE.put(record)
    reply = {"ok": True, "attached": True, "already": already,
             "browser": {"pid": record["pid"], "exe": record["exe"],
                         "profile": record["profile"],
                         "managed": record["managed"]},
             "cdp": {"port": record["port"], "reachable": True},
             "note": ("tab writes only — `close` will not stop it; "
                      "`detach` revokes this")}
    lock.warn(reply)
    return reply

def detach(port: int = 0, pid: int = 0, profile: str = "",
           detach_all: bool = False) -> dict:
    """`detach`: take the tab-write authorization away again."""
    if detach_all:
        if port or pid or profile:
            fail(ERR_BAD_ARGS, "detach: --all takes no other selector")
        keys, lock = attachments_lib.STORE.drop_all()
        reply = {"ok": True, "detached": keys, "count": 0}
        lock.warn(reply)
        return reply
    selector = Selector(port, pid, profile)
    selector.require_one("detach", extra=", or --all")
    given = selector.given()
    # the read-modify-write of the records runs under the root's lock (the
    # store owns it), so two `attach`/`detach` calls cannot lose each other's line
    keys, lock = attachments_lib.STORE.drop(selector.profile,
                                             pid=selector.pid,
                                             port=selector.port)
    if not keys:
        fail(ERR_NOT_ATTACHED, f"nothing is attached for {given[0]}")
    reply = {"ok": True, "detached": keys,
             "count": len(attachments_lib.STORE.records())}
    lock.warn(reply)
    return reply

def _open_tabs(profile: str, urls: list[str]) -> list[dict]:
    """One new tab per URL: every id from CDP, all verified together.

    `Target.createTarget` answers with the id it made and ONE poll loop then
    proves every id is in the tab list. A partial result refuses and names
    what is missing — and the tabs that did open stay open, because a refusal
    is not a reason to destroy work. The endpoint is checked against the kernel
    FIRST: creating a tab is a write, and a write does not go to whoever holds
    the port (a review measured that this one had no owner check at all).
    """
    _pkg._verify_profile_endpoint(profile)
    ids: list[str] = []
    for url in urls:
        result = cdp.browser_call(profile, "Target.createTarget", {"url": url})
        target_id = str(result.get("targetId") or "")
        if not target_id:
            fail(ERR_NO_PAGE_TAB, f"CDP made no tab for {url!r}")
        ids.append(target_id)
    found, missing = _pkg._wait_tabs(profile, ids)
    if missing:
        fail(ERR_NO_PAGE_TAB,
             f"{len(missing)} of {len(ids)} tabs never showed up in the tab "
             "list: " + ", ".join(missing[:4]))
    return [found[target_id] for target_id in ids]

def launch(urls: list[str] | None = None, browser: str = "") -> dict:
    """Start (or adopt) the managed browser and prove the pages are there.

    `urls` is what to open: a fresh start loads the FIRST as its startup page
    and opens the rest as tabs; an already-running browser is handed each as
    a new tab; an empty list just makes sure a page exists. `started` says
    which of the two happened and `opened` names the tabs this call made.

    Adoption needs more than an answer on the port (see `endpoint_owner`): a
    port file is a FILE. A port held by something that is not this profile's
    browser is refused when a process on that profile is running, and IGNORED
    (with a `warning` in the reply) when nothing is — a stale file must not
    stop a fresh start, and it must not make `open` hand URLs to a stranger.

    The whole check-and-act runs under the profile's lock (`_lock`). Without
    it, two `open`s at once both find "nothing running" and both start a
    browser on one profile — which is the corruption Chrome's own singleton
    warning describes — and both would report `started: true`. With it, the
    loser waits, then sees the endpoint (a real one, verified) and ADOPTS:
    `started: true` happens exactly once, or the loser refuses `profile-busy`
    naming the holder. Nothing is left to a third party's behaviour.
    """
    wanted = [safe_url(url) for url in (urls or [])]
    path = binary(browser)
    profile = instance_dir(path)
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError as e:
        raise ControlError(ERR_PROFILE_UNUSABLE,
                           f"cannot create {profile}: {e}") from e
    with _pkg._lock(lock_path(profile), "open") as lock:
        # the port is read INSIDE the lock: a value from before it can be 0
        # while the browser another call just started is already answering,
        # and judging that stale 0 is what turned a race into a refusal
        port = cdp.port_of(profile)
        owner = _pkg._await_owner(profile, port)
        if owner and not owner.get("verified"):
            if owner.get("profile_pid"):
                _pkg.not_local_refusal(profile, port,
                                  str(owner.get("reason") or ""))
            reason = str(owner.get("reason") or
                         "nothing of this browser holds it")
            stale = f"ignored a stale DevTools port ({port}): {reason}"
        else:
            stale = ""
        already = bool(owner.get("verified"))
        requests: list[str] = []
        opened: list[dict] = []
        if already:
            if wanted:
                requests = wanted
                opened = _open_tabs(profile, wanted)
        else:
            first = wanted[0] if wanted else "about:blank"
            requests = [first, *wanted[1:]]
            record_pid(profile, spawn([path, *flags(profile), first]))
            if not _pkg._wait_own_port(profile):
                fail(ERR_LAUNCH_FAILED,
                     f"started {path} on {profile} but no CDP endpoint "
                     f"answered within {LAUNCH_WAIT_S:g}s")
            row = _pkg._wait_url(profile, first)
            if row is None:
                fail(ERR_NO_PAGE_TAB,
                     f"{path} is up on {profile} but shows no tab for "
                     f"{first!r}")
            opened = [row]
            if len(wanted) > 1:
                opened += _open_tabs(profile, wanted[1:])
        rows = _pkg._wait_rows(profile)
    if not rows:
        fail(ERR_NO_PAGE_TAB,
             f"{path} is up on {profile} but shows no page tab — pass a URL "
             "(browser-control-cli open https://…)")
    notes = [text for text in (stale, lock.warning) if text]
    reply = {"ok": True, "started": not already, "browser": path,
             "profile": profile, "port": cdp.port_of(profile),
             "pid": pid_of(profile), "tabs": rows,
             "opened": [{"requested": requests[index], "id": row["id"],
                         "url": row["url"], "title": row["title"]}
                        for index, row in enumerate(opened)]}
    if len(opened) == 1:
        reply["tab"] = f"id:{opened[0]['id']}"
    if notes:
        reply["warning"] = "; ".join(notes)
    return reply

def _page_count(profile: str) -> int | None:
    """How many page tabs answer on that profile, or None when it is silent.

    None is not zero. A browser whose endpoint is already gone cannot be asked
    what it holds, and neither can one whose port is held by something that is
    not that profile's browser — the difference decides whether `close` may
    warn about tabs.
    """
    port = cdp.port_of(profile)
    if not port or not cdp.answers(port):
        return None
    if not _pkg.endpoint_owner(profile, port)["verified"]:
        return None
    try:
        return len(cdp.page_rows(profile))
    except ControlError:
        return None

def _named_browser(selector: Selector) -> dict:
    """The one live browser a caller NAMED, verified like `attach` verifies.

    The same bar as `attach`: a running Chromium-family MAIN process of this
    machine that answers CDP — and here also one whose endpoint VERIFIES, so
    the pid about to be signalled is the process holding that port. Naming it
    is the consent; this is what makes the name mean something.
    """
    row = selector.find(_pkg.browsers())
    what = selector.what()
    if row is None:
        fail(ERR_NO_BROWSER,
             f"no running Chromium-family browser matches {what}")
    if not endpoint_of(row)["reachable"]:
        fail(ERR_CDP_UNREACHABLE,
             f"pid {row['pid']} does not answer CDP on port "
             f"{row['cdp']['port']} — a browser this CLI cannot reach is not "
             "one it stops by name")
    if not endpoint_of(row)["verified"]:
        fail(ERR_CDP_NOT_LOCAL,
             f"the endpoint on port {row['cdp']['port']} is not pid "
             f"{row['pid']}'s ({row['cdp'].get('reason') or 'unknown'}) — "
             "refusing to signal it")
    return row

def stop(browser: str = "", force: bool = False, port: int = 0, pid: int = 0,
         profile: str = "") -> dict:
    """Stop a browser, and prove it stopped.

    Two ways in, and the difference is CONSENT:

    * **no selector** — the managed browser on this CLI's own root, the one
      `open` starts. An attached browser is never a candidate here: attaching
      grants tab writes, not the right to stop somebody else's browser.
    * **`--pid N` / `--port N` / `--profile DIR`** — exactly the browser the
      caller NAMED, which is how one this CLI did not start (another tool's,
      or one it merely attached to) gets stopped on purpose. Naming it is the
      consent; `_named_browser` is the verification that the name points at a
      live, answering, VERIFIED Chromium-family process. One selector per
      call.

    Both paths take the profile's lock (so a `close` cannot land inside an
    `open`), refuse `tabs-open` while page tabs are open unless `--force`, and
    require the process AND the endpoint to be gone before `stopped: true`.
    Nothing is SIGKILLed, and a pid that no longer claims that profile is never
    signalled.
    """
    selector = Selector(port, pid, profile)
    selector.require_one("close", allow_none=True)
    named = selector.named()
    row = _named_browser(selector) if named else {}
    target = str(row["profile"]) if row else managed_profile(browser)
    with _pkg._lock(lock_path(target), "close") as lock:
        target_pid = as_int(row["pid"]) if row else pid_of(target)
        tabs = _page_count(target)
        if not target_pid:
            if not cdp.reachable(target):
                reply = {"ok": True, "stopped": False, "profile": target,
                         "tabs": tabs,
                         "reason": "no managed browser was running"}
                lock.warn(reply)
                return reply
            fail(ERR_BROWSER_NOT_STOPPED,
                 f"a browser answers on {target} but no Chromium process on "
                 "it can be identified — refusing to signal a process this "
                 "CLI did not start")
        if not pid_on_profile(target_pid, target):
            # re-verified IMMEDIATELY before the signal, on BOTH paths: the
            # managed path took its pid before `_page_count` (an HTTP GET plus
            # a /proc walk), so a browser that exited in that window could have
            # its pid recycled under the SIGTERM (a review flagged it).
            fail(ERR_BROWSER_NOT_STOPPED,
                 f"pid {target_pid} is gone, or no longer runs {target} — "
                 "nothing was signalled")
        if tabs and not force:
            fail(ERR_TABS_OPEN,
                 f"{tabs} page tab(s) are open in {target} and stopping the "
                 "browser closes them with it (Chromium exits with its last "
                 "window) — pass --force to stop it anyway, or take the tabs "
                 "first with `tab close ...` (`tab list` shows them)")
        try:
            os.kill(target_pid, signal.SIGTERM)
        except OSError as e:
            fail(ERR_BROWSER_NOT_STOPPED, f"cannot stop pid {target_pid}: {e}")
        _attempts, alive = poll(lambda: pid_alive(target_pid), timeout=STOP_WAIT_S,
                                interval=POLL_NORMAL, accept=lambda a: not a,
                                on_error=lambda _e: True)
        if alive:
            fail(ERR_BROWSER_NOT_STOPPED,
                 f"pid {target_pid} survived SIGTERM for {STOP_WAIT_S:g}s — "
                 "stop it yourself; this CLI does not SIGKILL a browser")
        _attempts, answering = poll(lambda: cdp.reachable(target),
                                    timeout=PORT_WAIT_S,
                                    interval=POLL_NORMAL,
                                    accept=lambda a: not a,
                                    on_error=lambda _e: True)
        if answering:
            fail(ERR_BROWSER_NOT_STOPPED,
                 f"pid {target_pid} is gone but the CDP endpoint on {target} "
                 "still answers")
        Path(pid_file(target)).unlink(missing_ok=True)
    reply = {"ok": True, "stopped": True, "pid": target_pid,
             "profile": target, "tabs": tabs,
             "forced": bool(tabs and force), "named": named,
             "managed": bool(row["managed"]) if row else True}
    lock.warn(reply)
    return reply
