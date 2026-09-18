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
from browser_control.lib import browser, cdp  # noqa: E402  # pyright: ignore[reportMissingImports]
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


def t_cli_prints_the_service_reply() -> None:
    rows = [{"id": "A", "title": "t", "url": "u"}]

    def fake_tabs(browser: str = "") -> dict:
        return {"ok": True, "tabs": rows}

    original = cli_main.tabs
    cli_main.tabs = fake_tabs              # type: ignore[assignment]
    try:
        rc, out, err = run_cli(["tabs", "--browser=chromium"])
        assert rc == 0, (rc, err)
        assert json.loads(out)["tabs"] == rows, out
    finally:
        cli_main.tabs = original           # type: ignore[assignment]


def t_cli_lists() -> None:
    """`list` and `list-tabs` print the service reply and take no flags."""
    rows = [{"pid": 1, "exe": "chrome", "managed": False,
             "cdp": {"port": 0, "reachable": False}}]
    calls: list[str] = []

    def fake_list_browsers() -> dict:
        calls.append("list")
        return {"ok": True, "count": 1, "browsers": rows}

    def fake_list_tabs() -> dict:
        calls.append("list-tabs")
        return {"ok": True, "count": 0, "browsers": []}

    originals = (cli_main.list_browsers, cli_main.list_tabs)
    cli_main.list_browsers = fake_list_browsers     # type: ignore[assignment]
    cli_main.list_tabs = fake_list_tabs             # type: ignore[assignment]
    try:
        rc, out, err = run_cli(["list"])
        assert rc == 0 and json.loads(out)["browsers"] == rows, (rc, out, err)
        rc, out, _err = run_cli(["list-tabs"])
        assert rc == 0 and json.loads(out)["count"] == 0, out
        assert calls == ["list", "list-tabs"], calls
        for argv in (["list", "extra"], ["list", "--browser", "chromium"],
                     ["list-tabs", "x"], ["list-tabs", "--browser=chrome"]):
            rc, _out, err = run_cli(argv)
            assert rc == 2 and "ERR[bad-args]" in err, (argv, rc, err)
    finally:
        (cli_main.list_browsers, cli_main.list_tabs) = originals  # type: ignore[assignment]


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

    originals = (cli_main.launch, cli_main.new_tab)
    cli_main.launch = boom                 # type: ignore[assignment]
    cli_main.new_tab = boom                # type: ignore[assignment]
    try:
        rc, _out, err = run_cli(["frobnicate"])
        assert rc == 2 and "ERR[unknown-command]" in err, (rc, err)
        for argv in (["open", "--workspace", "1"],
                     ["open", "https://a", "-x"],
                     ["close", "now"],
                     ["tabs", "extra"],
                     ["close-tab"],
                     ["new-tab", "-x"],
                     ["list", "extra"]):
            rc, _out, err = run_cli(argv)
            assert rc == 2, (argv, rc)
            assert "ERR[bad-args]" in err, (argv, err)
        rc, _out, err = run_cli([])
        assert rc == 2 and "ERR[bad-args]" in err, (rc, err)
        rc, out, _err = run_cli(["--help"])
        assert rc == 0 and out.startswith("usage: browser-control-cli"), out
    finally:
        (cli_main.launch, cli_main.new_tab) = originals  # type: ignore[assignment]


def t_selftest() -> None:
    rc, out, err = run_cli(["selftest"])
    assert rc == 0, (rc, err)
    data = json.loads(out)
    assert data["ok"] is True and data["command"] == "browser-control-cli", data
    for verb in ("open", "close", "tabs", "new-tab", "close-tab",
                 "selftest"):
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
        ("cli lists browsers and tabs", t_cli_lists),
        ("cmdline flag values are read", t_cmdline_value),
        ("cli prints the service reply", t_cli_prints_the_service_reply),
        ("cli argv is strict", t_cli_argv_is_strict),
        ("selftest proves the install", t_selftest),
        ("pid liveness", t_pid_alive),
    ):
        check(name, fn)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
