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
audit: Any = import_module("browser_control.lib.audit")

CLI = os.environ.get("BROWSER_CONTROL_CLI") or str(REPO / "browser-control-cli")
# Created in `main()`, NOT here: this file is a module like any other, and a
# collector that imports it (pytest, an analyzer) must not create temp roots,
# start browsers or sweep anything. It did — that is where the orphan roots
# these tests kept finding came from.
ROOT = ""
# The run's own log, in its scratch directory (/tmp/browser-control-<ts>),
# created in `main()` for the same reason ROOT is.
LOG_DIR = ""
SUITE_LOG = ""
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
<style>
 #covered { position: absolute; left: 20px; top: 120px; }
 #cover { position: absolute; left: 0; top: 112px; width: 400px; height: 60px;
          background: rgba(255,0,0,.12); }
 #nested { width: 300px; height: 100px; overflow: auto; border: 1px solid #999; }
 #inner { height: 900px; }
 #tall { height: 2500px; }
</style>
<h1>Dom Fixture Heading</h1>
<button aria-label="Save the thing">save</button>
<button id="covered" aria-label="Covered Button">covered</button>
<div id="cover"></div>
<a href="/one.html">a link</a>
<input type="text" placeholder="search here">
<div role="button" tabindex="0">role button</div>
<button id="hidden-one" style="display:none">Save the thing</button>
<div id="form-zone" style="position: absolute; left: 0; top: 210px;">
<select id="pick" aria-label="Pick Colour">
  <option value="red">Red</option>
  <option value="green">Green</option>
  <option value="blue">Blue</option>
</select>
<input id="tick" type="checkbox" aria-label="Tick Box">
<input id="tick-off" type="checkbox" aria-label="Disabled Box" disabled>
<button id="hover-me" aria-label="Hover Target">hover target</button>
<button id="ask" aria-label="Ask Button"
        onclick="alert('battery alert')">ask</button>
<div id="events">events:</div>
</div>
<div id="host"></div>
<iframe src="/dom-frame" width="200" height="100"></iframe>
<p id="long-text">__FILLER__</p>
<div id="nested"><div id="inner">nested content</div></div>
<div id="tall"></div>
<button id="below" aria-label="Below Button">below</button>
<input id="field" type="text">
<form id="ins-form">
  <input id="ins" type="text" aria-label="Insert Target">
  <input id="pw" type="password" aria-label="Password Field">
  <button id="ins-go" type="submit">send</button>
</form>
<input id="upload" type="file" style="display:none">
<script>
  window.__keys = 0;
  document.addEventListener('keydown', () => { window.__keys += 1; });
  const note = (text) => {
    document.getElementById('events').textContent += ' ' + text;
  };
  document.getElementById('pick').addEventListener('change', (e) =>
    note('pick=' + e.target.value + ' trusted=' + e.isTrusted));
  document.getElementById('tick').addEventListener('change', (e) =>
    note('tick=' + e.target.checked + ' trusted=' + e.isTrusted));
  document.getElementById('ins-form')
    .addEventListener('submit', (e) => {
      e.preventDefault(); document.title = 'submitted';
    });
  const root = document.getElementById('host').attachShadow({mode: 'open'});
  root.innerHTML =
    '<button id="in-shadow" aria-label="Shadow Action">shadow</button>';
  document.querySelector('button[aria-label="Save the thing"]')
    .addEventListener('click', () => {
      document.title = 'clicked';
      document.getElementById('field').focus();
    });
  const delay = Number(__LATE__);
  setTimeout(() => { const el = document.createElement('button');
    el.id = 'late'; el.setAttribute('aria-label', 'Late Button');
    document.body.appendChild(el); }, delay);
