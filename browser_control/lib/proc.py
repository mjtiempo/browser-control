"""proc — Linux process identity, read from /proc. No browser semantics.

Who owns a port, which pid runs on which profile, and whether a pid is alive
are questions about the KERNEL, not about a profile file: `cdp` and `browser`
both ask them, so both ask them here. `/proc` is read in exactly one module —
a second reader is a second answer.

The NUL-preserving read is load-bearing: Chrome rewrites a CHILD's cmdline in
place (space-separated, one trailing NUL) while the main process keeps real
argv boundaries, and flattening here made `--user-data-dir` containing a space
lose its tail in the ownership check (a review flagged it).
"""
from __future__ import annotations

import contextlib
import ipaddress
import os
import stat
import subprocess

from browser_control.lib.errors import (
    ERR_LAUNCH_FAILED,
    ControlError,
)
from browser_control.lib.paths import (
    norm,
    pid_file,
)

# The EXECUTABLE names a Chromium-family pid can have: the PATH names a launch
# uses are wrappers (`google-chrome-stable` execs `chrome`), so an identity
# check against them alone would refuse a genuine browser.
BROWSER_EXES = ("chrome", "chromium", "chromium-browser", "google-chrome",
                "google-chrome-stable", "brave", "brave-browser", "msedge",
                "microsoft-edge", "vivaldi", "vivaldi-bin",
                "chrome-headless-shell", "headless_shell")

# The executables that are headless WITHOUT a flag: the dedicated shell binary
# exists for nothing else, so a row for one of these reports `headless: true`
# however its command line is spelled.
HEADLESS_EXES = ("chrome-headless-shell", "headless_shell")

# A pid record is ONE small decimal number (`record_pid` writes nothing else),
# so anything bigger at that path is not this CLI's record.
PID_FILE_CAP = 64

__all__ = ["BROWSER_EXES", "HEADLESS_EXES", "cmdline_value", "exe_name",
           "exe_path", "find_pid", "is_headless_cmd", "listener_of",
           "main_processes", "pid_alive", "pid_of", "pid_on_marker",
           "pid_on_profile", "proc_text", "record_pid", "spawn"]


