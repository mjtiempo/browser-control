# Remote CDP over a tunnel — plan (draft)

Status: **DRAFT for review. Not implemented, not decided.**
Revision 3: remote is a **command group**. Every remote action is spelled
`browser-control-cli remote …`, and the flat surface (`open`, `close`, `list`,
`info`, `attach`, `detach`, `tab`, `profile`) never addresses a remote browser.
That removes revision 2's `--cdp-url` selector on flat verbs, its `--profile
<key>` addressing, its `pid: 0` census rows and its synthetic-key collision:
the key is internal, the census stays `/proc`-only, and isolation is the
grammar's default.
This is the design that would reverse `docs/plan.md` §7 ("remote/forwarded
CDP" is out of v1), amend §4 rule 9 ("Nothing is driven through an unverified
endpoint") with a remote clause, and add the one exception to trust.md's
"Every drive is checked against the kernel".

## 1. Why

`browser-control-cli` is built for callers that cannot look at the screen. A
Firecracker microVM (`microvm-control create/start`, guest headless Chromium)
is exactly such a screen — and today this CLI cannot drive its browser:

- `browsers()` censuses the host's `/proc`; a guest process is not a row, so
  `attach --port 9222` refuses `no-browser` ("no running Chromium-family
  browser matches");
- even given the port, `endpoint_owner` asks the host kernel who holds the
  listening socket: behind an `ssh -L` tunnel that is `ssh`, not a browser exe,
  so the endpoint is unverified and a drive refuses `cdp-not-local`;
- there is no remote spelling at all.

The full surface (click, type, extract, wait, screenshot) is wanted against a
browser that runs where this CLI cannot see it, reachable only through a
loopback tunnel:

```bash
# where the browser runs
chromium --headless --no-sandbox --remote-debugging-port=9222 about:blank &
# on this machine
ssh -N -L 9222:127.0.0.1:9222 root@<vm-ip> &
```

What is missing is not transport — CDP-over-a-tunnel is ordinary loopback TCP,
which `lib/cdp/rpc.py` already speaks. What is missing is **identity**: the
kernel can no longer say whose endpoint this is, so the tool needs a different
oracle before it may write.

Identity is not the only missing piece. The kernel verdict is asked at several
places, and the census row is only the first: every websocket and every
`Target.*` call re-judges the listener immediately before it connects
(`owners.verify_ws_owner`, `owners.verify_port_owner`), because a verdict is
about a listener that can be gone by the time traffic goes out. Replacing the
oracle means replacing it at that seam too — §4 says where.

## 2. Shape

One command group, one stored record, one replacement oracle:

```console
$ browser-control-cli remote attach --cdp-url http://127.0.0.1:9222
{"ok": true, "attached": true, "url": "http://127.0.0.1:9222",
 "fingerprint": {"browser": "HeadlessChrome/153.0.0.0",
                 "protocol": "1.3", "user_agent": "Mozilla/5.0 …",
                 "v8": "15.3"},
 "note": "remote tab writes only — `remote close` never stops it;
          `remote detach` revokes this"}

$ browser-control-cli remote tab text --tab active | jq -r .text
Example Domain
$ browser-control-cli remote list
{"ok": true, "count": 1,
 "remotes": [{"url": "http://127.0.0.1:9222", "product": "HeadlessChrome/153",
              "running": true, "reachable": true, "verified": true,
              "attached_at": "2026-…"}]}
$ browser-control-cli remote detach --cdp-url http://127.0.0.1:9222
{"ok": true, "detached": ["http://127.0.0.1:9222"]}
```

`remote attach` is the consent step. It records the endpoint and the
**fingerprint the endpoint reports about itself** (`GET /json/version`); every
later CONNECTION re-reads that fingerprint immediately before it goes out and
refuses if it changed. Tab writes only: `remote close` never signals a remote
browser and `remote detach` revokes the record — closing its LAST page tab
still exits it, the same as any Chromium (`remote tab close` is a tab write,
and Chromium exits with its last window).

## 3. Decisions

| Item | Decision |
| --- | --- |
| Grammar | A `remote` noun with subcommands: `remote attach --cdp-url URL`, `remote detach --cdp-url URL \| --all`, `remote list`, `remote info [--cdp-url URL]`, `remote close [--cdp-url URL]`, and `remote tab <tab verb> … [--cdp-url URL]`. URL only — no `--host`/`--port` pair, no bare `tcp:`, no `ws://` (the websocket comes from `/json/version` like every other endpoint). `--cdp-url` may stand anywhere in the `remote` argv |
| Isolation | The flat surface NEVER reaches a remote: remote records do not join the process census, `--profile` never names one, `close --port N` cannot select one, and a `remote tab` call is the only way to drive one. A remote action outside `remote` is not a refusal to remember — it is not a spelling |
| Transport | Loopback only: `127.0.0.1`, `localhost`, `::1` — the same set `rpc._checked_ws` enforces, compared case-insensitively after `urlparse`. Userinfo, a path, a query, a fragment or a missing port refuse `bad-args`; so does a non-loopback host. The tunnel is the caller's contract; the tool never speaks to a foreign host |
| Target selection | `--cdp-url URL` names one record; without it, the SOLE remote attachment (zero → `cdp-unreachable` naming `remote attach`; more than one → `ambiguous-browser` naming both URLs) |
| Identity oracle | Trust-on-first-use: store `GET /json/version` fields (`Browser`, `Protocol-Version`, `User-Agent`, `V8-Version`) at attach; on every CONNECTION compare the same four fields exactly and refuse `cdp-identity-changed` on any difference |
| Verdict seam | The remote oracle lives inside `owners.EndpointGuard._judge`: a `(key, port)` that matches a remote record is judged by fingerprint, not by `/proc`. Every existing gate (`verify_profile_endpoint`, `verify_ws_owner`, `verify_port_owner`, the census row) inherits ONE rule, and a remote port is recorded in `verified_profile(port)` so the port-only callers (`tab wait`, `tab frames`) are authorised on the fingerprint. A remote verdict carries the refusal CODE it earns (`cdp-identity-changed` / `cdp-unreachable` / `cdp-not-local`) and the shared refusal helpers route it, rather than re-reading it as `cdp-not-local` |
| Re-verify on every connection | One bounded `GET /json/version` immediately before EACH websocket connection (browser endpoint and page endpoint), memo evicted like `verify_ws_owner` (`refresh=True`) — not "once per invocation": between an invocation-level check and a connection the port can be rebound, which is the gap the kernel path already closes. The same GET feeds the `version` block `_endpoint_details` reports. On mismatch nothing is sent; the residual check-then-connect window is the one `verify_ws_owner` already documents |
| Trust level | **Weaker than `/proc`, and documented as such.** The fingerprint is the endpoint's self-report; a hostile local process can forge it. The attach record is a CLI-level authorization, not a security boundary (trust.md already says loopback is not a per-user boundary) |
| Consent | `remote attach` is the consent verb (`write` class). No extra `--yes`; `--deny write` still stops it |
| Binding | `remote tab` runs under a binding: the chosen record is the ONE remote row for the duration of the call. `browsers()` appends a synthetic row for the ACTIVE binding only — never for records at large — so the tab verbs run through their existing resolution and the EndpointGuard seam unchanged, and a flat call (no binding) cannot see a remote at all. The local `--profile` scope stays empty for a remote call; `--profile`/`--browser` combined with a `remote` call refuse `bad-args` (one target) |
| Internal key | The record is keyed by `<root>/.remote/<host>-<port>` (the binding's profile identity), built at USE, never a directory, and never shown in a reply — URLs are what callers see. Nothing creates the key directory; a path under `<root>/.remote/` is never a profile |
| Record | `{kind: "remote", url, host, port, fingerprint, product, pid: 0, attached_at}`. A missing/malformed record still reads as "no attachment" |
| Family check | At attach only: an answer whose `Browser` is not a Chromium-family product fails `cdp-not-local`. The predicate matches product prefixes (`Chrome/`, `HeadlessChrome/`, `Chromium/`, `Edg/`, `Brave/`, `Vivaldi/`), not `proc.BROWSER_EXES` (binary names). Remote rows are verified by fingerprint, never upgraded to `/proc` trust |
| Local lifecycle verbs | `attach`/`detach`/`attach --list` keep their meaning and touch only `kind: "local"` records; `remote attach/detach/list` touch only `kind: "remote"`. `detach --all` does NOT revoke remote records — `remote detach --all` does. One store, two kinds, each noun owns its own |
| Visibility | `remote list`/`remote info` are where remote attachments are reported; `list`, `info`, `tab list` and `attach --list` stay about THIS machine (`remote tab list` reports the remote browser's tabs) |
| `remote close` | Always refuses `remote-not-closable` (declared, so the answer is a refusal with a code and not `unknown-command`). The record authorizes writes, not ownership; the core message stays environment-agnostic ("stop it where it runs") and the microVM spelling lives in docs/skills (§10.4) |
| `remote tab close` | Works, with the same caveat as any browser: closing the LAST page tab exits the remote browser (Chromium exits with its last window). `remote close` still refuses to signal it, and the attach note says so. No new code |
| `remote tab upload` | Refuses with `remote-path`: `DOM.setFileInputFiles` names a path on the **browser's** filesystem, which is not this machine's. Follow-up only if earned (§10.3) |
| `remote tab screenshot`, `remote tab js --out` | Work: the data crosses CDP and the file is written HERE (that is the useful direction) |
| `open` / `profile` | Unchanged: `open` launches a LOCAL managed browser, never adopts a remote. A `<root>/.remote/…` path is never a profile (`Instance.resolve`/`instance_dir` refuse it), so the key cannot become a directory or a `profile reset` target |
| Plugins | Out of scope for remote in v1: plugin actions stay local, and `remote <plugin>` is not a spelling. A plugin driving a remote browser is a follow-up if earned |
| Tunnel management | **Out of scope.** The CLI does not start, supervise or stop `ssh -L`; a dead tunnel is `cdp-unreachable` |
| Re-record after a mismatch | `remote attach` over an existing record with the SAME fingerprint is idempotent (`already: true`); with a DIFFERENT fingerprint it refuses `cdp-identity-changed` — `remote detach` first, then `remote attach`. A one-step `--force` re-bless does not exist |
| Stale records | No expiry: a record outlives its tunnel and earns `cdp-unreachable` on next use; `remote detach` (or `--all`) is the cleanup. `remote list` reports it (`running: false`) rather than hiding it |
| Policy classes | `remote attach`/`remote detach`/`remote close` are `write`; `remote list`/`remote info` are `read`; `remote tab <verb>` carries the SAME classes as `tab <verb>` (mode included: `remote tab wait --for js` is `code`+`write`+`egress`), resolved by delegation in `action_of` with a hermetic mirror check |

## 4. Integration points

The first two rows are the seam; the CLI rows are the new surface; the rest is
mechanical.

| Where | Change |
| --- | --- |
| `lib/browser/owners.py` | **The seam.** `EndpointGuard._judge(key, port)` checks the attachment store for a remote record for `(norm(key), port)`; when one matches, the verdict is the fingerprint comparison, and the port is recorded in `_verified[port]`, so `verify_port_owner` authorises remote ports too. `verify_profile_endpoint`/`verify_ws_owner` then re-read the fingerprint at their own call (each connection), and the refusal helpers (`not_local_refusal`, `verify_ws_owner`) route a remote verdict to its code instead of hard-coding `cdp-not-local` |
| `lib/browser/readback.py`, `lib/dom/tab.py`, `lib/dom/frames.py` | No new oracle here — these are the call sites that must INHERIT it: `browser_call_at`/`page_ws`/`page_session`/`page_eval`, and `verify_port_owner` in the DOM tier. Their tests are the proof (`remote tab wait`, `remote tab frames` run on the port-only path) |
| `cli/registry.py` | A `REMOTE_SUBCOMMANDS` table beside `TAB_SUBCOMMANDS`/`PROFILE_SUBCOMMANDS`, and `action_of` learns the noun: `remote attach/detach/list/info/close` resolve to their own declared actions, and `remote tab <verb> [<mode>]` resolves by DELEGATION to the declared `tab <verb>` action — the same reader the gate and the verb use, so the `--for` mode seam stays one function. `unclassified` gains the remote table; a hermetic test asserts the mirror (every `remote tab X` resolves to `tab X`'s classes) |
| `cli/main.py` | `cmd_remote`: parse the subcommand, pop `--cdp-url` (anywhere in the argv, like the existing `_switch`/`_pop` readers), resolve the record, refuse `--profile`/`--browser` combinations, bind the record, and delegate `remote tab …` to the existing `cmd_tab`; `remote close` is the declared refusal; usage lines and help |
| `lib/scope.py` | The remote binding, set and CLEARED by the dispatcher like `profile`/`frame` (the CLI builds one invocation per process; nothing inherits the last call's target) |
| `lib/browser/machine.py` `browsers()` | Append ONE synthetic row for the ACTIVE binding only: `{pid: 0, exe: "", path: "", profile: <key>, profile_from: "remote", managed: false, attached: true, headless: null, remote: true, product, cdp: {port, reachable, verified, remote: true, url, product, port_from: "record"}}`. One remote row can exist per census, so no `pid: 0` collision; the `headless: null` value must survive `BrowserRow.from_dict`/`as_brief`/`as_row` (no `bool()` collapse to "windowed") |
| `lib/browser/machine.py` `driver_port()` | Unchanged: the binding's row is what it finds, so a remote call's port resolves the same way a row-less local call's does |
| `new lib/browser/remote.py` (name TBD) | Fingerprint fetch + **pure** compare function + family predicate + refusal-code routing, so the hermetic tier can test it without a socket. The fetch REUSES `cdp._get_port` (loopback URL literal, redirect refusal, proxy suppression, `GET_DEADLINE_S` and the reader shutdown); a fresh HTTP client would lose the redirect guard. `version_at`'s error-swallowing is not enough: the fingerprint path separates "no answer" (`cdp-unreachable`) from "answered nonsense" (`cdp-not-local`) |
| `lib/attachments.py` | Record grows `kind`/`url`/`fingerprint`/`product`; keying, cap and fail-closed reads unchanged; kind-filtered lookups (`records(kind=…)`, a url lookup for attach/detach/re-attach, a key lookup for the seam); `remote list` reads the remote kind only |
| `lib/capabilities.py` | New declared actions for the lifecycle subcommands; the tab mirror is checked in the hermetic test, not copied into the table. `selftest` reports the remote surface like any other |
| `lib/dom/actions.py` upload | Refuse when the binding is remote, `remote-path` |
| `lib/profile/` (`Instance.resolve`, `info`, `reset`, `seed`, `stores.logins`) and `lifecycle.instance_dir()` | A path under `<root>/.remote/` is never a profile, `not-managed` — defense in depth, since the key is never advertised |
| `lib/locks.py` / `lib/paths.py` | No new lock file: `remote attach`/`detach` serialize through the attachment store's own root lock, and drives take no profile lock (they never did). Audit that no remote path creates the key directory |
| `lib/cdp/rpc.py` `_checked_ws` | **Unchanged** — the tunnel is loopback by construction. Second gate, not a hole |
| `lib/errors.py` | `ERR_CDP_IDENTITY_CHANGED`, `ERR_REMOTE_NOT_CLOSABLE`, `ERR_REMOTE_PATH` |
| `cli/verbs/…`, `lib/audit.py` | A `remote` verb module beside the others; audit logs url/host-port + fingerprint `product` (there is no secret to redact) |
| docs + skills | `README.md`, `docs/sessions.md`, `docs/trust.md`, `docs/refusals.md`, `docs/verbs.md`, `docs/progress.md`, `skills/browser-control/{SKILL.md,references/*}`, and the `mark-browser-control` skill installed for the agent — the `remote` group, the last-tab caveat, and, in docs/skills only, the `microvm-control stop <id>` hint |

## 5. What the oracle proves, and what it does not

The rule amendment has to be honest, because it trades a kernel fact for a
self-report:

- **It does prove** the endpoint answered `/json/version` as a Chromium-family
  browser at attach time, and that the same self-report still answers at every
  later connection. A port reused by a different local listener, or a
  different browser behind the same tunnel, fails the comparison →
  `cdp-identity-changed`, and **nothing is driven**. A restarted tunnel to a
  different machine fails the comparison too — but only because that machine's
  product, protocol, user-agent or V8 version differs; see the next bullet.
- **It does not prove** the endpoint is the browser the caller believes it
  is: `/json/version` is unauthenticated and forgeable by any local process
  that holds the port, and a re-provisioned VM from the same image reports the
  same fingerprint. A VM reboot with the same Chromium version keeps the
  fingerprint (intended); a guest browser upgrade changes it and earns a
  refusal until re-attached (intended, and it will surprise someone — say so
  in the message).
- **The trust delta is stated in trust.md**: the `/proc` rule stays the rule
  for local browsers; a `remote` attachment is the one endpoint this tool
  drives on stored identity alone.
- **The attach record is not a security boundary.** Loopback CDP has no
  authentication; any local process can already drive the tunnelled port. The
  record is this CLI's own authorization ledger, exactly like `attach` today.
- **Considered and not taken: listener identity at attach.** Storing the
  tunnel listener's kernel identity (pid + start time, or the socket inode —
  `cdp.listener_of` already answers it) and requiring "same listener AND same
  fingerprint" would catch port takeover with a kernel fact instead of a
  self-report. It is not in v1 because a restarted `ssh -L` (a new pid) would
  force a re-attach even when nothing else changed, and the churn buys little
  against the threat the record actually claims to meet. Revisit if the trust
  delta proves too wide in practice.

## 6. Verb semantics

| Call | Remote behavior |
| --- | --- |
| `remote attach`, `remote detach`, `remote list`, `remote info` | Work: the attachment lifecycle. `remote list` reports url, product, `reachable`, `verified`, `running` |
| `remote tab text/find/extract/js/wait/click/hover/check/select/scroll/focus/press/insert/type/dialog/media` | Work unchanged: CDP is CDP. `--tab`/`--frame` and every tab flag read the same as the local verb |
| `remote tab nav/back/forward/reload/activate`, `remote tab [URL…]`, `remote tab URL` | Work; `remote tab URL` creates tabs in the remote browser |
| `remote tab close` | Works. Closing the LAST page tab exits the remote browser (Chromium exits with its last window); `remote close` still refuses to signal it, and the attach note carries the caveat |
| `remote tab screenshot`, `remote tab js --out` | Work: the data crosses CDP and the file is written HERE |
| `remote tab upload` | Refuse `remote-path` (the path is the browser's filesystem, not this machine's) |
| `remote close` | Refuse `remote-not-closable` — declared so the answer is a code, not a missing verb |
| flat `open`/`close`/`list`/`info`/`attach`/`detach`/`tab`/`profile` | Unchanged, and never remote: a remote endpoint is not a row, a profile, or a target. `open` starts a local browser; `tab` drives local ones |
| `selftest` | Reports the `remote` actions like every other (delegated classes included) |
| plugins | Local only in v1 |

## 7. Refusal codes

| code | when |
| --- | --- |
| `bad-args` | a malformed `--cdp-url` (non-loopback, userinfo, path, query, no port, `ws://`); `remote detach` with neither `--cdp-url` nor `--all`; a `remote` call combined with `--profile`/`--browser`; a missing `remote` subcommand |
| `cdp-not-local` | the URL answers, but not as a Chromium-family CDP endpoint (checked at attach; an answer that is not readable JSON fails here too) |
| `cdp-unreachable` | the endpoint stopped answering — tunnel down, browser gone; or a target was needed and NO remote is attached |
| `cdp-identity-changed` | the endpoint answers, but its stored fingerprint changed; and `remote attach` over an existing record whose fingerprint differs. Fix: confirm the endpoint, then `remote detach` and `remote attach` |
| `remote-not-closable` | `remote close` |
| `remote-path` | `remote tab upload` — a file argument names the browser's filesystem, not this machine's |
| `ambiguous-browser` | a `remote` call with no `--cdp-url` while more than one remote is attached (reused; the message names both URLs and the fix) |
| `not-attached` | `remote detach`/`remote info` naming a URL with no record (reused) |

Codes that are NOT part of the remote story: `not-managed` stays only as the
defense-in-depth guard on a `<root>/.remote/…` path, because the key is never
a caller-facing name — it is not a refusal a remote user should ever meet.

## 8. Testing

Tier 2 (hermetic) already has the fakes; the remote path is mostly testable
without a browser:

1. `remote attach` URL policy: non-loopback, userinfo, no port, `ws://`,
   path/query, with `--profile`/`--browser` → `bad-args`; nothing written; no
   key directory.
2. Fake `/json/version` answering `HeadlessChrome/153…` → attach records the
   fingerprint; `remote list` shows url/`running: true`/`reachable: true`;
   no internal key in any reply.
3. Same fake + fake websocket peer → `remote tab text` **and the port-only
   path (`remote tab wait`, `remote tab frames`)** pass the gate.
4. Isolation, from the same fixture: a flat `tab text` cannot reach it (no
   row), `close --port N` refuses `no-browser`, `--profile <key>` does not
   drive, and after `remote detach` any spelling refuses.
5. The check is per CONNECTION: swap the fake between two connections in one
   invocation → the second refuses `cdp-identity-changed` and sends nothing on
   the new socket.
6. Swap the fake for a different `Browser`/`User-Agent` → a drive refuses
   `cdp-identity-changed` **and sends nothing to the socket**.
7. Kill the fake → `cdp-unreachable`; `remote list` reports `running: false`.
8. `remote close`, `remote tab upload` → the two new refusals.
9. Re-attach with the same fingerprint → `already: true`; with a changed
   fingerprint → `cdp-identity-changed`, and the old record survives until
   `remote detach`.
10. Two records with no `--cdp-url` → `ambiguous-browser`; none → `cdp-unreachable`;
    `remote detach --all` clears both; local `detach --all` leaves them.
11. `remote list`/`remote info` on a malformed store still read as no records.
12. Capabilities: `remote tab click` resolves to `tab click`'s classes,
    `remote tab wait --for js` to the `js` mode's, and `unclassified` is empty
    with the remote table included.
13. The census stays one `/proc` walk and, with no binding, contains no remote
    rows.

Tier 3 (live): a local Chromium started by hand outside the managed root
(`--remote-debugging-port=N --user-data-dir=<elsewhere>`), attached by
`remote attach --cdp-url` to its port — the same browser the census can see as
a process, which is exactly what makes the no-`/proc` path observable.

End-to-end (this is where the feature earns out, in the microvm repo):
`tests/accept/m6-remote-cdp.sh` — `microvm-control create/start`, guest
Chromium with `--remote-debugging-port`, `ssh -L 9222`,
`remote attach --cdp-url http://127.0.0.1:9222`, `remote tab text` +
`remote tab click` + `remote tab screenshot`, `remote detach`; then tunnel
killed → `cdp-unreachable`; then the port taken by a listener that ANSWERS
`/json/version` with a different product → `cdp-identity-changed` (a bare TCP
listener is `cdp-unreachable`, not an identity change).

## 9. Milestones

| # | Deliverable | Acceptance |
| --- | --- | --- |
| M1 | The `remote` grammar + registry/action_of/help + URL policy + records (kind, fingerprint) + attach/detach/list/info + the codes | Hermetic 1, 2, 8, 11, 12 |
| M2 | The binding + the `EndpointGuard` seam (per-connection fingerprint, `verify_port_owner` coverage) + `remote tab` delegation + isolation + `remote close`/`remote-path` refusals | Hermetic 3–7, 9, 10, 13 |
| M3 | `remote list` running semantics, `remote tab close` caveat, audit, policy review, docs + skills | Docs updated in the same change; the docs↔code sync check the microvm repo uses (verbs + codes) adapted here |
| M4 | Live battery + microVM acceptance + `docs/progress.md` + remove "remote/forwarded CDP" from plan.md §7 | Tier 3 + `m6-remote-cdp.sh` |

## 10. Open questions (with this revision's answers)

1. **Grammar.** DECIDED: the `remote` noun, as above. Every remote action is a
   `remote …` call; the flat verbs gain no remote spelling.
2. **Fingerprint field set.** DECIDED: `Browser`, `Protocol-Version`,
   `User-Agent`, `V8-Version`, compared exactly; a protocol-version change
   alone re-attaches; `webSocketDebuggerUrl`'s path is not part of the
   identity.
3. **Upload follow-up.** DECIDED for v1: refuse, and let the microVM flow copy
   first (`microvm-control cp`). A `--browser-path` escape is a caller
   assertion this tool cannot verify and is not planned.
4. **`remote close` message.** DECIDED: the core text stays
   environment-agnostic ("stop it where it runs"); the
   `microvm-control stop <id>` spelling lives in docs/skills, matching the
   existing no-tool-specific-advice precedent.
5. **Lock placement.** DECIDED: none needed. `remote attach`/`detach` hold the
   attachment store's root lock (the existing record discipline); drives take
   no profile lock, exactly as today, and the internal key's directory is
   never created.
6. **Code names.** STILL OPEN for sign-off: `cdp-identity-changed` /
   `remote-not-closable` / `remote-path`. The vocabulary is closed and every
   name is forever.
7. **Stale records.** DECIDED: no `gc`; a stale record is `cdp-unreachable`
   on next use, `remote list` reports `running: false`, and `remote detach`
   cleans up.
8. **Last-tab close.** DECIDED: `remote tab close` on the last page tab exits
   the remote browser and stays allowed (it is a tab write); the attach note
   and docs state the caveat, and no new code is added.
9. **Listener identity at attach.** CONSIDERED, not in v1 — see §5. Revisit
   only if the trust delta proves too wide.
10. **Plugins on a remote.** DEFERRED: v1 has no `remote <plugin>` spelling.
    If a plugin ever needs a remote browser, it should go through the same
    binding and declare the same classes.

## 11. Explicitly out of scope

Tunnel lifecycle (start/keep/stop `ssh -L`), non-loopback endpoints, TLS or
any authentication on the CDP hop, launching or stopping remote browsers,
uploading local files into a remote browser (including `--browser-path`),
remote profile seeding, plugin actions against a remote browser, any remote
spelling on the flat verbs, and any notion of a remote browser as `managed`.
