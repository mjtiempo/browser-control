"""Hermetic checks for the browser-control slice — no browser needed.

Run:  python3 tests/test_unit.py

What is covered here is what can be wrong without a browser: the URL policy,
the tab-spec resolution, the launch flags (a never-run profile only becomes
drivable WITH --no-first-run), the CLI's argv strictness and dispatch, and the
CDP HTTP read path against a fake endpoint.
"""
from __future__ import annotations

import contextlib
import glob
import http.server
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The sibling modules are not resolvable before the path insert above; the
# project-level pyright run resolves them, so only `E402` is suppressed here.
from browser_control.cli import main as cli_main  # noqa: E402
from browser_control.lib import (  # noqa: E402
    audit,
    browser,
    capabilities,
    cdp,
    dom,
)
from browser_control.lib import (
    profile as profile_lib,
)
from browser_control.lib.errors import ControlError  # noqa: E402

# Importing this module must be INERT: it used to mkdtemp a /tmp directory and
# `setdefault` the log, so a host that exports BROWSER_CONTROL_LOG had every
# in-process call append to the sheet's real log (the battery's own lesson,
# applied late — a review flagged it). `main()` pins the log, the root and the
# policy, unconditionally.
SUITE_LOG = "/dev/null"          # replaced in main()

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as e:                                     # noqa: BLE001
        FAIL.append(name)
        print(f"FAIL  {name}  {type(e).__name__}: {e}")
    else:
        PASS.append(name)
        print(f"PASS  {name}")


def refusal(fn: Callable[[], object], code: str) -> None:
    """The call must refuse with `code`, and nothing else."""
    try:
        fn()
    except ControlError as e:
        assert e.code == code, f"code {e.code!r} != {code!r}"
        return
    raise AssertionError(f"expected ERR[{code}], nothing was raised")


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli_main.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------- checks
def t_safe_url() -> None:
    for ok in ("https://example.com/x?a=1&b=2", "http://localhost:8000/",
               "about:blank"):
        assert browser.safe_url(ok) == ok, ok
    for bad in ("file:///etc/passwd", "data:text/html,<b>x</b>",
                "javascript:alert(1)", "ftp://example.com", "example.com",
                "https://example.com/a b"):
        refusal(lambda bad=bad: browser.safe_url(bad), "bad-args")


def t_resolve_tab() -> None:
    rows = [
        {"id": "AAAA1111", "title": "Inbox", "url": "https://mail.example.com/"},
        {"id": "BBBB2222", "title": "Docs", "url": "https://docs.example.com/"},
        {"id": "BBBB3333", "title": "Docs (old)",
         "url": "https://docs.example.com/v1"},
    ]
    assert browser.resolve_tab(rows, "id:AAAA")["id"] == "AAAA1111"
    assert browser.resolve_tab(rows, "id:aaaa1111")["id"] == "AAAA1111"
    assert browser.resolve_tab(rows, "mail.example")["id"] == "AAAA1111"
    assert browser.resolve_tab(rows, "Inbox")["id"] == "AAAA1111"
    refusal(lambda: browser.resolve_tab(rows, "id:FFFF"), "no-page-tab")
    refusal(lambda: browser.resolve_tab(rows, "id:"), "bad-args")
    refusal(lambda: browser.resolve_tab(rows, "nothing here"), "no-page-tab")
    # two tabs match a substring: never a silent pick
    refusal(lambda: browser.resolve_tab(rows, "docs.example"), "tab-ambiguous")
    refusal(lambda: browser.resolve_tab(rows, "  "), "bad-args")


def t_launch_flags() -> None:
    profile = os.path.join(tempfile.gettempdir(), "browser-control-profile")
    flags = browser.flags(profile)
    assert f"--user-data-dir={profile}" in flags, flags
    assert "--remote-debugging-port=0" in flags, flags
    # load-bearing: a never-run profile publishes no CDP port without these
    assert "--no-first-run" in flags, flags
    assert "--no-default-browser-check" in flags, flags


def t_profile_keyed_by_binary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        keep_root = os.environ.get("BROWSER_CONTROL_ROOT")
        os.environ["BROWSER_CONTROL_ROOT"] = tmp
        try:
            path = browser.profile_dir("/usr/bin/google-chrome-stable")
            assert path == os.path.join(tmp, "google-chrome-stable"), path
            assert browser.profile_dir("brave-browser") == \
                os.path.join(tmp, "brave-browser")
            assert browser.profiles() == [], browser.profiles()
            assert browser.live_profiles() == [], browser.live_profiles()
        finally:
            _restore_root(keep_root)


def t_port_file() -> None:
    with tempfile.TemporaryDirectory() as profile:
        assert cdp.port_of(profile) == 0
        Path(profile, cdp.PORT_FILE).write_text("0\n/devtools/browser/x\n")
        assert cdp.port_of(profile) == 0, "port 0 is 'no answer'"
        Path(profile, cdp.PORT_FILE).write_text("nonsense\n")
        assert cdp.port_of(profile) == 0
        Path(profile, cdp.PORT_FILE).write_text("70000\n")
        assert cdp.port_of(profile) == 0
        Path(profile, cdp.PORT_FILE).write_text("34591\n/devtools/browser/x\n")
        assert cdp.port_of(profile) == 34591


def _fake_endpoint(tabs: list[object]) -> tuple[http.server.HTTPServer, int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            # a real endpoint answers an OBJECT for /json/version and the tab
            # list for /json, so the fake does too
            body = {"Browser": "Fake/1.0"} if self.path == "/json/version" \
                else tabs
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


def t_page_rows_from_a_fake_endpoint() -> None:
    tabs: list[object] = [
        {"type": "page", "id": "B", "title": "second", "url": "https://b/"},
        {"type": "page", "id": "A", "title": "first", "url": "https://a/"},
        {"type": "service_worker", "id": "S", "title": "sw", "url": "sw.js"},
        "not-an-object",
    ]
    server, port = _fake_endpoint(tabs)
    try:
        with tempfile.TemporaryDirectory() as profile:
            Path(profile, cdp.PORT_FILE).write_text(f"{port}\n")
            rows = cdp.rows_to_tabs(cdp.page_rows(profile))
            assert [r["id"] for r in rows] == ["A", "B"], rows
            assert rows[0] == {"id": "A", "title": "first", "url": "https://a/"}
            assert cdp.reachable(profile) is True
    finally:
        server.shutdown()
        server.server_close()


def t_cli_new_verbs_grammar() -> None:
    """`tab activate|hover|check|select|dialog|screenshot` argv — as argv."""
    calls: list[tuple] = []
    # argv-only paths: `dom.screenshot` is faked below, so nothing is written
    shots = os.path.join(tempfile.gettempdir(), "browser-control-argv")
    shot_a, shot_b, shot_c = (f"{shots}/a.png", f"{shots}/b.png",
                              f"{shots}/c.png")

    def fake_activate(tab: str = "", browser: str = "") -> dict:
        calls.append(("activate", tab, browser))
        return {"ok": True}

    def fake_hover(text: str | None = None, selector: str | None = None,
                   index: int | None = None, tab: str = "",
                   browser: str = "") -> dict:
        calls.append(("hover", text, selector, index, tab, browser))
        return {"ok": True}

    def fake_check(text: str | None = None, selector: str | None = None,
                   index: int | None = None, uncheck: bool = False,
                   tab: str = "", browser: str = "") -> dict:
        calls.append(("check", text, selector, index, uncheck, tab, browser))
        return {"ok": True}

    def fake_select(text: str | None = None, selector: str | None = None,
                    value: str = "", index: int | None = None, tab: str = "",
                    browser: str = "") -> dict:
        calls.append(("select", text, selector, value, index, tab, browser))
        return {"ok": True}

    def fake_dialog(mode: str = "state", text: str | None = None,
                    tab: str = "", browser: str = "") -> dict:
        calls.append(("dialog", mode, text, tab, browser))
        return {"ok": True}

    def fake_shot(path: str, full: bool = False, force: bool = False,
                  tab: str = "", browser: str = "") -> dict:
        calls.append(("screenshot", path, full, force, tab, browser))
        return {"ok": True}

    originals = (cli_main.activate, dom.hover, dom.check, dom.select,
                 dom.dialog, dom.screenshot)
    (cli_main.activate, dom.hover, dom.check, dom.select, dom.dialog,
     dom.screenshot) = (fake_activate, fake_hover, fake_check, fake_select,
                        fake_dialog, fake_shot)   # type: ignore[assignment]
    try:
        for argv in (["tab", "activate"],
                     ["tab", "activate", "id:AB"],
                     ["tab", "hover", "Save"],
                     ["tab", "hover", "--selector", ".x", "--index", "1"],
                     ["tab", "check", "Tick"],
                     ["tab", "check", "Tick", "--uncheck",
                      "--tab", "id:AB"],
                     ["tab", "select", "--selector", "#pick",
                      "--value", "green"],
                     ["tab", "select", "Colour", "--value", "Blue",
                      "--index", "0"],
                     ["tab", "dialog"],
                     ["tab", "dialog", "accept"],
                     ["tab", "dialog", "dismiss", "--tab", "id:AB"],
                     ["tab", "dialog", "accept", "--text", "yes"],
                     ["tab", "screenshot", shot_a],
                     ["tab", "screenshot", "--path", shot_b,
                      "--full"],
                     ["tab", "screenshot", shot_c, "--force",
                      "--full"]):
            rc, _out, err = run_cli(argv)
            assert rc == 0, (argv, rc, err)
        assert calls == [
            ("activate", "", ""),
            ("activate", "id:AB", ""),
            ("hover", "Save", None, None, "", ""),
            ("hover", None, ".x", 1, "", ""),
            ("check", "Tick", None, None, False, "", ""),
            ("check", "Tick", None, None, True, "id:AB", ""),
            ("select", None, "#pick", "green", None, "", ""),
            ("select", "Colour", None, "Blue", 0, "", ""),
            ("dialog", "state", None, "", ""),
            ("dialog", "accept", None, "", ""),
            ("dialog", "dismiss", None, "id:AB", ""),
            ("dialog", "accept", "yes", "", ""),
            ("screenshot", shot_a, False, False, "", ""),
            ("screenshot", shot_b, True, False, "", ""),
            ("screenshot", shot_c, True, True, "", ""),
        ], calls
    finally:
        (cli_main.activate, dom.hover, dom.check, dom.select, dom.dialog,
         dom.screenshot) = originals               # type: ignore[assignment]
    # these refuse in argv, before any browser is involved
    for argv in (["tab", "activate", "a", "b"],
                 ["tab", "hover"],
                 ["tab", "hover", "a", "--selector", "b"],
                 ["tab", "hover", "a", "--index", "x"],
                 ["tab", "check"],
                 ["tab", "check", "a", "b"],
                 ["tab", "select", "--selector", "#pick"],
                 ["tab", "select", "--value", "x"],
                 ["tab", "select", "a", "--value", "x", "--index", "y"],
                 ["tab", "dialog", "dismiss", "--text", "no"],
                 ["tab", "screenshot"],
                 ["tab", "screenshot", "a.png", "--path", "b.png"]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)


def t_expressions_and_shot_rules() -> None:
    """The page expressions are wrapped and filled; the PNG rules refuse.

    A missing bracket in a JavaScript string is INVISIBLE to Python — one
    shipped as `js-error: SyntaxError` from the page (measured) — and the wrap
    is what was missing, so the shape is checked here: every expression is
    `JSON.stringify((() => {…})())`, and every placeholder it declares is one
    the caller actually fills.
    """
    placeholders = {"__MODE__": '"text"', "__NEEDLE__": '"x"',
                    "__SELECTOR__": '"#x"', "__INDEX__": "0",
                    "__VISIBLE__": "true", "__CAP__": "1",
                    "__VALUE__": '"v"', "__X__": "1", "__Y__": "1",
                    "__EXPR__": "true", "__IDLE_MS__": "100",
                    "__SCHEMA__": '{"each":"a"}'}
    seen = 0
    for name in dir(dom):
        src = getattr(dom, name)
        if not name.isupper() or not isinstance(src, str) \
                or not src.startswith("JSON.stringify"):
            continue
        seen += 1
        # the FIRST alternative used to subsume the second (`"})()"` ends
        # `"})())"`), so a wrap that dropped the last bracket passed the very
        # check written to catch it (a review found it)
        assert src.rstrip().endswith("})())"), (name, src[-12:])
        filled = src
        for key, value in placeholders.items():
            filled = filled.replace(key, value)
        left = [word for word in filled.replace("\n", " ").split()
                if word.startswith("__")]
        assert not left, (name, left[:3])
    assert seen, "no expressions were checked at all"
    # WAIT_EXPRS are not module-level strings, so the loop above never visited
    # them — including the one that carries the whole PRELUDE (a review flagged
    # the hole)
    for mode, expression in dom.WAIT_EXPRS.items():
        if mode == "js":
            continue                 # it interpolates caller code by design
        filled = expression
        for key, value in placeholders.items():
            filled = filled.replace(key, value)
        left = [word for word in filled.replace("\n", " ").split()
                if word.startswith("__")]
        assert not left, (mode, left[:3])
    assert seen >= 10, seen
    # the reserved spec never reaches the substring matcher
    rows = [{"pid": 1, "profile": "/p", "managed": True}]
    assert browser._spec_hits(rows, {1: []}, "active") == []   # noqa: SLF001
    # a PNG is judged by its bytes, and a device size by sane arithmetic
    assert dom._png_size(b"") == []                            # noqa: SLF001
    assert dom._png_size(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20) == []
    header = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
              + (1882).to_bytes(4, "big") + (842).to_bytes(4, "big"))
    assert dom._png_size(header) == [1882, 842]                # noqa: SLF001
    assert dom._pixels(941, 2) == 1882                          # noqa: SLF001
    for css, ratio in ((941, float("inf")), (941, float("nan")),
                       (10 ** 9, 2.0), (10, 0.0), (10, -1.0)):
        refusal(lambda css=css, ratio=ratio: dom._pixels(css, ratio),
                "screenshot-not-verified")
    # the screenshot path rules are pure: no browser, no file
    with tempfile.TemporaryDirectory() as tmp:
        refusal(lambda: dom._shot_target(f"{tmp}/x.jpg"),             # noqa: SLF001
                "bad-args")
        refusal(lambda: dom._shot_target(f"{tmp}/nope/x.png"),        # noqa: SLF001
                "bad-args")
        refusal(lambda: dom._shot_target(tmp),                         # noqa: SLF001
                "bad-args")
        assert dom._shot_target(f"{tmp}/ok.png") == f"{tmp}/ok.png"   # noqa: SLF001
    # a dialog MODE is checked before any tab is resolved, and the browser's
    # own words are what make accept/dismiss definitive
    refusal(lambda: dom.dialog("maybe"), "bad-args")
    assert dom._no_dialog(ControlError(                              # noqa: SLF001
        "cdp-error", "Page.handleJavaScriptDialog: No dialog is showing "
        "(code -32602)"))
    assert not dom._no_dialog(ControlError("cdp-error", "something else"))  # noqa: SLF001
    # a control that cannot be checked refuses, naming what it is
    refusal(lambda: dom._checkable({"checkable": False, "tag": "button"},  # noqa: SLF001
                                   {}), "not-checkable")
    refusal(lambda: dom._checkable({"checkable": True, "disabled": True},  # noqa: SLF001
                                   {}), "not-checkable")
    dom._checkable({"checkable": True, "disabled": False}, {})      # noqa: SLF001


def t_cli_dispatch() -> None:
    """One URL, several URLs, and the flags that are stripped — as argv."""
    seen: dict[str, object] = {}

    def fake_launch(urls: list[str] | None = None, browser: str = "") -> dict:
        seen["urls"] = list(urls or [])
        seen["browser"] = browser
        return {"ok": True, "opened": list(urls or [])}

    original = cli_main.launch
    cli_main.launch = fake_launch          # type: ignore[assignment]
    try:
        rc, out, err = run_cli(["open", "https://a.example",
                                "https://b.example",
                                "--browser", "brave-browser"])
        assert rc == 0, (rc, err)
        assert err == "", err
        assert json.loads(out)["opened"] == ["https://a.example",
                                              "https://b.example"]
        assert seen == {"urls": ["https://a.example", "https://b.example"],
                        "browser": "brave-browser"}, seen
        rc, out, err = run_cli(["open"])          # no URL is allowed
        assert rc == 0 and json.loads(out)["opened"] == [], (rc, out, err)
    finally:
        cli_main.launch = original         # type: ignore[assignment]


