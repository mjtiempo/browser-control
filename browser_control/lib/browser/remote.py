"""remote — a CDP endpoint this machine does not own: identity, records, gate.

A browser behind an `ssh -L` tunnel answers on loopback like any other, but
the KERNEL cannot say whose endpoint it is: the listener is `ssh`, not a
browser. This module is the different oracle the plan promises — a
trust-on-first-use fingerprint the endpoint reports about itself
(`/json/version`), stored when `remote attach` hands the endpoint over and
re-read before every connection the tool makes.

The rules, in one place:

* **Loopback only, `http://HOST:PORT` only** — `parse_url` is the attach-time
  half of `rpc._checked_ws`'s transport rule (userinfo, paths, queries, a
  websocket scheme, and non-loopback hosts all refuse `bad-args`).
* **The fingerprint is four reported fields**, compared exactly; any difference
  on a later connection is `cdp-identity-changed`, and nothing is driven.
* **One store, two kinds**: the record lives beside the local attachments and
  is only ever touched by the `remote` noun.
* **The binding**: `remote tab` runs with the chosen record as the ONE remote
  row in the census (`scope.current().remote`), so the ordinary tab verbs and
  the EndpointGuard seam work unchanged and the flat surface stays blind.

The internal key (`<root>/.remote/<host>-<port>`) is a NAME the verb machinery
needs to carry a profile-shaped identity; it is never shown in a reply, never a
directory, and never a profile.
"""
from __future__ import annotations

import contextlib
import time
import urllib.parse

from browser_control.lib import cdp
from browser_control.lib import scope as scope_state
from browser_control.lib.attachments import (
    KIND_REMOTE,
    STORE,
)
from browser_control.lib.coerce import as_int
from browser_control.lib.errors import (
    ERR_AMBIGUOUS_BROWSER,
    ERR_BAD_ARGS,
    ERR_CDP_ERROR,
    ERR_CDP_IDENTITY_CHANGED,
    ERR_CDP_NOT_LOCAL,
    ERR_CDP_UNREACHABLE,
    ERR_NOT_ATTACHED,
    ControlError,
    fail,
)
from browser_control.lib.paths import (
    norm,
    remote_key,
)

#: The host spellings the transport accepts, the same three `_checked_ws`
#: allows. Anything else is refused at attach, before a socket is opened.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

#: `Browser` product names that are Chromium family. Read from the product
#: string, not from `proc.BROWSER_EXES`: an exe name says what was launched,
#: the product says what answered.
FAMILY_PRODUCTS = ("chrome", "headlesschrome", "chromium", "edg", "brave",
                   "vivaldi")

#: The four `/json/version` fields that ARE the identity, and the stored names
#: they map to.
FINGERPRINT_FIELDS = (("Browser", "browser"),
                      ("Protocol-Version", "protocol"),
                      ("User-Agent", "user_agent"),
                      ("V8-Version", "v8"))

#: What every attach/lifecycle reply says about the grant.
NOTE = ("remote tab writes only — `remote close` never stops it; "
        "`remote detach` revokes this")

__all__ = ["FAMILY_PRODUCTS", "FINGERPRINT_FIELDS", "LOOPBACK_HOSTS", "NOTE",
           "active", "attach", "binding", "canonical", "check", "detach",
           "diff", "fingerprint", "info", "list_remotes", "parse_url",
           "scope_record", "select", "verdict"]


def canonical(host: str, port: int) -> str:
    """The one spelling of a loopback endpoint, IPv6 brackets included."""
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{as_int(port)}"


