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
import urllib.parse
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
# Created in `main()`, NOT here: this file is a module like any other, and a
# collector that imports it (pytest, an analyzer) must not create temp roots,
# start browsers or sweep anything. It did — that is where the orphan roots
# these tests kept finding came from.
ROOT = ""
TIMEOUT_S = 90
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

results: list[tuple[str, str, str]] = []
# What the checks discovered, and what the cleanup needs.
STATE: dict[str, Any] = {}
SERVER: http.server.ThreadingHTTPServer | None = None
# The DOM fixture the browser reaches over loopback: a labelled button, a
# display:none twin of it, a text input, a role=button, a SHADOW root with its
# own button, an iframe, a long paragraph, and a button that appears after
# `late` milliseconds (so `wait` has something to actually poll for).
DOM_FIXTURE = """<!doctype html><meta charset="utf-8"><title>dom fixture</title>
<h1>Dom Fixture Heading</h1>
<button aria-label="Save the thing">save</button>
<a href="/one.html">a link</a>
<input type="text" placeholder="search here">
<div role="button" tabindex="0">role button</div>
<button id="hidden-one" style="display:none">Save the thing</button>
<div id="host"></div>
<iframe src="/dom-frame" width="200" height="100"></iframe>
<p id="long-text">__FILLER__</p>
<script>
  const root = document.getElementById('host').attachShadow({mode: 'open'});
  root.innerHTML =
    '<button id="in-shadow" aria-label="Shadow Action">shadow</button>';
  const delay = Number(__LATE__);
  setTimeout(() => { const el = document.createElement('button');
    el.id = 'late'; el.setAttribute('aria-label', 'Late Button');
    document.body.appendChild(el); }, delay);
</script>"""
DOM_FRAME = ("<!doctype html><title>frame</title>"
             "<button aria-label=\"Frame Button\">in frame</button>")


def env() -> dict[str, str]:
    """The environment every CLI call gets: the throwaway root, and ours."""
    return {**os.environ, "BROWSER_CONTROL_ROOT": ROOT}


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
                          timeout=timeout, env=env())
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
        def _send(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.startswith("/go"):
                # a redirect, so the battery can prove that "it MOVED" is the
                # test and not "it arrived at the string it was handed"
                self.send_response(302)
                self.send_header("Location", "/eight-b")
                self.end_headers()
                return
            if self.path.startswith("/dom-frame"):
                self._send(DOM_FRAME.encode())
                return
            if self.path.startswith("/dom"):
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query)
                late = str((query.get("late") or ["0"])[0])
                if not late.isdigit():
                    late = "0"
                self._send(DOM_FIXTURE.replace("__FILLER__", "filler text. " * 200)
                           .replace("__LATE__", late).encode())
                return
            name = self.path.strip("/") or "root"
            self._send(f"<!doctype html><title>{name}</title>"
                       f"<h1>{name}</h1>".encode())

        def log_message(self, format: str, *args: object) -> None:
            pass

    SERVER = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def base_url() -> str:
    assert SERVER is not None, "the test page server is not running"
    return f"http://127.0.0.1:{SERVER.server_address[1]}"


# ------------------------------------------------------------------- checks
def dead_port() -> int:
    """A loopback port nothing answers on — CHECKED, not assumed.

    A port that is bound-but-unused would let the browser load a page instead
    of failing, so the check would be measuring the wrong thing.
    """
    for _ in range(5):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        with socket.socket() as check:
            check.settimeout(1.0)
            if check.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise AssertionError("no closed loopback port could be found")