def t_cli_tab_grammar() -> None:
    """`tab [URL...]`, `tab list`, `tab info SPEC`, `tab close SPEC...`.

    The subcommand is a reserved first word, so every call here must land in
    exactly one service — with the arguments it was given, none dropped.
    """
    calls: list[tuple] = []

    def fake_new_tab(urls: list[str] | None = None,
                     browser: str = "") -> dict:
        calls.append(("tab", tuple(urls or ()), browser))
        return {"ok": True, "opened": []}

    def fake_list_tabs(browser: str = "") -> dict:
        calls.append(("tab list", browser))
        return {"ok": True, "count": 0, "browsers": []}

    def fake_tab_info(spec: str, browser: str = "") -> dict:
        calls.append(("tab info", spec, browser))
        return {"ok": True, "tab": {"id": spec}}

    def fake_close_tabs(specs: list[str], browser: str = "",
                        title: str | None = None,
                        url: str | None = None,
                        all_tabs: bool = False,
                        excepts: list[str] | None = None,
                        like: list[str] | None = None,
                        dry: bool = False) -> dict:
        calls.append(("tab close", tuple(specs), browser, title, url,
                      all_tabs, tuple(excepts or ()), tuple(like or ()), dry))
        return {"ok": True, "closed": []}

    originals = (cli_main.new_tab, cli_main.list_tabs, cli_main.tab_info,
                 cli_main.close_tabs)
    (cli_main.new_tab, cli_main.list_tabs, cli_main.tab_info,
     cli_main.close_tabs) = (fake_new_tab, fake_list_tabs, fake_tab_info,
                             fake_close_tabs)          # type: ignore[assignment]
    try:
        for argv in (["tab"], ["tab", "https://a.example", "https://b.example"],
                     ["tab", "list"], ["tab", "list", "--browser=chrome"],
                     ["tab", "info", "id:ABC"],
                     ["tab", "info", "a.example", "--browser", "chromium"],
                     ["tab", "close", "a", "b"]):
            rc, _out, err = run_cli(argv)
            assert rc == 0, (argv, rc, err)
        assert calls == [
            ("tab", (), ""),
            ("tab", ("https://a.example", "https://b.example"), ""),
            ("tab list", ""),
            ("tab list", "chrome"),
            ("tab info", "id:ABC", ""),
            ("tab info", "a.example", "chromium"),
            ("tab close", ("a", "b"), "", None, None, False, (), (), False),
        ], calls
    finally:
        (cli_main.new_tab, cli_main.list_tabs, cli_main.tab_info,
         cli_main.close_tabs) = originals             # type: ignore[assignment]
    # a bare word is not a URL — and that is decided before any browser is
    # touched (the real service, not the fake)
    rc, _out, err = run_cli(["tab", "frobnicate"])
    assert rc == 2 and "ERR[bad-args]" in err, (rc, err)


def t_cli_lists() -> None:
    """`list` and `info` print the service reply and take no arguments."""
    rows = [{"pid": 1, "exe": "chrome", "managed": False,
             "cdp": {"port": 0, "reachable": False}}]
    calls: list[tuple] = []

    def fake_list_browsers() -> dict:
        calls.append(("list",))
        return {"ok": True, "count": 1, "browsers": rows}

    def fake_browser_info(browser: str = "") -> dict:
        calls.append(("info", browser))
        return {"ok": True, "running": False}

    originals = (cli_main.list_browsers, cli_main.browser_info)
    cli_main.list_browsers = fake_list_browsers     # type: ignore[assignment]
    cli_main.browser_info = fake_browser_info       # type: ignore[assignment]
    try:
        rc, out, err = run_cli(["list"])
        assert rc == 0 and json.loads(out)["browsers"] == rows, (rc, out, err)
        rc, out, _err = run_cli(["info", "--browser", "chromium"])
        assert rc == 0 and json.loads(out)["running"] is False, out
        assert calls == [("list",), ("info", "chromium")], calls
        for argv in (["list", "extra"], ["list", "--browser", "chromium"],
                     ["info", "extra"], ["tab", "list", "x"],
                     ["tab", "close"], ["tab", "info"],
                     ["tab", "info", "a", "b"], ["tab", "close", "-x"]):
            rc, _out, err = run_cli(argv)
            assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)
    finally:
        (cli_main.list_browsers, cli_main.browser_info) = originals  # type: ignore[assignment]


def t_cli_attach_grammar() -> None:
    """`attach`/`detach` argv lands in the right service call, none dropped."""
    calls: list[tuple] = []

    def fake_attach(port: int = 0, pid: int = 0, profile: str = "") -> dict:
        calls.append(("attach", port, pid, profile))
        return {"ok": True}

    def fake_attachments() -> dict:
        calls.append(("attach --list",))
        return {"ok": True, "count": 0, "attached": []}

    def fake_detach(port: int = 0, pid: int = 0, profile: str = "",
                    detach_all: bool = False) -> dict:
        calls.append(("detach", port, pid, profile, detach_all))
        return {"ok": True}

    originals = (cli_main.attach, cli_main.attachments, cli_main.detach)
    (cli_main.attach, cli_main.attachments,
     cli_main.detach) = (fake_attach, fake_attachments,
                         fake_detach)                # type: ignore[assignment]
    try:
        for argv in (["attach", "--port", "43903"],
                     ["attach", "--pid", "209380"],
                     ["attach", "--profile", "/x/y"],
                     ["attach", "--list"],
                     ["detach", "--port", "1"],
                     ["detach", "--all"]):
            rc, _out, err = run_cli(argv)
            assert rc == 0, (argv, rc, err)
        assert calls == [
            ("attach", 43903, 0, ""),
            ("attach", 0, 209380, ""),
            ("attach", 0, 0, "/x/y"),
            ("attach --list",),
            ("detach", 1, 0, "", False),
            ("detach", 0, 0, "", True),
        ], calls
        for argv in (["attach", "--port"], ["attach", "--port", "x"],
                     ["attach", "--port", "1", "--bogus"],
                     ["attach", "--list", "--port", "1"],
                     ["attach", "--browser", "chrome"],
                     ["detach", "--all", "--port", "1"],
                     ["detach", "--pid", "abc"]):
            rc, _out, err = run_cli(argv)
            assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)
    finally:
        (cli_main.attach, cli_main.attachments,
         cli_main.detach) = originals                # type: ignore[assignment]
    # the library refuses what the CLI cannot know: none, or two selectors
    for argv in (["attach"], ["attach", "--port", "1", "--pid", "2"],
                 ["detach"]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)


def t_attach_bookkeeping() -> None:
    """attach → the write gate opens → detach closes it again.

    Two fake browsers on a throwaway root: ours (managed) and a foreign one
    (attached). The point is the boundary, not the plumbing.
    """
    with tempfile.TemporaryDirectory() as tmp:
        keep_root = os.environ.get("BROWSER_CONTROL_ROOT")
        os.environ["BROWSER_CONTROL_ROOT"] = tmp
        ours = os.path.join(tmp, "ours")
        foreign = os.path.join(tmp, "foreign")
        real_browsers = browser.browsers
        real_rows = browser.cdp.page_rows_at

        def rows() -> list[dict]:
            return [
                {"pid": 1, "exe": "chrome", "path": "/usr/bin/chrome",
                 "profile": ours, "profile_from": "flag",
                 "managed": True, "attached": browser.is_attached(ours),
                 "cdp": {"port": 1616, "reachable": True, "verified": True,
                         "tabs": 1}},
                {"pid": 2, "exe": "chrome", "path": "/usr/bin/chrome",
                 "profile": foreign, "profile_from": "flag",
                 "managed": False,
                 "attached": browser.is_attached(foreign),
                 "cdp": {"port": 1515, "reachable": True, "verified": True,
                         "tabs": 1}},
            ]

        def page_rows_at(port: int) -> list[dict]:
            ident = "AB12" if port == 1515 else "CD34"
            return [{"type": "page", "id": ident, "title": ident,
                     "url": f"https://{port}/"}]

        browser.browsers = rows                      # type: ignore[assignment]
        browser.cdp.page_rows_at = page_rows_at      # type: ignore[assignment]
        try:
            # the foreign tab is READABLE, and refused for a write
            assert browser.tab_info("id:AB12")["browser"]["managed"] is False
            refusal(lambda: browser._resolve_across(["id:AB12"], "foreign",
                                                    for_write=True),
                    "not-managed")
            # lifecycle resolution never picks an attached browser
            assert browser.managed_profile("") == ours
            reply = browser.attach(port=1515)
            assert reply["attached"] is True, reply
            assert reply["already"] is False, reply
            assert reply["browser"]["profile"] == browser._norm(foreign), reply
            # two writable browsers refuse rather than pick one
            refusal(lambda: browser._writable_profile(""), "ambiguous-browser")
            # the same spec now resolves for a write
            found = browser._resolve_across(["id:AB12"], "foreign",
                                            for_write=True)
            assert found and found[0][1]["id"] == "AB12", found
            listed = browser.attachments()["attached"]
            assert listed and listed[0]["running"] is True, listed
            assert listed[0]["managed"] is False, listed
            # detach puts the refusal back
            gone = browser.detach(profile=foreign)
            assert gone["detached"] == [browser._norm(foreign)], gone
            refusal(lambda: browser._resolve_across(["id:AB12"], "foreign",
                                                    for_write=True),
                    "not-managed")
            refusal(lambda: browser.detach(port=1515), "not-attached")
        finally:
            browser.browsers = real_browsers          # type: ignore[assignment]
            browser.cdp.page_rows_at = real_rows      # type: ignore[assignment]
            _restore_root(keep_root)


def t_cli_nav_grammar() -> None:
    """`tab nav|back|forward|reload` argv, `--tab` included, none dropped."""
    calls: list[tuple] = []

    def fake_nav(url: str, tab: str = "", browser: str = "") -> dict:
        calls.append(("nav", url, tab, browser))
        return {"ok": True}

    def fake_history(direction: str, tab: str = "",
                     browser: str = "") -> dict:
        calls.append(("history", direction, tab, browser))
        return {"ok": True}

    def fake_reload(tab: str = "", browser: str = "") -> dict:
        calls.append(("reload", tab, browser))
        return {"ok": True}

    originals = (cli_main.nav, cli_main.history, cli_main.reload)
    cli_main.nav, cli_main.history, cli_main.reload = (
        fake_nav, fake_history, fake_reload)         # type: ignore[assignment]
    try:
        for argv in (["tab", "nav", "https://a.example"],
                     ["tab", "nav", "https://a.example", "--tab", "id:ABC"],
                     ["tab", "nav", "https://a.example", "--tab=id:ABC"],
                     ["tab", "nav", "https://a.example",
                      "--browser", "chromium"],
                     ["tab", "back"], ["tab", "back", "--tab", "x"],
                     ["tab", "forward"], ["tab", "reload"]):
            rc, _out, err = run_cli(argv)
            assert rc == 0, (argv, rc, err)
        assert calls == [
            ("nav", "https://a.example", "", ""),
            ("nav", "https://a.example", "id:ABC", ""),
            ("nav", "https://a.example", "id:ABC", ""),
            ("nav", "https://a.example", "", "chromium"),
            ("history", "back", "", ""),
            ("history", "back", "x", ""),
            ("history", "forward", "", ""),
            ("reload", "", ""),
        ], calls
        for argv in (["tab", "nav"], ["tab", "nav", "a", "b"],
                     ["tab", "nav", "--tab"], ["tab", "nav", "-x"],
                     ["tab", "back", "extra"], ["tab", "reload", "extra"],
                     ["tab", "forward", "--bogus"]):
            rc, _out, err = run_cli(argv)
            assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)
    finally:
        (cli_main.nav, cli_main.history,
         cli_main.reload) = originals                # type: ignore[assignment]


def t_same_page() -> None:
    """The net-change test: a trailing slash is not a different page."""
    same = browser._same_page                                    # noqa: SLF001
    assert same("https://a.example", "https://a.example/") is True
    assert same("https://a.example/x", "https://a.example/x/") is True
    assert same("https://a.example/x?a=1", "https://a.example/x?a=1")
    assert same("https://a.example/x?a=1", "https://a.example/x?a=2") is False
    assert same("https://a.example/x#one", "https://a.example/x#two") is False
    assert same("https://a.example/", "https://b.example/") is False
    assert same("", "https://a.example/") is False
    assert same("about:blank", "about:blank") is True


def t_one_tab_addressing() -> None:
    """A page verb acts on the only tab, refuses several, and never writes
    into a browser that is neither ours nor attached."""
    with tempfile.TemporaryDirectory() as tmp:
        keep_root = os.environ.get("BROWSER_CONTROL_ROOT")
        os.environ["BROWSER_CONTROL_ROOT"] = tmp
        ours = os.path.join(tmp, "ours")
        foreign = os.path.join(tmp, "foreign")
        real_browsers = browser.browsers
        real_rows = browser.cdp.page_rows_at
        tabs: dict[int, list[dict]] = {}

        def rows() -> list[dict]:
            return [
                {"pid": 1, "exe": "chrome", "path": "/usr/bin/chrome",
                 "profile": ours, "profile_from": "flag", "managed": True,
                 "attached": False,
                 "cdp": {"port": 1616, "reachable": True, "verified": True}},
                {"pid": 2, "exe": "chrome", "path": "/usr/bin/chrome",
                 "profile": foreign, "profile_from": "flag",
                 "managed": False, "attached": False,
                 "cdp": {"port": 1515, "reachable": True, "verified": True}},
            ]

        def page_rows_at(port: int) -> list[dict]:
            return tabs.get(port, [])

        def page(ident: str) -> dict:
            return {"type": "page", "id": ident, "title": ident,
                    "url": f"https://{ident}/"}

        browser.browsers = rows                      # type: ignore[assignment]
        browser.cdp.page_rows_at = page_rows_at      # type: ignore[assignment]
        try:
            tabs[1616] = [page("AB12")]
            row, one = browser._one_tab("", "", for_write=True)  # noqa: SLF001
            assert row["pid"] == 1 and one["id"] == "AB12", (row, one)
            tabs[1616] = [page("AB12"), page("CD34")]
            refusal(lambda: browser._one_tab("", "", for_write=True),  # noqa: SLF001
                    "tab-ambiguous")
            _row, named = browser._one_tab("CD34", "", for_write=True)  # noqa: SLF001
            assert named["id"] == "CD34", named
            tabs[1616] = []
            refusal(lambda: browser._one_tab("", "", for_write=True),  # noqa: SLF001
                    "no-page-tab")
            # a browser this CLI does not drive is not in the NO-SPEC scope:
            # the user's own browser running beside ours must not make every
            # unqualified read ambiguous. A spec reaches it; a write refuses.
            tabs[1515] = [page("EF56")]
            refusal(lambda: browser._one_tab("", "", for_write=False),  # noqa: SLF001
                    "no-page-tab")
            _row, readable = browser._one_tab("EF56", "", for_write=False)  # noqa: SLF001
            assert readable["id"] == "EF56", readable
            refusal(lambda: browser._one_tab("EF56", "", for_write=True),  # noqa: SLF001
                    "not-managed")
            # with NO managed browser up at all, a write refuses and NAMES the
            # stranger instead of writing into it (measured live: a stranger's
            # browser gained a tab from `tab about:blank`), while a read still
            # reaches it — reading a tab list is not typing into it
            browser.browsers = lambda: [rows()[1]]       # type: ignore[assignment]
            tabs[1515] = [page("EF56")]
            refusal(lambda: browser._one_tab("", "", for_write=True),  # noqa: SLF001
                    "not-managed")
            _row, stranger = browser._one_tab("", "", for_write=False)  # noqa: SLF001
            assert stranger["id"] == "EF56", stranger
            browser.browsers = rows                      # type: ignore[assignment]
            # a SCOPED write is refused too when the scope is outside the root:
            # `--profile /tmp/stranger` was returned as-is, so `tab about:blank`
            # ran `Target.createTarget` in a stranger's browser
            browser.scope(tempfile.gettempdir())
            try:
                refusal(lambda: browser._writable_profile(""),  # noqa: SLF001
                        "not-managed")
            finally:
                browser.scope("")
            # `attach` must not RECORD an endpoint that answered but did not
            # verify: that record is what opens the tab-write gate
            unverified = dict(rows()[1])
            unverified["cdp"] = {"port": 1515, "reachable": True,
                                 "verified": False,
                                 "reason": "a stranger holds that port"}
            browser.browsers = lambda: [unverified]      # type: ignore[assignment]
            try:
                refusal(lambda: browser.attach(port=1515), "cdp-not-local")
            finally:
                browser.browsers = rows                  # type: ignore[assignment]
            # a tabs-read that FAILED is a refusal, not "no tabs": seven callers
            # took `_tabs_of(row)[0]` and dropped the error, so a browser that
            # did not answer produced "no tab matches … have: none"
            real_tabs_of = browser._tabs_of
            browser._tabs_of = lambda row: ([], "the /json read timed out")
            try:
                refusal(lambda: browser._tabs_or_fail(rows()[0]),  # noqa: SLF001
                        "cdp-error")
            finally:
                browser._tabs_of = real_tabs_of          # type: ignore[assignment]
            tabs[1616] = [page("AB12")]
            assert [t["id"] for t in browser._tabs_or_fail(rows()[0])] == \
                ["AB12"], "a good read passes through"
            # a WRITE that goes straight to the browser endpoint asks the kernel
            # who owns the port first, and sends NOTHING when the answer is not
            # the browser we think — `Target.createTarget` used to go wherever
            # the port file pointed (a review found no test for it at all)
            calls: list[tuple] = []
            real_owner, real_port = browser.endpoint_owner, cdp.port_of
            real_call, real_verify = cdp.browser_call, browser._verify_profile_endpoint
            browser.endpoint_owner = lambda profile, port: {    # type: ignore[assignment]
                "verified": False, "reason": "a stranger holds that port"}
            cdp.port_of = lambda profile: 1515                # type: ignore[assignment]
            cdp.browser_call = lambda profile, method, params: (  # type: ignore[assignment]
                calls.append((profile, method)) or {})
            try:
                refusal(lambda: browser._open_tabs(         # noqa: SLF001
                    str(rows()[0]["profile"]), ["about:blank"]),
                    "cdp-not-local")
                assert calls == [], calls
            finally:
                browser.endpoint_owner = real_owner          # type: ignore[assignment]
                cdp.port_of = real_port                      # type: ignore[assignment]
                cdp.browser_call = real_call                 # type: ignore[assignment]
            # …and the tab-CLOSING path asks too, once per profile
            asked: list[str] = []
            browser._verify_profile_endpoint = \
                lambda profile: asked.append(profile)        # type: ignore[assignment]
            cdp.browser_call = lambda profile, method, params: \
                tabs.__setitem__(1616, []) or {}             # type: ignore[assignment]
            try:
                browser.close_tabs(["AB12"])
            finally:
                browser._verify_profile_endpoint = real_verify  # type: ignore[assignment]
                cdp.browser_call = real_call                 # type: ignore[assignment]
            assert asked == [str(rows()[0]["profile"])], asked
            # …and it REFUSES before any browser call when the endpoint is not
            # the browser we think (the probe above only asserted that it asked)
            calls2: list[tuple] = []
            cdp.browser_call = lambda profile, method, params: (  # type: ignore[assignment]
                calls2.append((profile, method)) or {})
            browser.endpoint_owner = lambda profile, port: {      # type: ignore[assignment]
                "verified": False, "reason": "a stranger holds that port"}
            cdp.port_of = lambda profile: 1515                 # type: ignore[assignment]
            tabs[1616] = [page("AB12")]
            try:
                refusal(lambda: browser.close_tabs(["AB12"]), "cdp-not-local")
                assert calls2 == [], calls2
            finally:
                browser.endpoint_owner = real_owner           # type: ignore[assignment]
                cdp.port_of = real_port                       # type: ignore[assignment]
                cdp.browser_call = real_call                  # type: ignore[assignment]
            # …and the tabs read that failed is the ONLY place the error may be
            # dropped: `_tabs_of(row)[0]` must not appear anywhere in the module
            source = (Path(__file__).resolve().parent.parent
                      / "browser_control/lib/browser.py").read_text(
                          encoding="utf-8")
            assert "_tabs_of(row)[0]" not in source, \
                "a failed tabs read must refuse, not read as 'no tabs'"
            assert "_tabs_or_fail(row)" in source
        finally:
            browser.browsers = real_browsers          # type: ignore[assignment]
            browser.cdp.page_rows_at = real_rows      # type: ignore[assignment]
            _restore_root(keep_root)