def parse_url(url: object) -> tuple[str, int]:
    """(host, port) for a `--cdp-url`, or `bad-args` naming what is wrong.

    Loopback `http` only, an explicit real port, and no userinfo/path/query/
    fragment: the URL names the TUNNEL's local end, and everything the caller
    could hide in the rest of it is refused rather than ignored.
    """
    text = str(url or "").strip()
    if not text:
        fail(ERR_BAD_ARGS,
             "--cdp-url needs a URL (http://127.0.0.1:9222, the local end of "
             "the tunnel)")
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError as e:
        fail(ERR_BAD_ARGS, f"--cdp-url {text!r} is not a URL ({e})")
    if parts.scheme != "http":
        fail(ERR_BAD_ARGS,
             f"--cdp-url must be http://HOST:PORT, got {text!r} — the "
             "websocket comes from /json/version like every other endpoint")
    if parts.username or parts.password:
        fail(ERR_BAD_ARGS,
             f"--cdp-url {text!r} carries userinfo — CDP has no credentials, "
             "so there is nothing to name and nothing to send")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        fail(ERR_BAD_ARGS,
             f"--cdp-url {text!r} names more than an origin — give "
             "http://HOST:PORT, with no path, query or fragment")
    host = str(parts.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        fail(ERR_BAD_ARGS,
             f"--cdp-url names {host or 'no host'!r} — only a loopback "
             "endpoint (" + ", ".join(LOOPBACK_HOSTS) + ") may be attached; "
             "the tunnel is the caller's contract, and this tool never speaks "
             "to a foreign host")
    try:
        port = parts.port
    except ValueError as e:
        fail(ERR_BAD_ARGS, f"--cdp-url {text!r} has no usable port ({e})")
    if not port:
        fail(ERR_BAD_ARGS,
             f"--cdp-url {text!r} needs a real port (1-65535)")
    return host, port


def fingerprint(port: int) -> dict[str, str]:
    """`/json/version`, read as the endpoint's own identity.

    The one fetch every verdict and every attach is built on: it REUSES
    `cdp._get_port` (loopback URL literal, redirect refusal, proxy suppression,
    and the read deadline with its reader shutdown), so this cannot become a
    second, weaker transport. An answer that is not readable CDP JSON, or a
    product that is not Chromium family, is `cdp-not-local` — the endpoint
    answered, it is just not the browser this tool drives; a transport failure
    stays `cdp-unreachable`.
    """
    where = f"http://127.0.0.1:{as_int(port)}/json/version"
    try:
        info = cdp._get_port(as_int(port), "/json/version")
    except ControlError as e:
        if e.code == ERR_CDP_ERROR:
            fail(ERR_CDP_NOT_LOCAL,
                 f"{where} answered something that is not CDP JSON "
                 f"({e.message}) — nothing was driven")
        raise
    if not isinstance(info, dict):
        fail(ERR_CDP_NOT_LOCAL,
             f"{where} answered no JSON object — nothing was driven")
    product = str(info.get("Browser") or "")
    name = product.split("/", 1)[0].strip().lower()
    if name not in FAMILY_PRODUCTS:
        fail(ERR_CDP_NOT_LOCAL,
             f"{where} reports Browser {product!r}, which is not a "
             "Chromium-family browser — nothing was driven")
    return {stored: str(info.get(reported) or "")
            for reported, stored in FINGERPRINT_FIELDS}


def diff(stored: dict, current: dict) -> str:
    """The changed fields between two fingerprints, "" when identical.

    PURE, so the hermetic tier can pin the comparison without a socket. Every
    field is compared as the string the endpoint printed — no normalisation, no
    version ordering: a protocol bump alone is a different browser build and
    earns a re-attach.
    """
    changed = [name for _reported, name in FINGERPRINT_FIELDS
               if str((stored or {}).get(name) or "")
               != str((current or {}).get(name) or "")]
    if not changed:
        return ""
    return ", ".join(f"{name}: {str((stored or {}).get(name) or '')!r} -> "
                     f"{str((current or {}).get(name) or '')!r}"
                     for name in changed)


def check(record: dict) -> dict:
    """The live verdict for one record: reachable, verified, and why.

    `reachable` is "the endpoint answered `/json/version` at all"; `verified`
    is "and it is still the fingerprint this record was attached with". The
    `reason` is a complete sentence a refusal can carry, hinting the two-step
    fix on a change.
    """
    port = as_int(record.get("port"))
    try:
        current = fingerprint(port)
    except ControlError as e:
        return {"reachable": False, "verified": False, "reason": e.message,
                "code": e.code, "fingerprint": {}}
    changed = diff(dict(record.get("fingerprint") or {}), current)
    if changed:
        return {"reachable": True, "verified": False,
                "reason": (f"the endpoint's fingerprint changed ({changed}) — "
                           "confirm the endpoint, then `remote detach` and "
                           "`remote attach` it again"),
                "code": ERR_CDP_IDENTITY_CHANGED, "fingerprint": current}
    return {"reachable": True, "verified": True, "reason": "", "code": "",
            "fingerprint": current}


def verdict(profile: str, port: int) -> dict | None:
    """The EndpointGuard verdict for a `(profile, port)`, or None.

    None means "not a remote record" and sends the caller down the kernel
    path. A match answers the SAME shape `_judge` does, plus the refusal
    `code` the oracle earned, so every existing gate routes one rule: no
    `held` judgement here (nothing on this machine is the browser), just
    answerability and the fingerprint comparison.
    """
    record = STORE.find(profile, port=port)
    if not record:
        return None
    live = check(record)
    return {"verified": bool(live["verified"]), "pid": 0, "exe": "",
            "held": bool(live["reachable"]), "profile_pid": 0,
            "reason": str(live["reason"]), "code": str(live["code"]),
            "remote": True}


def _record(url: str) -> tuple[str, int, str, str]:
    """(host, port, canonical url, internal key) for a caller's URL."""
    host, port = parse_url(url)
    return host, port, canonical(host, port), remote_key(host, port)


def attach(url: str) -> dict:
    """`remote attach --cdp-url …`: the consent step, and its record.

    Re-attaching the SAME endpoint with the SAME fingerprint is idempotent
    (`already: true`, the record's `attached_at` left alone). With a DIFFERENT
    fingerprint it refuses `cdp-identity-changed` — a silent swap is never
    blessed by one call; the caller detaches first.
    """
    host, port, shown, key = _record(url)
    current = fingerprint(port)
    existing = STORE.records(KIND_REMOTE).get(norm(key))
    if existing:
        changed = diff(dict(existing.get("fingerprint") or {}), current)
        if changed:
            fail(ERR_CDP_IDENTITY_CHANGED,
                 f"{shown} is already attached with a different fingerprint "
                 f"({changed}) — confirm the endpoint, then "
                 f"`remote detach --cdp-url {shown}` and attach it again")
        reply = {"ok": True, "attached": True, "already": True,
                 "kind": KIND_REMOTE, "url": shown,
                 "product": current["browser"], "fingerprint": current,
                 "note": NOTE}
        return reply
    record = {"profile": key, "kind": KIND_REMOTE, "url": shown,
              "host": host, "port": port, "fingerprint": current,
              "product": current["browser"], "pid": 0,
              "attached_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    already, lock = STORE.put(record)
    reply = {"ok": True, "attached": True, "already": bool(already),
             "kind": KIND_REMOTE, "url": shown, "product": current["browser"],
             "fingerprint": current, "note": NOTE}
    lock.warn(reply)
    return reply


def detach(url: str = "", *, all_remotes: bool = False) -> dict:
    """`remote detach`: revoke one remote record, or every one of them."""
    shown = ""
    if url:
        _host, _port, shown, _key = _record(url)
    urls, lock = STORE.drop_remote(url=shown, all_remotes=all_remotes)
    if not urls and not all_remotes:
        fail(ERR_NOT_ATTACHED, f"nothing is attached for {shown}")
    reply = {"ok": True, "detached": urls,
             "count": len(STORE.records(KIND_REMOTE))}
    lock.warn(reply)
    return reply


def _listed(record: dict) -> dict:
    """One `remote list` row: the record's facts and its live verdict."""
    live = check(record)
    return {"url": str(record.get("url") or ""),
            "product": str(record.get("product") or ""),
            "attached_at": str(record.get("attached_at") or ""),
            "fingerprint": dict(record.get("fingerprint") or {}),
            "reachable": bool(live["reachable"]),
            "verified": bool(live["verified"]),
            "running": bool(live["reachable"] and live["verified"]),
            "reason": str(live["reason"])}


def list_remotes() -> dict:
    """`remote list`: every remote attachment, and whether it is still usable."""
    records = STORE.records(KIND_REMOTE)
    rows = sorted((_listed(record) for record in records.values()),
                  key=lambda row: row["url"])
    return {"ok": True, "count": len(rows), "remotes": rows}


def info(url: str = "") -> dict:
    """`remote info`: one remote endpoint, its record and its live endpoint."""
    record = select(url)
    live = check(record)
    return {"ok": True,
            "running": bool(live["reachable"] and live["verified"]),
            "remote": {"url": str(record.get("url") or ""),
                       "host": str(record.get("host") or ""),
                       "port": as_int(record.get("port")),
                       "product": str(record.get("product") or ""),
                       "attached_at": str(record.get("attached_at") or ""),
                       "fingerprint": dict(record.get("fingerprint") or {})},
            "cdp": {"port": as_int(record.get("port")),
                    "reachable": bool(live["reachable"]),
                    "verified": bool(live["verified"]),
                    "remote": True,
                    "reason": str(live["reason"])}}


def select(url: str = "") -> dict:
    """The one record a remote call names — or the sole attachment.

    Zero records refuse `cdp-unreachable` naming the attach call; more than one
    and no `--cdp-url` refuses `ambiguous-browser` naming every URL; a URL with
    no record refuses `not-attached`. The plan's whole target grammar, in one
    function.
    """
    records = STORE.records(KIND_REMOTE)
    if url:
        _host, _port, shown, key = _record(url)
        record = records.get(norm(key))
        if not record:
            fail(ERR_NOT_ATTACHED,
                 f"nothing is attached for {shown} — `remote attach "
                 f"--cdp-url {shown}` hands it over")
        return record
    if not records:
        fail(ERR_CDP_UNREACHABLE,
             "no remote endpoint is attached — start the tunnel and run "
             "`remote attach --cdp-url http://127.0.0.1:9222` first")
    if len(records) > 1:
        urls = ", ".join(sorted(str(row.get("url")) for row in records.values()))
        fail(ERR_AMBIGUOUS_BROWSER,
             f"{len(records)} remote endpoints are attached ({urls}) — name "
             "one with --cdp-url")
    return next(iter(records.values()))


def scope_record(record: dict | None = None) -> dict:
    """Set, clear or read the process-wide remote binding.

    The same convention as the profile and frame scopes: `None` reads, `{}`
    clears, a record sets. `cli.main` clears it on every invocation, and the
    dispatcher clears it again on the way out of a `remote` call.
    """
    return scope_state.current().set_remote(record)


def active() -> dict:
    """The record the current call is bound to, {} when none is."""
    return scope_state.current().remote_record()


@contextlib.contextmanager
def binding(record: dict):  # noqa: ANN201
    """Run one call with `record` as the ONLY remote row in the census."""
    scope_record(record)
    try:
        yield record
    finally:
        scope_record({})
