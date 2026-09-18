"""browser — the managed profile, its lifecycle, and the tab operations.

One resolver for the browser (a PATH name; the profile is keyed by the
resolved binary), one endpoint (the port the browser itself publishes), and
every verb reads its own effect back before it reports:

* `launch`    — start (or adopt) the browser and prove a page tab exists
* `stop`      — signal only the pid running on OUR profile, then prove the
                process AND the endpoint are gone
* `tabs`      — the page tabs, id-sorted
* `new_tab`   — `Target.createTarget`, then the id is re-read from `/json`
* `close_tab` — `Target.closeTarget`, then the id must be ABSENT, or the
                refusal is `close-tab-not-verified`

The browser is never the user's: it runs on a managed profile of its own,
launched with `--remote-debugging-port=0` so the endpoint is the one it
publishes rather than an assumed 9222.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

# The project-level pyright run resolves these imports; the line-level ignore
# is for pi-lens's fallback index, which does not see the sibling modules.
from browser_control.lib import cdp  # pyright: ignore[reportMissingImports]
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ControlError,
    fail,
)

# The PATH names, in preference order. The profile is keyed off the basename
# of whichever resolves, so the same browser cannot end up with two profiles.
BROWSER_BINS = ("google-chrome-stable", "google-chrome", "chromium",
                "chromium-browser", "brave-browser", "microsoft-edge-stable",
                "vivaldi-stable")
# The EXECUTABLE names a Chromium-family pid can have: the PATH names above are
# wrappers (`google-chrome-stable` execs `chrome`), so an identity check
# against them alone would refuse a genuine browser.
BROWSER_EXES = ("chrome", "chromium", "chromium-browser", "google-chrome",
                "google-chrome-stable", "brave", "brave-browser", "msedge",
                "microsoft-edge", "vivaldi", "vivaldi-bin",
                "chrome-headless-shell", "headless_shell")
DEFAULT_ROOT = "~/.local/share/browser-control/cdp-profiles"
ROOT_ENV = "BROWSER_CONTROL_ROOT"
PID_FILE = ".pid"
LAUNCH_WAIT_S = 20.0
TAB_WAIT_S = 10.0
STOP_WAIT_S = 10.0
PORT_WAIT_S = 5.0


# ------------------------------------------------------------------ resolve
def root() -> str:
    """Where the managed profiles live (`BROWSER_CONTROL_ROOT` moves it)."""
    return os.path.abspath(os.path.expanduser(
        os.environ.get(ROOT_ENV) or DEFAULT_ROOT))


def binary(name: str = "") -> str:
    """The browser to drive: the one named, else the first on PATH.

    This is the ONLY resolver — `profile_dir` keys off the path it returns.
    """
    if name:
        found = shutil.which(name)
        if not found:
            raise ControlError("no-browser", f"{name!r} is not on PATH")
        return found
    for candidate in BROWSER_BINS:
        found = shutil.which(candidate)
        if found:
            return found
    raise ControlError(
        "no-browser",
        "no Chromium-family browser on PATH (looked for: "
        + ", ".join(BROWSER_BINS) + ")")


def profile_dir(binary_path: str) -> str:
    """The managed profile for a browser, keyed by the binary we run."""
    return os.path.join(root(), os.path.basename(binary_path))


def profiles() -> list[str]:
    """Every managed profile directory, sorted."""
    base = root()
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [os.path.join(base, name) for name in names
            if os.path.isdir(os.path.join(base, name))]


def live_profiles() -> list[str]:
    """The managed profiles a browser is answering CDP on right now."""
    return [path for path in profiles() if cdp.reachable(path)]


def resolve_profile(name: str = "") -> str:
    """The profile this call is about.

    The named browser's, else the ONE live managed browser, else the default
    browser's. Two live browsers refuse: the endpoint belongs to a browser
    identity, and silently picking one is how a verb drives the browser
    nobody asked about.
    """
    if name:
        return profile_dir(binary(name))
    live = live_profiles()
    if len(live) > 1:
        fail("ambiguous-browser",
             f"{len(live)} managed browsers are up "
             f"({', '.join(os.path.basename(p) for p in live)}) — "
             "pass --browser NAME")
    if len(live) == 1:
        return live[0]
    return profile_dir(binary())


def ensure_up(profile: str) -> None:
    """Refuse now when the verb needs a browser and none is drivable."""
    if not cdp.reachable(profile):
        fail("cdp-unreachable",
             f"no drivable browser on {profile} — run "
             "`browser-control-cli open`")


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
        fail("bad-args",
             f"refusing {text[:60]!r} as a URL (http(s) or about:blank only)")
    return text


def resolve_tab(rows: list[dict], spec: str) -> dict:
    """One page row for a spec: `id:<prefix>`, else a title/URL substring.

    Nothing, or several, refuses — never a silent first match.
    """
    needle = str(spec or "").strip()
    if not needle:
        fail("bad-args", "a TAB spec is required (id:<prefix> or a title/url "
                         "substring)")
    if needle.lower().startswith("id:"):
        want = needle[3:].strip().lower()
        if not want:
            fail("bad-args", "id: needs a target id prefix")
        hits = [r for r in rows
                if str(r.get("id") or "").lower().startswith(want)]
        if not hits:
            fail("no-page-tab",
                 f"no tab with id {want!r} (the tab was probably closed)")
    else:
        low = needle.lower()
        hits = [r for r in rows
                if low in str(r.get("url") or "").lower()
                or low in str(r.get("title") or "").lower()]
        if not hits:
            have = ", ".join(str(r.get("title") or "")[:30]
                             for r in rows[:4]) or "none"
            fail("no-page-tab", f"no tab matches {needle!r} (have: {have})")
    if len(hits) > 1:
        titles = ", ".join(str(r.get("title") or "")[:30] for r in hits[:5])
        fail("tab-ambiguous", f"{needle!r} matches {len(hits)} tabs: {titles}")
    return hits[0]


# ------------------------------------------------------------------ the pid
def _pid_file(profile: str) -> str:
    return os.path.join(profile, PID_FILE)


def _pid_alive(pid: int) -> bool:
    """A live process, and not a zombie: an unreaped child still answers
    `kill(pid, 0)`, which would refuse a stop that actually worked."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def _proc_text(pid: int, name: str) -> str:
    try:
        with open(f"/proc/{pid}/{name}", "rb") as f:
            raw = f.read(4096)
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _find_pid(profile: str) -> int:
    """The pid running ON this profile, from its own cmdline.

    The fallback when the pid file is gone (a crash restart). Only a MAIN
    process (no `--type=`) whose exe is Chromium-family counts, so this can
    never hand back a renderer — or a process we did not start.
    """
    marker = f"--user-data-dir={profile}"
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 0
    for entry in entries:
        try:
            pid = int(entry)
        except ValueError:
            continue
        cmdline = _proc_text(pid, "cmdline")
        if marker not in cmdline or "--type=" in cmdline:
            continue
        exe = os.path.basename(os.path.realpath(f"/proc/{pid}/exe"))
        if exe in BROWSER_EXES:
            return pid
    return 0