def t_cli_dom_grammar() -> None:
    """`tab js|find|text|wait` argv — flags stripped, none dropped."""
    calls: list[tuple] = []

    def fake_js(expression: str, tab: str = "", browser: str = "") -> dict:
        calls.append(("js", expression, tab, browser))
        return {"ok": True}

    def fake_find(text: str | None = None, selector: str | None = None,
                  cap: int = 10, tab: str = "", browser: str = "") -> dict:
        calls.append(("find", text, selector, cap, tab, browser))
        return {"ok": True}

    def fake_text(selector: str | None = None, chars: int = 0, tab: str = "",
                  browser: str = "") -> dict:
        calls.append(("text", selector, chars, tab, browser))
        return {"ok": True}

    def fake_wait(mode: str, selector: str | None = None,
                  expr: str | None = None, timeout: float = 15.0,
                  idle_ms: int = 500, tab: str = "",
                  browser: str = "") -> dict:
        calls.append(("wait", mode, selector, expr, timeout, idle_ms, tab,
                      browser))
        return {"ok": True}

    originals = (dom.js, dom.find, dom.text, dom.wait)
    dom.js, dom.find, dom.text, dom.wait = (fake_js, fake_find, fake_text,
                                            fake_wait)  # type: ignore[assignment]
    try:
        for argv in (["tab", "js", "document.title"],
                     ["tab", "js", "document.title", "--tab", "id:ABC"],
                     ["tab", "find", "Save"],
                     ["tab", "find", "--selector", ".x", "--cap", "3"],
                     ["tab", "text"],
                     ["tab", "text", "--selector", "#main", "--chars", "50"],
                     ["tab", "wait", "--for", "element", "--selector", ".x",
                      "--timeout", "5"],
                     ["tab", "wait", "--for", "js", "--expr", "true",
                      "--idle-ms", "100", "--tab", "id:ABC"]):
            rc, _out, err = run_cli(argv)
            assert rc == 0, (argv, rc, err)
        assert calls == [
            ("js", "document.title", "", ""),
            ("js", "document.title", "id:ABC", ""),
            ("find", "Save", None, dom.FIND_CAP, "", ""),
            ("find", None, ".x", 3, "", ""),
            ("text", None, dom.TEXT_CAP, "", ""),
            ("text", "#main", 50, "", ""),
            ("wait", "element", ".x", None, 5.0, dom.IDLE_DEFAULT_MS, "",
             ""),
            ("wait", "js", None, "true", dom.WAIT_DEFAULT_S, 100,
             "id:ABC", ""),
        ], calls
    finally:
        dom.js, dom.find, dom.text, dom.wait = originals  # type: ignore[assignment]
    # every one of these refuses BEFORE a browser is touched (there is none
    # running in a hermetic check), which is the property being held here
    for argv in (["tab", "js"], ["tab", "js", "a", "b"],
                 ["tab", "js", "--tab", "id:1"], ["tab", "find"],
                 ["tab", "find", "a", "--selector", "b"],
                 ["tab", "find", "a", "b"], ["tab", "text", "extra"],
                 ["tab", "text", "--chars", "x"], ["tab", "wait"],
                 ["tab", "wait", "--for", "nope"],
                 ["tab", "wait", "--for", "element"],
                 ["tab", "wait", "--for", "js"],
                 ["tab", "wait", "--for", "load", "--selector", ".x"],
                 ["tab", "wait", "--for", "load", "--timeout", "0"]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)


def t_dom_shape_filters() -> None:
    """A page-supplied row is filtered to shape; a bad int is not a crash."""
    rows = dom._well_formed(                              # noqa: SLF001
        [{"tag": "a", "box": [1, 2, 3, 4]}, {"tag": "a"}, "nope", None,
         {"box": [0, 0, 0, 0]}], ("tag", "box"))
    assert rows == [{"tag": "a", "box": [1, 2, 3, 4]}], rows
    assert dom._int("12") == 12                              # noqa: SLF001
    assert dom._int("x", 7) == 7                             # noqa: SLF001
    assert dom._int(None, 3) == 3                            # noqa: SLF001
    # the VALUES a page supplies are shapes too: `_ints` never raises out of a
    # verb, whatever the page answered (a review measured a TypeError)
    assert dom._ints([1, 2, 3, 4], 2) == [1, 2]              # noqa: SLF001
    assert dom._ints(["2", 3]) == [2, 3]                     # noqa: SLF001
    for bad in (7, None, "12", {"a": 1}, object()):
        assert dom._ints(bad) == [], bad                      # noqa: SLF001
    # the placeholder check has to be one that CAN fail: `"x" not in
    # expr.replace("x", "")` is a tautology (a review caught it)
    assert dom.FIND_EXPR.count("__SELECTOR__") == 1          # noqa: SLF001
    for placeholder in ("__MODE__", "__NEEDLE__", "__SELECTOR__", "__CAP__"):
        assert placeholder in dom.FIND_EXPR                  # noqa: SLF001


def t_cli_click_scroll_grammar() -> None:
    """`tab click|scroll` argv — flags stripped, none dropped."""
    calls: list[tuple] = []

    def fake_click(text: str | None = None, selector: str | None = None,
                   index: int | None = None, tab: str = "",
                   browser: str = "") -> dict:
        calls.append(("click", text, selector, index, tab, browser))
        return {"ok": True}

    def fake_scroll(by: int | None = None, edge: str | None = None,
                    text: str | None = None, selector: str | None = None,
                    index: int | None = None, at: str | None = None,
                    tab: str = "", browser: str = "") -> dict:
        calls.append(("scroll", by, edge, text, selector, index, at, tab,
                      browser))
        return {"ok": True}

    originals = (dom.click, dom.scroll)
    dom.click, dom.scroll = fake_click, fake_scroll   # type: ignore[assignment]
    try:
        for argv in (["tab", "click", "Save"],
                     ["tab", "click", "Save", "--index", "2",
                      "--tab", "id:AB"],
                     ["tab", "click", "--selector", ".x"],
                     ["tab", "scroll", "--by", "600"],
                     ["tab", "scroll", "--by", "600", "--at", "10,20"],
                     ["tab", "scroll", "--edge", "bottom"],
                     ["tab", "scroll", "Below"],
                     ["tab", "scroll", "--selector", "#x", "--index", "1"]):
            rc, _out, err = run_cli(argv)
            assert rc == 0, (argv, rc, err)
        assert calls == [
            ("click", "Save", None, None, "", ""),
            ("click", "Save", None, 2, "id:AB", ""),
            ("click", None, ".x", None, "", ""),
            ("scroll", 600, None, None, None, None, None, "", ""),
            ("scroll", 600, None, None, None, None, "10,20", "", ""),
            ("scroll", None, "bottom", None, None, None, None, "", ""),
            ("scroll", None, None, "Below", None, None, None, "", ""),
            ("scroll", None, None, None, "#x", 1, None, "", ""),
        ], calls
    finally:
        dom.click, dom.scroll = originals             # type: ignore[assignment]
    # and these refuse BEFORE a browser is touched (none runs hermetically)
    for argv in (["tab", "click"], ["tab", "click", "a", "--selector", "b"],
                 ["tab", "click", "a", "b"], ["tab", "click", "a", "-x"],
                 ["tab", "click", "--index", "x", "Save"],
                 ["tab", "scroll"], ["tab", "scroll", "--by", "0"],
                 ["tab", "scroll", "--by", "x"],
                 ["tab", "scroll", "--edge", "sideways"],
                 ["tab", "scroll", "--edge", "top", "--at", "1,2"],
                 ["tab", "scroll", "--at", "1,2"],
                 ["tab", "scroll", "--by", "600", "--at", "bad"],
                 ["tab", "scroll", "--by", "600", "--at", "x,y"]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)


def t_cli_close_bulk() -> None:
    """`tab close`: argv for every selector, and the pure refusals."""
    calls: list[tuple] = []
    # argv-only paths: the verbs are faked below, so nothing is written
    fake_profile = os.path.join(tempfile.gettempdir(),
                                "browser-control-argv-profile")

    def fake_close(specs: list[str], browser: str = "",
                   title: str | None = None, url: str | None = None,
                   all_tabs: bool = False,
                   excepts: list[str] | None = None,
                   like: list[str] | None = None,
                   dry: bool = False) -> dict:
        calls.append((tuple(specs), browser, title, url, all_tabs,
                      tuple(excepts or ()), tuple(like or ()), dry))
        return {"ok": True}

    original = cli_main.close_tabs
    cli_main.close_tabs = fake_close                   # type: ignore[assignment]
    try:
        for argv in (["tab", "close", "--title", "a"],
                     ["tab", "close", "--url", "http://a/"],
                     ["tab", "close", "--title=a", "--browser", "chrome"],
                     ["tab", "close", "id:AB", "id:CD"],
                     ["tab", "close", "http://a/"],
                     ["tab", "close", "--like", "x.com"],
                     ["tab", "close", "--like=a", "--like=b"],
                     ["tab", "close", "--all", "--dry"],
                     ["tab", "close", "dry", "--dry"],
                     ["tab", "close", "--all"],
                     ["tab", "close", "--all", "--except", "x.com"],
                     ["tab", "close", "--all", "--except=x.com"],
                     ["tab", "close", "--except", "a", "--except", "b"]):
            rc, _out, err = run_cli(argv)
            assert rc == 0, (argv, rc, err)
        assert calls == [
            ((), "", "a", None, False, (), (), False),
            ((), "", None, "http://a/", False, (), (), False),
            ((), "chrome", "a", None, False, (), (), False),
            (("id:AB", "id:CD"), "", None, None, False, (), (), False),
            (("http://a/",), "", None, None, False, (), (), False),
            ((), "", None, None, False, (), ("x.com",), False),
            ((), "", None, None, False, (), ("a", "b"), False),
            ((), "", None, None, True, (), (), True),
            (("dry",), "", None, None, False, (), (), True),
            ((), "", None, None, True, (), (), False),
            ((), "", None, None, True, ("x.com",), (), False),
            ((), "", None, None, True, ("x.com",), (), False),
            ((), "", None, None, False, ("a", "b"), (), False),
        ], calls
    finally:
        cli_main.close_tabs = original                 # type: ignore[assignment]
    # `close` reaches the service with --force and with a named browser, and
    # refuses anything else (the selector is the same one attach/detach take)
    stops: list[tuple] = []

    def fake_stop(browser: str = "", force: bool = False, port: int = 0,
                  pid: int = 0, profile: str = "") -> dict:
        stops.append((browser, force, port, pid, profile))
        return {"ok": True}

    original_stop = cli_main.stop
    cli_main.stop = fake_stop                         # type: ignore[assignment]
    try:
        for argv in (["close"], ["close", "--force"],
                     ["close", "--browser=x", "--force"],
                     ["close", "--pid", "42"],
                     ["close", "--port", "9222"],
                     ["close", "--profile", fake_profile],
                     ["close", "--pid", "42", "--force"]):
            rc, _out, err = run_cli(argv)
            assert rc == 0, (argv, rc, err)
        assert stops == [("", False, 0, 0, ""),
                         ("", True, 0, 0, ""),
                         ("x", True, 0, 0, ""),
                         ("", False, 0, 42, ""),
                         ("", False, 9222, 0, ""),
                         ("", False, 0, 0, fake_profile),
                         ("", True, 0, 42, "")], stops
    finally:
        cli_main.stop = original_stop                  # type: ignore[assignment]
    for argv in (["close", "now"], ["close", "--pid", "1", "--port", "2"],
                 ["close", "--pid"], ["close", "--list"],
                 # --profile IS a selector: naming two ways is bad-args
                 ["close", "--pid", "1", "--profile", fake_profile],
                 ["close", "--port", "1", "--profile", fake_profile]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)
    # a repeatable flag with no value is refused in argv
    for flag in ("--except", "--like", "--title"):
        rc, _out, err = run_cli(["tab", "close", flag])
        assert rc == 2 and "ERR[bad-args]" in err, (flag, rc, err)
    # and these are refused by the SERVICE, before any browser is touched:
    # one way to name tabs per call, never two, never none, never empty
    refusal(lambda: browser.close_tabs([]), "bad-args")
    refusal(lambda: browser.close_tabs([""]), "bad-args")
    refusal(lambda: browser.close_tabs(["  "]), "bad-args")
    refusal(lambda: browser.close_tabs(["id:"]), "bad-args")
    refusal(lambda: browser.close_tabs([], title=""), "bad-args")
    refusal(lambda: browser.close_tabs([], url=""), "bad-args")
    refusal(lambda: browser.close_tabs([], title="a", url="b"), "bad-args")
    refusal(lambda: browser.close_tabs(["id:AB"], title="a"), "bad-args")
    refusal(lambda: browser.close_tabs(["id:AB"], url="http://a/"),
            "bad-args")
    refusal(lambda: browser.close_tabs([], all_tabs=True, title="a"),
            "bad-args")
    refusal(lambda: browser.close_tabs(["id:AB"], all_tabs=True), "bad-args")
    refusal(lambda: browser.close_tabs([], like=[""]), "bad-args")
    refusal(lambda: browser.close_tabs(["id:AB"], like=["x"]), "bad-args")
    refusal(lambda: browser.close_tabs([], like=["x"], all_tabs=True),
            "bad-args")
    refusal(lambda: browser.close_tabs([], like=["x"], excepts=["y"]),
            "bad-args")
    refusal(lambda: browser.close_tabs([], excepts=["a", ""]), "bad-args")
    refusal(lambda: browser.close_tabs([], dry=True), "bad-args")
    # a SPEC NAMES a tab: `browser._exact_spec_match` is the whole rule, and
    # it is pure — the regression that made this necessary was `tab close a`
    # closing a tab whose title merely CONTAINED an `a`
    match = browser._exact_spec_match                            # noqa: SLF001
    a_tab = {"id": "AB12CD34", "url": "http://a/", "title": "a"}
    assert match(a_tab, "a") is True                            # its title
    assert match(a_tab, "http://a") is True                     # its URL
    assert match(a_tab, "http://a/") is True                    # trailing slash
    assert match(a_tab, "A") is True                            # case-insensitive
    assert match(a_tab, "id:AB12") is True                      # id prefix
    assert match(a_tab, "id:ab12") is True                      # prefix, case
    assert match(a_tab, "") is False                            # empty names nothing
    assert match(a_tab, "id:") is False      # an empty prefix names every
    assert match(a_tab, "id:  ") is False    # ...tab, so it names none
    x_tab = {"id": "FF00FF00", "url": "https://x.com/",
             "title": "X. It\u2019s what\u2019s happening / X"}
    assert match(x_tab, "a") is False       # the bug, in one assertion
    assert match(x_tab, "x.com") is False   # not the whole URL or title
    assert match(x_tab, "https://x.com") is True                # the whole URL
    assert match(x_tab, "X. It\u2019s what\u2019s happening / X") is True