def pid_alive(pid: int) -> bool:
    """A live process, and not a zombie: an unreaped child still answers
    `kill(pid, 0)`, which would refuse a stop that actually worked."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def proc_text(pid: int | str, name: str) -> str:
    """One /proc file, as text — read to the END, not to 4096 bytes.

    The cap silently truncated the text every identity decision reads: a
    renderer whose `--type=` fell past it looked like a MAIN process, and a
    browser whose `--user-data-dir=` fell past it looked like no browser at all
    (a review flagged it; measured here, the longest cmdline is 2344 bytes, so
    the cap was reachable in principle rather than in practice).

    NULs are PRESERVED: they are the argv boundaries, and flattening them to
    spaces made `cmdline_value` cut a `--user-data-dir` containing a space at
    the first space — the browser this CLI started became a stranger (a review
    flagged it).
    """
    try:
        with open(f"/proc/{pid}/{name}", "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    return raw.decode("utf-8", "replace").rstrip("\0\n")


def exe_name(pid: int | str) -> str:
    """The executable a pid is running, by name, or ""."""
    try:
        return os.path.basename(os.path.realpath(f"/proc/{pid}/exe"))
    except OSError:
        return ""


def exe_path(pid: int | str) -> str:
    """The executable a pid is running, by real path — or "" if unreadable.

    `os.path.realpath` on `/proc/<pid>/exe` RAISES for a process this user
    cannot inspect (GitHub's runner: pid 11 belongs to root), and the census
    calls this for every row — so one unreadable process took `list`, `tab
    list`, `info` and every shared resolver down with a traceback instead of
    reporting the machine it observed. `exe_name` already caught this; both
    now answer "" and the row keeps whatever else could be read.
    """
    try:
        return os.path.realpath(f"/proc/{pid}/exe")
    except OSError:
        return ""


def cmdline_value(cmd: str, flag: str) -> str:
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


def is_headless_cmd(cmd: str, exe: str = "") -> bool:
    """Does this process say it runs WITHOUT a window?

    Two oracles, because a headless browser need not carry a flag: the
    dedicated `chrome-headless-shell`/`headless_shell` binaries are headless by
    nature, and a full browser says so on its own command line — `--headless`
    (bare, the spelling current Chrome accepts as well as `--headless=new`), or
    any `--headless=…` value. A URL argument that merely CONTAINS the word is
    not a mode, so a part must EQUAL the bare flag or START WITH `--headless=`.
    The parts are scanned the way `cmdline_value` splits them: on NUL when one
    is present (the real argv form), else on spaces (the in-place rewrite
    Chrome gives a child).
    """
    if os.path.basename(str(exe)) in HEADLESS_EXES:
        return True
    text = str(cmd)
    parts = text.split("\0") if "\0" in text else text.split()
    return any(part == "--headless" or part.startswith("--headless=")
               for part in parts)


def main_processes() -> list[tuple[int, str, str]]:
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
        cmd = proc_text(pid, "cmdline")
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
        exe = exe_name(pid)
        if exe in BROWSER_EXES:
            found.append((pid, exe, cmd))
    return sorted(found)


def pid_on_marker(pid: int, cmd: str, profile: str) -> bool:
    """Does that process's cmdline name this profile, however it was spelled?

    The marker is `--user-data-dir=<normalised profile>`: a browser launched
    with a trailing slash, a `..` or a symlinked root is still this profile's.
    """
    spelled = cmdline_value(cmd, "--user-data-dir")
    return bool(spelled) and norm(spelled) == norm(profile)


def find_pid(profile: str) -> int:
    """The pid running ON this profile, from its own cmdline.

    The fallback when the pid file is gone (a crash restart). Only a MAIN
    process (no `--type=`) whose exe is Chromium-family counts, so this can
    never hand back a renderer — or a process we did not start. The path is
    compared NORMALISED, so the spelling a launcher chose does not matter.
    """
    for pid, _exe, cmd in main_processes():
        if pid_on_marker(pid, cmd, profile):
            return pid
    return 0


def pid_on_profile(pid: int, profile: str) -> bool:
    """Does that pid's OWN cmdline say it runs on this profile?

    The marker is the one `find_pid` matches and the one Chrome is started
    with, so a pid that cannot show it is not this profile's browser — which is
    the difference between stopping that browser and signalling whatever
    process inherited a recycled pid. The comparison is by NORMALISED path: an
    exact string compare would refuse to stop the browser this CLI itself
    started if the launcher spelled the directory differently.
    """
    return any(found == pid and pid_on_marker(found, cmd, profile)
               for found, _exe, cmd in main_processes())


def _small_file(path: str, cap: int) -> bytes:
    """At most `cap` bytes of a REGULAR file this process did not write.

    The same rule `cdp.endpoint` applies to `DevToolsActivePort`, spelled here
    because the two readers sit in different layers (a transport module and
    the process module must not import each other) and a pid file is chosen by
    whoever wrote the profile. `O_NOFOLLOW` refuses a symlink, `O_NONBLOCK`
    keeps a reader-less FIFO from blocking the OPEN — `open()` on one waits for
    a writer, before any read — and the `fstat` refuses anything that is not a
    small regular file. `b""` is "no record", which is what the caller wants;
    a refusal would turn a degraded `close` into an error.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return b""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            return b""
        return os.read(fd, cap)
    except OSError:
        return b""
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def pid_of(profile: str) -> int:
    """The pid recorded for this profile, or the one running on it now.

    A recorded pid is used only while the process still SAYS it is this
    profile's browser: the record is a hint, and a stale one plus a recycled
    pid would otherwise aim `stop` at an unrelated process. `find_pid` is the
    fallback, and it refuses to hand back a renderer or a stranger.

    The record is read like any file another process may have written
    (`_small_file`): a FIFO, a symlink or a device at the pid path is "no
    record", not a hung CLI — the profile directory is exactly the place a
    stranger can plant one.
    """
    raw = _small_file(pid_file(profile), PID_FILE_CAP)
    try:
        pid = int(raw.strip())
    except ValueError:
        pid = 0
    if pid and pid_alive(pid) and pid_on_profile(pid, profile):
        return pid
    return find_pid(profile)


def record_pid(profile: str, pid: int) -> None:
    """Write the pid this profile's browser runs as. A missing file degrades
    `close`; it does not break `open`.

    Written to a scratch file and renamed, and never through a link: a
    symlink planted at the pid path would have made `write_text` truncate
    whatever it pointed at (CWE-377, the pattern the screenshot writer
    already carries). Best effort throughout — a browser that started without
    its pid recorded is still a browser.
    """
    path = pid_file(profile)
    temp = f"{path}.bc-{os.getpid()}.part"
    try:
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o600)
    except OSError:
        return
    with contextlib.suppress(OSError):
        try:
            with os.fdopen(handle, "w") as out:
                out.write(str(pid))
            os.replace(temp, path)
        except OSError:
            os.remove(temp)