def sweep_stale_roots() -> list[str]:
    """Remove the temp roots of runs that were KILLED before their cleanup.

    A root whose path appears in no running process is abandoned — the Chrome
    a run starts carries the path in its cmdline — but only when it is old
    enough that it cannot be a run starting up right now (that run's browser
    would not exist yet, and sweeping it would be the bug). The point is not
    tidiness: a leftover here means a run did not finish, and saying so is
    more useful than silently collecting them.
    """
    removed: list[str] = []
    base = tempfile.gettempdir()
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        if not name.startswith("browser-control-live-") or path == ROOT \
                or not os.path.isdir(path):
            continue
        try:
            if time.time() - os.path.getmtime(path) < 120:
                continue                 # maybe a run starting up right now
        except OSError:
            continue
        if procs_on(path):
            continue                     # a live browser still runs on it
        shutil.rmtree(path, ignore_errors=True)
        if not Path(path).exists():
            removed.append(name)
    return removed


def our_tabs() -> list[dict]:
    """Our managed browser's tabs, narrowed by its own profile name.

    `--browser` prefers the managed match, so a browser the user also has open
    under the same name cannot leak into the answer.
    """
    reply = ok_json("tab", "list", "--browser", str(STATE["name"]))
    groups = reply["browsers"]
    assert groups, (f"`tab list --browser {STATE['name']}` found nothing",
                    reply)
    assert all(g["managed"] for g in groups), groups
    return [t for g in groups for t in g["tabs"]]


def c_selftest() -> str:
    data = ok_json("selftest")
    assert data["ok"] is True, data
    for verb in ("open", "close", "list", "info", "attach", "detach",
                 "tab", "selftest"):
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
    STATE.update(port=port, pid=pid, profile=profile,
                 name=os.path.basename(profile))
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


def c_tab_list_matches_the_tab_list() -> str:
    """`tab list --browser <ours>` is the browser's own tab list, re-read."""
    rows = our_tabs()
    assert [t["id"] for t in rows] == [t["id"] for t in pages(STATE["port"])], \
        (rows, pages(STATE["port"]))
    assert STATE["tab"] in {t["id"] for t in rows}, rows
    return "`tab list` for our browser is /json, filtered and id-sorted"


def c_tab_info_names_the_browser() -> str:
    """`tab info SPEC` resolves one handle and names the browser owning it."""
    reply = ok_json("tab", "info", f"id:{STATE['tab'][:8]}")
    assert reply["tab"]["id"] == STATE["tab"], reply
    assert isinstance(reply["tab"]["index"], int), reply
    assert reply["browser"]["managed"] is True, reply
    assert reply["browser"]["pid"] == STATE["pid"], reply
    return (f'tab {STATE["tab"][:8]}… is index {reply["tab"]["index"]} in '
            f'pid {reply["browser"]["pid"]}')


def c_new_tab() -> str:
    reply = ok_json("tab", f"{base_url()}/two")
    tid = str(reply["id"])
    assert tid and reply["tab"] == f"id:{tid}", reply
    assert reply["count"] == 2, reply
    assert tid in {t["id"] for t in our_tabs()}, reply
    STATE["second"] = tid
    return f"tab {tid[:8]}… re-read from the tab list"


def c_close_tab_by_id_prefix() -> str:
    tid = str(STATE["second"])
    reply = ok_json("tab", "close", f"id:{tid[:8]}")
    assert [row["id"] for row in reply["closed"]] == [tid], reply
    assert tid not in {t["id"] for t in pages(STATE["port"])}, \
        "the tab is still in /json"
    return f"{tid[:8]}… closed and absent from /json"


def c_close_tab_by_substring() -> str:
    tid = str(ok_json("tab", f"{base_url()}/three")["id"])
    reply = ok_json("tab", "close", "three")
    assert [row["id"] for row in reply["closed"]] == [tid], reply
    assert tid not in {t["id"] for t in pages(STATE["port"])}, reply
    return "a title/url substring resolves to exactly one tab"


