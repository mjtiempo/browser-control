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
import errno
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
from browser_control.lib.coerce import as_int  # pyright: ignore[reportMissingImports]
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
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
from browser_control.lib.text import flat  # pyright: ignore[reportMissingImports]

# The private spellings this module grew up with; the implementations live in
# lib/paths.py. `_pid_file` stays reachable because the live battery calls it.
_norm = norm
_is_managed = is_managed
_pid_file = pid_file
_lock_path = lock_path

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
LAUNCH_WAIT_S = 20.0
TAB_WAIT_S = 10.0
STOP_WAIT_S = 10.0
PORT_WAIT_S = 5.0


# ------------------------------------------------------------------ resolve
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


# `--profile DIR` — the same selector `attach`, `detach` and `close` already
# take. Every verb that narrows by `--browser` funnels through `_narrow`, so
# this one value addresses ONE instance everywhere: which is what makes two
# instances of the SAME browser usable (`open --profile <root>/work` and
# `open --profile <root>/personal`), instead of refusing `ambiguous-browser`.
SCOPE: dict[str, str] = {"profile": ""}


def scope(profile: str | None = None) -> str:
    """Set, clear or read the profile this process's calls are about.

    `None` reads it, `""` clears it (the CLI does that on every invocation that
    does not pass `--profile`, so one call never inherits another's), and a path
    sets it. A path outside this CLI's root is refused when `open` acts on it
    (`instance_dir`), because only the profiles under that root are the CLI's.
    """
    if profile is not None:
        text = str(profile).strip()
        SCOPE["profile"] = norm(text) if text else ""
    return SCOPE["profile"]


def instance_dir(binary_path: str) -> str:
    """The profile `open` is about: the SCOPED instance, else the default one.

    The default stays one profile per browser binary (what `profile_dir`
    builds); `--profile DIR` names a second instance of the same browser
    instead, and it has to live under this CLI's root: outside it we would be
    starting a browser we then refuse to write to.
    """
    scoped = SCOPE["profile"]
    if not scoped:
        return profile_dir(binary_path)
    if not _is_managed(scoped):
        fail("bad-args",
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


def live_profiles() -> list[str]:
    """The managed profiles a browser is answering CDP on right now."""
    return [path for path in profiles() if cdp.reachable(path)]


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
        have = ", ".join(flat(r.get("title"), 30)
                          for r in rows[:4]) or "none"
        fail("no-page-tab", f"no tab matches {needle!r} (have: {have})")
    if len(hits) > 1:
        titles = ", ".join(flat(r.get("title"), 30) for r in hits[:5])
        fail("tab-ambiguous", f"{needle!r} matches {len(hits)} tabs: {titles}")
    return hits[0]


# ------------------------------------------------------------------ the pid
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
    """One /proc file, as text — read to the END, not to 4096 bytes.

    The cap silently truncated the text every identity decision reads: a
    renderer whose `--type=` fell past it looked like a MAIN process, and a
    browser whose `--user-data-dir=` fell past it looked like no browser at all
    (a review flagged it; measured here, the longest cmdline is 2344 bytes, so
    the cap was reachable in principle rather than in practice).

    NULs are PRESERVED: they are the argv boundaries, and flattening them to
    spaces made `_cmdline_value` cut a `--user-data-dir` containing a space at
    the first space — the browser this CLI started became a stranger (a review
    flagged it).
    """
    try:
        with open(f"/proc/{pid}/{name}", "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    return raw.decode("utf-8", "replace").rstrip("\0\n")


def _cmdline_value(cmd: str, flag: str) -> str:
    """The value of `--flag=value` (or `--flag value`) in a /proc cmdline.

    Both spellings: Chrome accepts both and a launcher may write either. ""
    when the flag is absent. Splits on NUL when it is there (the real argv
    form), so a value containing spaces survives; the legacy space-joined
    form is still parsed for a caller that passed one.
    """
    text = str(cmd)
    parts = text.split("\0") if "\0" in text else text.split()
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
        if not cmd:
            continue
        # Chrome rewrites a CHILD's cmdline in place (space-separated, one
        # trailing NUL) while the main process keeps real argv boundaries. Both
        # forms must be recognised: per-entry `startswith` for the NUL form, a
        # substring only for the rewritten form — a main process whose URL
        # argument merely CONTAINS `--type=` must stay a main process (a review
        # flagged that), and a rewritten child title has no entries to check.
        if "\0" in cmd:
            if any(part.startswith("--type=") for part in cmd.split("\0")):
                continue
        elif "--type=" in cmd:
            continue
        exe = os.path.basename(os.path.realpath(f"/proc/{pid}/exe"))
        if exe in BROWSER_EXES:
            found.append((pid, exe, cmd))
    return sorted(found)


def _profile_marker(profile: str) -> str:
    """The `--user-data-dir` marker a browser on that profile carries.

    Built from the NORMALISED path, so a browser launched with a trailing
    slash, a `..` or a symlinked root is still recognised as this profile's — an
    exact string compare read a live profile as somebody else's.
    """
    return f"--user-data-dir={_norm(profile)}"


def _pid_on_marker(pid: int, cmd: str, profile: str) -> bool:
    """Does that process's cmdline name this profile, however it was spelled?"""
    spelled = _cmdline_value(cmd, "--user-data-dir")
    return bool(spelled) and _norm(spelled) == _norm(profile)


def _find_pid(profile: str) -> int:
    """The pid running ON this profile, from its own cmdline.

    The fallback when the pid file is gone (a crash restart). Only a MAIN
    process (no `--type=`) whose exe is Chromium-family counts, so this can
    never hand back a renderer — or a process we did not start. The path is
    compared NORMALISED, so the spelling a launcher chose does not matter.
    """
    for pid, _exe, cmd in _main_processes():
        if _pid_on_marker(pid, cmd, profile):
            return pid
    return 0


def _pid_on_profile(pid: int, profile: str) -> bool:
    """Does that pid's OWN cmdline say it runs on this profile?

    The marker is the one `_find_pid` matches and the one Chrome is started
    with, so a pid that cannot show it is not this profile's browser — which is
    the difference between stopping that browser and signalling whatever
    process inherited a recycled pid. The comparison is by NORMALISED path: an
    exact string compare would refuse to stop the browser this CLI itself
    started if the launcher spelled the directory differently.
    """
    return any(found == pid and _pid_on_marker(found, cmd, profile)
               for found, _exe, cmd in _main_processes())


def _pid_of(profile: str) -> int:
    """The pid recorded for this profile, or the one running on it now.

    A recorded pid is used only while the process still SAYS it is this
    profile's browser: the record is a hint, and a stale one plus a recycled
    pid would otherwise aim `stop` at an unrelated process. `_find_pid` is the
    fallback, and it refuses to hand back a renderer or a stranger.
    """
    pid = 0
    try:
        with open(_pid_file(profile)) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        pid = 0
    if pid and _pid_alive(pid) and _pid_on_profile(pid, profile):
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
def _default_profile(exe: str) -> str:
    """The default data directory of a browser executable, when it exists."""
    raw = DEFAULT_PROFILES.get(exe, "")
    if not raw:
        return ""
    path = expand(raw)
    return path if os.path.isdir(path) else ""


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
            port = as_int(_cmdline_value(cmd, "--remote-debugging-port"))
        reachable = cdp.answers(port)
        endpoint: dict = {"port": port, "reachable": reachable,
                          "verified": False}
        if reachable:
            # WHO holds the port: the row reports the pid and exe the kernel
            # says own the listening socket, and `verified` says whether that
            # process is this browser. Nothing here DRIVES an unverified row.
            owner = endpoint_owner(profile, port)
            endpoint["verified"] = bool(owner["verified"])
            endpoint["listener"] = {"pid": owner["pid"],
                                    "exe": owner["exe"]}
            if owner["verified"]:
                endpoint["tabs"] = len(cdp.page_rows_at(port))
            else:
                endpoint["reason"] = str(owner["reason"])
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
                                       not r["cdp"]["verified"], r["pid"]))