def t_keys_and_verdicts() -> None:
    """The key table is well formed and the text verdict is tri-state."""
    for name, triple in dom.KEYS.items():                     # noqa: SLF001
        key, code, vk, text = triple
        assert key and code and isinstance(text, str), name
        assert 8 <= vk <= 255, (name, vk)
    # a keyDown without text does not submit a form: Enter and Space carry it
    assert dom.KEYS["enter"][3] == "\r"                      # noqa: SLF001
    assert dom.KEYS["space"][3] == " "                       # noqa: SLF001
    before = {"active": "input#s", "target": "input#s@0.1",
              "length": 3, "frame": False}
    verdicts = [dom._text_verdict(before, after, 3)[0]         # noqa: SLF001
                for after in (
                    dict(before, length=6),        # it grew: verified
                    dict(before, length=3),        # readable, unchanged: NO
                    dict(before, target="input#t@0.2", length=6),  # moved
                    dict(before, frame=True, length=6),   # a frame: unclear
                    dict(before, length=None))]    # no value to read: unclear
    assert verdicts == [True, False, None, None, None], verdicts
    secrets = [dom._is_secret(probe) for probe in (            # noqa: SLF001
        {"secret": True}, {"frame": True}, {"unreadable": True},
        {"secret": False})]
    assert secrets == [True, True, True, False], secrets


def t_audit_redaction() -> None:
    """A proven secret never reaches the log; the log never breaks a verb."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "actions.jsonl")
        os.environ["BROWSER_CONTROL_LOG"] = path
        try:
            audit.LOG.begin("tab")
            audit.LOG.write(action="tab", ok=True, args=["insert", "hunter2"])
            audit.LOG.begin("tab")
            audit.LOG.mark_secret("hunter2")
            assert audit.LOG.redacted is True
            audit.LOG.write(action="tab", ok=False,
                            code="insert-not-verified",
                            args=["insert", "hunter2"])
            with open(path, encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]
            assert rows[0]["args"] == ["insert", "hunter2"], rows[0]
            assert rows[0].get("redacted") is None, rows[0]
            assert rows[1]["args"] == ["insert", "<redacted: 7 chars>"], rows[1]
            assert rows[1]["redacted"] is True, rows[1]
            assert rows[1]["ok"] is False and rows[1]["code"], rows[1]
            # the CONTROL line kept it (nothing was marked yet); the marked
            # line did not — that is the property, not "the file never held a
            # string a caller typed before marking it"
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            assert "hunter2" in lines[0], lines[0]
            assert "hunter2" not in lines[1], lines[1]
            # `off` writes nothing at all
            os.environ["BROWSER_CONTROL_LOG"] = "off"
            size = os.path.getsize(path)
            audit.LOG.write(action="tab", args=["x"])
            assert os.path.getsize(path) == size
            # an unwritable path is not a failure of the verb — and it is no
            # longer a LOST record: the line lands in the scratch directory
            os.environ["BROWSER_CONTROL_LOG"] = "/proc/nope/actions.jsonl"
            audit.LOG.write(action="tab", args=["x"])
            fallback = os.path.join(audit.scratch_dir(), "actions.jsonl")
            with open(fallback, encoding="utf-8") as handle:
                assert json.loads(handle.read().splitlines()[-1])["action"] \
                    == "tab", "the scratch copy is missing"
        finally:
            # back to the SUITE's log, never to the user's default
            os.environ["BROWSER_CONTROL_LOG"] = SUITE_LOG
            audit.LOG.begin("")


def t_cli_input_grammar() -> None:
    """`tab focus|press|insert|type|upload` argv — flags stripped, none lost."""
    calls: list[tuple] = []

    def fake_focus(text: str | None = None, selector: str | None = None,
                   index: int | None = None, tab: str = "",
                   browser: str = "") -> dict:
        calls.append(("focus", text, selector, index, tab, browser))
        return {"ok": True}

    def fake_press(key: str = "", tab: str = "", browser: str = "") -> dict:
        calls.append(("press", key, tab, browser))
        return {"ok": True}

    def fake_insert(text: str = "", tab: str = "",
                    browser: str = "") -> dict:
        calls.append(("insert", text, tab, browser))
        return {"ok": True}

    def fake_type(text: str = "", tab: str = "", browser: str = "") -> dict:
        calls.append(("type", text, tab, browser))
        return {"ok": True}

    def fake_upload(path: str = "", selector: str | None = None,
                    index: int | None = None, tab: str = "",
                    browser: str = "") -> dict:
        calls.append(("upload", path, selector, index, tab, browser))
        return {"ok": True}

    originals = (dom.focus, dom.press, dom.insert, dom.type_text, dom.upload)
    target = os.path.join(tempfile.gettempdir(), "upload-me.txt")
    (dom.focus, dom.press, dom.insert, dom.type_text,
     dom.upload) = (fake_focus, fake_press, fake_insert, fake_type,
                    fake_upload)                       # type: ignore[assignment]
    try:
        for argv in (["tab", "focus", "Search Box"],
                     ["tab", "focus", "--selector", "#s", "--index", "1",
                      "--tab", "id:AB"],
                     ["tab", "press", "enter"],
                     ["tab", "insert", "hello world"],
                     ["tab", "type", "abc"],
                     ["tab", "upload", target, "--selector", "#file"]):
            rc, _out, err = run_cli(argv)
            assert rc == 0, (argv, rc, err)
        assert calls == [
            ("focus", "Search Box", None, None, "", ""),
            ("focus", None, "#s", 1, "id:AB", ""),
            ("press", "enter", "", ""),
            ("insert", "hello world", "", ""),
            ("type", "abc", "", ""),
            ("upload", target, "#file", None, "", ""),
        ], calls
    finally:
        (dom.focus, dom.press, dom.insert, dom.type_text,
         dom.upload) = originals                      # type: ignore[assignment]
    # every one of these refuses BEFORE a browser is touched
    for argv in (["tab", "focus"], ["tab", "focus", "a", "--selector", "b"],
                 ["tab", "focus", "a", "b"], ["tab", "press"],
                 ["tab", "press", "enter", "tab"],
                 ["tab", "press", "nope"], ["tab", "insert"],
                 ["tab", "insert", "a", "b"], ["tab", "type"],
                 ["tab", "type", "a", "b"], ["tab", "upload"],
                 ["tab", "upload", "relative.txt"]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)
    rc, _out, err = run_cli(["tab", "upload", "/nope/missing.txt"])
    assert rc == 2 and "ERR[no-file]" in err, (rc, err)


def t_media_verdict() -> None:
    """The play/pause verdict is judged ONLY from the read-back.

    `play` needs the CLOCK to move: an element with no source reports
    `paused: false` and never plays a frame (measured in the battery), so "not
    paused" is not playing.
    """
    verdicts = [
        dom._playback_verdict("play", {"time": 0.0},                # noqa: SLF001
                              {"time": 0.6})[0],
        dom._playback_verdict("play", {"time": 0.0},                # noqa: SLF001
                              {"time": 0.0, "paused": False,
                               "ready_state": 4})[0],
        dom._playback_verdict("play", {"time": 0.0},                # noqa: SLF001
                              {"time": 0.0, "paused": False,
                               "ready_state": 0})[0],
        dom._playback_verdict("pause", {"paused": False},           # noqa: SLF001
                              {"paused": True})[0],
        dom._playback_verdict("pause", {"paused": False},           # noqa: SLF001
                              {"paused": False})[0],
    ]
    assert verdicts == [True, False, False, True, False], verdicts
    why = dom._playback_verdict("play", {"time": 0.0},             # noqa: SLF001
                                {"time": 0.0, "ready_state": 0})
    assert "nothing to play" in why[1], why
    assert dom._num("1.5") == 1.5                                # noqa: SLF001
    assert dom._num("nope", 2.0) == 2.0                         # noqa: SLF001
    assert dom._num(None, 3.0) == 3.0                            # noqa: SLF001


def t_cli_media_grammar() -> None:
    """`tab media state|play|pause` argv — flags stripped, none dropped."""
    calls: list[tuple] = []

    def fake_media(mode: str = "", index: int | None = None, tab: str = "",
                   browser: str = "") -> dict:
        calls.append(("media", mode, index, tab, browser))
        return {"ok": True}

    original = dom.media
    dom.media = fake_media                          # type: ignore[assignment]
    try:
        for argv in (["tab", "media", "state"],
                     ["tab", "media", "play", "--index", "1",
                      "--tab", "id:AB"],
                     ["tab", "media", "pause"]):
            rc, _out, err = run_cli(argv)
            assert rc == 0, (argv, rc, err)
        assert calls == [
            ("media", "state", None, "", ""),
            ("media", "play", 1, "id:AB", ""),
            ("media", "pause", None, "", ""),
        ], calls
    finally:
        dom.media = original                        # type: ignore[assignment]
    # and the mode is validated BEFORE a browser is touched
    for argv in (["tab", "media"], ["tab", "media", "stop"],
                 ["tab", "media", "play", "pause"],
                 ["tab", "media", "play", "-x"],
                 ["tab", "media", "state", "--index", "1"]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)


def t_cmdline_value() -> None:
    """Chrome writes `--flag=value` and `--flag value`; both are read.

    The NUL-separated argv form is the one that keeps a value with SPACES
    whole: flattening NULs to spaces made a profile path containing one look
    like a different profile (a review flagged it).
    """
    value = browser._cmdline_value                            # noqa: SLF001
    assert value("chrome\0--user-data-dir=/x/y z\0--type=renderer",
                 "--user-data-dir") == "/x/y z"
    assert value("chrome\0--user-data-dir\0/x/y z", "--user-data-dir") \
        == "/x/y z"
    assert value("chrome\0--user-data-dir=", "--user-data-dir") == ""
    assert value("chrome\0--user-data-dir", "--user-data-dir") == ""
    assert value("chrome", "--user-data-dir") == ""
    # the legacy space-joined form is still parsed
    assert value("chrome --remote-debugging-port=0",
                 "--remote-debugging-port") == "0"


def t_cli_argv_is_strict() -> None:
    """A refused argv must never reach the service — a tripwire, not a hope.

    The services are replaced with a boom: the run that found the multi-URL
    support (2026-09-18) started a real browser on the default profile root
    because an argv the parser was supposed to refuse turned out to be legal.
    """

    def boom(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("an argv that must be refused reached the service")

    originals = (cli_main.launch, cli_main.new_tab, cli_main.close_tabs,
                 cli_main.stop)
    (cli_main.launch, cli_main.new_tab, cli_main.close_tabs,
     cli_main.stop) = (boom, boom, boom, boom)   # type: ignore[assignment]
    try:
        rc, _out, err = run_cli(["frobnicate"])
        assert rc == 2 and "ERR[unknown-command]" in err, (rc, err)
        for argv in (["open", "--workspace", "1"],
                     ["open", "https://a", "-x"],
                     ["close", "now"],
                     ["close", "--browser", "x", "now"],
                     ["tab", "-x"],
                     ["tab", "list", "extra"],
                     ["tab", "info"],
                     ["tab", "info", "a", "b"],
                     ["info", "extra"],
                     ["list", "extra"]):
            rc, _out, err = run_cli(argv)
            assert rc == 2, (argv, rc)
            assert "ERR[bad-args]" in err, (argv, err)
        rc, _out, err = run_cli([])
        assert rc == 2 and "ERR[bad-args]" in err, (rc, err)
        rc, out, _err = run_cli(["--help"])
        assert rc == 0 and out.startswith("usage: browser-control-cli"), out
    finally:
        (cli_main.launch, cli_main.new_tab, cli_main.close_tabs,
         cli_main.stop) = originals              # type: ignore[assignment]


def t_selftest() -> None:
    rc, out, err = run_cli(["selftest"])
    assert rc == 0, (rc, err)
    data = json.loads(out)
    assert data["ok"] is True and data["command"] == "browser-control-cli", data
    for verb in ("open", "close", "list", "info", "tab", "selftest"):
        assert verb in data["verbs"], data["verbs"]
    assert data["version"] and data["python"], data
    # the one thing selftest must FAIL on: without websockets no verb can
    # speak CDP, and an install that cannot reach a browser should say so at
    # once rather than at the first `tabs`
    original = cli_main.cdp.websockets
    cli_main.cdp.websockets = None
    try:
        rc, _out, err = run_cli(["selftest"])
        assert rc == 2, (rc, err)
        assert "ERR[no-websockets]" in err, err
    finally:
        cli_main.cdp.websockets = original


def t_a_working_log_makes_no_scratch_dirs() -> None:
    """No scratch directory is created when the configured log works.

    The fallback is a second CHANCE, not a first move. Built eagerly it made
    one empty directory per CLI invocation — measured: 92 of them in three
    minutes of battery runs — which is exactly the clutter a tool must not
    make for itself.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # the AUDIT scratch shape (`browser-control-<stamp>-<rand>`), not the bare
    # `browser-control-*`: the battery's throwaway roots match that glob too,
    # and a concurrent battery run made this check fail for the wrong reason
    # (a review flagged it)
    pattern = os.path.join(
        tempfile.gettempdir(),
        "browser-control-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-"
        "[0-9][0-9][0-9][0-9][0-9][0-9]-*")
    before = set(glob.glob(pattern))
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ,
               "BROWSER_CONTROL_LOG": os.path.join(tmp, "actions.jsonl"),
               "BROWSER_CONTROL_ROOT": os.path.join(tmp, "profiles")}
        proc = subprocess.run([sys.executable,
                               os.path.join(repo, "browser-control-cli"),
                               "selftest"], capture_output=True, text=True,
                              timeout=60, env=env)
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        log = os.path.join(tmp, "actions.jsonl")
        assert os.path.exists(log), "a writable log must be written"
        rows = [json.loads(line)
                for line in Path(log).read_text(encoding="utf-8").splitlines()]
        assert [row["action"] for row in rows] == ["selftest"], rows
    made = sorted(set(glob.glob(pattern)) - before)
    assert not made, f"a working log created a scratch directory: {made}"