def c_nav_reads_the_address_back() -> str:
    """`tab nav` follows a redirect, and re-navigating the same page is fine."""
    base = base_url()
    reply = ok_json("tab", "nav", f"{base}/eight-a")
    assert reply["url_read"].endswith("/eight-a"), reply
    assert reply["moved"] is True and reply["loaded"] is True, reply
    assert reply["tab"] == f"id:{STATE['tab']}", reply
    redirected = ok_json("tab", "nav", f"{base}/go")
    assert redirected["url_read"].endswith("/eight-b"), redirected
    assert redirected["moved"] is True, redirected
    again = ok_json("tab", "nav", f"{base}/eight-b")
    assert again["url_read"].endswith("/eight-b"), again
    return "a redirect is a move; re-navigating the same page is no error"


def c_nav_refuses_a_dead_end() -> str:
    """A connection the browser cannot make is `nav-failed`, not a success."""
    dead = dead_port()
    err = refuses("nav-failed", "tab", "nav", f"http://127.0.0.1:{dead}/")
    assert "error page" in err, err
    return f"a refused connection is nav-failed, not a loaded page ({dead})"


def c_history_moves_the_address() -> str:
    """back and forward are verified by the address changing."""
    base = base_url()
    ok_json("tab", "nav", f"{base}/nine-a")
    ok_json("tab", "nav", f"{base}/nine-b")
    back = ok_json("tab", "back")
    assert back["url_read"].endswith("/nine-a"), back
    forward = ok_json("tab", "forward")
    assert forward["url_read"].endswith("/nine-b"), forward
    return "back and forward read the address back, in both directions"


def c_reload_makes_a_new_document() -> str:
    """`tab reload` is verified by `performance.timeOrigin`, not by a guess."""
    reply = ok_json("tab", "reload")
    assert reply["reloaded"] is True, reply
    assert reply["url_read"].endswith("/nine-b"), reply
    return "reload is verified by the document time changing"


def c_nav_names_the_tab() -> str:
    """Two tabs refuse without `--tab`; `--tab` moves exactly the one named."""
    base = base_url()
    other = str(ok_json("tab", "about:blank")["id"])
    try:
        err = refuses("tab-ambiguous", "tab", "nav", f"{base}/ten")
        assert "name one with --tab" in err, err
        named = ok_json("tab", "nav", f"{base}/ten",
                        "--tab", f"id:{STATE['tab'][:8]}")
        assert named["tab"] == f"id:{STATE['tab']}", named
        urls = {t["id"]: t["url"] for t in pages(STATE["port"])}
        assert str(urls.get(STATE["tab"])).endswith("/ten"), urls
        assert urls.get(other) == "about:blank", urls
    finally:
        ok_json("tab", "close", f"id:{other[:8]}")
    return "two tabs refuse without --tab; --tab moves exactly the one named"


def c_dom_text_reads_the_page() -> str:
    """`tab text` is the rendered text, and the cap is applied IN the page."""
    ok_json("tab", "nav", f"{base_url()}/dom")
    full = ok_json("tab", "text")
    assert "Dom Fixture Heading" in full["text"], full["text"][:80]
    assert full["truncated"] is False, full
    assert full["length"] == len(full["text"]) > 0, full
    assert full["visibility"] in ("visible", "hidden"), full
    assert full["viewport"][0] > 0 and full["viewport"][1] > 0, full
    cut = ok_json("tab", "text", "--chars", "20")
    assert cut["truncated"] is True and len(cut["text"]) == 20, cut
    assert cut["length"] > 20, cut
    return f'{full["length"]} chars; a 20-char read still reports the full length'


def c_dom_find_resolves_targets() -> str:
    """`tab find` matches labels, pierces shadow roots, skips what is hidden."""
    found = ok_json("tab", "find", "Save the thing")
    assert found["total"] == 2, found          # the visible button + its twin
    assert len(found["matches"]) == 1, found   # display:none is not a target
    match = found["matches"][0]
    assert match["hit"] is True, match
    assert match["box"][2] > 0 and match["box"][3] > 0, match
    assert match["center"][0] >= 0 and match["center"][1] >= 0, match
    shadow = ok_json("tab", "find", "--selector", "#in-shadow")
    assert shadow["matches"][0]["text"].startswith("Shadow Action"), shadow
    refuses("no-match", "tab", "find", "--selector", "#hidden-one")
    refuses("no-match", "tab", "find", "Frame Button")   # iframe content
    return "1 of 2 candidates visible; shadow found; hidden and framed skipped"