def list_browsers() -> dict:
    """`list`: every browser running here, drivable or not."""
    rows = browsers()
    return {"ok": True, "count": len(rows),
            "drivable": sum(1 for r in rows if r["cdp"]["verified"]),
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
    """The rows a name and/or the SCOPED profile select.

    `--browser NAME` matches the executable or the profile's basename (the name
    `open --browser NAME` keys a default profile by); `--profile DIR` (the
    process scope) matches the profile PATH and nothing else, so it can pick
    between two instances of the same browser.
    """
    wanted = os.path.basename(str(browser).strip())
    scoped = SCOPE["profile"]
    out: list[dict] = []
    for row in rows:
        if wanted and wanted not in (row["exe"],
                                     os.path.basename(str(row["profile"]))):
            continue
        if scoped and _norm(str(row["profile"])) != scoped:
            continue
        out.append(row)
    return out


def _who(rows: list[dict]) -> str:
    """The browsers in a message, named so a caller can act on them."""
    return "; ".join(f"{r['exe']} on {r['profile']}" for r in rows)


def _writable(browser: str = "") -> list[dict]:
    """The browsers a WRITE may target: the ones this CLI manages, plus the
    attached ones.

    `_drivable` decides what "drivable" means (the endpoint must be the browser
    it claims to be), and this narrows that to the browsers a write may touch.
    A browser nobody handed over is NOT one of them, however drivable it is:
    the README promises writes go to a managed or attached browser, and the
    fallback to "every drivable browser" that used to be here sent `tab press`
    and `tab about:blank` into a stranger's session — measured against a
    throwaway Chrome whose only sin was publishing a debugging port, which
    then gained a tab it never asked for. `_one_tab` and `_writable_profile`
    name the stranger and refuse instead of picking it.
    """
    return [r for r in _drivable(browser, strict=False)
            if r["managed"] or r["attached"]]


def _readable(browser: str = "") -> list[dict]:
    """The browsers a READ may target: ours and attached first, else every
    drivable one.

    Reading a stranger's tab list is not the same act as typing into it, so a
    read reaches what a write may not — but "ours first" is what keeps an
    unqualified read from refusing `tab-ambiguous` when the user's own browser
    happens to run beside ours.
    """
    return _writable(browser) or _drivable(browser)


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
    if SCOPE["profile"]:
        # the scope is a write target only when it is one this CLI manages or
        # was handed: `--profile /tmp/stranger` used to come straight back and
        # `tab about:blank` then ran `Target.createTarget` in a stranger's
        # browser (a review measured it). `instance_dir` refuses that path for
        # `open`; a write has to refuse it too — the two halves of the gate
        # disagreed about the same argv.
        if not _is_managed(SCOPE["profile"]) \
                and not is_attached(SCOPE["profile"]):
            fail("not-managed",
                 f"--profile {SCOPE['profile']} is not a profile this CLI "
                 "manages or has attached — `open --profile DIR` starts one "
                 "under the root, or `attach --port N` hands one over")
        return SCOPE["profile"]
    rows = _writable(browser)
    if len(rows) > 1:
        names = ", ".join(os.path.basename(str(r["profile"]))
                          + (" (attached)" if r["attached"] else "")
                          for r in rows)
        fail("ambiguous-browser",
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
    rows = _narrow([r for r in browsers()
                    if r["managed"] and r["cdp"]["reachable"]], browser)
    if not rows and SCOPE["profile"]:
        # a scoped call is about THAT instance, running or not: `open` starts
        # it, and nothing here may silently fall back to some other profile
        return SCOPE["profile"]
    if len(rows) > 1:
        # pid AND the whole profile path: two browsers can share an executable
        # name, and "(google-chrome-stable, google-chrome-stable)" is a message
        # nobody can act on (observed)
        names = "; ".join(f'{os.path.basename(str(r["profile"]))} '
                          f'(pid {r["pid"]}, {r["profile"]})' for r in rows)
        fail("ambiguous-browser",
             f"{len(rows)} managed browsers are up: {names} — pass "
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
        row = next((r for r in rows if r["pid"] == as_int(pid)), None)
    elif profile:
        want = _norm(profile)
        row = next((r for r in rows if _norm(r["profile"]) == want), None)
    else:
        want_port = as_int(port)
        row = next((r for r in rows
                    if as_int(r["cdp"]["port"]) == want_port), None)
    if row is None:
        fail("no-browser",
             f"no running Chromium-family browser matches {given[0]}")
    if not row["cdp"]["reachable"]:
        fail("cdp-unreachable",
             f"pid {row['pid']} does not answer CDP on port "
             f"{row['cdp']['port']} — attach needs a live endpoint")
    if not row["cdp"].get("verified"):
        # an endpoint that ANSWERS is not proof of WHOSE it is, and this record
        # is what opens the tab-write gate: `_named_browser` refuses an
        # unverified endpoint, and so must this (a review measured the gap —
        # a stale port was enough to authorise a write path)
        not_local_refusal(str(row["profile"]), as_int(row["cdp"]["port"]),
                          str(row["cdp"].get("reason") or "unknown"))
    record = {"profile": _norm(row["profile"]), "pid": row["pid"],
              "port": as_int(row["cdp"]["port"]), "exe": row["exe"],
              "managed": row["managed"],
              "attached_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    # the read-modify-write of the records runs under the root's lock: two
    # `attach`s at once used to overwrite each other's line
    with _lock(_lock_path(root()), "attach") as lock:
        records = _attached()
        already = record["profile"] in records
        records[record["profile"]] = record
        _write_attached(records)
    reply = {"ok": True, "attached": True, "already": already,
             "browser": {"pid": record["pid"], "exe": record["exe"],
                         "profile": record["profile"],
                         "managed": record["managed"]},
             "cdp": {"port": record["port"], "reachable": True},
             "note": ("tab writes only — `close` will not stop it; "
                      "`detach` revokes this")}
    if lock["warning"]:
        reply["warning"] = lock["warning"]
    return reply


def detach(port: int = 0, pid: int = 0, profile: str = "",
           detach_all: bool = False) -> dict:
    """`detach`: take the tab-write authorization away again."""
    if detach_all:
        if port or pid or profile:
            fail("bad-args", "detach: --all takes no other selector")
        with _lock(_lock_path(root()), "detach") as lock:
            keys = sorted(_attached())
            _write_attached({})
        reply = {"ok": True, "detached": keys, "count": 0}
        if lock["warning"]:
            reply["warning"] = lock["warning"]
        return reply
    given = [name for name, value in (("--port", port), ("--pid", pid),
                                      ("--profile", profile)) if value]
    if len(given) != 1:
        fail("bad-args",
             "detach: name ONE browser — --port N, --pid N, --profile DIR, "
             "or --all")
    # the read-modify-write of the records runs under the root's lock, so two
    # `attach`/`detach` calls cannot lose each other's line
    with _lock(_lock_path(root()), "detach") as lock:
        records = _attached()
        if profile:
            keys = [key for key in records if key == _norm(profile)]
        elif pid:
            keys = [key for key, rec in records.items()
                    if as_int(rec.get("pid")) == as_int(pid)]
        else:
            keys = [key for key, rec in records.items()
                    if as_int(rec.get("port")) == as_int(port)]
        if not keys:
            fail("not-attached", f"nothing is attached for {given[0]}")
        for key in keys:
            records.pop(key, None)
        _write_attached(records)
    reply = {"ok": True, "detached": keys, "count": len(records)}
    if lock["warning"]:
        reply["warning"] = lock["warning"]
    return reply


# One /proc walk per (profile, port) per process: the guard runs on every drive
# path, and the walk behind it costs milliseconds.
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
    profile_pid = _find_pid(profile) if profile else 0
    if not owner:
        verdict = {"verified": False, "pid": 0, "exe": "",
                   "profile_pid": profile_pid,
                   "reason": "no process holds the port (the socket is gone)"}
    elif exe not in BROWSER_EXES:
        verdict = {"verified": False, "pid": pid, "exe": exe,
                   "profile_pid": profile_pid,
                   "reason": f"pid {pid} holds the port and is not a "
                             f"Chromium-family browser ({exe or 'unknown'})"}
    elif profile and not _pid_on_marker(pid, cmd, profile):
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
    fail("cdp-not-local",
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
    suspects = _narrow([r for r in rows if r["cdp"].get("reachable")
                        and not r["cdp"].get("verified")], browser)
    if suspects:
        row = suspects[0]
        not_local_refusal(str(row["profile"]), as_int(row["cdp"]["port"]),
                          str(row["cdp"].get("reason") or "unknown"))


def _drivable(browser: str = "", strict: bool = True) -> list[dict]:
    """The running browsers that answer CDP AND are the browsers they claim.

    `browser` narrows to the one named — its executable, or the basename of
    its profile, which is the name `open --browser NAME` keys a profile by.
    One name can match two browsers (the same browser on two profiles, as a
    tool that manages its own copy makes likely): the MANAGED one wins, since
    that is the browser this CLI would drive and the only one a write may
    touch. An empty result is []: whether that is a refusal is the caller's
    question.

    `strict` makes an endpoint that answered but did NOT verify a refusal
    (`cdp-not-local`) rather than a silent omission — `list` wants the row, a
    drive wants the refusal. Nothing is ever driven from an unverified row.
    """
    answering = [r for r in browsers() if r["cdp"].get("reachable")]
    verified = [r for r in answering if r["cdp"].get("verified")]
    matches = _narrow(verified, browser)
    if browser and matches:
        rows = [r for r in matches if r["managed"] or r["attached"]] or matches
    else:
        rows = matches
    if strict and not rows:
        _drive_refusal(browser, answering)
    return sorted(rows, key=lambda r: (r["exe"], str(r["profile"])))


def _tabs_or_fail(row: dict) -> list[dict]:
    """The tabs of one browser, or a REFUSAL when it did not answer.

    `_tabs_of` returns (tabs, error) and seven callers took `[0]`, dropping the
    error: a list that could not be read then became a claim of ABSENCE — "no
    tab matches … have: none" for a browser that simply did not answer, which is
    exactly what the error value exists to prevent (a review flagged it).
    """
    tabs, error = _tabs_of(row)
    if error:
        fail("cdp-error", f"{row['exe']} on {row['profile']}: {error}")
    return tabs


def _tabs_of(row: dict) -> tuple[list[dict], str]:
    """(page tabs, error) for one browser row.

    A browser that stopped answering between the probe and the read is
    REPORTED: an empty list is "no tabs", an error is "no answer". A row whose
    endpoint did not VERIFY is reported the same way — the tab list of a
    stranger is not this browser's tab list, and no count is claimed.
    """
    if row["cdp"].get("reachable") and not row["cdp"].get("verified"):
        return [], str(row["cdp"].get("reason") or
                       "the endpoint is not this profile's browser")
    try:
        port = as_int(row["cdp"]["port"])
        return cdp.rows_to_tabs(cdp.page_rows_at(port)), ""
    except ControlError as e:
        return [], e.message


def _brief(row: dict) -> dict:
    """One browser row, small enough to ride along in a tab reply."""
    return {"pid": row["pid"], "exe": row["exe"], "profile": row["profile"],
            "managed": row["managed"], "attached": row["attached"],
            "port": as_int(row["cdp"]["port"])}


def _row(row: dict) -> dict:
    """One browser row without its endpoint block (reported apart)."""
    return {key: row[key] for key in ("pid", "exe", "path", "profile",
                                      "profile_from", "managed",
                                      "attached")}


def _endpoint_details(row: dict) -> dict:
    """One browser row's endpoint, with the version it reports itself."""
    details = dict(row["cdp"])
    if details.get("reachable"):
        version = cdp.version_at(as_int(details.get("port")))
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

    A browser whose endpoint did not VERIFY is not listed under `browsers`
    (nothing of it is driven or counted) — it appears under `unverified`, with
    the pid and exe that actually hold the port, because a row that silently
    vanished would be a claim of absence.
    """
    groups: list[dict] = []
    total = 0
    for row in _drivable(browser, strict=False):
        tabs, error = _tabs_of(row)
        group = {**_brief(row), "tabs": tabs, "count": len(tabs)}
        if error:
            group["error"] = error
        total += len(tabs)
        groups.append(group)
    reply: dict = {"ok": True, "count": total, "browsers": groups}
    suspects = _narrow([r for r in browsers()
                        if r["cdp"].get("reachable")
                        and not r["cdp"].get("verified")], browser)
    if suspects:
        reply["unverified"] = [
            {**_brief(r), "listener": r["cdp"].get("listener"),
             "reason": r["cdp"].get("reason")}
            for r in suspects]
    return reply


def browser_info(browser: str = "") -> dict:
    """`info`: the browser this CLI would drive — or the one named — and its
    endpoint.

    "Would drive" includes an ATTACHED browser: that is the one a `tab` write
    goes to. `running: false` is an ANSWER, not a refusal — which browser
    `open` would start, and where its profile lives, is knowable without one
    running. `--browser NAME` and `--profile DIR` both narrow it, so a scoped
    call reports the instance it is about.
    """
    rows = _narrow(browsers(), browser)
    # ours first, then an attached one: those are the browsers a tab write
    # could reach, and the one `info` is really about
    live = [r for r in rows if r["managed"] or r["attached"]]
    if len(live) > 1:
        names = ", ".join(os.path.basename(str(r["profile"]))
                          + (" (attached)" if r["attached"] else "")
                          for r in live)
        fail("ambiguous-browser",
             f"{len(live)} writable browsers are up ({names}) — name one with "
             "--profile DIR (the instance) or --browser NAME")
    if live:
        return {"ok": True, "running": True, "browser": _row(live[0]),
                "cdp": _endpoint_details(live[0])}
    if rows and (browser or SCOPE["profile"]):
        # a NAMED browser this CLI cannot (or may not) drive: report it rather
        # than pretend. With no name and no scope, an unmanaged and unreachable
        # browser is not an answer at all — say `running: false` instead.
        row = rows[0]
        return {"ok": True, "running": bool(row["cdp"]["reachable"]),
                "browser": _row(row), "cdp": _endpoint_details(row)}
    path = binary(browser) if browser else binary()
    profile = instance_dir(path)
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


def _wait_own_port(profile: str, timeout: float = LAUNCH_WAIT_S) -> bool:
    """The endpoint THIS call started must ANSWER and VERIFY.

    `_wait_port` only proves something answers, and the port file it reads can
    be the stale one the caller just decided to ignore: `open` then reported
    `started: true` with a stranger's port and the stranger's tabs (a review
    flagged it). The owner is re-judged on the CURRENT port file, memo evicted,
    until it verifies — the browser this call spawned names the profile in its
    own command line, so its endpoint is the one that can pass.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        port = cdp.port_of(profile)
        if port:
            _OWNER_CACHE.pop((profile, port), None)
            if endpoint_owner(profile, port).get("verified"):
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


def _require_tab_list_readable(profile: str, error: ControlError) -> None:
    """Refuse to read "the list could not be read" as "the ids are gone".

    Only an unreachable endpoint with no port file at all is the browser
    having exited. A timeout, a malformed `/json` or an oversized body refuses
    instead of claiming absence — the same class of bug `_tabs_or_fail` was
    fixed for (a review flagged it).
    """
    if error.code != "cdp-unreachable" or cdp.port_of(profile):
        fail("close-tab-not-verified",
             f"the tab list could not be read back after the close: "
             f"{error.message}")


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
        except ControlError as e:
            _require_tab_list_readable(profile, e)
            open_ids = set()          # the browser is gone: so are its tabs
        left = [i for i in ids if i in open_ids]
        if not left or time.time() >= deadline:
            return left
        time.sleep(0.2)


def _verify_profile_endpoint(profile: str) -> None:
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
        fail("cdp-unreachable",
             f"no port file in {profile} — nothing to drive there")
    owner = endpoint_owner(profile, port)
    if not owner.get("verified"):
        not_local_refusal(profile, port, str(owner.get("reason") or "unknown"))


def _open_tabs(profile: str, urls: list[str]) -> list[dict]:
    """One new tab per URL: every id from CDP, all verified together.

    `Target.createTarget` answers with the id it made and ONE poll loop then
    proves every id is in the tab list. A partial result refuses and names
    what is missing — and the tabs that did open stay open, because a refusal
    is not a reason to destroy work. The endpoint is checked against the kernel
    FIRST: creating a tab is a write, and a write does not go to whoever holds
    the port (a review measured that this one had no owner check at all).
    """
    _verify_profile_endpoint(profile)
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
# ------------------------------------------------------------------ the lock
# A check-then-act two processes can enter at once is two browsers on one
# profile — the corruption Chrome's own "profile appears to be in use" warning
# exists to prevent. These verbs serialize it themselves: `flock` on a file in
# the profile (or in the root, for the attach records), which the kernel
# releases when the holder dies, so there is no stale lock to clean up.
LOCK_WAIT_S = 20.0          # as long as a cold launch is given


def _lock_holder(handle: Any) -> str:
    """What the holder wrote: "pid 123 since 12:34:56 (open)", or ""."""
    try:
        handle.seek(0)
        parts = handle.read().strip().split("\t")
    except OSError:
        return ""
    if len(parts) < 2:
        return ""
    stamp = parts[1][11:19] or parts[1]
    verb = f" ({parts[2]})" if len(parts) > 2 and parts[2] else ""
    return f"pid {parts[0]} since {stamp}{verb}"


def _hold(handle: Any, verb: str) -> None:
    """Say who holds it, and since when, so a refusal can name them."""
    with contextlib.suppress(OSError):
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\t{time.strftime('%Y-%m-%dT%H:%M:%S')}\t"
                     f"{verb}\n")
        handle.flush()


def _contention(error: OSError) -> bool:
    """Is that flock failure someone else holding the lock?

    The difference decides what happens next: contention is worth waiting for,
    while a filesystem that cannot lock at all has to be reported instead.
    """
    return error.errno in (errno.EACCES, errno.EAGAIN)


def _expired(deadline: float) -> bool:
    """Has the wait run out? A call, so a refusal handler reads as one."""
    return time.time() >= deadline


def _holder_text(handle: Any) -> str:
    """What the holder wrote, or a phrase for "nothing usable"."""
    return _lock_holder(handle) or "no details"


def _acquire(handle: Any, path: str, verb: str, wait: float) -> str:
    """Take the lock, or say why not: "" when taken, else a warning.

    Contention is waited on and then refused `profile-busy` (naming the pid
    and verb the holder wrote); a filesystem that cannot lock at all comes back
    as a warning, which the caller REPORTS rather than failing the verb — a
    guard that silently does nothing would be worse than none.
    """
    import fcntl

    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _hold(handle, verb)
            return ""
        except OSError as e:
            if _contention(e):
                if _expired(deadline):
                    fail("profile-busy",
                         f"another browser-control call holds {path} "
                         f"[{_holder_text(handle)}] and has been for "
                         f"{wait:g}s — nothing was started or stopped here. "
                         "Wait for that call, then run this again")
                time.sleep(0.15)
                continue
            return f"{path} cannot be locked ({e})"


@contextlib.contextmanager
def _lock(path: str, verb: str, wait: float = LOCK_WAIT_S):
    """Hold `path` while a check-then-act runs, or refuse `profile-busy`.

    Yields ``{"held": bool, "warning": str}``: `held: False` is the
    filesystem-cannot-lock case, which the caller proceeds through and reports.
    Contention never reaches the caller as a warning — it waits, then refuses.

    `flock` rather than an `O_EXCL` file precisely for the stale case: the
    kernel drops it when the holder exits, crashes or is killed, so there is
    nothing to clean up and nothing to trust.
    """
    import fcntl

    with contextlib.ExitStack() as stack:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            handle = stack.enter_context(
                open(path, "a+", encoding="utf-8"))
        except OSError as e:
            yield {"held": False,
                   "warning": f"could not open a lock at {path}: {e}"}
            return
        warning = _acquire(handle, path, verb, wait)
        try:
            yield {"held": bool(not warning), "warning": warning}
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)


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
    owner = endpoint_owner(profile, port) if cdp.reachable(profile) else {}
    if not port:
        # no port at all is not an endpoint to judge: whoever asked may have
        # read the port file before the browser wrote it (measured in the
        # concurrent `open` case, where that stale 0 refused a whole call)
        return {}
    if not owner or owner.get("verified") or not owner.get("profile_pid"):
        return owner
    deadline = time.time() + LAUNCH_WAIT_S
    while time.time() < deadline:
        time.sleep(0.25)
        _OWNER_CACHE.pop((profile, port), None)
        owner = endpoint_owner(profile, port) if cdp.reachable(profile) else {}
        if owner.get("verified"):
            return owner
    return owner


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
        raise ControlError("profile-unusable",
                           f"cannot create {profile}: {e}") from e
    with _lock(_lock_path(profile), "open") as lock:
        # the port is read INSIDE the lock: a value from before it can be 0
        # while the browser another call just started is already answering,
        # and judging that stale 0 is what turned a race into a refusal
        port = cdp.port_of(profile)
        owner = _await_owner(profile, port)
        if owner and not owner.get("verified"):
            if owner.get("profile_pid"):
                not_local_refusal(profile, port,
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
            _record_pid(profile, _spawn([path, *flags(profile), first]))
            if not _wait_own_port(profile):
                fail("launch-failed",
                     f"started {path} on {profile} but no CDP endpoint "
                     f"answered within {LAUNCH_WAIT_S:g}s")
            row = _wait_url(profile, first)
            if row is None:
                fail("no-page-tab",
                     f"{path} is up on {profile} but shows no tab for "
                     f"{first!r}")
            opened = [row]
            if len(wanted) > 1:
                opened += _open_tabs(profile, wanted[1:])
        rows = _wait_rows(profile)
    if not rows:
        fail("no-page-tab",
             f"{path} is up on {profile} but shows no page tab — pass a URL "
             "(browser-control-cli open https://…)")
    notes = [text for text in (stale, lock["warning"]) if text]
    reply = {"ok": True, "started": not already, "browser": path,
             "profile": profile, "port": cdp.port_of(profile),
             "pid": _pid_of(profile), "tabs": rows,
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
    if not endpoint_owner(profile, port)["verified"]:
        return None
    try:
        return len(cdp.page_rows(profile))
    except ControlError:
        return None


def _named_browser(selector: dict) -> dict:
    """The one live browser a caller NAMED, verified like `attach` verifies.

    The same bar as `attach`: a running Chromium-family MAIN process of this
    machine that answers CDP — and here also one whose endpoint VERIFIES, so
    the pid about to be signalled is the process holding that port. Naming it
    is the consent; this is what makes the name mean something.
    """
    rows = browsers()
    port = as_int(selector.get("port"))
    pid = as_int(selector.get("pid"))
    profile = str(selector.get("profile") or "")
    if pid:
        row = next((r for r in rows if r["pid"] == pid), None)
        what = f"--pid {pid}"
    elif profile:
        want = _norm(profile)
        row = next((r for r in rows if _norm(r["profile"]) == want), None)
        what = f"--profile {profile}"
    else:
        row = next((r for r in rows
                    if as_int(r["cdp"]["port"]) == port), None)
        what = f"--port {port}"
    if row is None:
        fail("no-browser",
             f"no running Chromium-family browser matches {what}")
    if not row["cdp"]["reachable"]:
        fail("cdp-unreachable",
             f"pid {row['pid']} does not answer CDP on port "
             f"{row['cdp']['port']} — a browser this CLI cannot reach is not "
             "one it stops by name")
    if not row["cdp"]["verified"]:
        fail("cdp-not-local",
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
    given = [name for name, value in (("--port", port), ("--pid", pid),
                                      ("--profile", profile)) if value]
    if len(given) > 1:
        fail("bad-args",
             "close: name ONE browser — --port N, --pid N or --profile DIR")
    named = bool(given)
    row = _named_browser({"port": port, "pid": pid, "profile": profile}) \
        if named else {}
    target = str(row["profile"]) if row else managed_profile(browser)
    with _lock(_lock_path(target), "close") as lock:
        target_pid = as_int(row["pid"]) if row else _pid_of(target)
        tabs = _page_count(target)
        if not target_pid:
            if not cdp.reachable(target):
                reply = {"ok": True, "stopped": False, "profile": target,
                         "tabs": tabs,
                         "reason": "no managed browser was running"}
                if lock["warning"]:
                    reply["warning"] = lock["warning"]
                return reply
            fail("browser-not-stopped",
                 f"a browser answers on {target} but no Chromium process on "
                 "it can be identified — refusing to signal a process this "
                 "CLI did not start")
        if not _pid_on_profile(target_pid, target):
            # re-verified IMMEDIATELY before the signal, on BOTH paths: the
            # managed path took its pid before `_page_count` (an HTTP GET plus
            # a /proc walk), so a browser that exited in that window could have
            # its pid recycled under the SIGTERM (a review flagged it).
            fail("browser-not-stopped",
                 f"pid {target_pid} is gone, or no longer runs {target} — "
                 "nothing was signalled")
        if tabs and not force:
            fail("tabs-open",
                 f"{tabs} page tab(s) are open in {target} and stopping the "
                 "browser closes them with it (Chromium exits with its last "
                 "window) — pass --force to stop it anyway, or take the tabs "
                 "first with `tab close ...` (`tab list` shows them)")
        try:
            os.kill(target_pid, signal.SIGTERM)
        except OSError as e:
            fail("browser-not-stopped", f"cannot stop pid {target_pid}: {e}")
        deadline = time.time() + STOP_WAIT_S
        while time.time() < deadline and _pid_alive(target_pid):
            time.sleep(0.2)
        if _pid_alive(target_pid):
            fail("browser-not-stopped",
                 f"pid {target_pid} survived SIGTERM for {STOP_WAIT_S:g}s — "
                 "stop it yourself; this CLI does not SIGKILL a browser")
        deadline = time.time() + PORT_WAIT_S
        while time.time() < deadline and cdp.reachable(target):
            time.sleep(0.2)
        if cdp.reachable(target):
            fail("browser-not-stopped",
                 f"pid {target_pid} is gone but the CDP endpoint on {target} "
                 "still answers")
        Path(_pid_file(target)).unlink(missing_ok=True)
    reply = {"ok": True, "stopped": True, "pid": target_pid,
             "profile": target, "tabs": tabs,
             "forced": bool(tabs and force), "named": named,
             "managed": bool(row["managed"]) if row else True}
    if lock["warning"]:
        reply["warning"] = lock["warning"]
    return reply


ACTIVE_SPEC = "active"


def _visible(profile: str, target_id: str) -> bool | None:
    """Does that tab's page report itself visible? None when it cannot say.

    `document.visibilityState` is what makes a tab "the active one": measured
    in this project, a hidden tab still has a viewport, layout and hit-testing,
    so geometry cannot tell the two apart — this can.
    """
    try:
        return _eval(profile, target_id, "document.visibilityState",
                     timeout=5) == "visible"
    except ControlError:
        return None


def _spec_hits(rows: list[dict], tabs_of: dict,
               spec: str) -> list[tuple[dict, dict, int]]:
    """Every tab of every drivable browser a spec names.

    `active` is a RESERVED spec — the tab whose page answers `visible`, at most
    one per window, among the browsers this CLI DRIVES — so a page whose title
    merely contains the word is reached by `id:` or a longer substring, the
    same way `tab list` can never mean a site called "list".
    """
    if str(spec).strip().lower() == ACTIVE_SPEC:
        return [(row, tab, index)
                for row in rows
                for index, tab in enumerate(tabs_of[row["pid"]])
                if _visible(str(row["profile"]), str(tab["id"]))]
    hits: list[tuple[dict, dict, int]] = []
    for row in rows:
        tabs = tabs_of[row["pid"]]
        positions = {str(t["id"]): index for index, t in enumerate(tabs)}
        hits += [(row, tab, positions[str(tab["id"])])
                 for tab in _match_spec(tabs, spec)]
    return hits


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
        _no_drive(browser)
    tabs_of = {row["pid"]: _tabs_or_fail(row) for row in rows}
    found: list[tuple[dict, dict, int]] = []
    for spec in specs:
        # `active` is about the browser this CLI DRIVES: the user's own Chrome
        # has a visible tab in its own window as well, and counting it would
        # make every `--tab active` refuse — the same reason an unqualified tab
        # verb picks among OUR browsers instead of every browser on the machine
        if str(spec).strip().lower() == ACTIVE_SPEC:
            own = [row for row in rows if row["managed"] or row["attached"]]
            hits = _spec_hits(own, tabs_of, spec)
            if not hits:
                fail("no-page-tab",
                     "no tab of a browser this CLI drives reports itself "
                     "VISIBLE — read one with `tab list`, or name it with "
                     "`--tab SPEC`")
        else:
            hits = _spec_hits(rows, tabs_of, spec)
        if not hits:
            have = ", ".join(f'{flat(t["title"], 20) or flat(t["url"], 20)} '
                             f'({r["exe"]})'
                             for r in rows for t in tabs_of[r["pid"]][:2])
            fail("no-page-tab",
                 f"no tab matches {spec!r} (have: {have or 'none'})")
        if len(hits) > 1:
            # pid in the message: two browsers can share an executable name
            where = ", ".join(
                f'{flat(t["title"], 20) or flat(t["id"], 8)} in {r["exe"]}'
                f':{os.path.basename(str(r["profile"]))} (pid {r["pid"]})'
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
    # a READ-BACK after a close must not turn into a refusal: `tab close --all`
    # on the last tab makes the browser exit, and its endpoint can stop
    # answering before the close is reported — the strict form then said
    # `cdp-not-local` about an unrelated browser AFTER the tabs were gone (a
    # review flagged it).
    return sum(len(_tabs_or_fail(row)) for row in _drivable(strict=False))


def tab_info(spec: str, browser: str = "") -> dict:
    """`tab info`: one tab, resolved across the drivable browsers.

    The read side of a handle: which browser owns it, and what it is now.
    """
    (row, tab, index), = _resolve_across([spec], browser, for_write=False)
    return {"ok": True, "tab": {**tab, "index": index},
            "browser": _brief(row)}


def _foreign_row(row: dict, tab: dict) -> dict:
    """One match this CLI may not close, small enough to report whole."""
    return {"id": tab["id"], "title": tab["title"], "url": tab["url"],
            "pid": row["pid"], "exe": row["exe"],
            "profile": row["profile"]}


def _closeable(browser: str) -> list[dict]:
    """The drivable browser rows, or a refusal naming what to do about it."""
    rows = _drivable(browser)
    if not rows:
        _no_drive(browser)
    return rows


def _split(rows: list[dict]) -> tuple[list[tuple[dict, dict, int]],
                                      list[dict]]:
    """(tabs this CLI may close, matches in browsers it may not).

    A match this CLI does not drive is REPORTED, never closed: a bulk close
    over the whole machine that hits the user's own browsing must say so
    rather than act on it, fail, or pretend it did not see it.
    """
    ours: list[tuple[dict, dict, int]] = []
    foreign: list[dict] = []
    for row in rows:
        for index, tab in enumerate(_tabs_or_fail(row)):
            if row["managed"] or row["attached"]:
                ours.append((row, tab, index))
            else:
                foreign.append(_foreign_row(row, tab))
    return ours, foreign


def _exact_matches(field: str, value: str,
                   browser: str) -> tuple[list[tuple[dict, dict, int]],
                                         list[dict]]:
    """EXACT (case-insensitive) title/URL matches, ours and the rest.

    Exact means the whole title (or URL): `--title a` is the tab called "a",
    not every tab with an "a" somewhere in it.
    """
    wanted = str(value).lower()
    ours: list[tuple[dict, dict, int]] = []
    foreign: list[dict] = []
    for row in _closeable(browser):
        for index, tab in enumerate(_tabs_or_fail(row)):
            if str(tab.get(field) or "").lower() != wanted:
                continue
            if row["managed"] or row["attached"]:
                ours.append((row, tab, index))
            else:
                foreign.append(_foreign_row(row, tab))
    return ours, foreign


def _exact_spec_match(tab: dict, spec: str) -> bool:
    """Does this spec NAME that tab — exactly?

    `id:<prefix>`, the whole URL (a trailing slash is not a different page,
    the same rule `_same_page` uses), or the whole title, case-insensitively.
    A SUBSTRING is not a name: `tab close a` closed a tab whose title merely
    contained an `a` (measured, on a real browser), which is why the sweeping
    form has to be asked for by name (`--like`).
    """
    needle = str(spec or "").strip()
    if not needle:
        return False
    if needle.lower().startswith("id:"):
        # `id:` with no prefix names NOTHING. `"".startswith("")` is True, so
        # an unchecked empty prefix matched every tab — measured: `tab close
        # id:` closed the last tab on the page, and Chromium took the window
        # with it. `_match_spec` refuses it; this must not match it.
        prefix = needle[3:].strip().lower()
        return bool(prefix) and str(tab.get("id") or "").lower().startswith(
            prefix)
    low = needle.lower()
    return (str(tab.get("url") or "").rstrip("/").lower()
            == low.rstrip("/")
            or str(tab.get("title") or "").strip().lower() == low)


def _spec_matches(specs: list[str], browser: str, loose: bool = False) -> tuple[
        list[tuple[dict, dict, int]], list[dict]]:
    """Every tab a SPEC set names — the union, deduplicated.

    Two rules, and the difference matters for a verb that DESTROYS tabs:

    * a SPEC NAMES a tab (`_exact_spec_match`): `tab close http://a` closes
      every tab on `http://a/`, `tab close a` closes the tabs titled `a`, and
      neither touches a tab that merely mentions them;
    * `loose=True` (the `--like` flag) matches a SUBSTRING, which is a sweep —
      every tab whose title or URL contains it — and exists because a caller
      sometimes means exactly that, asked for out loud.

    A spec that matches nothing refuses instead of reading as "nothing to do",
    and one that matches only foreign tabs refuses `not-managed`.

    Closing the LAST page tab of a browser takes its window with it, and
    Chromium exits with its last window — so `--all` on the only tab stops the
    browser as well. The ids are verified gone either way, which is why that
    reads as a success with `count: 0` and not as a lost endpoint.
    """
    rows = _closeable(browser)
    ours: list[tuple[dict, dict, int]] = []
    foreign: list[dict] = []
    seen: set[str] = set()
    for spec in specs:
        if not loose and not str(spec).strip():
            fail("bad-args", "tab close: a TAB spec cannot be empty")
        hits = 0
        for row in rows:
            for index, tab in enumerate(_tabs_or_fail(row)):
                named = (bool(_match_spec([tab], spec)) if loose
                         else _exact_spec_match(tab, spec))
                if not named:
                    continue
                hits += 1
                if str(tab["id"]) in seen:
                    continue
                if row["managed"] or row["attached"]:
                    seen.add(str(tab["id"]))
                    ours.append((row, tab, index))
                else:
                    foreign.append(_foreign_row(row, tab))
        if not hits:
            have = ", ".join(f'{flat(t["title"], 20) or flat(t["url"], 20)}'
                             for r in rows for t in _tabs_or_fail(r)[:2])
            if loose:
                fail("no-page-tab",
                     f"no tab contains {spec!r} in its title or URL "
                     f"(have: {have or 'none'})")
            fail("no-page-tab",
                 f"no tab is NAMED {spec!r}: a SPEC names a tab exactly — its "
                 "whole URL, its whole title, or id:<prefix> — and for a "
                 f"substring sweep use `tab close --like {spec!r}` "
                 f"(have: {have or 'none'})")
        if not ours and foreign:
            fail("not-managed",
                 f"every tab matching {spec!r} is in a browser this CLI did "
                 f"not start ({foreign[0]['exe']} on "
                 f"{foreign[0]['profile']}) — a write needs "
                 "`attach --port N`, or `--browser NAME` to narrow it")
    return ours, foreign


def close_tabs(specs: list[str], browser: str = "", title: str | None = None,
               url: str | None = None, all_tabs: bool = False,
               excepts: list[str] | None = None,
               like: list[str] | None = None, dry: bool = False) -> dict:
    """`tab close`: close every tab the arguments name, and prove it.

    Five ways to name tabs, one per call:

    * **SPECs** — `id:<prefix>`, the whole URL (a trailing slash is not a
      different page) or the whole title, case-insensitively; every match
      closes, so `tab close http://a/` takes all of them;
    * **`--like VALUE`** — a SUBSTRING sweep over titles and URLs: the loose
      form, which has to be asked for by name because `tab close a` used to
      sweep up a tab whose title merely CONTAINED an `a` (measured);
    * **`--title VALUE` / `--url VALUE`** — an exact match in one field;
    * **`--all`** — every page tab this CLI drives;
    * **`--except SPEC`** (which implies `--all`) — everything but the tabs
      those specs name; the KEEP side matches loosely (erring toward keeping is
      the safe direction), several are allowed, and one that matches NOTHING
      refuses, so a typo cannot silently keep what should have gone.

    `dry=True` resolves exactly as it would and returns the set instead of
    closing it — same arguments, same refusals, no `Target.closeTarget`. The
    reply says `dry: true` and carries `would_close` (never `closed`), so a
    caller cannot mistake a preview for an action.

    Everything to close resolves FIRST, so a bad argument, an unmatched
    exception or an ambiguous filter cannot leave a half-applied close. The
    close then goes per browser and every requested id is read back: a
    survivor is a refusal that names it. Tabs in browsers this CLI neither
    manages nor has attached are reported in `skipped`, never closed.
    """
    excepts = list(excepts or [])
    likes = list(like or [])
    named = bool(specs) or title is not None or url is not None or bool(likes)
    for flag, value in (("--title", title), ("--url", url)):
        if value is not None and not str(value):
            fail("bad-args",
                 f"tab close: {flag} needs a value — an exact title or URL")
    if any(not str(spec) for spec in excepts):
        fail("bad-args", "tab close: --except needs a value — a TAB spec")
    if any(not str(value) for value in likes):
        fail("bad-args",
             "tab close: --like needs a value — the substring to sweep for")
    if any(not str(spec).strip() for spec in specs):
        fail("bad-args", "tab close: a TAB spec cannot be empty")
    if any(str(spec).strip().lower() in ("id:", "id: ")
           for spec in specs):
        fail("bad-args",
             "tab close: `id:` needs a target id prefix — without one it "
             "names every tab")
    if title is not None and url is not None:
        fail("bad-args",
             "tab close: name tabs by --title or by --url, not both")
    if specs and (title is not None or url is not None):
        fail("bad-args",
             "tab close: name tabs by SPEC or by --title/--url, not both")
    if likes and (specs or title is not None or url is not None):
        fail("bad-args",
             "tab close: --like is a substring sweep, so it takes no SPEC, "
             "--title or --url — name tabs one way per call")
    if all_tabs and named:
        fail("bad-args",
             "tab close: --all takes no SPEC, --like, --title or --url — it "
             "already names every tab")
    if excepts and named:
        fail("bad-args",
             "tab close: --except means EVERY tab but those, so it takes no "
             "SPEC, --like, --title or --url (add --all to say it explicitly)")
    if not named and not all_tabs and not excepts:
        fail("bad-args",
             "tab close: name tabs with SPEC, --like VALUE, --title VALUE, "
             "--url URL, or --all [--except SPEC]")
    skipped: list[dict] = []
    filter_used: dict = {}
    if all_tabs or excepts:
        ours, foreign = _split(_closeable(browser))
        keepers: set[str] = set()
        for spec in excepts:
            if not str(spec).strip():
                fail("bad-args", "tab close: a --except spec cannot be empty")
            matched = [tab for _r, tab, _i in ours if _match_spec([tab], spec)]
            matched += [tab for tab in foreign if _match_spec([tab], spec)]
            if not matched and (ours or foreign):
                fail("no-page-tab",
                     f"tab close: --except {spec!r} matches no tab, so it "
                     "would keep nothing — `tab list` shows what is open")
            keepers |= {str(tab["id"]) for tab in matched}
        ours = [(row, tab, index) for row, tab, index in ours
                if str(tab["id"]) not in keepers]
        skipped = [tab for tab in foreign if str(tab["id"]) not in keepers]
    elif likes:
        ours, skipped = _spec_matches(likes, browser, loose=True)
    elif title is not None or url is not None:
        field = "title" if title is not None else "url"
        value = str(title if title is not None else url)
        ours, skipped = _exact_matches(field, value, browser)
        filter_used = {field: value}
        if not ours:
            if skipped:
                fail("not-managed",
                     f"{len(skipped)} tab(s) match {field} {value!r}, and "
                     f"every one of them is in a browser this CLI did not "
                     f"start ({skipped[0]['exe']} on "
                     f"{skipped[0]['profile']}) — a write needs "
                     "`attach --port N`, or `--browser NAME` to narrow it"
                     )
            fail("no-page-tab",
                 f"no tab in a browser this CLI drives has {field} exactly "
                 f"{value!r} — `tab list` shows what is open")
    else:
        ours, skipped = _spec_matches(list(specs), browser)
    closing: list[dict] = [{"id": tab["id"], "title": tab["title"],
                            "url": tab["url"], "pid": row["pid"],
                            "managed": row["managed"]}
                           for row, tab, _index in ours]
    if dry:
        # a preview: same resolution, same refusals, no Target.closeTarget —
        # and a different KEY, so `closed` never means "would have closed"
        reply = {"ok": True, "dry": True, "would_close": closing,
                 "count": _tab_count(),
                 "note": ("nothing was closed: --dry resolves and reports "
                          "the set the same call would take")}
        if all_tabs or excepts:
            reply["all"] = True
        if excepts:
            reply["except"] = list(excepts)
        if likes:
            reply["like"] = likes
        if filter_used:
            reply["filter"] = {**filter_used, "exact": True}
        if skipped:
            reply["skipped"] = skipped
        return reply
    by_profile: dict[str, list[str]] = {}
    for row, tab, _index in ours:
        by_profile.setdefault(str(row["profile"]), []).append(str(tab["id"]))
    for profile, ids in by_profile.items():
        # closing a tab is a WRITE, and `browser_call` re-reads the port FILE at
        # call time: ask the kernel once per profile first (a review measured
        # that this loop went wherever the port file pointed)
        _verify_profile_endpoint(profile)
        for target_id in ids:
            cdp.browser_call(profile, "Target.closeTarget",
                             {"targetId": target_id})
    survivors: list[str] = []
    for profile, ids in by_profile.items():
        survivors += _wait_ids_gone(profile, ids)
    if survivors:
        fail("close-tab-not-verified",
             f"{len(survivors)} of {len(ours)} tabs are still open: "
             + ", ".join(str(i)[:10] for i in survivors[:4]))
    reply = {"ok": True, "closed": closing, "count": _tab_count()}
    if all_tabs or excepts:
        reply["all"] = True
    if excepts:
        reply["except"] = list(excepts)
    if likes:
        reply["like"] = likes
    if filter_used:
        reply["filter"] = {**filter_used, "exact": True}
    if skipped:
        reply["skipped"] = skipped
    if not ours:
        reply["note"] = ("there was no tab to close: every one was kept by "
                          "--except, or none matched")
    return reply


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
ACTIVATE_TIMEOUT_S = 3.0    # a tab becomes visible immediately, or it will not
# One expression for "the document is there and parsed", read by both the
# single read and the poll.
READY_EXPR = "document.readyState + (document.body ? '+body' : '')"


def _no_drive(browser: str = "", reason: str = "") -> None:
    """Refuse a drive with nothing to drive — and say WHICH thing is missing.

    "No drivable browser" is honest for an unscoped call. A scoped one asked
    about ONE instance, so the refusal names that instance and the command that
    would start it; a call narrowed by name gets the name. An endpoint that
    ANSWERED but did not verify is a different refusal (`cdp-not-local`), which
    the callers check first — this one is for nothing being there at all.
    """
    scoped = SCOPE["profile"]
    asked = [part for part in ((f"on {scoped}" if scoped else ""),
                               (f"matching {browser!r}" if browser else ""))
             if part]
    where = f" {' '.join(asked)}" if asked else ""
    fix = (f"`open --profile {scoped}` starts it" if scoped
           else "run `browser-control-cli open`")
    fail("cdp-unreachable",
         f"no drivable browser{where} — {fix}"
         + (f" ({reason})" if reason else ""))


def _one_tab(spec: str, browser: str, for_write: bool) -> tuple[dict, dict]:
    """(browser row, tab row) for the ONE tab a page verb acts on.

    With a spec: the same resolution every other verb uses (one match, or a
    refusal naming the candidates) — which is also how a tab in a browser
    this CLI only READS is reached.

    Without one: the tab of a browser this CLI drives (managed, or attached).
    Not "the only tab on the machine": a second drivable browser — the user's
    own, running beside ours — would otherwise make every unqualified read
    refuse `tab-ambiguous`. Several tabs in that browser still refuse: a page
    verb must never pick among tabs nobody named. For a WRITE, a browser
    nobody handed over is not a candidate at all: it is named and refused.
    """
    if spec:
        row, tab, _index = _resolve_across([spec], browser, for_write)[0]
        return row, tab
    rows = _writable(browser) if for_write else _readable(browser)
    if not rows:
        # ask `_drivable` in its STRICT form first: an endpoint that answered
        # but did not verify has to refuse `cdp-not-local`. Then a WRITE with
        # only a stranger's browser up refuses `not-managed`, naming it and the
        # two ways to hand it over — never a silent write into a session nobody
        # offered. Only when nothing answered at all is the answer "there is
        # nothing to drive": the first version blamed the endpoint for both,
        # which is a lie when no browser is running.
        strangers = _drivable(browser)
        if for_write and strangers:
            fail("not-managed",
                 f"{_who(strangers)} is drivable, but this CLI neither manages "
                 "nor has attached it — a write needs a browser it started "
                 "(`open`), an `attach --port N` grant, or a --tab SPEC to "
                 "read a tab instead")
        _no_drive(browser)
    pairs = [(row, tab) for row in rows for tab in _tabs_or_fail(row)]
    if not pairs:
        fail("no-page-tab",
             "no page tabs to act on — open one with "
             "`browser-control-cli tab URL`")
    if len(pairs) > 1:
        where = ", ".join(f'{flat(t["title"], 20) or flat(t["url"], 30)} '
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
        # `before_url and …`: with an unreadable before, `_same_page(now, "")`
        # is always false, so the move used to be reported as PROVEN by a
        # tautology (a review measured it). An unknown before is handled by the
        # caller, which judges the AFTER state instead.
        if now and before_url and not _same_page(now, before_url):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def _wait_url_change(profile: str, target_id: str, before: str,
                     timeout: float = HISTORY_TIMEOUT_S) -> bool:
    """Did the tab's address leave `before` within the deadline?

    `before` must be KNOWN (`before and …`): with an unreadable address,
    `_same_page(now, "")` is always false, so any readable address would count
    as "it changed" — the same tautology `nav`'s `moved` had (a review found it
    here after that one was fixed). Callers that cannot know the address judge
    the AFTER state instead.
    """
    deadline = time.time() + timeout
    while True:
        now = _href(profile, target_id)
        if now and before and not _same_page(now, before):
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
    """`tab nav`: navigate ONE tab with the BROWSER's own command.

    `Page.navigate` runs no JavaScript in the page, so a tab whose renderer is
    parked (a suppressed dialog, a script that never yields) is still
    navigable — the browser replaces the document AND its renderer, which makes
    this verb the way OUT of that state (measured: the parked tab answers again
    as soon as the new document commits).

    The read-backs are unchanged: the address as OBSERVED and whether the
    document finished loading. Chromium refuses the navigation itself for a
    name that does not resolve (`nav-failed`), a tab that never left the page
    it was on refuses `nav-not-verified` (a beforeunload prompt, typically),
    and a URL the browser turns into a DOWNLOAD is named as one: no page
    navigated, so calling it a navigation would be a lie.
    """
    target = safe_url(url)
    row, tab_row = _one_tab(tab, browser, for_write=True)
    profile, target_id = str(row["profile"]), str(tab_row["id"])
    before = _href(profile, target_id)
    before_origin = _time_origin(profile, target_id)
    with cdp.Session(cdp.target_ws(cdp.port_of(profile), target_id)) as session:
        try:
            reply = session.call("Page.navigate", {"url": target})
        except ControlError as e:
            fail("nav-failed",
                 f"the browser refused to navigate to {target!r}: {e.message}")
    refused = str(reply.get("errorText") or "")
    if reply.get("isDownload"):
        fail("nav-failed",
             f"the browser treated {target!r} as a DOWNLOAD, so no page "
             "navigated and the tab is where it was")
    # `moved` is NULL when the address BEFORE could not be read (a parked tab is
    # exactly that case): "did it move" has no oracle then, so the read-back
    # below takes its place — and it POLLS, because the new document's renderer
    # may not answer on the first try (a review found this branch reading once
    # where every other path polls)
    moved: bool | None = None if not before else _wait_move(profile, target_id,
                                                            before,
                                                            before_origin)
    if moved is None:
        deadline = time.time() + NAV_MOVE_S
        while time.time() < deadline:
            url_read = _href(profile, target_id)
            if url_read and _same_page(target, url_read):
                break
            time.sleep(0.25)
    loaded = _wait_document(profile, target_id) if moved else _ready(profile,
                                                                     target_id)
    url_read = _href(profile, target_id)
    if url_read.startswith("chrome-error://") or refused:
        fail("nav-failed",
             f"the browser could not load {target!r}"
             f"{f' ({refused})' if refused else ''} — the tab is on its own "
             "error page (a name that does not resolve, a refused connection "
             "or a certificate problem), not the requested document")
    if moved is None and (not url_read or not _same_page(target, url_read)):
        fail("nav-not-verified",
             f"this tab's address could not be read before the navigation and "
             f"reports {url_read!r} after it — the navigation did not verify "
             "(the tab may still be parked on a dialog: `tab dialog state`)")
    if moved is not None and not moved and not _same_page(target, before):
        fail("nav-not-verified",
             f"the tab is still at {before!r} after navigating to {target!r} "
             "— a beforeunload prompt the browser is waiting on (`tab dialog "
             "state`), or a navigation Chromium cancelled")
    return {"ok": True, "tab": f"id:{target_id}", "url": target,
            "url_read": url_read, "loaded": loaded, "moved": moved,
            "browser": _brief(row)}


def history(direction: str, tab: str = "", browser: str = "") -> dict:
    """`tab back` / `tab forward`: move the tab's history, then read it back.

    `history.back()` with nothing to go back to is not an error the page
    reports, so the reply is the OBSERVED change: a tab still at the same
    address after the wait refuses `nav-not-verified`.

    The MOVE is the protocol's own (`Page.getNavigationHistory` +
    `Page.navigateToHistoryEntry`), not page JavaScript: a page that shadows
    `history.back` could otherwise turn a real move into a refusal, or into a
    navigation of its own choosing — and "whatever CDP can do natively, do that"
    is this project's rule (a review flagged the `history.back()` call).
    """
    if direction not in ("back", "forward"):
        fail("bad-args", f"history: {direction!r} is not back or forward")
    row, tab_row = _one_tab(tab, browser, for_write=True)
    profile, target_id = str(row["profile"]), str(tab_row["id"])
    before = _href(profile, target_id)
    tab_ws = cdp.target_ws(cdp.port_of(profile), target_id)
    listing = cdp.call(tab_ws, "Page.getNavigationHistory")
    entries = listing.get("entries") if isinstance(listing, dict) else None
    index = as_int(listing.get("currentIndex")) if isinstance(listing, dict) \
        else -1
    if not isinstance(entries, list) or not entries:
        fail("nav-failed",
             f"the browser reports no history for this tab, so there is no "
             f"{direction} entry to move to")
    wanted = index - 1 if direction == "back" else index + 1
    if wanted < 0 or wanted >= len(entries):
        fail("nav-failed",
             f"the tab is at history entry {index + 1} of {len(entries)} — "
             f"there is no {direction} entry to move to")
    cdp.call(tab_ws, "Page.navigateToHistoryEntry",
             {"entryId": (entries[wanted] or {}).get("id")})
    if not _wait_url_change(profile, target_id, before):
        if not before:
            # the address could not be read BEFORE, so "it changed" has no
            # oracle: what proves the move is the tab answering on a real page
            after = _href(profile, target_id)
            if not after or after.startswith("chrome-error://"):
                fail("nav-not-verified",
                     f"this tab's address could not be read before {direction} "
                     f"and reports {after!r} after it — the move did not verify")
        else:
            fail("nav-not-verified",
                 f"the tab is still at {before!r} after {direction} — the "
                 "browser moved to a history entry whose address did not "
                 "change (a same-document entry), or the page re-set it")
    url_read = _href(profile, target_id)
    if url_read.startswith("chrome-error://"):
        fail("nav-failed",
             f"the {direction} entry did not load — the tab is on the "
             "browser's own error page")
    return {"ok": True, "direction": direction, "tab": f"id:{target_id}",
            "url_before": before, "url_read": url_read,
            "browser": _brief(row)}


def activate(tab: str = "", browser: str = "") -> dict:
    """`tab activate`: bring ONE tab to the front, verified by the page.

    `Page.bringToFront` makes the tab the active one and raises its window —
    the one thing a background tab cannot do for itself, and what a page that
    visibility-gates (or gets throttled) needs. Chromium is not asked to
    confirm anything, so the oracle is the page's own
    `document.visibilityState`: read before, read after, and a tab that still
    reports `hidden` is a refusal rather than a claim. A second window's own
    active tab is ALSO visible, which is why `--tab active` is ambiguous with
    two windows instead of picking one.
    """
    row, tab_row = _one_tab(tab, browser, for_write=True)
    profile, target_id = str(row["profile"]), str(tab_row["id"])
    with cdp.Session(cdp.target_ws(cdp.port_of(profile), target_id)) as session:
        before = str(session.evaluate("document.visibilityState") or "")
        session.call("Page.bringToFront")
        after = before
        deadline = time.time() + ACTIVATE_TIMEOUT_S
        while after != "visible" and time.time() < deadline:
            time.sleep(0.15)
            with contextlib.suppress(ControlError):
                after = str(session.evaluate("document.visibilityState",
                                             timeout=4) or after)
    if after != "visible":
        fail("activate-not-verified",
             f"the tab still reports visibility={after or 'unreadable'!r} "
             "after `Page.bringToFront` — its window may be hidden entirely "
             "(another workspace, or iconified), and this CLI drives "
             "browsers, not the desktop")
    return {"ok": True, "activated": True, "verified": True,
            "tab": f"id:{target_id}", "visibility_before": before,
            "visibility": after, "changed": before != after,
            "url": tab_row.get("url"), "title": tab_row.get("title"),
            "note": ("Page.bringToFront activates the tab and raises its "
                     "window; the read-back is the page's own "
                     "document.visibilityState"),
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