def t_action_log_lands_on_disk() -> None:
    """The log really is written, and its directory is made for it.

    This is the check the fail-open `off` in the suite's own env was hiding:
    `DEFAULT_LOG`'s directory did not exist, so every write to the default path
    was dropped without a word.
    """
    path = str(audit.LOG.path())
    assert path, "this check needs the log on"
    run_cli(["selftest"])                      # one more invocation, one line
    with open(path, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    assert rows, f"{path} has no lines"
    assert any(row.get("action") == "selftest" for row in rows), rows[-2:]
    assert all(row.get("src") == "browser-control-cli" for row in rows), \
        rows[-1]
    # a default path whose directory does not exist yet is created, not lost
    with tempfile.TemporaryDirectory() as tmp:
        deep = os.path.join(tmp, "state", "browser-control", "actions.jsonl")
        os.environ["BROWSER_CONTROL_LOG"] = deep
        try:
            audit.LOG.write(action="open", ok=True, args=["about:blank"])
        finally:
            os.environ["BROWSER_CONTROL_LOG"] = SUITE_LOG
        with open(deep, encoding="utf-8") as handle:
            line = json.loads(handle.read().splitlines()[0])
        assert line["action"] == "open", line
    # and the scratch directory is the tool's own name, under the temp dir
    scratch = audit.scratch_dir()
    assert scratch.startswith(tempfile.gettempdir() + os.sep), scratch
    assert os.path.basename(scratch).startswith("browser-control-"), scratch
    assert os.path.isdir(scratch), scratch


def t_endpoint_ownership() -> None:
    """The port is checked against the KERNEL, not against a file.

    A listener of THIS process stands in for the stale-port case: a plain HTTP
    server where the profile says its browser is. The guard must say so, the
    drive must refuse with the mundane advice — and no browser is involved.
    """
    class Quiet(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:                            # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = int(server.server_address[1])
    try:
        owner = cdp.listener_of(port)
        assert owner.get("pid") == os.getpid(), owner      # this very process
        assert owner.get("exe"), owner
        with tempfile.TemporaryDirectory() as tmp:
            profile = os.path.join(tmp, "google-chrome-stable")
            os.makedirs(profile)
            verdict = browser.endpoint_owner(profile, port)
            assert verdict["verified"] is False, verdict
            assert verdict["pid"] == os.getpid(), verdict
            assert "not a Chromium-family browser" in verdict["reason"], verdict
            assert verdict["profile_pid"] == 0, verdict
            # the refusal a drive gets, with the advice that is true here
            rows = [{"pid": 4242, "exe": "python3", "profile": profile,
                     "managed": True, "attached": False,
                     "cdp": {"port": port, "reachable": True,
                             "verified": False,
                             "reason": verdict["reason"]}}]
            try:
                browser._drive_refusal("", rows)                # noqa: SLF001
            except ControlError as e:
                assert e.code == "cdp-not-local", e
                assert "close --force" in e.message, e.message
                assert "Nothing was sent" in e.message, e.message
            else:
                raise AssertionError("an unverified endpoint was not refused")
    finally:
        server.shutdown()
        server.server_close()
    # nothing holds a port that was just closed: {}
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = int(probe.getsockname()[1])
    assert cdp.listener_of(free) == {}, free
    assert cdp.listener_of(0) == {}, "port 0 is not a question"


def t_capability_surface() -> None:
    """Every verb is CLASSIFIED, in a closed vocabulary, and `selftest` says so.

    This is the plan's *Kind* column turned into something a policy gate can
    read: the check and the runtime answer come from the same function, so a
    verb added without a class fails here AND shows up in the reply.
    """
    assert capabilities.unclassified(
        cli_main.HANDLERS,
        {"tab": cli_main.TAB_SUBCOMMANDS,
         "profile": cli_main.PROFILE_SUBCOMMANDS}) == [], \
        capabilities.unclassified(cli_main.HANDLERS,
                                  {"tab": cli_main.TAB_SUBCOMMANDS,
                                   "profile": cli_main.PROFILE_SUBCOMMANDS})
    for action, classes in capabilities.ACTIONS.items():
        assert classes, f"{action} has no class"
        for name in classes:
            assert name in capabilities.CLASSES, (action, name)
        top = action.split()[0]
        assert top in cli_main.HANDLERS, action
        parts = action.split()
        if top == "tab" and len(parts) > 1:
            assert parts[1] in cli_main.TAB_SUBCOMMANDS, action
    # the classes a caller would guess, including the ones a MODE decides
    assert capabilities.ACTIONS["tab js"] == ("code", "write")
    assert capabilities.ACTIONS["tab text"] == ("read",)
    assert capabilities.ACTIONS["tab extract"] == ("read",)
    assert capabilities.ACTIONS["tab screenshot"] == ("read", "file")
    assert capabilities.ACTIONS["tab upload"] == ("write", "file")
    assert capabilities.ACTIONS["profile info"] == ("read",)
    assert capabilities.ACTIONS["profile seed"] == ("write", "file")
    assert capabilities.ACTIONS["profile reset"] == ("write",)
    assert capabilities.ACTIONS["tab dialog state"] == ("read",)
    assert capabilities.ACTIONS["tab dialog accept"] == ("write",)
    assert capabilities.ACTIONS["tab wait"] == ("read",)
    assert capabilities.ACTIONS["tab wait --for js"] == ("code",)
    assert capabilities.ACTIONS["selftest"] == ("read",)
    # and the reply a caller gets is the table the library declares
    rc, out, err = run_cli(["selftest"])
    assert rc == 0, (rc, err)
    caps = json.loads(out)["capabilities"]
    assert caps["classes"] == list(capabilities.CLASSES), caps["classes"]
    assert caps["unclassified"] == [], caps
    assert caps["by_class"]["code"] == ["tab js", "tab wait --for js"], \
        caps["by_class"]["code"]
    assert caps["by_class"]["write"] == sorted(
        action for action, classes in capabilities.ACTIONS.items()
        if "write" in classes), caps["by_class"]["write"]
    assert caps["by_class"]["file"] == ["profile seed", "tab screenshot",
                                        "tab upload"], caps["by_class"]["file"]
    assert caps["by_class"]["egress"] == [], caps["by_class"]["egress"]


def t_lock_serializes_a_check_then_act() -> None:
    """A held lock makes the second caller wait, then refuse by NAME.

    In one process on purpose: `flock` belongs to the open file DESCRIPTION,
    so two `open` calls on one path conflict even from here — which is what
    makes this hermetic, and why flock was chosen over an `O_EXCL` file (the
    kernel drops it when the holder dies, so there is no stale lock).
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "lock")
        with browser._lock(path, "open", wait=0.0) as first:      # noqa: SLF001
            assert first == {"held": True, "warning": ""}, first
            try:
                with browser._lock(path, "tab", wait=0.3):        # noqa: SLF001
                    raise AssertionError("a held lock was taken")
            except ControlError as e:
                assert e.code == "profile-busy", e
                assert str(os.getpid()) in e.message, e.message
                assert "(open)" in e.message, e.message    # names the holder
        # free again, with nothing to clean up: the lock file may stay
        with browser._lock(path, "open", wait=0.0) as again:      # noqa: SLF001
            assert again["held"] is True, again
        assert os.path.exists(path), path
        # a path that cannot be opened at all is a WARNING, never a failure:
        # a guard that silently does nothing would be worse than none
        with browser._lock("/proc/nope/lock", "open") as broken:  # noqa: SLF001
            assert broken["held"] is False, broken
            assert broken["warning"], broken


def t_policy_gate() -> None:
    """The gate: fail closed, name the rule, and never block its own answer.

    Module state on purpose — the policy is per PROCESS, set by the CLI from
    `--allow`/`--deny` or the environment — so this restores it afterwards.
    """
    original = dict(capabilities.POLICY)
    try:
        for name in (capabilities.ALLOW_ENV, capabilities.DENY_ENV):
            os.environ.pop(name, None)
        # no policy: everything the declared surface holds is allowed
        capabilities.policy()
        assert capabilities.allowed("tab js")[0] is True
        # an allow-list: EVERY class of the action has to be in it
        capabilities.policy("read,write", None)
        assert capabilities.allowed("open")[0] is True          # write
        assert capabilities.allowed("tab js")[0] is False       # code+write
        assert capabilities.allowed("tab upload")[0] is False   # write+file
        # a deny-list: any one class is enough to block
        capabilities.policy(None, "code")
        blocked, why = capabilities.allowed("tab wait --for js")
        assert blocked is False and "denied" in why, why
        assert capabilities.allowed("tab wait")[0] is True
        # `*` means every class; an unknown class is REFUSED, not ignored
        capabilities.policy("*", None)
        assert capabilities.allowed("tab js")[0] is True
        refusal(lambda: capabilities.policy("nonsense", None), "bad-args")
        # something nobody classified is refused rather than waved through
        blocked, why = capabilities.allowed("tab frobnicate")
        assert blocked is False and "declared surface" in why, why
        # the environment is the default source; a flag names ONE side, and it
        # may only NARROW what the environment set for the session (the bugs
        # this replaced: `--deny X` voided a host's `BROWSER_CONTROL_ALLOW=read`
        # whitelist, and — mirrored — `--deny egress` replaced a host's deny-list
        # so `tab js` ran while `BROWSER_CONTROL_DENY=code` stood)
        os.environ[capabilities.DENY_ENV] = "code"
        assert capabilities.policy()["source"] == capabilities.DENY_ENV
        both = capabilities.policy("read", None)
        assert both["source"] == f"--allow + {capabilities.DENY_ENV}", both
        assert both["allow"] == ("read",) and both["deny"] == ("code",), both
        # the argv route, in-process: the gate refuses, `selftest` never is
        rc, _out, err = run_cli(["tab", "text", "--deny", "read"])
        assert rc == 2 and "ERR[not-allowed]" in err, (rc, err)
        rc, out, _err = run_cli(["selftest", "--deny", "read"])
        assert rc == 0, (rc, out)
        policy = json.loads(out)["policy"]
        # the flag ADDS to the environment's list rather than replacing it
        assert policy["deny"] == ["read", "code"], policy
        assert policy["enforced"] is True, policy
        rc, _out, err = run_cli(["tab", "text", "--allow", "nonsense"])
        assert rc == 2 and "ERR[bad-args]" in err, (rc, err)
    finally:
        for name in (capabilities.ALLOW_ENV, capabilities.DENY_ENV):
            os.environ.pop(name, None)
        capabilities.POLICY.update(original)
    # `list` reports the machine, so it refuses a scope instead of dropping it
    outside = os.path.join(tempfile.gettempdir(),
                           "browser-control-outside-profile")
    rc, _out, err = run_cli(["list", "--profile", outside])
    assert rc == 2 and "ERR[bad-args]" in err, (rc, err)
    # which DECLARED action a call is: the mode decides, where a mode exists
    assert cli_main.action("tab", ["wait", "--for", "js"]) == \
        "tab wait --for js"
    assert cli_main.action("tab", ["wait", "--for", "load"]) == "tab wait"
    assert cli_main.action("tab", ["dialog", "accept"]) == \
        "tab dialog accept"
    assert cli_main.action("tab", ["dialog"]) == "tab dialog state"
    assert cli_main.action("tab", ["media", "play"]) == "tab media play"
    assert cli_main.action("tab", ["media"]) == "tab media state"
    assert cli_main.action("tab", ["text"]) == "tab text"
    assert cli_main.action("tab", ["https://x.example"]) == "tab"
    assert cli_main.action("profile", ["seed"]) == "profile seed"
    assert cli_main.action("open", []) == "open"


def t_gate_and_argv_hardening() -> None:
    """The gate and argv, after a review measured three ways past them.

    Every case here was a REAL failure before it was a check: an argv made only
    of global flags crashed with an IndexError, a mode spelled `--for JS` was
    authorised as a read and then ran caller code, an empty `--allow` meant
    "allow everything", and `--tab ""` acted on a tab nobody named.
    """
    # 1. a verb is required, and it is a REFUSAL — not a traceback, not exit 1
    for argv in (["--allow", "read"], ["--browser=chrome"], ["--frame", "0"],
                 ["--profile", tempfile.gettempdir(), "--deny", "code"]):
        rc, out, err = run_cli(argv)
        assert rc == 2, (argv, rc)
        assert "ERR[bad-args]" in err and "verb is required" in err, (argv, err)
        assert "Traceback" not in err and out == "", (argv, out, err)
    # 2. a mode is resolved the way the VERB resolves it — case, whitespace,
    #    and a repeated flag (the last one wins, which is what `_pop` leaves),
    #    and a `--tab` whose VALUE is literally `--for` is not the mode
    assert cli_main.action("tab", ["wait", "--tab", "--for", "--for", "js"]) \
        == "tab wait --for js"
    # …and a `--tab` whose VALUE is a mode flag must not be read as the mode: the
    # handler pops `--tab` FIRST, so the gate has to as well (a review measured
    # this argv running `js` while the gate had authorised `tab wait`, a read)
    assert cli_main.action("tab", ["wait", "--for", "js", "--expr", "1",
                                    "--tab", "--for=load", "--tab", "id:0"]) \
        == "tab wait --for js"
    rc, _out, err = run_cli(["tab", "wait", "--for", "js", "--expr", "1",
                             "--tab", "--for=load", "--tab", "id:0",
                             "--allow", "read"])
    assert rc == 2 and "ERR[not-allowed]" in err, (rc, err)
    assert cli_main.action("tab", ["wait", "--for", " js "]) == \
        "tab wait --for js"
    assert cli_main.action("tab", ["wait", "--for", "load", "--for", "js"]) \
        == "tab wait --for js"
    assert cli_main.action("tab", ["dialog", "ACCEPT"]) == \
        "tab dialog accept"
    assert cli_main.action("tab", ["media", "PLAY"]) == "tab media play"
    assert cli_main.action("tab", ["media", "--index", "1", "play"]) == \
        "tab media play"
    assert cli_main.action("tab", ["media", "--index", "1"]) == \
        "tab media state"
    # a mode nobody declares is refused HERE, so it is `bad-args` with or
    # without a policy rather than a gate verdict about the wrong action
    refusal(lambda: cli_main.action("tab", ["wait", "--for", "wibble"]),
            "bad-args")
    rc, _out, err = run_cli(["tab", "wait", "--for", "wibble", "--allow",
                             "read"])
    assert rc == 2 and "ERR[bad-args]" in err, (rc, err)
    # END TO END: a read-only policy cannot authorise the write that `--for JS`
    # becomes — and the refusal happens before any browser is looked for
    rc, _out, err = run_cli(["tab", "wait", "--for", "JS", "--expr", "1",
                             "--allow", "read"])
    assert rc == 2 and "ERR[not-allowed]" in err, (rc, err)
    #   …and the POSITIONAL modes too, through the same route (a break in
    #   `resolved_mode` for a positional mode would otherwise pass the suite)
    for argv in (["tab", "dialog", "accept", "--allow", "read"],
                 ["tab", "dialog", "ACCEPT", "--allow", "read"],
                 ["tab", "media", "PLAY", "--allow", "read"]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[not-allowed]" in err, (argv, rc, err)
    # 3. an empty policy VALUE is refused instead of read as "no policy"
    for argv in (["list", "--allow", ""], ["list", "--deny", ""]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)
    # 4. the two sides of the policy are resolved INDEPENDENTLY: a `--deny`
    #    cannot void the environment's allow-list (measured: it did)
    original = dict(capabilities.POLICY)
    saved = {name: os.environ.get(name)
             for name in (capabilities.ALLOW_ENV, capabilities.DENY_ENV)}
    try:
        os.environ[capabilities.ALLOW_ENV] = "read"
        capabilities.policy(None, "egress")
        allowed, why = capabilities.allowed("tab js")
        assert allowed is False and "not allowed" in why, why
        described = capabilities.describe()
        assert described["source"] == f"{capabilities.ALLOW_ENV} + --deny", \
            described
        assert described["enforced"] is True, described
        # …and the MIRROR: the env-DENY side survives a `--deny` naming something
        # else. The first version of this fix still let that through (a review
        # measured `BROWSER_CONTROL_DENY=code` + `--deny egress` running js)
        os.environ.pop(capabilities.ALLOW_ENV, None)
        os.environ[capabilities.DENY_ENV] = "code"
        rc, _out, err = run_cli(["tab", "js", "1", "--deny", "egress"])
        assert rc == 2 and "ERR[not-allowed]" in err, (rc, err)
        # an ENVIRONMENT value that names no class is refused exactly like the
        # flag (it used to read as "no policy" and switch the gate OFF) — on
        # EITHER side, and even when a valid flag is present, because the blank
        # side is a mistake and not an absence (a review found only the allow
        # side, only without a flag, was pinned)
        for name in (capabilities.ALLOW_ENV, capabilities.DENY_ENV):
            for blank in (" ", "\t"):
                os.environ[name] = blank
                rc, _out, err = run_cli(["list"])
                assert rc == 2 and "ERR[bad-args]" in err, (name, blank, rc, err)
            rc, _out, err = run_cli(["list", "--allow", "read"])
            assert rc == 2 and "ERR[bad-args]" in err, (name, rc, err)
            os.environ.pop(name, None)
        # …and the allow side INTERSECTS. `tab wait --for js` is `code` ONLY, so
        # a flag that REPLACED the environment would let it through; the
        # environment's `read` is what refuses it, and the refusal names both
        # sources (a review showed the earlier assertion could not tell the two
        # behaviours apart)
        os.environ[capabilities.ALLOW_ENV] = "read"
        rc, _out, err = run_cli(["tab", "wait", "--for", "js", "--expr", "1",
                                 "--allow", "code"])
        assert rc == 2 and "ERR[not-allowed]" in err, (rc, err)
        assert "--allow" in err and capabilities.ALLOW_ENV in err, err
        # a flag may NARROW the session allow-list, never widen it
        rc, _out, err = run_cli(["tab", "js", "1", "--allow", "code"])
        assert rc == 2 and "ERR[not-allowed]" in err, (rc, err)
        # …and when the two allow-lists share no class, NOTHING is allowed: an
        # empty allow-list is not "no policy"
        capabilities.policy("write", None)
        assert capabilities.allowed("list")[0] is False, capabilities.describe()
        # a value that NAMES NO CLASS must not switch the gate off
        for argv in (["tab", "js", "1", "--allow", ","],
                     ["list", "--deny", ","]):
            rc, _out, err = run_cli(argv)
            assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        capabilities.POLICY.update(original)
    # 5. an empty value is not "the only tab" (the library refuses an empty
    #    spec, so the CLI has to refuse it too, not hand it one)
    for argv in (["tab", "text", "--tab", ""], ["tab", "activate", ""],
                 ["tab", "nav", ""], ["open", ""]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)
    # 6. `--frame` is refused by every verb it cannot scope (a scope that is
    #    silently dropped is a caller who thinks they asked for something), and
    #    accepted by the ones it can (they then fail for want of a BROWSER)
    for argv in (["tab", "frames", "--frame", "1"],
                 ["list", "--frame", "1"],
                 ["open", "--frame", "1", "https://example.com/"],
                 ["profile", "info", "--frame", "1"],
                 ["tab", "text", "--frame", " ", "--tab", "id:0"]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)
    rc, _out, err = run_cli(["tab", "text", "--frame", "1", "--tab", "id:0"])
    assert rc == 2 and "bad-args" not in err, (rc, err)
    rc, _out, _err = run_cli(["selftest", "--frame", "1"])
    assert rc == 0, rc
    # 7. a typo says what it is, and points at the nearest subcommand
    rc, _out, err = run_cli(["tab", "frams"])
    assert rc == 2 and "neither a subcommand" in err and "frames" in err, err
    # 8. an unknown SUBCOMMAND is a typo, not a policy verdict
    rc, _out, err = run_cli(["profile", "bogus", "--allow", "read"])
    assert rc == 2 and "ERR[bad-args]" in err, (rc, err)
    # 9. an unexpected failure is an ERR[code], never a traceback
    keeper = cli_main.HANDLERS["list"]

    def explode(_rest: list[str], _browser: str) -> dict:
        raise RuntimeError("boom")

    try:
        cli_main.HANDLERS["list"] = explode            # type: ignore[assignment]
        rc, _out, err = run_cli(["list"])
        assert rc == 2 and "ERR[internal]: RuntimeError: boom" in err, (rc, err)
        assert "Traceback" not in err, err
    finally:
        cli_main.HANDLERS["list"] = keeper


def t_frames_bind_to_their_tab() -> None:
    """`--frame` resolves to a frame of the TAB it was asked about.

    Measured before this check existed: with two tabs embedding the same widget,
    both resolved to ONE CDP target, so `--frame` typed into the other tab. The
    browser says which tab owns an iframe target (`Target.getTargets` carries
    `parentId`), and these are the rules that read it — plus what happens when
    the browser does NOT say, which must be a refusal rather than a guess.
    """
    census = [{"index": 0, "url": "http://localhost:9/widget.html",
               "name": "", "box": [0, 0, 300, 120], "visible": True,
               "same_process": False}]
    real = {name: getattr(cdp, name) for name in
            ("evaluate", "frame_targets", "frame_rows", "target_ws", "port_of")}
    real_frames_of = dom.frames_of
    cdp.evaluate = lambda ws, expr, timeout=15.0: census    # type: ignore[assignment]
    cdp.port_of = lambda profile: 1234                      # type: ignore[assignment]
    cdp.target_ws = lambda port, target, kind="page": f"ws://{kind}/{target}"  # type: ignore[assignment]
    try:
        # 1. the browser says who owns each target: only THIS tab's frame is
        #    a candidate, and the other tab's target is never reached
        cdp.frame_targets = lambda port: [                  # type: ignore[assignment]
            {"id": "AAA", "url": "http://localhost:9/widget.html",
             "parent": "PAGE_A"},
            {"id": "BBB", "url": "http://localhost:9/widget.html",
             "parent": "PAGE_B"}]
        rows = dom.frames_of(1234, "PAGE_A")
        assert [r["target"] for r in rows] == ["AAA"], rows
        assert rows[0]["candidates"] == 0, rows
        assert dom.frames_of(1234, "PAGE_B")[0]["target"] == "BBB"
        assert dom._frame_target(1234, "PAGE_A", "widget")["target"] == \
            "AAA"                                        # noqa: SLF001
        # …and the other tab's target is never reached even when its id sorts
        # FIRST: the id is a random token, so THIS is the case that tells a
        # parent-scoped match from a URL-only one (a review found the PAGE_A
        # assertion above passed either way by id luck)
        cdp.frame_targets = lambda port: [                  # type: ignore[assignment]
            {"id": "AAA", "url": "http://localhost:9/widget.html",
             "parent": "PAGE_B"},
            {"id": "ZZZ", "url": "http://localhost:9/widget.html",
             "parent": "PAGE_A"}]
        assert dom.frames_of(1234, "PAGE_A")[0]["target"] == "ZZZ"
        assert dom.frames_of(1234, "PAGE_B")[0]["target"] == "AAA"
        # …and a duplicate URL in ONE tab cannot be told apart by URL at all:
        # target ids are random, so pairing by id order can bind the sibling
        cdp.frame_targets = lambda port: [                  # type: ignore[assignment]
            {"id": "AAA", "url": "http://localhost:9/widget.html",
             "parent": "PAGE_A"},
            {"id": "CCC", "url": "http://localhost:9/widget.html",
             "parent": "PAGE_A"}]
        rows = dom.frames_of(1234, "PAGE_A")
        assert rows[0]["target"] == "" and rows[0]["candidates"] == 2, rows
        refusal(lambda: dom._frame_target(1234, "PAGE_A", "widget"),  # noqa: SLF001
                "frame-ambiguous")
        # 2. a browser that does NOT say who owns a target attributes NOTHING:
        #    a URL that appears once is not proof (a cross-origin frame in this
        #    page's own process has no target of its own, while another tab's
        #    single target with that URL looks unique), so `--frame` refuses
        #    rather than driving the wrong tab — a review constructed exactly it
        cdp.frame_targets = lambda port: []                 # type: ignore[assignment]
        rows = dom.frames_of(1234, "PAGE_A")
        assert rows[0]["target"] == "", rows
        assert rows[0]["attribution"] == "unattributable", rows
        refusal(lambda: dom._frame_target(1234, "PAGE_A", "widget"),  # noqa: SLF001
                "frame-unattributable")
        # …and a frame the PAGE says SHARES its process never takes a target,
        # even where the browser does attribute them
        cdp.frame_targets = lambda port: [                  # type: ignore[assignment]
            {"id": "SOLO", "url": "http://localhost:9/widget.html",
             "parent": "PAGE_A"}]
        census.append({"index": 1, "url": "http://localhost:9/widget.html",
                       "name": "", "box": [], "visible": True,
                       "same_process": True})
        rows = dom.frames_of(1234, "PAGE_A")
        assert rows[0]["target"] == "SOLO", rows          # the cross-origin one
        assert rows[1]["target"] == "" and rows[1]["candidates"] == 0, rows
        refusal(lambda: dom._frame_target(1234, "PAGE_A", "1"),  # noqa: SLF001
                "frame-not-separate")
        census.pop()
        # 3. the census names its facts apart, and an unreadable one is an
        #    ERROR — never an empty page (`{}` is "no frames")
        row = {"pid": 4321, "exe": "chrome", "profile": "/profiles/x",
               "managed": True, "attached": False,
               "cdp": {"port": 1515}}
        tab_row = {"id": "PAGE_A"}
        two = [
            # a same-process frame: its document is readable, no target of its own
            {"index": 0, "url": "about:srcdoc", "name": "", "box": [],
             "visible": True, "same_process": True, "target": "",
             "candidates": 0},
            # a cross-origin one: a target of its own, and an unreadable document
            {"index": 1, "url": "u", "name": "", "box": [], "visible": True,
             "same_process": False, "target": "AAA", "candidates": 0}]
        dom.frames_of = lambda port, page, census=None: two  # type: ignore[assignment]
        summary = dom._frame_summary(row, tab_row)           # noqa: SLF001
        assert summary["total"] == 2, summary
        assert summary["separate"] == 1, summary        # has a CDP target
        assert summary["cross_origin"] == 1, summary    # unreadable document
        assert summary["same_process"] == 1, summary
        assert "frame(s)" in dom._frames_note(row, tab_row)  # noqa: SLF001
        # a browser that attributes NOTHING reports `separate: NULL`, never 0:
        # "none of them can be driven" is a claim the reply cannot support (a
        # review found `tab frames` saying 0 while `--frame` refused
        # `frame-unattributable`)
        dom.frames_of = lambda port, page, census=None: [    # type: ignore[assignment]
            {"index": 0, "url": "u", "name": "", "box": [], "visible": True,
             "same_process": False, "target": "",
             "attribution": "unattributable", "candidates": 0}]
        listed = dom.frames(row, tab_row)                     # noqa: SLF001
        assert listed["separate"] is None, listed
        assert "cannot attribute" in listed["note"], listed
        dom.frames_of = lambda port, page, census=None: two  # type: ignore[assignment]

        def explode(port: int, page: str,
                    census: list | None = None) -> list[dict]:
            raise ControlError("cdp-error", "boom")

        dom.frames_of = explode                             # type: ignore[assignment]
        partial = dom._frame_summary(row, tab_row)           # noqa: SLF001
        # the DOM census answered and the TARGET list did not: the frames are
        # reported and `separate` is NULL — never 0, which would claim that none
        # of them can be driven
        assert partial["total"] == 1 and partial["separate"] is None, partial
        assert "error" not in partial, partial
        # an unreadable CENSUS is an error instead — never an empty page
        patched = cdp.evaluate

        def no_census(ws: str, expression: str,
                      timeout: float = 15.0) -> object:
            raise ControlError("cdp-error", "no census")

        cdp.evaluate = no_census                            # type: ignore[assignment]
        try:
            broken = dom._frame_summary(row, tab_row)        # noqa: SLF001
            assert broken["total"] is None and "no census" in broken["error"], \
                broken
            # the note says it too — and it is asserted while the census is
            # still failing, which is the only time that message is the truth
            assert "could not be read" in dom._frames_note(row, tab_row)  # noqa: SLF001
        finally:
            cdp.evaluate = patched                          # type: ignore[assignment]
        # a page with no frames says NOTHING (`{}`), not a zeroed census
        cdp.evaluate = lambda ws, expr, timeout=15.0: []     # type: ignore[assignment]
        try:
            assert dom._frame_summary(row, tab_row) == {}     # noqa: SLF001
            assert dom._frames_note(row, tab_row) == ""       # noqa: SLF001
        finally:
            cdp.evaluate = patched                          # type: ignore[assignment]
    finally:
        dom.frames_of = real_frames_of
        for name, fn in real.items():
            setattr(cdp, name, fn)
    # 4. the RESOLVED frame travels in the reply, and is cleared with the scope:
    #    an index is the page's live order, so "which document did that act in"
    #    cannot be inferred from the argument
    dom.frame("frame.html")
    assert dom.frame_resolved() is None
    dom.FRAME["resolved"] = {"index": 0, "url": "u", "target": "AAA"}
    assert dom.frame_resolved() == {"index": 0, "url": "u", "target": "AAA"}
    assert dom.frame_resolved() is not dom.FRAME["resolved"]     # a COPY
    dom.frame("")
    assert dom.frame_resolved() is None, "the scope was cleared but the frame was not"


def t_frames_and_points() -> None:
    """`--frame`/`--at`: the scope, the point syntax, the verb list.

    Resolving a frame needs a browser (it reads the DOM and `/json`), so that
    oracle is the battery. What is hermetic: the scope is SET AND CLEARED per
    invocation like `--profile`, a point is two numbers with a refusal that
    names the verb that asked, and only the verbs that can be scoped say so.
    """
    rc, _out, _err = run_cli(["selftest", "--frame", "localhost"])
    assert rc == 0 and dom.frame() == "localhost", dom.frame()
    rc, _out, _err = run_cli(["selftest"])          # no flag: CLEARED again
    assert rc == 0 and dom.frame() == "", dom.frame()
    assert dom._at_point("10,20") == (10, 20)          # noqa: SLF001
    assert dom._at_point(" 10, 20 ") == (10, 20)       # noqa: SLF001
    assert dom._at_point("-5,3") == (-5, 3)            # noqa: SLF001
    # the RANGE refusal names the verb that asked (it said "tab scroll" for all
    # three — a review flagged it)
    try:
        dom._point("9999,9999", [100, 100], "tab click")     # noqa: SLF001
    except ControlError as e:
        assert "tab click" in e.message, e.message
    else:
        raise AssertionError("a point outside the viewport must refuse")
    for bad in ("10", "x,y", "10,", ",20", ""):
        refusal(lambda bad=bad: dom._at_point(bad, "tab click"),  # noqa: SLF001
                "bad-args")
    calls: list[tuple] = []

    def fake_click(text: str | None = None, selector: str | None = None,
                   index: int | None = None, tab: str = "",
                   browser: str = "", at: str | None = None) -> dict:
        calls.append((text, selector, at))
        return {"ok": True}

    original = cli_main.dom.click
    cli_main.dom.click = fake_click                   # type: ignore[assignment]
    try:
        rc, _out, err = run_cli(["tab", "click", "--at", "10,20"])
        assert rc == 0 and calls == [(None, None, "10,20")], (rc, calls, err)
        for argv in (["tab", "click", "--at", "10,20", "--selector", "#x"],
                     ["tab", "click", "Save", "--at", "10,20"],
                     ["tab", "click"]):
            rc, _out, err = run_cli(argv)
            assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)
        assert calls == [(None, None, "10,20")], calls
    finally:
        cli_main.dom.click = original                 # type: ignore[assignment]
    # a point that is not two numbers refuses BEFORE any browser is touched
    # (the real `dom.click`, so the real `_at_point` judgement runs)
    rc, _out, err = run_cli(["tab", "click", "--at", "10"])
    assert rc == 2 and "ERR[bad-args]" in err, (rc, err)
    rc, _out, err = run_cli(["tab", "hover", "--at", "x,y"])
    assert rc == 2 and "ERR[bad-args]" in err, (rc, err)
    # the verbs that CLAIM a frame have to be tab subcommands, and the ones that
    # drive the TAB must not claim one
    for name in sorted(dom.FRAME_VERBS):
        assert name in cli_main.TAB_SUBCOMMANDS, name
    for name in ("nav", "list", "info", "close", "frames", "activate"):
        assert name not in dom.FRAME_VERBS, name


def t_pid_alive() -> None:
    assert browser._pid_alive(os.getpid()) is True                 # noqa: SLF001
    assert browser._pid_alive(999999) is False                     # noqa: SLF001


def t_transport_against_a_fake_peer() -> None:
    """The websocket path, against a peer that misbehaves on purpose.

    Every branch that carries CDP traffic AFTER the HTTP read had no test at
    all — framing, the reply-for-another-id filter, error envelopes, and the
    parked rule — and neither did `_checked_ws`, the one guard between this tool
    and a non-loopback endpoint that would receive every click, keystroke and
    uploaded file (`plan.md` §5 tier 2 promised this peer).

    `websockets.sync.server` runs the peer in a thread, so the suite stays
    synchronous and hermetic: nothing leaves 127.0.0.1.
    """
    from websockets.sync.server import serve

    seen: list[dict] = []

    def handler(connection: object) -> None:
        send = connection.send                    # type: ignore[union-attr]
        for raw in connection:                    # type: ignore[union-attr]
            message = json.loads(raw)
            seen.append(message)
            mid, method = message.get("id"), message.get("method")
            if method == "Page.enable":
                # a PROTOCOL refusal: it must not be recorded as "parked"
                send(json.dumps({"id": mid, "error": {
                    "code": -32000, "message": "enabling is not allowed"}}))
            elif method == "Runtime.evaluate":
                # a reply for ANOTHER id first: the caller must ignore it
                send(json.dumps({"id": 9999, "result": {
                    "result": {"type": "number", "value": -1}}}))
                send(json.dumps({"id": mid, "result": {
                    "result": {"type": "number", "value": 42}}}))
            elif method == "Nonsense":
                send("this is not JSON")
            else:
                send(json.dumps({"id": mid, "result": {}}))

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        # websockets 16 wants an explicit `serve_forever` (the context manager
        # only guarantees the close), so the peer runs on a daemon thread
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        url = f"ws://127.0.0.1:{port}/devtools/page/FAKE"
        try:
            # 1. the host check, which no other test in either suite exercises:
            #    it lives in `evaluate`/`Session` (the entry points every verb
            #    uses), not in the raw `call` primitive
            refusal(lambda: cdp.evaluate("ws://evil.example/x", "1"),
                    "cdp-not-local")
            refusal(lambda: cdp.evaluate("wss://127.0.0.1.evil/x", "1"),
                    "cdp-not-local")
            refusal(lambda: cdp.evaluate("ws://user@evil.example/x", "1"),
                    "cdp-not-local")
            # 2. a reply for another id does not become the answer
            result = cdp.call(url, "Runtime.evaluate", {"expression": "1"})
            assert result["result"]["value"] == 42, result
            # 3. a non-JSON frame is a refusal, not a decode traceback
            refusal(lambda: cdp.call(url, "Nonsense"), "cdp-error")
            # 4. a PROTOCOL refusal leaves the session usable and NOT parked:
            #    only a blocked `Page.enable` means "this tab is parked"
            with cdp.Session(url, page_domain=True) as session:
                # the session connects LAZILY, so the refusal has to be caused
                # before it can be judged — and the point of the case is that
                # the session still answers afterwards
                assert session.evaluate("1") == 42
                assert session.page_domain_ok is False, session.__dict__
                assert session.parked is False, "a protocol error is not a dialog"
        finally:
            server.shutdown()
            serving.join(timeout=5)
    # what the peer was actually asked, in order: the id filter, the non-JSON
    # frame, `Page.enable` (refused), and the evaluate that still answered
    assert [m.get("method") for m in seen] == [
        "Runtime.evaluate", "Nonsense", "Page.enable",
        "Runtime.evaluate"], seen


def t_click_presses_at_the_proven_point() -> None:
    """`tab click` presses at `hit_at` — the point the HIT-TEST proved.

    Measured before the fix: the probe CLAMPED into the viewport and the press
    did not, so an element whose centre is below the fold was TESTED inside it
    and PRESSED outside, while the reply said `clicked: true`. A fake session
    records what actually crossed `Input.dispatchMouseEvent`.
    """
    events: list[dict] = []

    class FakeSession:
        def __enter__(self) -> FakeSession:
            return self

        def __exit__(self, exc_type: object, exc: object,
                     tb: object) -> None:
            return None

        def evaluate(self, expression: str, timeout: float = 0.0) -> object:
            return {"url": "u", "title": "t", "active": "body",
                    "scroll": [0, 0], "x": 0, "y": 0}

        def call(self, method: str, params: dict | None = None,
                 timeout: float = 0.0) -> dict:
            events.append({"method": method, **(params or {})})
            return {}

    real = (dom._resolve, dom._session, dom._matches_in, dom._under_point)
    fake_row = {"pid": 4321, "exe": "chrome", "profile": "/profiles/x",
                "managed": True, "attached": False,
                "cdp": {"port": 1515}}
    dom._resolve = lambda tab, browser, for_write: (     # type: ignore[assignment]
        fake_row, {"id": "TAB", "url": "u"})
    dom._session = lambda row, tab_row: FakeSession()    # type: ignore[assignment]
    dom._matches_in = lambda session, needle, css, cap: {  # type: ignore[assignment]
        "matches": [{"tag": "button", "box": [10, 20, 30, 40],
                     "point": [999, 999], "hit_at": [12, 34],
                     "in_viewport": True, "hit": True,
                     "hit_element": "button#x", "text": "x"}],
        "title": "t", "url": "u", "ready": "complete", "total": 1,
        "offscreen": 0, "active": "body", "scroll": [0, 0]}
    dom._under_point = lambda session, x, y: "button#x"  # type: ignore[assignment]
    try:
        reply = dom.click(selector="#x")
    finally:
        (dom._resolve, dom._session, dom._matches_in,
         dom._under_point) = real
    pressed = [e for e in events if e.get("type") == "mousePressed"]
    assert pressed and (pressed[0]["x"], pressed[0]["y"]) == (12, 34), events
    assert reply["point"] == [12, 34], reply
    assert reply["under"] == "button#x", reply


def _restore_root(keep: str | None) -> None:
    """Put BROWSER_CONTROL_ROOT back the way the caller found it."""
    if keep is None:
        os.environ.pop("BROWSER_CONTROL_ROOT", None)
    else:
        os.environ["BROWSER_CONTROL_ROOT"] = keep


def t_audit_redaction_beats_truncation() -> None:
    """A secret longer than ARG_CAP is redacted BEFORE the cap truncates it.

    The order was `_censor(_brief(...))`: a 5000-char secret became
    `secret[:4096] + marker`, which no longer CONTAINED the secret, so
    `_censor` passed it through and the first 4096 characters went to disk
    still marked `redacted: true` (a review measured it).
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "actions.jsonl")
        os.environ["BROWSER_CONTROL_LOG"] = path
        try:
            secret = "S" * 5000
            audit.LOG.begin("tab")
            audit.LOG.mark_secret(secret)
            audit.LOG.write(action="tab", ok=True, args=["insert", secret])
        finally:
            os.environ["BROWSER_CONTROL_LOG"] = SUITE_LOG
            audit.LOG.begin("")
        with open(path, encoding="utf-8") as handle:
            line = handle.read().strip()
        assert "S" * 64 not in line, "a prefix of the secret reached the log"
        assert json.loads(line)["redacted"] is True, line
        assert "<redacted: 5000 chars>" in line, line


def t_audit_short_write_is_not_success() -> None:
    """`os.write` may write fewer bytes; that is not a complete line."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "actions.jsonl")
        os.environ["BROWSER_CONTROL_LOG"] = path
        real_write = os.write

        def half(fd: int, data: bytes) -> int:
            return real_write(fd, data[:max(1, len(data) // 2)])

        try:
            os.write = half                      # type: ignore[assignment]
            audit.LOG.begin("tab")
            audit.LOG.write(action="tab", args=["x"])
        finally:
            os.write = real_write                # type: ignore[assignment]
            os.environ["BROWSER_CONTROL_LOG"] = SUITE_LOG
            audit.LOG.begin("")
        with open(path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle]
        assert len(rows) == 1 and rows[0]["action"] == "tab", rows


def t_http_read_never_uses_a_proxy() -> None:
    """`http_proxy` must not divert a loopback CDP read.

    `build_opener(_LoopbackOnly)` kept urllib's environment-driven
    ProxyHandler, so the answer parsed as CDP JSON came from the proxy — for a
    DEAD port, too (a review measured it). With `ProxyHandler({})` the dead
    port is a plain `cdp-unreachable` and the proxy is never asked.
    """
    server, proxy_port = _fake_endpoint([])
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead = probe.getsockname()[1]
        keep = {name: os.environ.pop(name)
                for name in ("http_proxy", "HTTP_PROXY", "no_proxy",
                             "NO_PROXY")
                if name in os.environ}
        try:
            os.environ["http_proxy"] = f"http://127.0.0.1:{proxy_port}"
            refusal(lambda: cdp._get_bytes(f"http://127.0.0.1:{dead}/json"),
                    "cdp-unreachable")
        finally:
            os.environ.update(keep)
    finally:
        server.shutdown()
        server.server_close()


def t_transport_typed_refusals() -> None:
    """A malformed endpoint URL is `cdp-not-local`, not a raw ValueError."""
    refusal(lambda: cdp._checked_ws("ws://[::1/x"), "cdp-not-local")
    # a page's exception text is one bounded, escape-free line
    result = {"exceptionDetails": {"exception": {
        "description": "\x1b]52;c;AAAA\x07\n" + "A" * 5000}}}
    try:
        cdp._value_of(result)
        raise AssertionError("expected ERR[js-error]")
    except ControlError as e:
        assert e.code == "js-error", e.code
        assert "\x1b" not in e.message and "\n" not in e.message, e.message
        assert len(e.message) <= 300, len(e.message)


def t_text_cap_counts_characters() -> None:
    """A CJK page is not refused because `json.dumps` escapes it 6x."""
    value = "中" * 20_000
    got = cdp._value_of({"result": {"value": json.dumps(
        {"text": value}, ensure_ascii=False)}})
    assert got["text"] == value


def t_wait_own_port_requires_a_verified_owner() -> None:
    """An answering endpoint that is not ours must not satisfy `open`."""
    server, port = _fake_endpoint([
        {"type": "page", "id": "S", "title": "stranger",
         "url": "https://stranger/"}])
    try:
        with tempfile.TemporaryDirectory() as profile:
            Path(profile, cdp.PORT_FILE).write_text(
                f"{port}\n/devtools/browser/x\n")
            assert browser._wait_port(profile, timeout=0.5) is True, \
                "the fake endpoint answers"
            assert browser._wait_own_port(profile, timeout=0.5) is False, \
                "answering is not owning"
    finally:
        server.shutdown()
        server.server_close()


def t_tab_count_does_not_refuse_on_a_stranger() -> None:
    """The read-back after a close sums what verified; it never refuses."""
    row = {"pid": 1, "exe": "chrome", "path": "/x",
           "profile": os.path.join(tempfile.gettempdir(), "stranger"),
           "profile_from": "", "managed": False, "attached": False,
           "cdp": {"port": 9222, "reachable": True, "verified": False,
                   "reason": "not ours"}}
    real = browser.browsers
    browser.browsers = lambda: [row]              # type: ignore[assignment]
    try:
        assert browser._tab_count() == 0
    finally:
        browser.browsers = real                   # type: ignore[assignment]


def t_unreadable_tab_list_is_not_absence() -> None:
    """`close` must not read "cannot read the list" as "the ids are gone"."""
    with tempfile.TemporaryDirectory() as profile:
        Path(profile, cdp.PORT_FILE).write_text("1515\n")

        def boom(path: str) -> list[dict]:
            raise ControlError("cdp-error", "the endpoint answered no JSON")

        real = browser._rows
        browser._rows = boom                      # type: ignore[assignment]
        try:
            refusal(lambda: browser._wait_ids_gone(profile, ["A"],
                                                   timeout=0.1),
                    "close-tab-not-verified")
        finally:
            browser._rows = real                  # type: ignore[assignment]


def t_page_titles_are_flat_in_refusals() -> None:
    """A page title cannot forge stderr lines or inject escapes."""
    rows = [{"id": "A", "title": "a\nERR[fake]", "url": "https://a/"},
            {"id": "B", "title": "a\x1b]52;c;AAAA\x07", "url": "https://a/"}]
    try:
        browser.resolve_tab(rows, "a")
        raise AssertionError("expected ERR[tab-ambiguous]")
    except ControlError as e:
        assert e.code == "tab-ambiguous", e.code
        assert "\n" not in e.message and "\x1b" not in e.message, e.message


def t_profile_symlink_target_is_refused() -> None:
    """seed/reset never write or wipe THROUGH a symlinked target."""
    root = tempfile.mkdtemp(prefix="browser-control-hermetic-")
    keep_root = os.environ.get("BROWSER_CONTROL_ROOT")
    os.environ["BROWSER_CONTROL_ROOT"] = root
    try:
        real = os.path.join(root, "real")
        os.makedirs(real)
        link = os.path.join(root, "link")
        os.symlink(real, link)
        source = os.path.join(root, "source")
        os.makedirs(source)
        Path(source, "Cookies").write_text("login", encoding="utf-8")
        refusal(lambda: profile_lib.seed(source=source, profile=link,
                                         force=True), "not-managed")
        refusal(lambda: profile_lib.reset(profile=link, force=True),
                "not-managed")
        refusal(lambda: profile_lib.info(profile=tempfile.gettempdir()),
                "not-managed")
        assert not os.path.exists(os.path.join(real, "Cookies")), \
            "a seed wrote through the symlink"
    finally:
        _restore_root(keep_root)


def t_seed_lands_where_chrome_reads() -> None:
    """A profile dir seeds into <instance>/Default; a user-data dir into root.

    Chrome reads `<user-data-dir>/Default`, so a profile directory written to
    the instance ROOT was a profile the browser never read — cookies and all
    (found in use: a "seeded" browser showed a login wall while the real
    login sat one directory above the profile Chrome opened).
    """
    root = tempfile.mkdtemp(prefix="browser-control-hermetic-")
    keep_root = os.environ.get("BROWSER_CONTROL_ROOT")
    os.environ["BROWSER_CONTROL_ROOT"] = root
    try:
        # a single PROFILE directory -> <instance>/Default
        profile_src = os.path.join(root, "src-profile")
        os.makedirs(profile_src)
        Path(profile_src, "Cookies").write_text("cookie", encoding="utf-8")
        Path(profile_src, "Preferences").write_text("{}", encoding="utf-8")
        target = os.path.join(root, "instance-a")
        reply = profile_lib.seed(source=profile_src, profile=target,
                                 force=True)
        assert reply["profile_dir"] == os.path.join(target, "Default"), reply
        assert Path(target, "Default", "Cookies").read_text() == "cookie"
        assert not os.path.exists(os.path.join(target, "Cookies")), \
            "a profile file landed in the instance root, where Chrome ignores it"

        # a whole USER-DATA directory -> the instance root (its Default/ travels)
        data_src = os.path.join(root, "src-data")
        os.makedirs(os.path.join(data_src, "Default"))
        Path(data_src, "Local State").write_text("{}", encoding="utf-8")
        Path(data_src, "Default", "Cookies").write_text("cookie2",
                                                          encoding="utf-8")
        target2 = os.path.join(root, "instance-b")
        reply = profile_lib.seed(source=data_src, profile=target2, force=True)
        assert reply["profile_dir"] == target2, reply
        assert Path(target2, "Default", "Cookies").read_text() == "cookie2"
        assert Path(target2, "Local State").exists()
    finally:
        _restore_root(keep_root)


def t_seed_dry_writes_nothing() -> None:
    """`--dry` promises "without writing anything" — it creates no target."""
    root = tempfile.mkdtemp(prefix="browser-control-hermetic-")
    keep_root = os.environ.get("BROWSER_CONTROL_ROOT")
    os.environ["BROWSER_CONTROL_ROOT"] = root
    try:
        source = os.path.join(root, "source")
        os.makedirs(source)
        Path(source, "Cookies").write_text("login", encoding="utf-8")
        target = os.path.join(root, "fresh")
        reply = profile_lib.seed(source=source, profile=target, dry=True)
        assert reply["dry"] is True and reply["verified"] is False, reply
        assert not os.path.exists(target), \
            "a dry run created the target profile"
    finally:
        _restore_root(keep_root)


def t_reset_guard_counts_skipped_content() -> None:
    """A cache-only profile refuses `reset` without `--force` — and counts."""
    root = tempfile.mkdtemp(prefix="browser-control-hermetic-")
    keep_root = os.environ.get("BROWSER_CONTROL_ROOT")
    os.environ["BROWSER_CONTROL_ROOT"] = root
    try:
        target = os.path.join(root, "cache-only")
        os.makedirs(os.path.join(target, "Cache"))
        Path(target, "Cache", "data").write_bytes(b"x" * 4096)
        refusal(lambda: profile_lib.reset(profile=target), "profile-exists")
        reply = profile_lib.reset(profile=target, force=True)
        assert reply["files"] >= 1 and reply["bytes_freed"] >= 4096, reply
    finally:
        _restore_root(keep_root)


def t_screenshot_rules_and_symlinks() -> None:
    """A relative screenshot path is refused; the temp name is O_NOFOLLOW."""
    refusal(lambda: dom._shot_target("shot.png"), "bad-args")
    with tempfile.TemporaryDirectory() as tmp:
        victim = os.path.join(tmp, "victim")
        Path(victim).write_bytes(b"keep")
        target = os.path.join(tmp, "shot.png")
        os.symlink(victim, f"{target}.bc-{os.getpid()}.part")
        with contextlib.suppress(ControlError):
            # the refusal is the fix's answer; a success is checked below by
            # the victim's bytes, which must not move either way
            dom._write_shot(target, b"\x89PNG\r\n\x1a\n", force=True)
        assert Path(victim).read_bytes() == b"keep", \
            "the write followed a planted symlink"


def t_page_lists_are_guarded() -> None:
    """`tab select` slices page answers only when they ARE lists."""
    assert dom._list(["a", "b"]) == ["a", "b"]
    assert dom._list(7) == []
    assert dom._list({"labels": []}) == []
    assert dom._list(None) == []


def t_cli_flag_hardening() -> None:
    """Empty names, repeated policies and dropped scopes are refused."""
    for argv in (["--profile", "", "profile", "info"],
                 ["--browser", "", "info"]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)
    for argv in (["selftest", "--deny", "read", "--deny", "write"],
                 ["selftest", "--allow=read", "--allow=code"]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)
    for argv in (["attach", "--list", "--profile",
                  os.path.join(tempfile.gettempdir(), "stranger")],
                 ["profile", "info", "--browser", "chrome"],
                 ["tab", "click", "--at", "1,2", "--index", "3"],
                 ["tab", "hover", "--at", "1,2", "--index", "3"]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)


def t_cli_no_args_is_logged() -> None:
    """The no-args refusal lands in the action log like every other."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "actions.jsonl")
        os.environ["BROWSER_CONTROL_LOG"] = path
        try:
            rc, _out, err = run_cli([])
        finally:
            os.environ["BROWSER_CONTROL_LOG"] = SUITE_LOG
        assert rc == 2 and "ERR[bad-args]" in err, (rc, err)
        with open(path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle]
        assert rows and rows[-1]["code"] == "bad-args", rows


def t_cli_broken_pipe_is_a_refusal() -> None:
    """A closed stdout reader is ERR[broken-pipe]/2, not status 120."""
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    read_fd, write_fd = os.pipe()
    os.close(read_fd)                      # the reader is gone
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(cwd, "browser-control-cli"),
             "selftest"],
            stdout=write_fd, stderr=subprocess.PIPE, cwd=cwd,
            check=False)
    finally:
        os.close(write_fd)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert b"ERR[broken-pipe]" in proc.stderr, proc.stderr
    assert b"Exception ignored" not in proc.stderr, proc.stderr


