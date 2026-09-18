#!/usr/bin/env python3
"""Live checks for browser-control — a real browser, a throwaway profile root.

Run:  python3 tests/live_test.py

Every check drives the CLI the way a user does, then reads independent state
back: a raw socket connect to the CDP port, a direct HTTP GET of `/json`, and
`/proc` for the process — never the CLI's own reply alone. The whole battery
runs against a temporary `BROWSER_CONTROL_ROOT` on a local test page, so the
user's own browsers, profiles and network are never involved.

Set `BROWSER_CONTROL_CLI` to point the battery at an installed command
instead of the checkout script.

Exit: 0 = every check passed, 1 = failures, 2 = skips (with or without
failures) — a skip is not a pass.
"""
from __future__ import annotations

import contextlib
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from importlib import import_module
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# The list of browsers to look for: the package's own, so a name added there
# is a name the battery checks for too. Imported through importlib because the
# path insert above must come first.
browser_lib: Any = import_module("browser_control.lib.browser")
cdp: Any = import_module("browser_control.lib.cdp")

CLI = os.environ.get("BROWSER_CONTROL_CLI") or str(REPO / "browser-control-cli")
ROOT = tempfile.mkdtemp(prefix="browser-control-live-")
ENV = {**os.environ, "BROWSER_CONTROL_ROOT": ROOT}
TIMEOUT_S = 90
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

results: list[tuple[str, str, str]] = []
# What the checks discovered, and what the cleanup needs.
STATE: dict[str, Any] = {}
SERVER: http.server.ThreadingHTTPServer | None = None


# ------------------------------------------------------------------- harness
def check(name: str, fn) -> None:                              # noqa: ANN001
    try:
        detail = str(fn() or "")
    except Exception as e:                                     # noqa: BLE001
        results.append((FAIL, name, f"{type(e).__name__}: {e}"))
        print(f"FAIL  {name}  {type(e).__name__}: {e}")
    else:
        results.append((PASS, name, detail))
        print(f"PASS  {name}  {detail}")


def run(*argv: str, timeout: int = TIMEOUT_S) -> tuple[int, str, str]:
    """One CLI call on the throwaway root: (returncode, stdout, stderr).

    The command is executed AS a command, so its own shebang picks the
    interpreter — a checkout script and an installed console script in some
    other venv both work.
    """
    proc = subprocess.run([CLI, *argv], capture_output=True, text=True,
                          timeout=timeout, env=ENV)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def ok_json(*argv: str) -> dict:
    """A CLI call that must succeed, as parsed JSON."""
    rc, out, err = run(*argv)
    assert rc == 0, f"{' '.join(argv)} rc={rc}: {err or out}"
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"{' '.join(argv)} printed no JSON: {out[:200]}") from e


def refuses(code: str, *argv: str) -> str:
    """A CLI call that must refuse with `code`, and exit 2."""
    rc, out, err = run(*argv)
    assert rc == 2, f"{' '.join(argv)} rc={rc} (wanted 2): {err or out}"
    assert f"ERR[{code}]" in err, \
        f"{' '.join(argv)}: wanted ERR[{code}], got {err!r}"
    return err


# --------------------------------------------------- independent state reads
def listening(port: int) -> bool:
    """A raw connect to the CDP port — no HTTP, no CLI, no library."""
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def http_json(port: int, path: str = "/json") -> Any:
    """The endpoint read directly, bypassing the CLI."""
    with urllib.request.urlopen(                       # noqa: S310 (loopback)
            f"http://127.0.0.1:{port}{path}", timeout=5) as reply:
        return json.loads(reply.read().decode("utf-8", "replace"))


def cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def procs_on(marker: str) -> list[int]:
    """Every pid whose cmdline mentions `marker` — the profile, so the answer
    covers the browser and its children."""
    pids = []
    for entry in os.listdir("/proc"):
        if entry.isdigit() and marker in cmdline(int(entry)):
            pids.append(int(entry))
    return pids


def pages(port: int) -> list[dict]:
    return [t for t in http_json(port) if t.get("type") == "page"]