def c_dom_js_is_declared_unverified() -> str:
    """`tab js` returns the page's value — capped, and not verified."""
    plain = ok_json("tab", "js", "document.title")
    assert plain["value"] == "dom fixture", plain
    assert plain["verified"] is False, plain
    decoded = ok_json("tab", "js", "JSON.stringify({a: 1, b: [2, 3]})")
    assert decoded["value"] == {"a": 1, "b": [2, 3]}, decoded
    err = refuses("js-error", "tab", "js", "throw new Error('boom')")
    assert "boom" in err, err
    refuses("result-too-large", "tab", "js", "'x'.repeat(100000)")
    return "a value, a decoded JSON value, js-error, result-too-large"


def c_dom_wait_polls() -> str:
    """`tab wait` polls one predicate to a deadline, then refuses."""
    ok_json("tab", "nav", f"{base_url()}/dom?late=2000")
    late = ok_json("tab", "wait", "--for", "element", "--selector", "#late",
                   "--timeout", "10")
    assert late["waited_s"] >= 1.0, late
    assert late["samples"] >= 2, late
    ok_json("tab", "wait", "--for", "load")
    ok_json("tab", "wait", "--for", "js", "--expr",
            "document.title.length > 0")
    err = refuses("wait-timeout", "tab", "wait", "--for", "element",
                  "--selector", "#never", "--timeout", "1")
    assert "1s" in err, err
    return (f'the late element took {late["waited_s"]}s over '
            f'{late["samples"]} samples')


def c_ambiguous_spec_refuses() -> str:
    first = str(ok_json("tab", f"{base_url()}/four-a")["id"])
    second = str(ok_json("tab", f"{base_url()}/four-b")["id"])
    err = refuses("tab-ambiguous", "tab", "close", "four")
    assert first[:6] in err or "four" in err, err
    left = {t["id"] for t in pages(STATE["port"])}
    assert first in left and second in left, \
        "the refusal must not have closed anything"
    # several specs in ONE call, both verified together
    reply = ok_json("tab", "close", f"id:{first[:8]}", f"id:{second[:8]}")
    assert {row["id"] for row in reply["closed"]} == {first, second}, reply
    left = {t["id"] for t in pages(STATE["port"])}
    assert first not in left and second not in left, left
    return "ambiguous refused with nothing closed; two specs closed in one call"


def c_refusals() -> str:
    refuses("no-page-tab", "tab", "close", "zzz-nothing")
    refuses("bad-args", "tab", "close")
    refuses("bad-args", "tab", "info")
    refuses("bad-args", "tab", "close", "id:")
    refuses("bad-args", "tab", "frobnicate")      # a bare word is not a URL
    refuses("bad-args", "open", "file:///etc/passwd")
    refuses("unknown-command", "frobnicate")
    return "seven refusals, each with its own code, exit 2"


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
    refuses("no-page-tab", "tab", "info", f"id:{STATE['tab'][:8]}")
    data = ok_json("tab", "list")
    assert all(g["pid"] != pid for g in data["browsers"]), data
    info = ok_json("info")
    assert info["running"] is False, info
    return f"pid {pid} gone, port {port} closed, tab info refuses"


def c_close_is_idempotent() -> str:
    reply = ok_json("close")
    assert reply["stopped"] is False, reply
    assert "no managed browser" in str(reply.get("reason", "")), reply
    return "a second close is a no-op, not a failure"


def c_no_leftover_process() -> str:
    left = procs_on(ROOT)
    assert not left, f"processes still running on the throwaway root: {left}"
    return "nothing of ours is left running"