def _pid_of(profile: str) -> int:
    """The pid recorded for this profile, or the one running on it now."""
    pid = 0
    try:
        with open(_pid_file(profile)) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        pid = 0
    if pid and _pid_alive(pid):
        return pid
    return _find_pid(profile)


def _record_pid(profile: str, pid: int) -> None:
    # a missing pid file degrades `close`; it does not break `open`
    with contextlib.suppress(OSError):
        Path(_pid_file(profile)).write_text(str(pid), encoding="utf-8")


def _spawn(argv: list[str]) -> int:
    """Start a detached browser process, or refuse.

    Detached (`start_new_session`) so the browser outlives this CLI call; the
    pid it returns is what `stop` signals.
    """
    try:
        proc = subprocess.Popen(argv, start_new_session=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as e:
        raise ControlError("launch-failed",
                           f"cannot start {argv[0]}: {e}") from e
    return proc.pid


# ------------------------------------------------------------- read-backs
def _rows(profile: str) -> list[dict]:
    """The page tabs, as every verb reports them."""
    return cdp.rows_to_tabs(cdp.page_rows(profile))


def _wait_port(profile: str, timeout: float = LAUNCH_WAIT_S) -> bool:
    """The endpoint must ANSWER, not merely have a port file."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp.reachable(profile):
            return True
        time.sleep(0.25)
    return False


def _wait_rows(profile: str, timeout: float = TAB_WAIT_S) -> list[dict]:
    deadline = time.time() + timeout
    rows: list[dict] = []
    while time.time() < deadline:
        try:
            rows = _rows(profile)
        except Exception:                                      # noqa: BLE001
            rows = []                 # mid-startup: the endpoint is not up yet
        if rows:
            return rows
        time.sleep(0.25)
    return rows


def _wait_tab(profile: str, target_id: str,
              timeout: float = TAB_WAIT_S) -> dict | None:
    """The row for a target id, once the list shows it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for row in _rows(profile):
            if row["id"] == target_id:
                return row
        time.sleep(0.25)
    return None


def _wait_gone(profile: str, target_id: str,
               timeout: float = PORT_WAIT_S) -> bool:
    """True when the id is STILL open after a bounded wait.

    `Target.closeTarget` answers before the tab is gone, and a page with a
    beforeunload handler can keep it open: the list is the only honest proof.
    """
    deadline = time.time() + timeout
    while True:
        if target_id not in {r["id"] for r in _rows(profile)}:
            return False
        if time.time() >= deadline:
            return True
        time.sleep(0.2)


def _open_tab(profile: str, url: str) -> dict:
    """One new tab, verified: the id CDP made must appear in the tab list."""
    result = cdp.browser_call(profile, "Target.createTarget", {"url": url})
    target_id = str(result.get("targetId") or "")
    if not target_id:
        fail("no-page-tab", "CDP made no tab to name")
    row = _wait_tab(profile, target_id)
    if row is None:
        raise ControlError(
            "no-page-tab",
            f"CDP made a tab ({target_id}) that the tab list does not show")
    return row


# ------------------------------------------------------------------ verbs
def launch(url: str = "", browser: str = "") -> dict:
    """Start the managed browser (or adopt the running one) and prove it.

    A fresh start always gets a page (`about:blank` when no URL was asked
    for). An already-running browser is handed the URL as a new tab; with no
    URL it is left alone. `started` says which of the two happened.
    """
    path = binary(browser)
    profile = profile_dir(path)
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError as e:
        raise ControlError("profile-unusable",
                           f"cannot create {profile}: {e}") from e
    wanted = safe_url(url) if url else ""
    already = cdp.reachable(profile)
    opened = None
    if already:
        if wanted:
            opened = _open_tab(profile, wanted)
    else:
        _record_pid(profile, _spawn([path, *flags(profile),
                                     wanted or "about:blank"]))
        if not _wait_port(profile):
            fail("launch-failed",
                 f"started {path} on {profile} but no CDP endpoint answered "
                 f"within {LAUNCH_WAIT_S:g}s")
    rows = _wait_rows(profile)
    if not rows:
        fail("no-page-tab",
             f"{path} is up on {profile} but shows no page tab — pass a URL "
             "(browser-control-cli open https://…)")
    reply = {"ok": True, "started": not already, "browser": path,
             "profile": profile, "port": cdp.port_of(profile),
             "pid": _pid_of(profile), "tabs": rows}
    if opened:
        reply["tab"] = f"id:{opened['id']}"
    return reply


def stop(browser: str = "") -> dict:
    """Stop the managed browser this CLI started, and prove it stopped.

    Only the pid running on THIS profile's user-data-dir is signalled —
    recorded, else re-found by cmdline and exe — and the reply requires the
    process AND the endpoint to be gone. Nothing is SIGKILLed.
    """
    profile = resolve_profile(browser)
    pid = _pid_of(profile)
    if not pid:
        if not cdp.reachable(profile):
            return {"ok": True, "stopped": False, "profile": profile,
                    "reason": "no managed browser was running"}
        fail("browser-not-stopped",
             f"a browser answers on {profile} but no Chromium process on it "
             "can be identified — refusing to signal a process this CLI did "
             "not start")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        fail("browser-not-stopped", f"cannot stop pid {pid}: {e}")
    deadline = time.time() + STOP_WAIT_S
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.2)
    if _pid_alive(pid):
        fail("browser-not-stopped",
             f"pid {pid} survived SIGTERM for {STOP_WAIT_S:g}s — stop it "
             "yourself; this CLI does not SIGKILL a browser")
    deadline = time.time() + PORT_WAIT_S
    while time.time() < deadline and cdp.reachable(profile):
        time.sleep(0.2)
    if cdp.reachable(profile):
        fail("browser-not-stopped",
             f"pid {pid} is gone but the CDP endpoint on {profile} still "
             "answers")
    Path(_pid_file(profile)).unlink(missing_ok=True)
    return {"ok": True, "stopped": True, "pid": pid, "profile": profile}

