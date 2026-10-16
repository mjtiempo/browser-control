"""verbs.remote — the `remote` subcommand handlers.

Every remote action lives under this noun: `attach`/`detach` are the consent
verbs, `list`/`info` are reads, `close` is the declared refusal, and `tab` is
the delegation to the ordinary tab dispatcher (in `cli.main`, where the tab
verbs are registered). The handlers here are argv adapters only — the identity
rules, the records and the fingerprint oracle are `lib/browser/remote.py`.
"""
from __future__ import annotations

from browser_control.cli.argv import (
    _none,
    _pop,
    _switch,
)
from browser_control.lib.browser import remote as remote_lib
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    ERR_REMOTE_NOT_CLOSABLE,
    fail,
)

__all__ = ["cmd_remote_attach", "cmd_remote_close", "cmd_remote_detach",
           "cmd_remote_info", "cmd_remote_list"]


def cmd_remote_attach(rest: list[str], browser: str) -> dict:
    """`remote attach --cdp-url URL` — the consent step, once per endpoint."""
    rest, url = _pop(rest, "--cdp-url", "remote attach")
    _none(rest, "remote attach")
    if url is None:
        fail(ERR_BAD_ARGS,
             "remote attach: --cdp-url URL is required (the LOCAL end of the "
             "tunnel, http://127.0.0.1:9222)")
    return remote_lib.attach(url)


def cmd_remote_detach(rest: list[str], browser: str) -> dict:
    """`remote detach --cdp-url URL | --all` — revoke the authorization."""
    rest, url = _pop(rest, "--cdp-url", "remote detach")
    rest, all_remotes = _switch(rest, "--all")
    _none(rest, "remote detach")
    if url is not None and all_remotes:
        fail(ERR_BAD_ARGS,
             "remote detach: --cdp-url names ONE endpoint and --all names "
             "every one — give one of the two, not both")
    if url is None and not all_remotes:
        fail(ERR_BAD_ARGS,
             "remote detach: name one with --cdp-url URL, or --all")
    return remote_lib.detach(url or "", all_remotes=all_remotes)


def cmd_remote_list(rest: list[str], browser: str) -> dict:
    """`remote list` — every remote attachment, and whether it is usable."""
    _none(rest, "remote list")
    return remote_lib.list_remotes()


def cmd_remote_info(rest: list[str], browser: str) -> dict:
    """`remote info [--cdp-url URL]` — one endpoint, its record and verdict."""
    rest, url = _pop(rest, "--cdp-url", "remote info")
    _none(rest, "remote info")
    return remote_lib.info(url or "")


def cmd_remote_close(rest: list[str], browser: str) -> dict:
    """`remote close` — DECLARED refusal: a remote browser is stopped there.

    The code exists so the answer is a refusal a caller can branch on, not
    `unknown-command`; the URL, when given, is still validated first, so a
    malformed target reads as `bad-args` rather than as this refusal.
    """
    rest, url = _pop(rest, "--cdp-url", "remote close")
    _none(rest, "remote close")
    if url:
        remote_lib.parse_url(url)
    fail(ERR_REMOTE_NOT_CLOSABLE,
         "a remote browser is stopped where it RUNS (stop the browser and the "
         "tunnel there); `remote detach` only revokes this CLI's tab writes. "
         "This CLI does not signal a browser it cannot own")