def c_new_tab_several() -> str:
    """`tab a b c` — one call, three tabs, every id re-read."""
    base = base_url()
    urls = [f"{base}/six-a", f"{base}/six-b", f"{base}/six-c"]
    before = len(pages(STATE["port"]))
    reply = ok_json("tab", *urls)
    opened = [row["id"] for row in reply["opened"]]
    assert len(opened) == 3 and len(set(opened)) == 3, reply
    assert [row["requested"] for row in reply["opened"]] == urls, reply
    rows = pages(STATE["port"])
    assert len(rows) == before + 3, (before, rows)
    assert set(opened) <= {r["id"] for r in rows}, (opened, rows)
    assert reply["count"] == len(rows), reply
    ok_json("tab", "close", *[f"id:{i[:8]}" for i in opened])
    assert opened[0] not in {t["id"] for t in pages(STATE["port"])}
    return f"3 tabs from one call: {', '.join(i[:6] for i in opened)}"


def c_open_several() -> str:
    """`open a b c` — every site opens, and each is named by its own id."""
    base = base_url()
    urls = [f"{base}/seven-a", f"{base}/seven-b", f"{base}/seven-c"]
    before = len(pages(STATE["port"]))
    reply = ok_json("open", *urls)
    assert reply["started"] is False, reply           # the browser was up
    assert reply["pid"] == STATE["pid"], reply
    assert [row["requested"] for row in reply["opened"]] == urls, reply
    rows = pages(STATE["port"])
    assert len(rows) == before + 3, (before, rows)
    for url in urls:
        assert any(str(r["url"]).startswith(url) for r in rows), (url, rows)
    for row in reply["opened"]:                       # leave it as we found it
        ok_json("tab", "close", f'id:{row["id"]}')
    return "3 sites in one call, each named by its id"


def c_list_sees_our_browser() -> str:
    data = ok_json("list")
    ours = [b for b in data["browsers"] if b["pid"] == STATE["pid"]]
    assert len(ours) == 1, data
    row = ours[0]
    assert row["managed"] is True, row
    assert row["cdp"]["reachable"] is True, row
    assert row["cdp"]["port"] == STATE["port"], row
    assert row["profile"] == STATE["profile"], row
    assert row["cdp"]["tabs"] == len(pages(STATE["port"])), row
    assert data["drivable"] >= 1, data
    return (f'ours is managed + drivable on port {STATE["port"]} — '
            f'{data["count"]} browser(s) running here')


def c_list_tabs_groups_by_browser() -> str:
    data = ok_json("tab", "list")
    groups = [g for g in data["browsers"] if g["pid"] == STATE["pid"]]
    assert len(groups) == 1, data
    group = groups[0]
    assert group["managed"] is True and group["port"] == STATE["port"], group
    assert [t["id"] for t in group["tabs"]] == \
        [t["id"] for t in pages(STATE["port"])], group
    assert STATE["tab"] in {t["id"] for t in group["tabs"]}, group
    assert group["count"] == len(group["tabs"]), group
    return (f'{data["count"]} tab(s) across {len(data["browsers"])} '
            "drivable browser(s), grouped and id-matched")