# ------------------------------------------------------------- the test page
def start_server() -> None:
    """A local page server, so tabs have distinct URLs without the network."""
    global SERVER

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            name = self.path.strip("/") or "root"
            body = (f"<!doctype html><title>{name}</title>"
                    f"<h1>{name}</h1>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    SERVER = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def base_url() -> str:
    assert SERVER is not None, "the test page server is not running"
    return f"http://127.0.0.1:{SERVER.server_address[1]}"


# ------------------------------------------------------------------- checks
def c_selftest() -> str:
    data = ok_json("selftest")
    assert data["ok"] is True, data
    for verb in ("open", "close", "tabs", "new-tab", "close-tab", "selftest"):
        assert verb in data["verbs"], data["verbs"]
    assert data["websockets"] and data["python"], data
    assert Path(data["python"]).exists(), data["python"]
    assert data["browsers"], "selftest found no browser yet the battery runs"
    return (f'{data["version"]} on {data["python_version"]}, '
            f'websockets {data["websockets"]}')


def c_open_starts_a_browser() -> str:
    reply = ok_json("open", f"{base_url()}/one")
    assert reply["started"] is True, reply
    port, pid = int(reply["port"]), int(reply["pid"])
    profile = str(reply["profile"])
    assert profile.startswith(ROOT), \
        f"the browser runs outside the throwaway root: {profile}"
    STATE.update(port=port, pid=pid, profile=profile)
    # independent: the port answers, and the pid is a process on our profile
    deadline = time.time() + 10
    while time.time() < deadline and not listening(port):
        time.sleep(0.2)
    assert listening(port), f"port {port} accepts no connection"
    assert "Chrome" in str(http_json(port, "/json/version").get("Browser")), \
        "the endpoint is not a Chromium"
    line = cmdline(pid)
    assert f"--user-data-dir={profile}" in line, f"pid {pid}: {line[:120]}"
    rows = pages(port)
    assert len(rows) == 1, rows
    assert rows[0]["id"] == reply["tabs"][0]["id"], (rows, reply["tabs"])
    STATE["tab"] = rows[0]["id"]
    return f"pid {pid}, port {port}, tab {rows[0]['id'][:8]}…"


def c_tabs_matches_the_tab_list() -> str:
    reply = ok_json("tabs")
    assert reply["count"] == len(reply["tabs"]) == 1, reply
    assert reply["tabs"][0]["id"] == STATE["tab"], reply
    assert [t["id"] for t in pages(STATE["port"])] == [STATE["tab"]], \
        "the CLI's tab list and /json disagree"
    return "the CLI's tab list is /json, filtered and id-sorted"


def c_new_tab() -> str:
    reply = ok_json("new-tab", f"{base_url()}/two")
    tid = str(reply["id"])
    assert tid and reply["tab"] == f"id:{tid}", reply
    assert reply["count"] == 2, reply
    assert tid in {t["id"] for t in ok_json("tabs")["tabs"]}, reply
    STATE["second"] = tid
    return f"tab {tid[:8]}… re-read from the tab list"


def c_close_tab_by_id_prefix() -> str:
    tid = str(STATE["second"])
    reply = ok_json("close-tab", f"id:{tid[:8]}")
    assert reply["closed"]["id"] == tid, reply
    assert reply["count"] == 1, reply
    assert tid not in {t["id"] for t in pages(STATE["port"])}, \
        "the tab is still in /json"
    return f"{tid[:8]}… closed and absent from /json"


def c_close_tab_by_substring() -> str:
    tid = str(ok_json("new-tab", f"{base_url()}/three")["id"])
    reply = ok_json("close-tab", "three")
    assert reply["closed"]["id"] == tid, reply
    assert tid not in {t["id"] for t in pages(STATE["port"])}, reply
    return "a title/url substring resolves to exactly one tab"


def c_ambiguous_spec_refuses() -> str:
    first = str(ok_json("new-tab", f"{base_url()}/four-a")["id"])
    second = str(ok_json("new-tab", f"{base_url()}/four-b")["id"])
    err = refuses("tab-ambiguous", "close-tab", "four")
    assert first[:6] in err or "four" in err, err
    for tid in (first, second):
        ok_json("close-tab", f"id:{tid}")
    left = {t["id"] for t in pages(STATE["port"])}
    assert first not in left and second not in left, left
    return "two matches refused rather than picked, both closed by id"


def c_refusals() -> str:
    refuses("no-page-tab", "close-tab", "zzz-nothing")
    refuses("bad-args", "close-tab")
    refuses("bad-args", "open", "file:///etc/passwd")
    refuses("bad-args", "open", "a", "b")
    refuses("unknown-command", "frobnicate")
    return "five refusals, each with its own code, exit 2"


def c_open_adopts_the_running_browser() -> str:
    before = len(pages(STATE["port"]))
    reply = ok_json("open", f"{base_url()}/five")
    assert reply["started"] is False, reply
    assert reply["pid"] == STATE["pid"], reply
    rows = pages(STATE["port"])
    assert len(rows) == before + 1, (before, rows)
    assert any(str(r["url"]).endswith("/five") for r in rows), rows
    return "the running browser was handed the URL as a new tab"


def c_close_stops_the_browser() -> str:
    pid, port = STATE["pid"], STATE["port"]
    reply = ok_json("close")
    assert reply["stopped"] is True and reply["pid"] == pid, reply
    assert not Path(f"/proc/{pid}").exists(), f"pid {pid} is still in /proc"
    deadline = time.time() + 10
    while time.time() < deadline and listening(port):
        time.sleep(0.2)
    assert not listening(port), f"port {port} still accepts connections"
    refuses("cdp-unreachable", "tabs")
    return f"pid {pid} gone, port {port} closed, tabs refuses"


def c_close_is_idempotent() -> str:
    reply = ok_json("close")
    assert reply["stopped"] is False, reply
    assert "no managed browser" in str(reply.get("reason", "")), reply
    return "a second close is a no-op, not a failure"


def c_no_leftover_process() -> str:
    left = procs_on(ROOT)
    assert not left, f"processes still running on the throwaway root: {left}"
    return "nothing of ours is left running"


CHECKS = (
    ("selftest answers without a browser", c_selftest),
    ("open starts a managed browser", c_open_starts_a_browser),
    ("tabs reads the tab list back", c_tabs_matches_the_tab_list),
    ("new-tab names the tab it made", c_new_tab),
    ("close-tab by id prefix", c_close_tab_by_id_prefix),
    ("close-tab by substring", c_close_tab_by_substring),
    ("an ambiguous spec refuses", c_ambiguous_spec_refuses),
    ("refusals carry their codes", c_refusals),
    ("open adopts a running browser", c_open_adopts_the_running_browser),
    ("close stops the browser, verified", c_close_stops_the_browser),
    ("close again is a no-op", c_close_is_idempotent),
    ("no process is left behind", c_no_leftover_process),
)


# ------------------------------------------------------------------- runner
def prereq() -> str:
    """`""` when the battery can run, else the reason to SKIP every check.

    Asked of the CLI's own `selftest`, so the answer is about the command
    under test rather than about this interpreter.
    """
    if not Path(CLI).exists() and not shutil.which(CLI):
        return f"no command to test at {CLI}"
    if Path(CLI).exists() and not os.access(CLI, os.X_OK):
        return f"{CLI} is not executable"
    rc, out, err = run("selftest", timeout=30)
    if rc != 0:
        return f"`selftest` refused (rc={rc}): {err or out}"
    try:
        found = json.loads(out).get("browsers") or []
    except json.JSONDecodeError:
        return f"`selftest` printed no JSON: {out[:120]}"
    if not found:
        return ("no Chromium-family browser on PATH (selftest lists none): "
                + ", ".join(browser_lib.BROWSER_BINS))
    return ""


def cleanup() -> None:
    """Close what we opened, kill what we started (and wait for it to go),
    then remove the temp root.

    The wait matters: a browser that has just been SIGTERMed still writes to
    its profile for a moment, and removing the tree under it leaves an empty
    directory behind.
    """
    try:
        if STATE.get("port") and listening(int(STATE["port"])):
            run("close", timeout=60)
        deadline = time.time() + 10
        while True:
            left = procs_on(ROOT)
            if not left or time.time() >= deadline:
                break
            for pid in left:            # ours by construction: the root
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
            time.sleep(0.2)
        left = procs_on(ROOT)
        if left:
            print(f"      (cleanup: {left} still running on {ROOT})")
    finally:
        if SERVER is not None:
            SERVER.shutdown()
            SERVER.server_close()
        for _ in range(3):
            shutil.rmtree(ROOT, ignore_errors=True)
            if not Path(ROOT).exists():
                break
            time.sleep(0.3)
        if Path(ROOT).exists():
            print(f"      (cleanup: {ROOT} survived removal)")


def main() -> int:
    reason = prereq()
    try:
        if reason:
            for name, _fn in CHECKS:
                results.append((SKIP, name, reason))
                print(f"SKIP  {name}  {reason}")
        else:
            start_server()
            print(f"root {ROOT}\npage {base_url()}\ncli  {CLI}\n")
            for name, fn in CHECKS:
                check(name, fn)
    finally:
        # always: a battery that skipped must not leave its temp root behind
        # either (the first version leaked one per skip)
        cleanup()
    passed = sum(1 for kind, _, _ in results if kind == PASS)
    failed = sum(1 for kind, _, _ in results if kind == FAIL)
    skipped = sum(1 for kind, _, _ in results if kind == SKIP)
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    if failed:
        return 2 if skipped else 1
    return 2 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
