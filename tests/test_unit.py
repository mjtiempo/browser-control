"""Hermetic checks for the browser-control slice — no browser needed.

Run:  python3 tests/test_unit.py

What is covered here is what can be wrong without a browser: the URL policy,
the tab-spec resolution, the launch flags (a never-run profile only becomes
drivable WITH --no-first-run), the CLI's argv strictness and dispatch, and the
CDP HTTP read path against a fake endpoint.
"""
from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_control.cli import main as cli_main  # noqa: E402  # pyright: ignore[reportMissingImports]
from browser_control.lib import browser, cdp, dom  # noqa: E402  # pyright: ignore[reportMissingImports]
from browser_control.lib.errors import ControlError  # noqa: E402  # pyright: ignore[reportMissingImports]

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
        os.environ["BROWSER_CONTROL_ROOT"] = tmp
        try:
            path = browser.profile_dir("/usr/bin/google-chrome-stable")
            assert path == os.path.join(tmp, "google-chrome-stable"), path
            assert browser.profile_dir("brave-browser") == \
                os.path.join(tmp, "brave-browser")
            assert browser.profiles() == [], browser.profiles()
            assert browser.live_profiles() == [], browser.live_profiles()
        finally:
            del os.environ["BROWSER_CONTROL_ROOT"]


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

    def fake_close_tabs(specs: list[str], browser: str = "") -> dict:
        calls.append(("tab close", tuple(specs), browser))
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
            ("tab close", ("a", "b"), ""),
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
                 "cdp": {"port": 1616, "reachable": True, "tabs": 1}},
                {"pid": 2, "exe": "chrome", "path": "/usr/bin/chrome",
                 "profile": foreign, "profile_from": "flag",
                 "managed": False,
                 "attached": browser.is_attached(foreign),
                 "cdp": {"port": 1515, "reachable": True, "tabs": 1}},
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
            del os.environ["BROWSER_CONTROL_ROOT"]


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
                 "cdp": {"port": 1616, "reachable": True}},
                {"pid": 2, "exe": "chrome", "path": "/usr/bin/chrome",
                 "profile": foreign, "profile_from": "flag",
                 "managed": False, "attached": False,
                 "cdp": {"port": 1515, "reachable": True}},
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
        finally:
            browser.browsers = real_browsers          # type: ignore[assignment]
            browser.cdp.page_rows_at = real_rows      # type: ignore[assignment]
            del os.environ["BROWSER_CONTROL_ROOT"]


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
    assert "__SELECTOR__" not in dom.FIND_EXPR.replace("__SELECTOR__", "")  # noqa: SLF001
    for placeholder in ("__MODE__", "__NEEDLE__", "__SELECTOR__", "__CAP__"):
        assert placeholder in dom.FIND_EXPR                  # noqa: SLF001


def t_cmdline_value() -> None:
    """Chrome writes `--flag=value` and `--flag value`; both are read."""
    value = browser._cmdline_value                            # noqa: SLF001
    assert value("chrome --user-data-dir=/x/y z", "--user-data-dir") == "/x/y"
    assert value("chrome --user-data-dir /x/y z", "--user-data-dir") == "/x/y"
    assert value("chrome --user-data-dir=", "--user-data-dir") == ""
    assert value("chrome --user-data-dir", "--user-data-dir") == ""
    assert value("chrome", "--user-data-dir") == ""
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
                     ["tab", "close"],
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


def t_pid_alive() -> None:
    assert browser._pid_alive(os.getpid()) is True                 # noqa: SLF001
    assert browser._pid_alive(999999) is False                     # noqa: SLF001


def main() -> int:
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
        ("dom shape filters", t_dom_shape_filters),
        ("the net-change test", t_same_page),
        ("one page verb, one tab", t_one_tab_addressing),
        ("attach/detach grammar", t_cli_attach_grammar),
        ("attach opens the write gate", t_attach_bookkeeping),
        ("cli lists browsers and their info", t_cli_lists),
        ("cmdline flag values are read", t_cmdline_value),
        ("cli argv is strict", t_cli_argv_is_strict),
        ("selftest proves the install", t_selftest),
        ("pid liveness", t_pid_alive),
    ):
        check(name, fn)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