</script>"""
DOM_FRAME = ("<!doctype html><title>frame</title>"
             "<button aria-label=\"Frame Button\">in frame</button>")
# A page with REAL playable media, made offline: a canvas is recorded to a
# blob and handed to a muted, looping <video> — so `media play` has something
# the browser will actually start without a gesture.
MEDIA_PAGE = """<!doctype html><meta charset="utf-8"><title>media fixture</title>
<style>video { width: 320px; height: 180px; background: #000; }</style>
<video id="v" muted loop></video>
<script>
  const canvas = document.createElement('canvas');
  canvas.width = 64; canvas.height = 64;
  const ctx = canvas.getContext('2d');
  const rec = new MediaRecorder(canvas.captureStream(10),
                                {mimeType: 'video/webm'});
  const chunks = [];
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  rec.onstop = () => {
    const v = document.getElementById('v');
    v.src = URL.createObjectURL(new Blob(chunks, {type: 'video/webm'}));
    v.load();
  };
  let t = 0;
  const draw = () => {
    ctx.fillStyle = (t % 2) ? '#123456' : '#654321';
    ctx.fillRect(0, 0, 64, 64);
    t += 1;
    if (t < 90) requestAnimationFrame(draw);
  };
  draw();
  rec.start();
  setTimeout(() => rec.stop(), 1200);
</script>"""
# A page whose <video> has NO source: play() is a promise the browser rejects,
# which is the refusal `media-blocked` exists for.
BARE_MEDIA_PAGE = ("<!doctype html><meta charset=\"utf-8\">"
                   "<title>bare media</title>"
                   "<style>video{width:320px;height:180px}</style>"
                   "<video id=\"v\"></video>")


def env() -> dict[str, str]:
    """The environment every CLI call gets: the throwaway root, and an action
    log in the run's scratch directory — so redaction can be asserted, the log
    is really exercised, and the user's real log is never touched."""
    return {**os.environ, "BROWSER_CONTROL_ROOT": ROOT,
            "BROWSER_CONTROL_LOG": SUITE_LOG or os.path.join(ROOT,
                                                            "actions.jsonl")}


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


def run(*argv: str, timeout: int = TIMEOUT_S,
        env_extra: dict | None = None) -> tuple[int, str, str]:
    """One CLI call on the throwaway root: (returncode, stdout, stderr).

    The command is executed AS a command, so its own shebang picks the
    interpreter — a checkout script and an installed console script in some
    other venv both work. `env_extra` overrides entries for THIS call only.
    """
    call_env = env()
    call_env.update(env_extra or {})
    proc = subprocess.run([CLI, *argv], capture_output=True, text=True,
                          timeout=timeout, env=call_env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def ok_json(*argv: str, env_extra: dict | None = None) -> dict:
    """A CLI call that must succeed, as parsed JSON."""
    rc, out, err = run(*argv, env_extra=env_extra)
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


def refuses_any(codes: tuple[str, ...], *argv: str) -> str:
    """A CLI call that must refuse with ONE of `codes`, and exit 2.

    For a property two honest codes can prove — "that tab is no longer
    addressable" arrives as `no-page-tab` or as `cdp-unreachable`, depending on
    whether anything else drivable is up.
    """
    rc, out, err = run(*argv)
    assert rc == 2, f"{' '.join(argv)} rc={rc} (wanted 2): {err or out}"
    assert any(f"ERR[{code}]" in err for code in codes), \
        f"{' '.join(argv)}: wanted one of {codes}, got {err!r}"
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
            if self.path.startswith("/media-bare"):
                self._send(BARE_MEDIA_PAGE.encode())
                return
            if self.path.startswith("/media"):
                self._send(MEDIA_PAGE.encode())
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


def c_dom_click_is_real_input() -> str:
    """`tab click` presses with CDP input, and refuses what it cannot reach."""
    ok_json("tab", "nav", f"{base_url()}/dom")
    ok_json("tab", "scroll", "--edge", "top")
    # the refusals come FIRST: the successful click below focuses a field at
    # the bottom of the page, and the browser scrolls it into view — which is
    # why the reply carries the scroll position, and why this order matters
    err = refuses("occluded", "tab", "click", "Covered Button")
    assert "cover" in err, err
    err = refuses("no-viewport-target", "tab", "click", "Below Button")
    assert "scroll" in err, err
    reply = ok_json("tab", "click", "Save the thing")
    assert reply["clicked"] is True, reply
    assert reply["element"]["hit"] is True, reply
    assert reply["changed"] is True, reply
    assert reply["after"]["title"] == "clicked", reply
    return "occluded and off-screen refused; a trusted press landed"


def c_dom_scroll_moves_the_document_and_nested() -> str:
    """Wheels are real input: they reach nested scrollers and the document."""
    ok_json("tab", "nav", f"{base_url()}/dom")
    bottom = ok_json("tab", "scroll", "--edge", "bottom")
    assert bottom["moved"] is True, bottom
    assert abs(bottom["document"]["after"]
               - bottom["document"]["max"]) <= 2, bottom
    top = ok_json("tab", "scroll", "--edge", "top")
    assert top["document"]["after"] == 0, top
    by = ok_json("tab", "scroll", "--by", "600")
    assert by["document"]["after"] >= 500, by
    ok_json("tab", "scroll", "--edge", "top")
    point = ok_json("tab", "find", "--selector", "#nested")["matches"][0]["point"]
    nested = ok_json("tab", "scroll", "--by", "240", "--at",
                     f"{point[0]},{point[1]}")
    assert nested["nested"]["after"][1] == 240, nested
    assert nested["document"]["after"] == 0, nested     # the page did not move
    return "a wheel scrolled the document, and one scrolled a nested div"


def c_dom_reveal_then_click() -> str:
    """`tab scroll TEXT` reveals via a CDP method, then the element is clickable."""
    ok_json("tab", "scroll", "--edge", "top")
    revealed = ok_json("tab", "scroll", "Below Button")
    assert revealed["revealed"] is True, revealed
    assert revealed["element"]["in_viewport"] is True, revealed
    clicked = ok_json("tab", "click", "Below Button")
    assert clicked["clicked"] is True, clicked
    assert clicked["element"]["hit"] is True, clicked
    return "an off-screen button was revealed by CDP and then clicked"


def c_dom_focus_insert_and_type() -> str:
    """`tab focus` → `tab insert` → `tab type`, each with its read-back."""
    ok_json("tab", "nav", f"{base_url()}/dom")
    focused = ok_json("tab", "focus", "Insert Target")
    assert focused["focused"] is True, focused
    inserted = ok_json("tab", "insert", "hello")
    assert inserted["verified"] is True, inserted
    assert inserted["length_after"] - inserted["length_before"] == 5, inserted
    value = ok_json("tab", "js", "document.querySelector('#ins').value")
    assert value["value"] == "hello", value
    typed = ok_json("tab", "type", " xy")
    assert typed["verified"] is True, typed
    keys = int(ok_json("tab", "js", "String(window.__keys)")["value"])
    assert keys >= 3, keys          # one keydown per character, and Enter before
    return (f"insert {inserted['length_before']}→{inserted['length_after']}, "
            f"type → {keys} keydowns")


def c_dom_press_reaches_the_page() -> str:
    """`tab press` dispatches a real key; the effect is the page's to show."""
    ok_json("tab", "focus", "Insert Target")
    pressed = ok_json("tab", "press", "enter")
    assert pressed["key"] == "Enter", pressed
    assert pressed["verified"] is False, pressed      # declared unverified
    title = ok_json("tab", "js", "document.title")["value"]
    assert title == "submitted", title               # the form's handler ran
    err = refuses("bad-args", "tab", "press", "nope")
    assert "unknown key" in err, err
    refuses("focus-not-verified", "tab", "focus", "Dom Fixture Heading")
    return "Enter reached the form handler; an unknown key and an unfocusable heading refused"


def c_dom_upload_attaches_a_file() -> str:
    """`tab upload` fills the one control JavaScript cannot: a HIDDEN input."""
    path = os.path.join(tempfile.gettempdir(), "browser-control-upload.txt")
    Path(path).write_text("uploaded by the battery\n", encoding="utf-8")
    size = os.path.getsize(path)
    try:
        reply = ok_json("tab", "upload", path, "--selector", "#upload")
        files = reply["input"]["files"]
        assert [f["name"] for f in files] == [os.path.basename(path)], reply
        assert files[0]["size"] == size, reply
        refuses("no-file", "tab", "upload", "/nope/missing.txt")
        refuses("bad-args", "tab", "upload", "relative.txt")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    return f"the hidden input holds {os.path.basename(path)} ({size} bytes)"


def c_dom_password_never_reaches_the_log() -> str:
    """A proven secret is written as a length: `redacted: true`, no text."""
    secret = f"battery-{os.getpid()}-not-in-the-log"
    ok_json("tab", "focus", "Password Field")
    inserted = ok_json("tab", "insert", secret)
    assert inserted["verified"] is True, inserted
    log = Path(SUITE_LOG)
    assert log.exists(), "the battery's own action log was not written"
    text = log.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    marked = [row for row in rows
              if row.get("redacted") and row["args"][:1] == ["insert"]]
    assert marked, rows[-3:]
    assert secret not in json.dumps(marked[-1]), marked[-1]
    assert marked[-1]["args"][1].startswith("<redacted:"), marked[-1]
    assert secret not in text, "the secret reached the log"
    return f"the password insert logged {marked[-1]['args'][1]}"


def c_dom_text_needs_a_focus() -> str:
    """Typing with nothing focused refuses instead of doing nothing quietly."""
    ok_json("tab", "nav", "about:blank")
    err = refuses("no-focus", "tab", "insert", "x")
    assert "tab focus" in err, err
    err = refuses("no-focus", "tab", "type", "x")
    assert "nothing is focused" in err, err
    return "insert and type refused with no focus, naming the fix"


def c_dom_media_state_play_pause() -> str:
    """`tab media` reads, starts and stops the page's media — verified."""
    ok_json("tab", "nav", f"{base_url()}/media")
    ok_json("tab", "wait", "--for", "js", "--expr",
            "document.querySelector('#v').readyState >= 2", "--timeout", "20")
    idle = ok_json("tab", "media", "state")
    assert idle["paused"] is True and idle["playing"] is False, idle
    assert idle["element"] == "video" and idle["count"] == 1, idle
    played = ok_json("tab", "media", "play")
    assert played["playing"] is True and played["paused"] is False, played
    time.sleep(0.6)
    running = ok_json("tab", "media", "state")
    assert running["playing"] is True, running
    assert running["time"] > played["time"], (played["time"], running["time"])
    paused = ok_json("tab", "media", "pause")
    assert paused["paused"] is True and paused["playing"] is False, paused
    return f"play → the clock reached {running['time']}s; pause → paused"


def c_dom_media_refuses_what_it_cannot_do() -> str:
    """A play that cannot work refuses with a reason, never as `ok`.

    A `<video>` with NO source is refused EITHER by the page (the promise
    rejects with the autoplay policy) OR by the verdict (it reports `paused:
    false` and never plays a frame) — which one depends on whether the tab has
    seen a real gesture yet, so both are honest and this accepts both.
    """
    ok_json("tab", "nav", f"{base_url()}/media-bare")
    rc, out, err = run("tab", "media", "play")
    assert rc == 2, (rc, out, err)
    assert "ERR[media-blocked]" in err or "ERR[media-not-verified]" in err, err
    assert "nothing to play" in err or "refused" in err, err
    ok_json("tab", "nav", f"{base_url()}/dom")          # no media at all
    refuses("no-media", "tab", "media", "state")
    refuses("bad-args", "tab", "media", "stop")
    return "a source-less video and a media-less page refused, each named"


def c_tab_activate() -> str:
    """`tab activate` is verified by the page's own visibility, not by a call."""
    base, fixture = base_url(), str(STATE["tab"])
    ok_json("tab", "nav", f"{base}/dom")
    other = str(ok_json("tab", "about:blank")["id"])
    try:
        # the tab just created is the active one, so the fixture is background
        first = ok_json("tab", "activate", f"id:{fixture[:8]}")
        assert first["visibility_before"] == "hidden", first
        assert first["visibility"] == "visible", first
        assert first["changed"] is True and first["verified"] is True, first
        again = ok_json("tab", "activate", f"id:{fixture[:8]}")
        assert again["changed"] is False, again
        # `active` is a reserved spec, and it means OURS (the user's own
        # browser has a visible tab too, and it must not be a candidate)
        active = ok_json("tab", "info", "active")
        assert active["tab"]["id"] == fixture, active
    finally:
        ok_json("tab", "close", f"id:{other[:8]}")
    return "hidden -> visible, read back from the page; `active` resolves to ours"


def c_tab_hover() -> str:
    """`tab hover` is proven by the engine's own `:hover` state."""
    ok_json("tab", "nav", f"{base_url()}/dom")
    reply = ok_json("tab", "hover", "--selector", "#hover-me")
    assert reply["hovered"] is True and reply["verified"] is True, reply
    assert reply["point"][0] > 0 and reply["point"][1] > 0, reply
    refuses("no-match", "tab", "hover", "--selector", "#no-such-thing")
    refuses("occluded", "tab", "hover", "Covered Button")
    return "the element matches `:hover` at that point; hidden and covered refuse"


def c_tab_check() -> str:
    """`tab check` clicks for real, reads back, and never double-clicks."""
    ok_json("tab", "nav", f"{base_url()}/dom")
    on = ok_json("tab", "check", "--selector", "#tick")
    assert on["checked"] is True and on["changed"] is True, on
    assert on["verified"] is True, on
    again = ok_json("tab", "check", "--selector", "#tick")
    assert again["changed"] is False, again          # a click would UNtick it
    off = ok_json("tab", "check", "--selector", "#tick", "--uncheck")
    assert off["checked"] is False and off["changed"] is True, off
    ok_json("tab", "check", "--selector", "#tick")
    refuses("not-checkable", "tab", "check", "--selector", "#hover-me")
    refuses("not-checkable", "tab", "check", "--selector", "#tick-off")
    log = ok_json("tab", "text", "--selector", "#events")
    assert "tick=true trusted=true" in log["text"], log["text"]
    assert "tick=false trusted=true" in log["text"], log["text"]
    return "checked and unchecked with events the page sees as trusted"


def c_tab_select() -> str:
    """`tab select` chooses with REAL arrow keys, by value or by label."""
    ok_json("tab", "nav", f"{base_url()}/dom")
    by_value = ok_json("tab", "select", "--selector", "#pick",
                       "--value", "green")
    assert by_value["value"] == "green", by_value
    assert by_value["by"] == "value" and by_value["keys"] == 1, by_value
    assert by_value["trusted"] is True, by_value
    by_label = ok_json("tab", "select", "--selector", "#pick",
                       "--value", "Blue")
    assert by_label["value"] == "blue" and by_label["by"] == "label", by_label
    assert by_label["keys"] == 1, by_label       # green -> blue is one step
    same = ok_json("tab", "select", "--selector", "#pick", "--value", "blue")
    assert same["changed"] is False and same["keys"] == 0, same
    refuses("no-match", "tab", "select", "--selector", "#pick",
            "--value", "mauve")
    refuses("not-a-select", "tab", "select", "--selector", "#tick",
            "--value", "green")
    log = ok_json("tab", "text", "--selector", "#events")
    assert "pick=green trusted=true" in log["text"], log["text"]
    assert "pick=blue trusted=true" in log["text"], log["text"]
    return "value then label, and the page's change handler sees isTrusted"


def c_tab_screenshot() -> str:
    """`tab screenshot` writes a PNG whose own header matches the page."""
    ok_json("tab", "nav", f"{base_url()}/dom")
    path = os.path.join(ROOT, "shot.png")
    reply = ok_json("tab", "screenshot", path)
    assert reply["verified"] is True and reply["bytes"] > 1000, reply
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", data[:8]
    assert len(data) == reply["bytes"], (len(data), reply["bytes"])
    assert [int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big")] == [reply["width"],
                                                    reply["height"]], reply
    refuses("file-exists", "tab", "screenshot", path)
    forced = ok_json("tab", "screenshot", path, "--force")
    assert forced["bytes"] == reply["bytes"], forced
    full = ok_json("tab", "screenshot", os.path.join(ROOT, "shot-full.png"),
                   "--full")
    assert full["full"] is True and full["height"] >= reply["height"], full
    refuses("bad-args", "tab", "screenshot", os.path.join(ROOT, "shot.jpg"))
    refuses("bad-args", "tab", "screenshot", os.path.join(ROOT, "no", "x.png"))
    return f'{reply["width"]}x{reply["height"]} on disk; --full is taller'


def c_tab_dialog() -> str:
    """A dialog this CLI causes is NAMED, and a parked tab is recoverable.

    Measured, and the reason the verb exists: Chrome suppresses a dialog no
    client was attached to answer, which parks the renderer for good — so
    `accept` has to say so for certain (its refusal IS the answer) and the way
    out is a browser-side `tab nav`, which replaces the document.
    """
    base = base_url()
    ok_json("tab", "nav", f"{base}/dom")
    quiet = ok_json("tab", "dialog")
    assert quiet["open"] is False and quiet["verified"] is True, quiet
    refuses("no-dialog", "tab", "dialog", "dismiss")
    started = time.time()
    err = refuses("blocked", "tab", "click", "--selector", "#ask")
    took = time.time() - started
    assert "battery alert" in err, err        # the dialog is named, not guessed
    assert took < 8, took                     # the grace, not a 15s write-off
    parked = ok_json("tab", "dialog")
    assert parked["open"] is None and parked["verified"] is False, parked
    assert parked["blocked"] is True, parked
    refuses("no-dialog", "tab", "dialog", "accept")   # suppressed: nothing left
    refuses("blocked", "tab", "text")                 # and every read says so
    moved = ok_json("tab", "nav", f"{base}/dom")      # the way out
    assert moved["moved"] is True, moved
    back = ok_json("tab", "text", "--chars", "40")
    assert back["length"] > 0, back
    healthy = ok_json("tab", "dialog")
    assert healthy["open"] is False, healthy
    return (f"a click's alert is named in {took:.1f}s; `tab nav` recovers the "
            "parked renderer")


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
    # the tab id is no longer addressable: `no-page-tab` when some other
    # drivable browser is up, `cdp-unreachable` when this closed browser was
    # the only one — both prove the tab is gone, and which one arrives depends
    # on what else is running on the machine (measured)
    err = refuses_any(("no-page-tab", "cdp-unreachable"),
                      "tab", "info", f"id:{STATE['tab'][:8]}")
    data = ok_json("tab", "list")
    assert all(g["pid"] != pid for g in data["browsers"]), data
    info = ok_json("info")
    assert info["running"] is False, info
    return f"pid {pid} gone, port {port} closed, tab info refuses ({err.split(']')[0]}])"


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
    ("tab click is real input", c_dom_click_is_real_input),
    ("tab scroll wheels the page and nested scrollers",
     c_dom_scroll_moves_the_document_and_nested),
    ("tab scroll reveals, then click lands", c_dom_reveal_then_click),
    ("tab focus, insert, type", c_dom_focus_insert_and_type),
    ("tab press reaches the page", c_dom_press_reaches_the_page),
    ("tab upload fills a hidden input", c_dom_upload_attaches_a_file),
    ("a password never reaches the log", c_dom_password_never_reaches_the_log),
    ("typing needs a focus", c_dom_text_needs_a_focus),
    ("tab media reads, plays and pauses", c_dom_media_state_play_pause),
    ("tab media refuses what it cannot do",
     c_dom_media_refuses_what_it_cannot_do),
    ("tab activate makes a background tab visible", c_tab_activate),
    ("tab hover proves `:hover`", c_tab_hover),
    ("tab check clicks and reads back", c_tab_check),
    ("tab select uses real arrow keys", c_tab_select),
    ("tab screenshot writes a verified PNG", c_tab_screenshot),
    ("tab dialog names the dialog and recovery works", c_tab_dialog),
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
    global ROOT, LOG_DIR, SUITE_LOG
    ROOT = tempfile.mkdtemp(prefix="browser-control-live-")
    LOG_DIR = audit.scratch_dir() or ROOT
    SUITE_LOG = os.path.join(LOG_DIR, "live-actions.jsonl")
    reason = prereq()
    try:
        if reason:
            for name, _fn in CHECKS:
                results.append((SKIP, name, reason))
                print(f"SKIP  {name}  {reason}")
        else:
            start_server()
            swept = sweep_stale_roots()
            print(f"root {ROOT}\nlog  {SUITE_LOG}\npage {base_url()}\ncli  "
                  f"{CLI}\n")
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