def tabs(browser: str = "") -> dict:
    """Every page tab, id-sorted."""
    profile = resolve_profile(browser)
    ensure_up(profile)
    rows = _rows(profile)
    return {"ok": True, "profile": profile, "port": cdp.port_of(profile),
            "count": len(rows), "tabs": rows}


def new_tab(url: str = "", browser: str = "") -> dict:
    """Open ONE tab and name it (`id:<target id>`)."""
    profile = resolve_profile(browser)
    ensure_up(profile)
    row = _open_tab(profile, safe_url(url) if url else "about:blank")
    return {"ok": True, "tab": f"id:{row['id']}", "id": row["id"],
            "url": row["url"], "title": row["title"],
            "count": len(_rows(profile))}


def close_tab(spec: str, browser: str = "") -> dict:
    """Close ONE tab and prove it is gone."""
    profile = resolve_profile(browser)
    ensure_up(profile)
    target = resolve_tab(cdp.page_rows(profile), spec)
    target_id = str(target["id"])
    cdp.browser_call(profile, "Target.closeTarget", {"targetId": target_id})
    if _wait_gone(profile, target_id):
        fail("close-tab-not-verified",
             f"tab {target_id} is still open after the close")
    return {"ok": True,
            "closed": {"id": target_id,
                       "title": str(target.get("title") or ""),
                       "url": str(target.get("url") or "")},
            "count": len(_rows(profile))}