def c_list_shows_a_browser_outside_cdp() -> str:
    """A browser with no debugging port is LISTED, and never driven.

    Launched here on its own throwaway profile: the point of the check is the
    distinction `list` makes — visible in /proc, no endpoint, so `list-tabs`
    and every other verb must leave it alone.
    """
    path = browser_lib.binary()
    profile = tempfile.mkdtemp(prefix="browser-control-plain-")
    proc = subprocess.Popen(
        [path, f"--user-data-dir={profile}", "--no-first-run",
         "--no-default-browser-check", "about:blank"],
        start_new_session=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    stopped = False
    try:
        deadline = time.time() + 30
        row = None
        while time.time() < deadline:
            row = next((b for b in ok_json("list")["browsers"]
                        if b["pid"] == proc.pid), None)
            if row:
                break
            time.sleep(0.5)
        assert row is not None, f"pid {proc.pid} never showed up in `list`"
        assert row["managed"] is False, row
        assert row["profile"] == profile, row
        assert row["cdp"] == {"port": 0, "reachable": False}, row
        assert all(g["pid"] != proc.pid
                   for g in ok_json("tab", "list")["browsers"]), row
    finally:
        with contextlib.suppress(OSError):
            os.kill(proc.pid, signal.SIGTERM)
        # reaps it: an unreaped child stays in /proc as a zombie
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        stopped = not browser_lib._pid_alive(proc.pid)  # noqa: SLF001
        for _ in range(3):          # a dying browser still writes to it
            shutil.rmtree(profile, ignore_errors=True)
            if not Path(profile).exists():
                break
            time.sleep(0.3)
    assert stopped, f"pid {proc.pid} survived SIGTERM"
    return f"pid {proc.pid} listed, cdp unreachable, absent from tab list"


def c_info_reports_the_endpoint() -> str:
    """`info` names the browser this CLI would drive, endpoint included."""
    data = ok_json("info")
    assert data["running"] is True, data
    assert data["browser"]["pid"] == STATE["pid"], data
    assert data["browser"]["managed"] is True, data
    assert data["cdp"]["reachable"] is True, data
    assert data["cdp"]["port"] == STATE["port"], data
    assert data["cdp"]["tabs"] == len(pages(STATE["port"])), data
    assert "Chrome" in data["cdp"]["version"], data
    assert data["cdp"]["protocol"], data
    return (f'info: {data["cdp"]["version"]}, protocol '
            f'{data["cdp"]["protocol"]}, {data["cdp"]["tabs"]} tabs')


def c_attach_grants_writes() -> str:
    """`attach` is what lets `tab` write to a browser this CLI did not start.

    A real foreign browser on its own profile: it is readable, a write
    refuses `not-managed`, `attach --port N` turns it writable (a new tab
    lands there and closes verified), the lifecycle verb still will not stop
    it, and `detach` puts the refusal back.
    """
    path = browser_lib.binary()
    profile = tempfile.mkdtemp(prefix="browser-control-foreign-")
    proc = subprocess.Popen(
        [path, f"--user-data-dir={profile}", "--remote-debugging-port=0",
         "--no-first-run", "--no-default-browser-check", "about:blank"],
        start_new_session=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    stopped = False
    try:
        port, deadline = 0, time.time() + 40
        while time.time() < deadline:
            try:
                with open(Path(profile, "DevToolsActivePort")) as handle:
                    port = int(handle.readline().strip() or 0)
            except (OSError, ValueError):
                port = 0
            if port and listening(port):
                break
            time.sleep(0.5)
        assert port, f"the foreign browser never published a port: {profile}"
        group = next((g for g in ok_json("tab", "list")["browsers"]
                      if g["pid"] == proc.pid), None)
        assert group is not None, "`tab list` does not show the foreign browser"
        assert group["managed"] is False and group["attached"] is False, group
        tid = str(group["tabs"][0]["id"])
        # readable, and refused for a write
        info = ok_json("tab", "info", f"id:{tid[:8]}")
        assert info["browser"]["pid"] == proc.pid, info
        refuses("not-managed", "tab", "close", f"id:{tid[:8]}")
        assert tid in {t["id"] for t in pages(port)}, \
            "the refusal closed a tab in someone else's browser"
        # `tab js` runs caller code, so it is a WRITE: refused too
        refuses("not-managed", "tab", "js", "document.title",
                "--tab", f"id:{tid[:8]}")
        # attach: the write lands
        reply = ok_json("attach", "--port", str(port))
        assert reply["attached"] is True and reply["already"] is False, reply
        assert reply["browser"]["pid"] == proc.pid, reply
        listed = next(b for b in ok_json("list")["browsers"]
                      if b["pid"] == proc.pid)
        assert listed["attached"] is True, listed
        attached = ok_json("attach", "--list")["attached"]
        assert [row["port"] for row in attached] == [port], attached
        made = str(ok_json("tab")["id"])
        assert made in {t["id"] for t in pages(port)}, \
            "the tab did not land in the attached browser"
        closed = ok_json("tab", "close", f"id:{made[:8]}")
        assert [row["id"] for row in closed["closed"]] == [made], closed
        # the lifecycle verb still refuses to stop a foreign browser
        stop_reply = ok_json("close")
        assert stop_reply["stopped"] is False, stop_reply
        assert Path(f"/proc/{proc.pid}").exists(), \
            "close stopped a browser this CLI did not start"
        # detach puts the refusal back
        gone = ok_json("detach", "--port", str(port))
        assert gone["detached"], gone
        refuses("not-managed", "tab", "close", f"id:{tid[:8]}")
        assert tid in {t["id"] for t in pages(port)}, "the refused close acted"
        assert ok_json("attach", "--list")["count"] == 0
    finally:
        run("detach", "--all", timeout=30)          # best effort
        with contextlib.suppress(OSError):
            os.kill(proc.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        stopped = not browser_lib._pid_alive(proc.pid)  # noqa: SLF001
        for _ in range(3):
            shutil.rmtree(profile, ignore_errors=True)
            if not Path(profile).exists():
                break
            time.sleep(0.3)
    assert stopped, f"pid {proc.pid} survived SIGTERM"
    return f"foreign pid {proc.pid}: read, refused, attached, written, detached"


CHECKS = (
    ("selftest answers without a browser", c_selftest),
    ("open starts a managed browser", c_open_starts_a_browser),
    ("list sees it, drivable", c_list_sees_our_browser),
    ("tab list reads the tab list back", c_tab_list_matches_the_tab_list),
    ("tab info names the browser", c_tab_info_names_the_browser),
    ("tab list groups by browser", c_list_tabs_groups_by_browser),
    ("tab names the tab it made", c_new_tab),
    ("tab with several URLs", c_new_tab_several),
    ("open with several URLs", c_open_several),
    ("tab close by id prefix", c_close_tab_by_id_prefix),
    ("tab close by substring", c_close_tab_by_substring),
    ("tab nav reads the address back", c_nav_reads_the_address_back),
    ("tab nav refuses a dead end", c_nav_refuses_a_dead_end),
    ("tab back/forward move the address", c_history_moves_the_address),
    ("tab reload makes a new document", c_reload_makes_a_new_document),
    ("tab nav names the tab", c_nav_names_the_tab),
    ("tab text reads the page", c_dom_text_reads_the_page),
    ("tab find resolves targets", c_dom_find_resolves_targets),
    ("tab js is declared unverified", c_dom_js_is_declared_unverified),
    ("tab wait polls", c_dom_wait_polls),
    ("an ambiguous spec refuses", c_ambiguous_spec_refuses),
    ("refusals carry their codes", c_refusals),
    ("info reports the endpoint", c_info_reports_the_endpoint),
    ("open adopts a running browser", c_open_adopts_the_running_browser),
    ("close stops the browser, verified", c_close_stops_the_browser),
    ("close again is a no-op", c_close_is_idempotent),
    ("a browser outside CDP is listed, not driven",
     c_list_shows_a_browser_outside_cdp),
    ("a foreign CDP browser is read, then attached for writes",
     c_attach_grants_writes),
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
    global ROOT
    ROOT = tempfile.mkdtemp(prefix="browser-control-live-")
    reason = prereq()
    try:
        if reason:
            for name, _fn in CHECKS:
                results.append((SKIP, name, reason))
                print(f"SKIP  {name}  {reason}")
        else:
            start_server()
            swept = sweep_stale_roots()
            print(f"root {ROOT}\npage {base_url()}\ncli  {CLI}\n")
            if swept:
                print(f"(swept {len(swept)} temp root(s) a killed run left: "
                      f"{', '.join(swept)})\n")
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
