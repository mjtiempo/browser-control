"""browser — the managed profile, its lifecycle, and the tab operations.

One resolver for the browser (a PATH name; the profile is keyed by the
resolved binary), one endpoint (the port the browser itself publishes), and
every verb reads its own effect back before it reports:

* `launch`    — start (or adopt) the browser and prove a page tab exists
* `stop`      — signal only the pid running on OUR profile, then prove the
                process AND the endpoint are gone
* `new_tab`   — `Target.createTarget`, then the id is re-read from `/json`
* `close_tabs`— `Target.closeTarget` per tab, then every id must be ABSENT
* `nav`       — the assignment returns before the document moves, so the reply
                is the address AS OBSERVED and the load state; a tab that
                never left, and the browser's own error page, are refusals
* `history`   — back/forward, verified by the address actually changing
* `reload`    — verified by `performance.timeOrigin`: a NEW document

A write goes to a browser this CLI manages, or to one ATTACHED to it.
`attach` grants tab writes only — `close` never stops an attached browser.
The browser is never the user's: it runs on a managed profile of its own,
launched with `--remote-debugging-port=0` so the endpoint is the one it
publishes rather than an assumed 9222.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

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
# What a browser uses when it was launched WITHOUT `--user-data-dir`. Only
# used to say which profile a running browser is on, so a missing entry or a
# stale path costs an empty string, never a wrong claim.
DEFAULT_PROFILES = {
    "chrome": "~/.config/google-chrome",
    "google-chrome": "~/.config/google-chrome",
    "google-chrome-stable": "~/.config/google-chrome",
    "chromium": "~/.config/chromium",
    "chromium-browser": "~/.config/chromium",
    "brave": "~/.config/BraveSoftware/Brave-Browser",
    "brave-browser": "~/.config/BraveSoftware/Brave-Browser",
    "msedge": "~/.config/microsoft-edge",
    "microsoft-edge": "~/.config/microsoft-edge",
    "vivaldi": "~/.config/vivaldi",
    "vivaldi-bin": "~/.config/vivaldi",
}
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


def _norm(path: object) -> str:
    """One identity for a profile path: absolute, `~` expanded."""
    return os.path.abspath(os.path.expanduser(str(path or "")))


def ensure_up(profile: str) -> None:
    """Refuse now when the verb needs a browser and none is drivable."""
    if not cdp.reachable(profile):
        fail("cdp-unreachable",
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
        fail("bad-args",
             f"refusing {text[:60]!r} as a URL (http(s) or about:blank only)")
    return text


def _match_spec(tabs: list[dict], spec: str) -> list[dict]:
    """The tabs of ONE browser a spec names — 0, 1 or several.

    `id:<prefix>` matches ids, anything else a title/URL substring; both
    case-insensitive. Pure and per-browser, so the cross-browser resolver can
    ask every browser and decide on the whole picture.
    """
    needle = str(spec or "").strip()
    if not needle:
        fail("bad-args", "a TAB spec is required (id:<prefix> or a title/url "
                         "substring)")
    if needle.lower().startswith("id:"):
        want = needle[3:].strip().lower()
        if not want:
            fail("bad-args", "id: needs a target id prefix")
        return [t for t in tabs
                if str(t.get("id") or "").lower().startswith(want)]
    low = needle.lower()
    return [t for t in tabs
            if low in str(t.get("url") or "").lower()
            or low in str(t.get("title") or "").lower()]


def resolve_tab(rows: list[dict], spec: str) -> dict:
    """One page row for a spec, within ONE browser's tabs.

    Nothing, or several, refuses — never a silent first match.
    """
    needle = str(spec or "").strip()
    hits = _match_spec(rows, spec)
    if not hits:
        if needle.lower().startswith("id:"):
            fail("no-page-tab", f"no tab with id {needle[3:]!r} "
                                "(the tab was probably closed)")
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


def _cmdline_value(cmd: str, flag: str) -> str:
    """The value of `--flag=value` (or `--flag value`) in a /proc cmdline.

    Both spellings: Chrome accepts both and a launcher may write either. ""
    when the flag is absent.
    """
    parts = str(cmd).split()
    for index, part in enumerate(parts):
        if part.startswith(flag + "="):
            return part[len(flag) + 1:]
        if part == flag and index + 1 < len(parts):
            return parts[index + 1]
    return ""


def _main_processes() -> list[tuple[int, str, str]]:
    """(pid, exe, cmdline) for every Chromium-family MAIN process here.

    A renderer, GPU or zygote process carries `--type=`; the main process does
    not, and it is the one that owns a profile and answers CDP.
    """
    found: list[tuple[int, str, str]] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        try:
            pid = int(entry)
        except ValueError:
            continue
        cmd = _proc_text(pid, "cmdline")
        if not cmd or "--type=" in cmd:
            continue
        exe = os.path.basename(os.path.realpath(f"/proc/{pid}/exe"))
        if exe in BROWSER_EXES:
            found.append((pid, exe, cmd))
    return sorted(found)


def _find_pid(profile: str) -> int:
    """The pid running ON this profile, from its own cmdline.

    The fallback when the pid file is gone (a crash restart). Only a MAIN
    process (no `--type=`) whose exe is Chromium-family counts, so this can
    never hand back a renderer — or a process we did not start.
    """
    marker = f"--user-data-dir={profile}"
    for pid, _exe, cmd in _main_processes():
        if marker in cmd:
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


# ------------------------------------------------------------ what is running
def _to_int(value: object) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _default_profile(exe: str) -> str:
    """The default data directory of a browser executable, when it exists."""
    raw = DEFAULT_PROFILES.get(exe, "")
    if not raw:
        return ""
    path = os.path.abspath(os.path.expanduser(raw))
    return path if os.path.isdir(path) else ""


def _is_managed(profile: str) -> bool:
    """Is this profile one of ours? Prefix-safe, so a sibling root is not."""
    return bool(profile) and os.path.abspath(profile).startswith(
        os.path.abspath(root()) + os.sep)


def browsers() -> list[dict]:
    """Every Chromium-family browser RUNNING here, ours or the user's.

    One /proc pass over the main processes: each with the profile it runs on
    (`--user-data-dir`, else the default data directory for that executable)
    and whether a DevTools endpoint answers for it — which is what makes it
    drivable. `managed` says the profile is one this CLI owns and `attached`
    that it was attached for tab writes, so the answer is about the whole
    machine rather than our own corner of it.
    """
    rows: list[dict] = []
    attached = _attached()
    for pid, exe, cmd in _main_processes():
        flag = _cmdline_value(cmd, "--user-data-dir")
        profile = flag or _default_profile(exe)
        port = cdp.port_of(profile) if profile else 0
        if not port:
            port = _to_int(_cmdline_value(cmd, "--remote-debugging-port"))
        reachable = cdp.answers(port)
        endpoint: dict = {"port": port, "reachable": reachable}
        if reachable:
            endpoint["tabs"] = len(cdp.page_rows_at(port))
        rows.append({"pid": pid, "exe": exe,
                     "path": os.path.realpath(f"/proc/{pid}/exe"),
                     "profile": profile,
                     "profile_from": ("flag" if flag else
                                      ("default" if profile else "")),
                     "managed": _is_managed(profile),
                     "attached": _norm(profile) in attached,
                     "cdp": endpoint})
    # ours first, then the attached ones, then whatever can be driven, by pid
    return sorted(rows, key=lambda r: (not r["managed"], not r["attached"],
                                       not r["cdp"]["reachable"], r["pid"]))


def list_browsers() -> dict:
    """`list`: every browser running here, drivable or not."""
    rows = browsers()
    return {"ok": True, "count": len(rows),
            "drivable": sum(1 for r in rows if r["cdp"]["reachable"]),
            "browsers": rows}


ATTACH_FILE = "attached.json"


def _attached() -> dict[str, dict]:
    """The attach records, keyed by absolute profile path.

    A missing, unreadable or malformed file is {}: an attachment that cannot
    be read is not an authorization to write anywhere.
    """
    try:
        with open(os.path.join(root(), ATTACH_FILE)) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, list):
        return {}
    return {_norm(row["profile"]): row for row in data
            if isinstance(row, dict) and row.get("profile")}


def _write_attached(records: dict[str, dict]) -> None:
    """Replace the attach file. One scratch file and a rename, so a crash
    cannot leave a half-written list of authorizations."""
    path = os.path.join(root(), ATTACH_FILE)
    temp = f"{path}.new"
    try:
        os.makedirs(root(), exist_ok=True)
        with open(temp, "w") as handle:
            json.dump(sorted(records.values(),
                             key=lambda r: str(r.get("profile"))),
                      handle, indent=1)
        os.replace(temp, path)
    except OSError as e:
        fail("attach-failed", f"cannot write {path}: {e}")


def is_attached(profile: str) -> bool:
    """Is this profile attached for tab writes?"""
    return _norm(profile) in _attached()


def _narrow(rows: list[dict], browser: str) -> list[dict]:
    """The rows whose browser a name matches: its executable, or the basename
    of its profile — the name `open --browser NAME` keys a profile by."""
    if not browser:
        return rows
    wanted = os.path.basename(str(browser).strip())
    return [r for r in rows
            if wanted in (r["exe"], os.path.basename(str(r["profile"])))]


def _writable(browser: str = "") -> list[dict]:
    """The running browsers a WRITE may target: ours, plus the attached ones."""
    return _narrow([r for r in browsers()
                    if (r["managed"] or r["attached"])
                    and r["cdp"]["reachable"]], browser)


def _writable_profile(browser: str = "") -> str:
    """The profile a tab write goes to: the named browser's, else the ONE
    writable browser that is up, else this CLI's own (which `open` starts).

    Two writable browsers refuse — the endpoint belongs to a browser identity,
    and picking one silently is how a tab lands in the browser nobody asked
    about. `attach`/`detach` decide which browsers are candidates; `--browser`
    picks among them.
    """
    rows = _writable(browser)
    if len(rows) > 1:
        names = ", ".join(os.path.basename(str(r["profile"]))
                          + (" (attached)" if r["attached"] else "")
                          for r in rows)
        fail("ambiguous-browser",
             f"{len(rows)} writable browsers are up ({names}) — pass "
             "--browser NAME, or `detach` one")
    if rows:
        return str(rows[0]["profile"])
    return profile_dir(binary(browser)) if browser else profile_dir(binary())


def managed_profile(browser: str = "") -> str:
    """The profile a LIFECYCLE verb (stop) is about: ours, or the one `open`
    would start. An attached browser is never a candidate — attaching grants
    tab writes, not the right to stop somebody else's browser."""
    rows = _narrow([r for r in browsers()
                    if r["managed"] and r["cdp"]["reachable"]], browser)
    if len(rows) > 1:
        names = ", ".join(os.path.basename(str(r["profile"])) for r in rows)
        fail("ambiguous-browser",
             f"{len(rows)} managed browsers are up ({names}) — pass "
             "--browser NAME")
    if rows:
        return str(rows[0]["profile"])
    return profile_dir(binary(browser)) if browser else profile_dir(binary())