def t_audit_modes_caps_redirect_and_symlink() -> None:
    """The previous round's fixes each carry their own check.

    The log's mode, a redirect off loopback, a symlink planted at a seed
    destination, and the argv caps (a review found all four claimed in
    docs/progress.md and none asserted).
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "deep", "actions.jsonl")
        os.environ["BROWSER_CONTROL_LOG"] = path
        try:
            audit.LOG.begin("open")
            audit.LOG.write(action="open", args=["x"])
        finally:
            os.environ["BROWSER_CONTROL_LOG"] = SUITE_LOG
            audit.LOG.begin("")
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(audit.scratch_dir()).st_mode) == 0o700
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "actions.jsonl")
        os.environ["BROWSER_CONTROL_LOG"] = path
        try:
            audit.LOG.begin("tab")
            audit.LOG.write(action="tab", args=["x" * 9000, "y"])
        finally:
            os.environ["BROWSER_CONTROL_LOG"] = SUITE_LOG
            audit.LOG.begin("")
        row = json.loads(Path(path).read_text(encoding="utf-8").strip())
        assert len(row["args"][0]) < 4200, row
        assert "<truncated: 9000 chars>" in row["args"][0], row

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", "http://example.com/json")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as profile:
            Path(profile, cdp.PORT_FILE).write_text(
                f"{int(server.server_address[1])}\n")
            refusal(lambda: cdp.page_rows(profile), "cdp-not-local")
    finally:
        server.shutdown()
        server.server_close()

    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "source")
        target = os.path.join(tmp, "target")
        os.makedirs(source)
        os.makedirs(os.path.join(target, "Default"))
        Path(source, "Cookies").write_text("login", encoding="utf-8")
        victim = os.path.join(tmp, "victim")
        Path(victim).write_text("keep", encoding="utf-8")
        # the destination a profile-dir source now lands in
        os.symlink(victim, os.path.join(target, "Default", "Cookies"))
        keep_root = os.environ.get("BROWSER_CONTROL_ROOT")
        os.environ["BROWSER_CONTROL_ROOT"] = tmp
        try:
            # the planted link is NOT written through: the copy skips it and
            # the read-back refuses `seed-not-verified` instead of claiming a
            # login that did not land
            refusal(lambda: profile_lib.seed(source=source, profile=target,
                                             force=True), "seed-not-verified")
        finally:
            _restore_root(keep_root)
        assert Path(victim).read_text(encoding="utf-8") == "keep"


def t_extract_schema_and_records() -> None:
    """The extraction engine's spec parsing and record shaping are pure."""
    schema = dom._extract_schema(                            # noqa: SLF001
        "article",
        ['text=[data-testid="tweetText"]', "time=time@datetime",
         'url=a[href*="/status/"]@href', "label=:scope", "href=@href"],
        5, 800, True)
    assert schema["each"] == "article" and schema["cap"] == 5
    assert schema["chars"] == 800 and schema["visible"] is True
    assert schema["fields"]["text"] == {
        "sel": '[data-testid="tweetText"]', "attr": ""}
    assert schema["fields"]["time"] == {"sel": "time", "attr": "datetime"}
    assert schema["fields"]["url"] == {
        "sel": 'a[href*="/status/"]', "attr": "href"}
    assert schema["fields"]["label"] == {"sel": ":scope", "attr": ""}
    assert schema["fields"]["href"] == {"sel": "", "attr": "href"}
    refusal(lambda: dom._extract_schema("", ["a=:scope"]), "bad-args")
    refusal(lambda: dom._extract_schema("a", []), "bad-args")
    refusal(lambda: dom._extract_schema("a", ["no-equals"]), "bad-args")
    refusal(lambda: dom._extract_schema("a", ["=:scope"]), "bad-args")
    refusal(lambda: dom._extract_schema("a", ["a="]), "bad-args")
    refusal(lambda: dom._extract_schema("a", ["a=:scope"], cap=0),
            "bad-args")
    records = dom._extract_records(                          # noqa: SLF001
        [{"a": "x", "b": 7}, "nope", {"a": None}, {"a": "y"}],
        ["a", "b"])
    assert records == [{"a": "x", "b": None}, {"a": None, "b": None},
                       {"a": "y", "b": None}], records
    assert dom._extract_records("not a list", ["a"]) == []