def spawn(argv: list[str]) -> int:
    """Start a detached browser process, or refuse.

    Detached (`start_new_session`) so the browser outlives this CLI call; the
    pid it returns is what `stop` signals.
    """
    try:
        proc = subprocess.Popen(argv, start_new_session=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as e:
        raise ControlError(ERR_LAUNCH_FAILED,
                           f"cannot start {argv[0]}: {e}") from e
    return proc.pid


def _address(
        local_address: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The IP a `/proc/net/tcp{,6}` local ADDRESS names, or None.

    The field is `HEXADDR:HEXPORT`, written little-endian: `0100007F` is
    127.0.0.1, `00000000` is 0.0.0.0, and an IPv6 address is four little-endian
    32-bit words (`::1` is `00000000000000000000000001000000`).
    """
    text = str(local_address or "").strip().lower()
    try:
        if len(text) == 8:
            return ipaddress.IPv4Address(bytes.fromhex(text)[::-1])
        if len(text) == 32:
            raw = bytes.fromhex(text)
            return ipaddress.IPv6Address(
                b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))
    except ValueError:
        return None
    return None


def _tcp_listeners(text: str, port: int) -> tuple[set[str], str]:
    """One `/proc/net/tcp{,6}` table split by BIND ADDRESS for that port.

    Returns `(loopback inodes, readable exposed bind)`: `sl local_address
    rem_address st … inode`, port in HEX and `0A` meaning LISTEN. The ADDRESS
    is read, not just the port — a listener on `0.0.0.0` or a LAN address is
    reachable off this machine, and is NOT this profile's browser, however it
    answers (a review flagged that only the port was matched).
    """
    wanted = f"{port:04X}"
    loopback: set[str] = set()
    exposed = ""
    for line in str(text).splitlines()[1:]:          # the header line
        fields = line.split()
        if len(fields) < 10 or fields[3] != "0A":
            continue
        address, _sep, port_hex = fields[1].rpartition(":")
        if port_hex.upper() != wanted:
            continue
        found = _address(address)
        if found is not None and found.is_loopback:
            loopback.add(fields[9])
        elif not exposed:
            exposed = f"{found if found is not None else address}:{port}"
    return loopback, exposed


def _listening_inodes(port: int) -> tuple[set[str], str]:
    """The socket inodes LISTENING on that port on LOOPBACK, and the exposed
    bind when the only listener is elsewhere ("" when there is none).

    Both tables are read. The kernel's answer for the port is tcp4 in
    `/proc/net/tcp` and tcp6 in `/proc/net/tcp6`; a listener bound wide shows
    up in one of them, and either way its ADDRESS is what decides whether it is
    the loopback endpoint this profile's browser owns.
    """
    loopback: set[str] = set()
    exposed = ""
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(name) as handle:
                text = handle.read()
        except OSError:
            continue
        loop, wide = _tcp_listeners(text, port)
        loopback |= loop
        exposed = exposed or wide
    return loopback, exposed


def listener_of(port: int) -> dict:
    """Which process LISTENS on that port: {pid, exe, cmd} — or {}.

    The port came from a FILE (`DevToolsActivePort`), and a file can be stale
    or its port can be taken by something else. This asks the KERNEL instead:
    the listening socket's inode from `/proc/net/tcp{,6}`, then the process
    holding that inode through `/proc/<pid>/fd`. A few milliseconds, which is
    why the caller memoises.

    Only a LOOPBACK listener counts. A socket bound to `0.0.0.0` or a LAN
    address is reachable off this machine, so it is not this profile's browser
    — it is reported as `{"exposed": "<addr>:<port>"}` and every caller that
    would drive it refuses (a review flagged that the address was ignored, so
    any wide bind passed as the profile's own browser).

    A pid whose fd table cannot be read is skipped rather than guessed at: the
    answer is either the process holding the socket or nothing.
    """
    if not port:
        return {}
    inodes, exposed = _listening_inodes(port)
    if not inodes:
        return {"exposed": exposed} if exposed else {}
    marks = {f"socket:[{inode}]" for inode in inodes}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return {}
    for entry in entries:
        if not entry.isdigit():
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            handles = os.listdir(fd_dir)
        except OSError:
            continue
        for handle in handles:
            try:
                if os.readlink(f"{fd_dir}/{handle}") in marks:
                    return {"pid": int(entry), "exe": exe_name(entry),
                            "cmd": proc_text(entry, "cmdline")}
            except OSError:
                continue
    return {}