def attachments() -> dict:
    """`attach --list`: what is attached, and whether it is still there."""
    records = _attached()
    live = {_norm(r["profile"]): r for r in browsers()}
    rows: list[dict] = []
    for key in sorted(records):
        record = dict(records[key])
        row = live.get(key)
        record["running"] = row is not None
        record["reachable"] = bool(row and row["cdp"]["reachable"])
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
    given = [name for name, value in (("--port", port), ("--pid", pid),
                                      ("--profile", profile)) if value]
    if len(given) != 1:
        fail("bad-args",
             "attach: name ONE browser — --port N, --pid N or --profile DIR")
    rows = browsers()
    if pid:
        row = next((r for r in rows if r["pid"] == _to_int(pid)), None)
    elif profile:
        want = _norm(profile)
        row = next((r for r in rows if _norm(r["profile"]) == want), None)
    else:
        want_port = _to_int(port)
        row = next((r for r in rows
                    if _to_int(r["cdp"]["port"]) == want_port), None)
    if row is None:
        fail("no-browser",
             f"no running Chromium-family browser matches {given[0]}")
    if not row["cdp"]["reachable"]:
        fail("cdp-unreachable",
             f"pid {row['pid']} does not answer CDP on port "
             f"{row['cdp']['port']} — attach needs a live endpoint")
    record = {"profile": _norm(row["profile"]), "pid": row["pid"],
              "port": _to_int(row["cdp"]["port"]), "exe": row["exe"],
              "managed": row["managed"],
              "attached_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    records = _attached()
    already = record["profile"] in records
    records[record["profile"]] = record
    _write_attached(records)
    return {"ok": True, "attached": True, "already": already,
            "browser": {"pid": record["pid"], "exe": record["exe"],
                        "profile": record["profile"],
                        "managed": record["managed"]},
            "cdp": {"port": record["port"], "reachable": True},
            "note": ("tab writes only — `close` will not stop it; "
                     "`detach` revokes this")}


def detach(port: int = 0, pid: int = 0, profile: str = "",
           detach_all: bool = False) -> dict:
    """`detach`: take the tab-write authorization away again."""
    if detach_all:
        if port or pid or profile:
            fail("bad-args", "detach: --all takes no other selector")
        keys = sorted(_attached())
        _write_attached({})
        return {"ok": True, "detached": keys, "count": 0}
    given = [name for name, value in (("--port", port), ("--pid", pid),
                                      ("--profile", profile)) if value]
    if len(given) != 1:
        fail("bad-args",
             "detach: name ONE browser — --port N, --pid N, --profile DIR, "
             "or --all")
    records = _attached()
    if profile:
        keys = [key for key in records if key == _norm(profile)]
    elif pid:
        keys = [key for key, rec in records.items()
                if _to_int(rec.get("pid")) == _to_int(pid)]
    else:
        keys = [key for key, rec in records.items()
                if _to_int(rec.get("port")) == _to_int(port)]
    if not keys:
        fail("not-attached", f"nothing is attached for {given[0]}")
    for key in keys:
        records.pop(key, None)
    _write_attached(records)
    return {"ok": True, "detached": keys, "count": len(records)}


def _drivable(browser: str = "") -> list[dict]:
    """The running browsers that answer CDP, sorted by browser.

    `browser` narrows to the one named — its executable, or the basename of
    its profile, which is the name `open --browser NAME` keys a profile by.
    One name can match two browsers (the same browser on two profiles, as a
    tool that manages its own copy makes likely): the MANAGED one wins, since
    that is the browser this CLI would drive and the only one a write may
    touch. An empty result is []: whether that is a refusal is the caller's
    question.
    """
    rows = [r for r in browsers() if r["cdp"]["reachable"]]
    matches = _narrow(rows, browser)
    if browser and matches:
        rows = [r for r in matches if r["managed"] or r["attached"]] or matches
    else:
        rows = matches
    return sorted(rows, key=lambda r: (r["exe"], str(r["profile"])))


def _tabs_of(row: dict) -> tuple[list[dict], str]:
    """(page tabs, error) for one browser row.

    A browser that stopped answering between the probe and the read is
    REPORTED: an empty list is "no tabs", an error is "no answer".
    """
    try:
        port = _to_int(row["cdp"]["port"])
        return cdp.rows_to_tabs(cdp.page_rows_at(port)), ""
    except ControlError as e:
        return [], e.message


def _brief(row: dict) -> dict:
    """One browser row, small enough to ride along in a tab reply."""
    return {"pid": row["pid"], "exe": row["exe"], "profile": row["profile"],
            "managed": row["managed"], "attached": row["attached"],
            "port": _to_int(row["cdp"]["port"])}


def _row(row: dict) -> dict:
    """One browser row without its endpoint block (reported apart)."""
    return {key: row[key] for key in ("pid", "exe", "path", "profile",
                                      "profile_from", "managed",
                                      "attached")}


def _endpoint_details(row: dict) -> dict:
    """One browser row's endpoint, with the version it reports itself."""
    details = dict(row["cdp"])
    if details.get("reachable"):
        version = cdp.version_at(_to_int(details.get("port")))
        details["version"] = str(version.get("Browser") or "")
        details["protocol"] = str(version.get("Protocol-Version") or "")
        details["user_agent"] = str(version.get("User-Agent") or "")
    return details


def list_tabs(browser: str = "") -> dict:
    """`tab list`: the page tabs of every DRIVABLE browser, by browser.

    Sorted by browser, which is the order `list` uses and the reason a tab
    handle only means something next to the browser it came from: two tabs
    with the same title in two browsers are two rows, not one. `browser`
    narrows the answer to one of them.
    """
    groups: list[dict] = []
    total = 0
    for row in _drivable(browser):
        tabs, error = _tabs_of(row)
        group = {**_brief(row), "tabs": tabs, "count": len(tabs)}
        if error:
            group["error"] = error
        total += len(tabs)
        groups.append(group)
    return {"ok": True, "count": total, "browsers": groups}


def browser_info(browser: str = "") -> dict:
    """`info`: the browser this CLI would drive — or the one named — and its
    endpoint.

    "Would drive" includes an ATTACHED browser: that is the one a `tab` write
    goes to. `running: false` is an ANSWER, not a refusal — which browser
    `open` would start, and where its profile lives, is knowable without one
    running.
    """
    name = os.path.basename(str(browser).strip())
    rows = browsers()
    if name:
        matches = [r for r in rows if name in (
            r["exe"], os.path.basename(str(r["profile"])))]
        # ours first, then an attached one: those are the browsers a tab
        # write could reach, and the one `info` is really about
        writable = next((r for r in matches
                         if r["managed"] or r["attached"]), None)
        row = writable if writable is not None else (
            matches[0] if matches else None)
        if row is not None:
            return {"ok": True, "running": True, "browser": _row(row),
                    "cdp": _endpoint_details(row)}
        path = binary(browser)          # refuses no-browser when unknown
    else:
        live = [r for r in rows
                if (r["managed"] or r["attached"]) and r["cdp"]["reachable"]]
        if len(live) > 1:
            names = ", ".join(os.path.basename(str(r["profile"]))
                              + (" (attached)" if r["attached"] else "")
                              for r in live)
            fail("ambiguous-browser",
                 f"{len(live)} writable browsers are up ({names}) — name one "
                 "with --browser NAME")
        if live:
            return {"ok": True, "running": True, "browser": _row(live[0]),
                    "cdp": _endpoint_details(live[0])}
        path = binary()
    profile = profile_dir(path)
    return {"ok": True, "running": False,
            "browser": {"pid": 0, "exe": os.path.basename(path),
                        "path": path, "profile": profile,
                        "profile_from": "managed", "managed": True,
                        "attached": False},
            "cdp": {"port": 0, "reachable": False}}


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


def _wait_tabs(profile: str, ids: list[str],
               timeout: float = TAB_WAIT_S) -> tuple[dict, list[str]]:
    """(rows by id, ids still missing) after ONE bounded poll."""
    deadline = time.time() + timeout
    while True:
        try:
            rows = _rows(profile)
        except ControlError:
            rows = []                  # mid-startup, or the browser is gone
        seen = {r["id"]: r for r in rows if r["id"] in ids}
        missing = [i for i in ids if i not in seen]
        if not missing or time.time() >= deadline:
            return seen, missing
        time.sleep(0.25)


def _wait_url(profile: str, url: str,
              timeout: float = TAB_WAIT_S) -> dict | None:
    """The tab a just-started browser opened for `url`, once it shows it.

    Matched by the requested URL, tolerant of the trailing slash the browser
    adds: a startup page has no id we were told, so the URL is what names it.
    """
    want = url.rstrip("/")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for row in _rows(profile):
            got = str(row["url"]).rstrip("/")
            if got == want or got.startswith(want):
                return row
        time.sleep(0.25)
    return None


def _wait_ids_gone(profile: str, ids: list[str],
                   timeout: float = PORT_WAIT_S) -> list[str]:
    """The ids STILL in that browser's tab list after a bounded wait.

    `Target.closeTarget` answers before the tab is gone, and a page with a
    beforeunload handler can keep it open: the list is the only honest proof,
    and its survivors ARE the report.
    """
    deadline = time.time() + timeout
    while True:
        try:
            open_ids = {r["id"] for r in _rows(profile)}
        except ControlError:
            open_ids = set()          # the browser is gone: so are its tabs
        left = [i for i in ids if i in open_ids]
        if not left or time.time() >= deadline:
            return left
        time.sleep(0.2)


def _open_tabs(profile: str, urls: list[str]) -> list[dict]:
    """One new tab per URL: every id from CDP, all verified together.

    `Target.createTarget` answers with the id it made and ONE poll loop then
    proves every id is in the tab list. A partial result refuses and names
    what is missing — and the tabs that did open stay open, because a refusal
    is not a reason to destroy work.
    """
    ids: list[str] = []
    for url in urls:
        result = cdp.browser_call(profile, "Target.createTarget", {"url": url})
        target_id = str(result.get("targetId") or "")
        if not target_id:
            fail("no-page-tab", f"CDP made no tab for {url!r}")
        ids.append(target_id)
    found, missing = _wait_tabs(profile, ids)
    if missing:
        fail("no-page-tab",
             f"{len(missing)} of {len(ids)} tabs never showed up in the tab "
             "list: " + ", ".join(missing[:4]))
    return [found[target_id] for target_id in ids]


# ------------------------------------------------------------------ verbs
def launch(urls: list[str] | None = None, browser: str = "") -> dict:
    """Start (or adopt) the managed browser and prove the pages are there.

    `urls` is what to open: a fresh start loads the FIRST as its startup page
    and opens the rest as tabs; an already-running browser is handed each as
    a new tab; an empty list just makes sure a page exists. `started` says
    which of the two happened and `opened` names the tabs this call made.
    """
    wanted = [safe_url(url) for url in (urls or [])]
    path = binary(browser)
    profile = profile_dir(path)
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError as e:
        raise ControlError("profile-unusable",
                           f"cannot create {profile}: {e}") from e
    already = cdp.reachable(profile)
    requests: list[str] = []
    opened: list[dict] = []
    if already:
        if wanted:
            requests = wanted
            opened = _open_tabs(profile, wanted)
    else:
        first = wanted[0] if wanted else "about:blank"
        requests = [first, *wanted[1:]]
        _record_pid(profile, _spawn([path, *flags(profile), first]))
        if not _wait_port(profile):
            fail("launch-failed",
                 f"started {path} on {profile} but no CDP endpoint answered "
                 f"within {LAUNCH_WAIT_S:g}s")
        row = _wait_url(profile, first)
        if row is None:
            fail("no-page-tab",
                 f"{path} is up on {profile} but shows no tab for {first!r}")
        opened = [row]
        if len(wanted) > 1:
            opened += _open_tabs(profile, wanted[1:])
    rows = _wait_rows(profile)
    if not rows:
        fail("no-page-tab",
             f"{path} is up on {profile} but shows no page tab — pass a URL "
             "(browser-control-cli open https://…)")
    reply = {"ok": True, "started": not already, "browser": path,
             "profile": profile, "port": cdp.port_of(profile),
             "pid": _pid_of(profile), "tabs": rows,
             "opened": [{"requested": requests[index], "id": row["id"],
                         "url": row["url"], "title": row["title"]}
                        for index, row in enumerate(opened)]}
    if len(opened) == 1:
        reply["tab"] = f"id:{opened[0]['id']}"
    return reply


def stop(browser: str = "") -> dict:
    """Stop the managed browser this CLI started, and prove it stopped.

    Only the pid running on THIS profile's user-data-dir is signalled —
    recorded, else re-found by cmdline and exe — and the reply requires the
    process AND the endpoint to be gone. Nothing is SIGKILLed.
    """
    profile = managed_profile(browser)
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


def _resolve_across(specs: list[str], browser: str,
                    for_write: bool) -> list[tuple[dict, dict, int]]:
    """[(browser row, tab, index)] for every spec, across the drivable ones.

    EVERY spec is resolved before any of them is acted on, so an ambiguous or
    missing one cannot leave a half-applied change; two specs that name the
    same tab collapse into one entry. `for_write` refuses a tab in a browser
    this CLI did not start: reads cover every drivable browser, writes only
    its own.
    """
    rows = _drivable(browser)
    if not rows:
        fail("cdp-unreachable",
             "no drivable browser"
             + (f" matching {browser!r}" if browser else "")
             + " — run `browser-control-cli open`")
    tabs_of = {row["pid"]: _tabs_of(row)[0] for row in rows}
    found: list[tuple[dict, dict, int]] = []
    for spec in specs:
        hits: list[tuple[dict, dict, int]] = []
        for row in rows:
            tabs = tabs_of[row["pid"]]
            positions = {str(t["id"]): index
                         for index, t in enumerate(tabs)}
            hits += [(row, tab, positions[str(tab["id"])])
                     for tab in _match_spec(tabs, spec)]
        if not hits:
            have = ", ".join(f'{t["title"][:20] or t["url"][:20]} '
                             f'({r["exe"]})'
                             for r in rows for t in tabs_of[r["pid"]][:2])
            fail("no-page-tab",
                 f"no tab matches {spec!r} (have: {have or 'none'})")
        if len(hits) > 1:
            where = ", ".join(
                f'{t["title"][:20] or t["id"][:8]} in {r["exe"]}'
                f':{os.path.basename(str(r["profile"]))}'
                for r, t, _index in hits[:4])
            fail("tab-ambiguous",
                 f"{spec!r} matches {len(hits)} tabs: {where}")
        row, tab, index = hits[0]
        if for_write and not (row["managed"] or row["attached"]):
            fail("not-managed",
                 f"{spec!r} is in {row['exe']} on {row['profile']}, which "
                 "this CLI neither manages nor has attached — `tab list` "
                 "and `tab info` read every drivable browser, but a write "
                 "needs `attach --port N` (or a browser this CLI started)")
        if not any(str(t["id"]) == str(tab["id"]) for _r, t, _i in found):
            found.append((row, tab, index))
    return found


def _tab_count() -> int:
    """How many page tabs the drivable browsers show right now."""
    return sum(len(_tabs_of(row)[0]) for row in _drivable())


def tab_info(spec: str, browser: str = "") -> dict:
    """`tab info`: one tab, resolved across the drivable browsers.

    The read side of a handle: which browser owns it, and what it is now.
    """
    (row, tab, index), = _resolve_across([spec], browser, for_write=False)
    return {"ok": True, "tab": {**tab, "index": index},
            "browser": _brief(row)}


def close_tabs(specs: list[str], browser: str = "") -> dict:
    """`tab close`: close every tab the specs name, and prove the set is gone.

    All specs resolve first (across the drivable browsers), so nothing is
    closed when one of them is ambiguous, missing, or in a browser this CLI
    did not start. The close then goes per browser, and every requested id is
    read back: a survivor is a refusal that names it.
    """
    if not specs:
        fail("bad-args",
             "tab close: at least one TAB spec is required (id:<prefix> or a "
             "title/url substring)")
    found = _resolve_across(list(specs), browser, for_write=True)
    by_profile: dict[str, list[str]] = {}
    for row, tab, _index in found:
        by_profile.setdefault(str(row["profile"]), []).append(str(tab["id"]))
    for profile, ids in by_profile.items():
        for target_id in ids:
            cdp.browser_call(profile, "Target.closeTarget",
                             {"targetId": target_id})
    survivors: list[str] = []
    for profile, ids in by_profile.items():
        survivors += _wait_ids_gone(profile, ids)
    if survivors:
        fail("close-tab-not-verified",
             f"{len(survivors)} of {len(found)} tabs are still open: "
             + ", ".join(str(i)[:10] for i in survivors[:4]))
    return {"ok": True,
            "closed": [{"id": tab["id"], "title": tab["title"],
                        "url": tab["url"], "pid": row["pid"],
                        "managed": row["managed"]}
                       for row, tab, _index in found],
            "count": _tab_count()}


def new_tab(urls: list[str] | None = None, browser: str = "") -> dict:
    """Open one tab per URL (`about:blank` when none was given), all named.

    A write, so it goes to a MANAGED browser: the one `--browser` names, else
    the live managed one — never a browser this CLI did not start.
    """
    # the URL policy comes FIRST: an address this tool will never open is
    # wrong whether or not a browser is running (the order `launch` uses)
    wanted = [safe_url(url) for url in (urls or [])] or ["about:blank"]
    profile = _writable_profile(browser)
    ensure_up(profile)
    opened = _open_tabs(profile, wanted)
    reply: dict = {
        "ok": True, "count": len(_rows(profile)),
        "opened": [{"requested": url, "id": row["id"], "url": row["url"],
                    "title": row["title"]}
                   for url, row in zip(wanted, opened, strict=True)]}
    if len(opened) == 1:
        reply.update({"tab": f"id:{opened[0]['id']}", "id": opened[0]["id"],
                      "url": opened[0]["url"], "title": opened[0]["title"]})
    return reply


# ---------------------------------------------------------------- the page
# A page verb acts on ONE tab: `--tab SPEC`, else the only page tab there is.
# It drives the tab over the tab's OWN websocket (`cdp.target_ws`), so the
# tab it acts on cannot change between the resolution and the assignment.
NAV_TIMEOUT_S = 25.0        # a cold page; long enough, short enough to report
NAV_MOVE_S = 10.0           # how long the tab gets to LEAVE the old document
HISTORY_TIMEOUT_S = 10.0
RELOAD_TIMEOUT_S = 20.0
# One expression for "the document is there and parsed", read by both the
# single read and the poll.
READY_EXPR = "document.readyState + (document.body ? '+body' : '')"


def _one_tab(spec: str, browser: str, for_write: bool) -> tuple[dict, dict]:
    """(browser row, tab row) for the ONE tab a page verb acts on.

    With a spec: the same resolution every other verb uses (one match, or a
    refusal naming the candidates). Without one: the only page tab there is —
    several tabs refuse `tab-ambiguous` rather than let a navigation land in
    the tab nobody named.
    """
    if spec:
        row, tab, _index = _resolve_across([spec], browser, for_write)[0]
        return row, tab
    rows = _writable(browser) if for_write else _drivable(browser)
    if not rows:
        fail("cdp-unreachable",
             "no " + ("writable" if for_write else "drivable") + " browser"
             + (f" matching {browser!r}" if browser else "")
             + " — run `browser-control-cli open`, or attach one")
    pairs = [(row, tab) for row in rows for tab in _tabs_of(row)[0]]
    if not pairs:
        fail("no-page-tab",
             "no page tabs to act on — open one with "
             "`browser-control-cli tab URL`")
    if len(pairs) > 1:
        where = ", ".join(f'{str(t["title"])[:20] or str(t["url"])[:30]} '
                          f'({r["exe"]})' for r, t in pairs[:5])
        fail("tab-ambiguous",
             f"{len(pairs)} page tabs are open — name one with --tab SPEC "
             f"(id:<prefix> or a title/url substring): {where}")
    return pairs[0]


def _same_page(left: object, right: object) -> bool:
    """Do two URLs name the same page?

    The browser normalises what it was given (`https://a.com` becomes
    `https://a.com/`), so a trailing slash alone is not a different page —
    while a different query or fragment is.
    """
    return str(left or "").rstrip("/") == str(right or "").rstrip("/")


def _eval(profile: str, target_id: str, expression: str,
          timeout: float = 15.0) -> Any:
    """Evaluate one expression on that tab's own connection."""
    ws = cdp.target_ws(cdp.port_of(profile), target_id)
    return cdp.evaluate(ws, expression, timeout=timeout)


def _href(profile: str, target_id: str) -> str:
    """The tab's address, or "" when it cannot be read (mid-navigation)."""
    try:
        return str(_eval(profile, target_id, "location.href", timeout=5) or "")
    except ControlError:
        return ""


def _wait_document(profile: str, target_id: str,
                   timeout: float = NAV_TIMEOUT_S) -> bool:
    """Poll until the document is complete AND parsed, or the deadline.

    Every sample is bounded by what is left of the budget: a page that stops
    answering must not spend a fresh 15s on each attempt.
    """
    deadline = time.time() + timeout
    while True:
        with contextlib.suppress(ControlError):
            if _eval(profile, target_id, READY_EXPR,
                     timeout=max(0.5, deadline - time.time())) == "complete+body":
                return True
        if time.time() >= deadline:
            return False
        time.sleep(0.3)


def _ready(profile: str, target_id: str) -> bool:
    """Is the current document complete and parsed, right now?"""
    try:
        return _eval(profile, target_id, READY_EXPR,
                     timeout=5) == "complete+body"
    except ControlError:
        return False


def _wait_move(profile: str, target_id: str, before_url: str,
               before_origin: float | None,
               timeout: float = NAV_MOVE_S) -> bool:
    """Did the tab LEAVE the document it was on?

    This is the first question, and `readyState` cannot answer it: the page
    being left is ALREADY complete, so waiting for "complete" returns before
    the navigation starts and the read-back then sees the old URL (that is how
    a redirect came back as `nav-not-verified`). A real navigation creates a
    new document (`performance.timeOrigin`), a fragment navigation changes the
    address — either one is the move.
    """
    deadline = time.time() + timeout
    while True:
        origin = _time_origin(profile, target_id)
        if origin is not None and before_origin is not None \
                and origin != before_origin:
            return True
        now = _href(profile, target_id)
        if now and not _same_page(now, before_url):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def _wait_url_change(profile: str, target_id: str, before: str,
                     timeout: float = HISTORY_TIMEOUT_S) -> bool:
    """Did the tab's address leave `before` within the deadline?"""
    deadline = time.time() + timeout
    while True:
        now = _href(profile, target_id)
        if now and not _same_page(now, before):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def _time_origin(profile: str, target_id: str) -> float | None:
    """The document's `performance.timeOrigin`, or None when unreadable.

    It changes exactly when a NEW document is created, which is the only
    honest way to tell a fast reload from a read of the old page.
    """
    try:
        return float(_eval(profile, target_id, "performance.timeOrigin",
                           timeout=5))
    except (ControlError, TypeError, ValueError):
        return None


def _wait_new_document(profile: str, target_id: str, before: float,
                       timeout: float = RELOAD_TIMEOUT_S) -> bool:
    """Is there a document whose timeOrigin differs from `before`?"""
    deadline = time.time() + timeout
    while True:
        now = _time_origin(profile, target_id)
        if now is not None and now != before:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def nav(url: str, tab: str = "", browser: str = "") -> dict:
    """`tab nav`: navigate ONE tab, and read back where it landed.

    The assignment returns before the document moves, so the reply carries the
    address as OBSERVED (`url_read`) and whether the document finished
    loading. Chromium's own error page refuses `nav-failed`; a tab that never
    left the page it was on refuses `nav-not-verified`. A redirect is a
    success: the test is that it MOVED, not that it arrived at the literal
    string it was handed.
    """
    target = safe_url(url)
    row, tab_row = _one_tab(tab, browser, for_write=True)
    profile, target_id = str(row["profile"]), str(tab_row["id"])
    before = _href(profile, target_id)
    before_origin = _time_origin(profile, target_id)
    _eval(profile, target_id,
          f"location.href = {json.dumps(target)}; 'navigating'", timeout=10)
    # FIRST the move, then the load: the document being left is already
    # complete, so "complete" alone would answer before the navigation starts
    moved = _wait_move(profile, target_id, before, before_origin)
    loaded = _wait_document(profile, target_id) if moved else _ready(profile,
                                                                     target_id)
    url_read = _href(profile, target_id)
    if url_read.startswith("chrome-error://"):
        fail("nav-failed",
             f"the browser could not load {target!r} — the tab is on its own "
             "error page (a name that does not resolve, a refused connection "
             "or a certificate problem), not the requested document")
    if not moved and not _same_page(target, before):
        fail("nav-not-verified",
             f"the tab is still at {before!r} after navigating to {target!r} "
             "— a beforeunload prompt, or an assignment the page ignored")
    return {"ok": True, "tab": f"id:{target_id}", "url": target,
            "url_read": url_read, "loaded": loaded, "moved": moved,
            "browser": _brief(row)}


def history(direction: str, tab: str = "", browser: str = "") -> dict:
    """`tab back` / `tab forward`: move the tab's history, then read it back.

    `history.back()` with nothing to go back to is not an error the page
    reports, so the reply is the OBSERVED change: a tab still at the same
    address after the wait refuses `nav-not-verified`.
    """
    if direction not in ("back", "forward"):
        fail("bad-args", f"history: {direction!r} is not back or forward")
    row, tab_row = _one_tab(tab, browser, for_write=True)
    profile, target_id = str(row["profile"]), str(tab_row["id"])
    before = _href(profile, target_id)
    _eval(profile, target_id, f"history.{direction}(); 'moving'", timeout=10)
    if not _wait_url_change(profile, target_id, before):
        fail("nav-not-verified",
             f"the tab is still at {before!r} after {direction} — its history "
             f"may have no {direction} entry")
    url_read = _href(profile, target_id)
    if url_read.startswith("chrome-error://"):
        fail("nav-failed",
             f"the {direction} entry did not load — the tab is on the "
             "browser's own error page")
    return {"ok": True, "direction": direction, "tab": f"id:{target_id}",
            "url_before": before, "url_read": url_read,
            "browser": _brief(row)}


def reload(tab: str = "", browser: str = "") -> dict:      # noqa: A001
    """`tab reload`: reload ONE tab and prove a NEW document is there.

    `location.reload()` returns immediately and a fast page can be complete
    again before a read, so the oracle is `performance.timeOrigin` — it
    changes exactly when a document is created.
    """
    row, tab_row = _one_tab(tab, browser, for_write=True)
    profile, target_id = str(row["profile"]), str(tab_row["id"])
    before = _time_origin(profile, target_id)
    if before is None:
        fail("reload-not-verified",
             f"tab {target_id[:10]}… did not report a document time — there "
             "is nothing to compare a reload against")
    _eval(profile, target_id, "location.reload(); 'reloading'", timeout=10)
    if not _wait_new_document(profile, target_id, before):
        fail("reload-not-verified",
             f"no new document within {RELOAD_TIMEOUT_S:g}s — the page may "
             "block the reload, or it is still loading")
    return {"ok": True, "tab": f"id:{target_id}", "reloaded": True,
            "url_read": _href(profile, target_id), "browser": _brief(row)}