def t_cli_extract_grammar() -> None:
    """`tab extract` argv reaches dom.extract once, flags intact."""
    calls: list[tuple] = []

    def fake_extract(each: str = "", fields: list | None = None,
                     cap: int = 0, chars: int = 0, visible: bool = False,
                     unique: str = "", tab: str = "",
                     browser: str = "") -> dict:
        calls.append((each, list(fields or []), cap, chars, visible, unique,
                      tab, browser))
        return {"ok": True}

    original = dom.extract
    dom.extract = fake_extract                    # type: ignore[assignment]
    try:
        rc, _out, err = run_cli([
            "tab", "extract", "--each", "article",
            "--field", "text=[data-testid=tweetText]",
            "--field", "time=time@datetime",
            "--cap", "5", "--chars", "700", "--visible",
            "--unique", "text", "--tab", "id:AB"])
        assert rc == 0, (rc, err)
        assert calls == [("article",
                          ["text=[data-testid=tweetText]",
                           "time=time@datetime"],
                          5, 700, True, "text", "id:AB", "")], calls
    finally:
        dom.extract = original                    # type: ignore[assignment]
    for argv in (["tab", "extract"],
                 ["tab", "extract", "--each", "a"],
                 ["tab", "extract", "--field", "x=:scope"],
                 ["tab", "extract", "--each", "a", "--field", "x=:scope",
                  "--unique", "nope"],
                 ["tab", "extract", "--each", "a", "--field", "x=:scope",
                  "--cap", "0"]):
        rc, _out, err = run_cli(argv)
        assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)


def t_plugin_system() -> None:
    """Plugins load, dispatch, gate and report; broken ones fail open."""
    valid = (
        "from browser_control.lib.errors import fail\n"
        "def run(rest, browser):\n"
        "    if not rest:\n"
        "        fail('bad-args', 'hello: a NAME is required')\n"
        "    return {'ok': True, 'hello': rest[0], 'browser': browser}\n"
        "PLUGIN = {'api': 1, 'name': 'hello', 'description': 'a test plugin',\n"
        "          'actions': {'hello': {'run': run, 'classes': ('read',),\n"
        "                                'usage': 'hello NAME'}}}\n")
    collide = (
        "PLUGIN = {'api': 1, 'name': 'bad', 'actions': {\n"
        "    'open': {'run': lambda rest, browser: {},\n"
        "             'classes': ('read',)}}}\n")
    wrong_api = (
        "PLUGIN = {'api': 99, 'name': 'old', 'actions': {\n"
        "    'old': {'run': lambda rest, browser: {}, 'classes': ('read',)}}}\n")
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "hello.py").write_text(valid, encoding="utf-8")
        Path(tmp, "broken.py").write_text("raise ValueError('boom')\n",
                                          encoding="utf-8")
        Path(tmp, "collide.py").write_text(collide, encoding="utf-8")
        Path(tmp, "old.py").write_text(wrong_api, encoding="utf-8")
        keep = os.environ.get("BROWSER_CONTROL_PLUGIN_PATH")
        os.environ["BROWSER_CONTROL_PLUGIN_PATH"] = tmp
        try:
            rc, out, err = run_cli(["hello", "world"])
            assert rc == 0, (rc, err)
            assert json.loads(out)["hello"] == "world", out
            rc, _out, err = run_cli(["--deny", "read", "hello", "world"])
            assert rc == 2 and "ERR[not-allowed]" in err, (rc, err)
            rc, _out, err = run_cli(["hello"])
            assert rc == 2 and "ERR[bad-args]" in err, (rc, err)
            rc, out, err = run_cli(["selftest"])
            assert rc == 0, (rc, err)
            data = json.loads(out)
            assert [p["name"] for p in data["plugins"]] == ["hello"], \
                data["plugins"]
            errors = "\n".join(data["plugin_errors"])
            assert "broken.py" in errors and "boom" in errors, errors
            assert "collide.py" in errors and "already taken" in errors, errors
            assert "old.py" in errors and "api 99" in errors, errors
            assert "hello" in capabilities.PLUGIN_ACTIONS, \
                capabilities.PLUGIN_ACTIONS
        finally:
            if keep is None:
                os.environ.pop("BROWSER_CONTROL_PLUGIN_PATH", None)
            else:
                os.environ["BROWSER_CONTROL_PLUGIN_PATH"] = keep
        # the path is gone: a fresh invocation has no plugin and no ghost
        rc, out, _err = run_cli(["selftest"])
        assert json.loads(out)["plugins"] == [], out
        rc, _out, err = run_cli(["hello", "world"])
        assert rc == 2 and "ERR[unknown-command]" in err, (rc, err)


def t_x_plugin_offline() -> None:
    """The X plugin builds the Latest URL and maps rows to posts."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    keep = os.environ.get("BROWSER_CONTROL_PLUGIN_PATH")
    os.environ["BROWSER_CONTROL_PLUGIN_PATH"] = os.path.join(repo, "plugins")
    navs: list[tuple] = []
    real_nav, real_wait, real_extract = (browser.nav, dom.wait, dom.extract)

    def fake_nav(url: str, tab: str = "", browser: str = "") -> dict:
        navs.append((url, tab))
        return {"ok": True}

    def fake_wait(mode: str, selector: str | None = None,
                  expr: str | None = None, timeout: float = 0.0,
                  idle_ms: int = 0, tab: str = "", browser: str = "") -> dict:
        return {"ok": True}

    def fake_extract(each: str = "", fields: list | None = None,
                     cap: int = 0, chars: int = 0, visible: bool = False,
                     unique: str = "", tab: str = "",
                     browser: str = "") -> dict:
        if each == "article":
            return {"ok": True, "truncated": False, "matches": [
                {"text": "first", "time": "2026-09-20T07:37:24.000Z",
                 "url": "/alice/status/101"},
                {"text": "same id", "time": "2026-09-20T07:00:00.000Z",
                 "url": "/bob/status/101"},
                {"text": "no link", "time": "", "url": None}]}
        return {"ok": True, "matches": [
            {"label": "Latest", "selected": "true"},
            {"label": "Top", "selected": "false"}]}

    browser.nav = fake_nav                        # type: ignore[assignment]
    dom.wait = fake_wait                          # type: ignore[assignment]
    dom.extract = fake_extract                    # type: ignore[assignment]
    try:
        rc, out, err = run_cli(["x", "search", '"Pardon Snowden"',
                                "--latest", "--cap", "5"])
        assert rc == 0, (rc, err)
        data = json.loads(out)
        assert data["sort"] == "latest", data
        assert data["count"] == 1, data
        assert data["posts"][0] == {
            "id": "101", "handle": "alice",
            "url": "https://x.com/alice/status/101",
            "time": "2026-09-20T07:37:24.000Z", "text": "first"}, data
        assert navs and "f=live" in navs[0][0], navs
        assert "Pardon%20Snowden" in navs[0][0], navs
        rc, _out, err = run_cli(["x", "search"])
        assert rc == 2 and "ERR[bad-args]" in err, (rc, err)
        rc, _out, err = run_cli(["x", "search", "q", "--latest", "--top"])
        assert rc == 2 and "ERR[bad-args]" in err, (rc, err)
    finally:
        browser.nav = real_nav                    # type: ignore[assignment]
        dom.wait = real_wait                      # type: ignore[assignment]
        dom.extract = real_extract                # type: ignore[assignment]
        if keep is None:
            os.environ.pop("BROWSER_CONTROL_PLUGIN_PATH", None)
        else:
            os.environ["BROWSER_CONTROL_PLUGIN_PATH"] = keep


def main() -> int:
    global SUITE_LOG
    # Pin everything the checks depend on, UNCONDITIONALLY: a host-exported
    # log, root or policy must not change what the suite exercises — or let it
    # append to the user's real log, or reach a real browser.
    log_dir = (audit.scratch_dir()
               or tempfile.mkdtemp(prefix="browser-control-"))
    SUITE_LOG = os.path.join(log_dir, "hermetic-actions.jsonl")
    os.environ["BROWSER_CONTROL_LOG"] = SUITE_LOG
    os.environ["BROWSER_CONTROL_ROOT"] = tempfile.mkdtemp(
        prefix="browser-control-hermetic-")
    # plugins are pinned to an EMPTY directory, so the suite never loads the
    # user's installed plugins (the plugin tests point the variable at their
    # own fixture directory and restore this one)
    os.environ["BROWSER_CONTROL_PLUGIN_PATH"] = tempfile.mkdtemp(
        prefix="browser-control-plugins-")
    for name in ("BROWSER_CONTROL_ALLOW", "BROWSER_CONTROL_DENY"):
        os.environ.pop(name, None)
    for name, fn in (
        ("safe_url policy", t_safe_url),
        ("resolve_tab refuses ambiguity", t_resolve_tab),
        ("launch flags make a profile drivable", t_launch_flags),
        ("profile keyed by the resolved binary", t_profile_keyed_by_binary),
        ("port file is not proof of a port", t_port_file),
        ("page rows from a fake endpoint", t_page_rows_from_a_fake_endpoint),
        ("cli dispatches with flags stripped", t_cli_dispatch),
        ("tab grammar lands in one service", t_cli_tab_grammar),
        ("nav/history/reload grammar", t_cli_nav_grammar),
        ("dom verbs' argv", t_cli_dom_grammar),
        ("click/scroll argv", t_cli_click_scroll_grammar),
        ("tab close --title/--url argv", t_cli_close_bulk),
        ("activate/hover/check/select/dialog/screenshot argv",
         t_cli_new_verbs_grammar),
        ("page expressions parse; shot and dialog rules",
         t_expressions_and_shot_rules),
        ("keys, verdicts and secrets", t_keys_and_verdicts),
        ("the log redacts and never fails a verb", t_audit_redaction),
        ("the action log lands on disk", t_action_log_lands_on_disk),
        ("a working log makes no scratch directory",
         t_a_working_log_makes_no_scratch_dirs),
        ("the port is checked against the kernel", t_endpoint_ownership),
        ("the lock serializes a check-then-act", t_lock_serializes_a_check_then_act),
        ("the policy gate fails closed", t_policy_gate),
        ("gate and argv hardening", t_gate_and_argv_hardening),
        ("transport against a fake peer", t_transport_against_a_fake_peer),
        ("the press lands at the proven point", t_click_presses_at_the_proven_point),
        ("frames and points: scopes, syntax, verbs", t_frames_and_points),
        ("frames bind to their tab", t_frames_bind_to_their_tab),
        ("every verb is classified", t_capability_surface),
        ("input verbs' argv", t_cli_input_grammar),
        ("media verdict and argv", t_media_verdict),
        ("media argv", t_cli_media_grammar),
        ("dom shape filters", t_dom_shape_filters),
        ("extract schema and records", t_extract_schema_and_records),
        ("tab extract grammar", t_cli_extract_grammar),
        ("plugins load, dispatch and gate", t_plugin_system),
        ("x plugin maps records offline", t_x_plugin_offline),
        ("the net-change test", t_same_page),
        ("one page verb, one tab", t_one_tab_addressing),
        ("attach/detach grammar", t_cli_attach_grammar),
        ("attach opens the write gate", t_attach_bookkeeping),
        ("cli lists browsers and their info", t_cli_lists),
        ("cmdline flag values are read", t_cmdline_value),
        ("audit redacts beyond the cap", t_audit_redaction_beats_truncation),
        ("a short write is not a line", t_audit_short_write_is_not_success),
        ("the CDP read never uses a proxy", t_http_read_never_uses_a_proxy),
        ("malformed endpoints and page text refuse typed",
         t_transport_typed_refusals),
        ("the text cap counts characters", t_text_cap_counts_characters),
        ("open needs its OWN endpoint",
         t_wait_own_port_requires_a_verified_owner),
        ("the close read-back tolerates a stranger",
         t_tab_count_does_not_refuse_on_a_stranger),
        ("an unreadable tab list is not absence",
         t_unreadable_tab_list_is_not_absence),
        ("page titles are flat in refusals",
         t_page_titles_are_flat_in_refusals),
        ("a symlinked profile target is refused",
         t_profile_symlink_target_is_refused),
        ("seed --dry writes nothing", t_seed_dry_writes_nothing),
        ("seed lands where Chrome reads", t_seed_lands_where_chrome_reads),
        ("reset counts skipped content",
         t_reset_guard_counts_skipped_content),
        ("screenshot rules and symlinks", t_screenshot_rules_and_symlinks),
        ("prior fixes carry checks", t_audit_modes_caps_redirect_and_symlink),
        ("page lists are guarded", t_page_lists_are_guarded),
        ("cli flags refuse silent drops", t_cli_flag_hardening),
        ("the no-args refusal is logged", t_cli_no_args_is_logged),
        ("a broken pipe is a refusal", t_cli_broken_pipe_is_a_refusal),
        ("cli argv is strict", t_cli_argv_is_strict),
        ("selftest proves the install", t_selftest),
        ("pid liveness", t_pid_alive),
    ):
        check(name, fn)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
