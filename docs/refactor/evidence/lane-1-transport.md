# Lane 1: transport — `lib/cdp.py`, `lib/errors.py`, `lib/capabilities.py`

## Summary
- This slice is the CDP transport floor: `cdp.py` finds the endpoint (`DevToolsActivePort` → `/proc` listener → HTTP `/json`), shapes target rows, and speaks one websocket method call (plus a `Session` for call sequences); `errors.py` is the single refusal type; `capabilities.py` is the declared action→class table plus the policy gate.
- Biggest structural problem, ranked 1st: `cdp.py` is 891 lines that mix three responsibilities (endpoint discovery, target-row shaping, websocket session) — a package-split candidate.
- Ranked 2nd: connection state has **one** owner (`Session`) but **two** ways to speak CDP — `call`/`evaluate`/`evaluate_until` each open their own loop/connection and bypass `Session` entirely, and the RPC send/await/demux loop is written four times.
- Ranked 3rd: `capabilities.py` keeps the gate in two module-level mutable globals (`PLUGIN_ACTIONS`, `POLICY`) that test code mutates in place; the declaration and the gate are one module.
- Already fine: import direction is clean and acyclic (`errors` is a leaf with zero package imports; `cdp` and `capabilities` depend only on `errors`); loopback/host checks, payload caps and untrusted-text flattening are single choke points (`_checked_ws`, `_get_bytes`, `_foreign`); `Session` is already the correct class shape for state ownership.
- `errors.py` is correctly tiny, but it holds no taxonomy at all — every error code is an unregistered bare string literal at ~160 raise sites.

## Findings

### F-1.1 `Session` owns connection state, but three module-level functions speak CDP around it
- category: class-candidate
- locations: `browser_control/lib/cdp.py:667` — `class Session`, `browser_control/lib/cdp.py:441` — `call`, `browser_control/lib/cdp.py:635` — `evaluate`, `browser_control/lib/cdp.py:651` — `evaluate_until`, `browser_control/lib/cdp.py:408` — `_call`, `browser_control/lib/cdp.py:578` — `_evaluate`, `browser_control/lib/cdp.py:599` — `_evaluate_until`
- evidence:
  - `Session.__init__` (cdp.py:678-700) is the only place that owns a live connection's state: `self._ws_url`, `self._timeout`, `self._page_domain`, `self.page_domain_ok` (692), `self.parked` (693), `self._loop`/`self._ws`/`self._rid`, `self.events` (700); `Session._connect` (703) creates the loop and the socket.
  - The one-shot helpers never touch `Session`: `call` is `return asyncio.run(_call(ws_url, method, params or {}, timeout))` (441-444), `evaluate` is `asyncio.run(_evaluate(...))` (647), `evaluate_until` is `asyncio.run(_evaluate_until(...))` (663) — three private coroutines (`_call` 408, `_evaluate` 578, `_evaluate_until` 599) that each build their own connection.
  - `Session` already carries the exact switch the browser-level path needs: `def __init__(self, ws_url: str, timeout: float = 15.0, page_domain: bool = True)` (cdp.py:679-680), whose docstring says `page_domain=False` exists for callers that must not run `Page.enable`.
  - `call` is the browser/Target path (`browser_ws` → `browser_call` at cdp.py:403-405, `frame_targets` at cdp.py:308) and `evaluate`/`evaluate_until` are the page path, so both branches are reachable from one class.
- proposal: make `Session` the single connection owner in the new `browser_control/lib/cdp/session.py`: `call`/`evaluate`/`evaluate_until` become thin wrappers (`with Session(ws_url, page_domain=False) as s: return s.call(...)`), and `cdp.py:308` / `cdp.py:403` call those wrappers internally. Names and signatures of `call`/`evaluate`/`evaluate_until`/`Session` stay identical, so caller migration is a no-op at: `browser_control/lib/browser.py:2151`, `browser_control/lib/browser.py:2355`, `browser_control/lib/browser.py:2368`, `browser_control/lib/dom.py:715`, `browser_control/lib/dom.py:869`, `browser_control/lib/dom.py:1183`.
- dependencies: F-1.2 (extract the demux first), F-1.3 (target module location)
- risk: high — every CDP path flows through these, and the error codes (`cdp-error`, `eval-timeout`, `blocked`) are frozen contract; the parked/eval-timeout mapping differs subtly between `_evaluate` and `Session._call` (cdp.py:590 vs cdp.py:797-798) and must be reconciled deliberately.
- acceptance:
  - `grep -n "websockets.connect" browser_control/lib/cdp/*.py` returns exactly one site, inside `Session._connect`
  - `cdp.call`, `cdp.evaluate`, `cdp.evaluate_until` keep their current signatures and return types
  - `tests/test_unit.py` green (supervisor to run)
  - `selftest` and one live `tab js` / `tab click` produce byte-identical JSON fields as before

### F-1.2 The JSON-RPC send / await / demux loop is implemented four times
- category: duplication
- locations: `browser_control/lib/cdp.py:408` — `_call`, `browser_control/lib/cdp.py:512` — `_page_enable`, `browser_control/lib/cdp.py:551` — `_sample`, `browser_control/lib/cdp.py:776` — `Session._call`
- evidence:
  - The send frame `json.dumps({"id": …, "method": …, "params": …})` is written at cdp.py:416-417, 514-515, 553-554, 780-781.
  - The reply demux `if msg.get("id") == rid:` appears at cdp.py:425, 528, 567, 811.
  - The timeout→refusal mapping is re-derived in each: `fail("cdp-error", …)` (426-431), `raise ControlError("blocked", BLOCKED_HINT)` (534, 548), `fail("cdp-error", …)` (570), and `code = ("eval-timeout" if method.startswith("Runtime.evaluate") else "cdp-error")` (797-798).
  - `websockets.connect(...)` is called at exactly four sites: cdp.py:412, 581, 611, 708.
  - Non-JSON frames are handled separately in each loop (`except (ValueError, TypeError)` at 536, 563, 806).
- proposal: one coroutine `_await_reply(ws, rid, budget) -> dict` in the session module that owns send, `asyncio.wait_for(ws.recv())`, id demux, error→`ControlError` mapping and the non-JSON guard; `_page_enable`, `_sample` and `Session._call` call it. `Session._call` keeps the parked/dialog branch layered on top.
- dependencies: blocks F-1.1
- risk: medium — pure factoring, but the four copies differ in error text and in whether they `fail` vs `raise`; the unified helper must preserve each message.
- acceptance:
  - one `json.dumps({"id":` send site and one `msg.get("id")` demux site in the session module
  - `tests/test_unit.py` green (supervisor to run)
  - `Page.enable` refusal still reports `blocked` with `BLOCKED_HINT` (cdp.py:506)

### F-1.3 `cdp.py` (891 lines) holds three responsibilities that should be a package
- category: package-split
- locations: `browser_control/lib/cdp.py:54-267` — endpoint discovery (`port_of`, `_get_bytes`, `_get_port`, `get_json`, `_proc_text`, `_exe_basename`, `_listening_inodes`, `listener_of`, `answers`, `version_at`, `reachable`), `browser_control/lib/cdp.py:269-406` — target rows / URL shaping (`page_rows`, `page_rows_at`, `frame_rows`, `frame_targets`, `_of_kind`, `_pages`, `target_ws`, `rows_to_tabs`, `_checked_ws`, `browser_ws`, `browser_call`), `browser_control/lib/cdp.py:408-891` — websocket session (`_call`…`Session`, `_foreign`, `_value_of`, constants 501-509)
- evidence:
  - The module docstring already draws the seam: "Only two things speak CDP here: `get_json` (HTTP, capped) and `call` (one websocket call, one deadline, never retried)." (cdp.py:5-8).
  - The endpoint group reads `/proc/net/tcp{,6}` and `/proc/<pid>/fd` (`_listening_inodes` 171, `listener_of` 195) and HTTP (urllib) — no websocket code — while the session group never touches `/proc` or urllib except the host check.
  - The two live call sites in-repo import the module whole and reach ~40 attributes (`cdp.X` appears 55 times across `browser.py`, `dom.py`, `cli/main.py`; e.g. `browser.py:171`, `dom.py:667`, `main.py:360`), so a facade matters.
- proposal: `browser_control/lib/cdp/` package:
  - `browser_control/lib/cdp/__init__.py` — re-export the current public names so `cdp.X` call sites need no edit.
  - `browser_control/lib/cdp/endpoint.py` — port file + HTTP JSON + kernel listener (`port_of`, `get_json`, `listener_of`, `answers`, `version_at`, `reachable`).
  - `browser_control/lib/cdp/targets.py` — `/json` row shaping and host-checked ws URLs (`page_rows*`, `frame_rows`, `frame_targets`, `target_ws`, `rows_to_tabs`, `_checked_ws`, `browser_ws`, `browser_call`).
  - `browser_control/lib/cdp/session.py` — `Session` plus `call`/`evaluate`/`evaluate_until` (F-1.1).
  - `browser_control/lib/cdp/rpc.py` — `_await_reply` (F-1.2), `_foreign`, `_value_of`, the cap constants.
- dependencies: F-1.1, F-1.2
- risk: medium — a pure module move, but a package needs `__init__.py` re-exports or 55 call sites change; `cdp.PORT_FILE` (cdp.py:40) and `cdp.websockets` (33) are read externally (`tests/test_unit.py:141`, `cli/main.py:360`) and must survive re-export.
- acceptance:
  - `python -c "from browser_control.lib import cdp; cdp.Session; cdp.get_json; cdp.page_rows"` succeeds
  - `grep -rn "^from browser_control.lib import cdp" browser_control` shows unchanged imports
  - no module in `lib/cdp/` exceeds ~350 lines
  - `tests/test_unit.py` green (supervisor to run)

### F-1.4 `capabilities.py` keeps the gate in mutable module globals and mixes declaration with enforcement
- category: hidden-state
- locations: `browser_control/lib/capabilities.py:138` — `PLUGIN_ACTIONS`, `browser_control/lib/capabilities.py:141-150` — `set_plugins` (mutates in place), `browser_control/lib/capabilities.py:167-169` — `POLICY`, `browser_control/lib/capabilities.py:211-269` — `policy` (updates `POLICY` in place), `browser_control/lib/capabilities.py:120-131` — `by_class`, `browser_control/lib/capabilities.py:272-278` — `describe`, `browser_control/lib/capabilities.py:280-317` — `allowed`
- evidence:
  - `PLUGIN_ACTIONS: dict[str, tuple[str, ...]] = {}` (138) is cleared and refilled by `set_plugins` (147-150); every reader (`by_class` 125, `allowed` 285) reads the global rather than a passed value.
  - `POLICY: dict = {...}` (167-169) is overwritten via `POLICY.update({...})` inside `policy()` (263-268) and read by `describe` (274-277) and `allowed` (283-305).
  - Tests reach into both globals to restore them: `capabilities.POLICY.update(original)` (`tests/test_unit.py:1697`, `1844`) and `original = dict(capabilities.POLICY)` (1649, 1784).
  - The module holds both halves at once: the frozen declaration (`CLASSES` 37, `ACTIONS` 42) and the enforcement (`# the gate` section begins at capabilities.py:153).
- proposal: split the value objects from the module functions:
  - `browser_control/lib/capabilities.py :: class Surface` — owns `CLASSES`, `ACTIONS`, the plugin actions, and derives `unclassified()` / `by_class()` / `classes_for(action)` once instead of scanning `ACTIONS` with `startswith` on every call (unclassified 108-118).
  - `browser_control/lib/policy.py :: class Policy` — a value object with `allow`, `deny`, `allow_set`, `deny_set`, `source`, `enforced`, a constructor `Policy.from_sources(allow, deny, env)` (the body of `policy()` 211-269) and `allowed(action)` (280-317).
  - `main()` builds one `Policy` per invocation and passes it to the gate; `describe()`/`by_class()` become methods.
  - caller migration: `browser_control/cli/main.py:378-386` (selftest report), `:1080` (`_modes` reads `ACTIONS`), `:1178` (`set_plugins`), `:1234` (`policy`), `:1267` (`allowed`); `browser_control/lib/plugins.py:175,180` (reads `CLASSES`).
- dependencies: none
- risk: medium — the gate is process-global by design (env vars, one CLI invocation), so the change must keep the "no policy set means no gate" and fail-closed behaviour intact; converting to an instance is mechanical but touches 6 call sites and 4 tests.
- acceptance:
  - zero module-level mutable dicts in `capabilities.py` (no `PLUGIN_ACTIONS`, no `POLICY`)
  - `import direction is capabilities -> errors only` (unchanged) and `policy -> errors, capabilities` only
  - `capabilities.unclassified(cli_main.HANDLERS, {...}) == []` still holds (`tests/test_unit.py:1567`)
  - `tests/test_unit.py` green (supervisor to run)

### F-1.5 Error codes are unregistered bare string literals; `errors.py` holds no taxonomy
- category: naming
- locations: `browser_control/lib/errors.py:11` — `class ControlError`, `browser_control/lib/errors.py:20` — `def fail`; raise sites e.g. `browser_control/lib/cdp.py:355` — `code = "no-page-tab" if kind == "page" else "no-frame"`, `browser_control/lib/browser.py:98` — `ControlError("no-browser", …)`, `browser_control/cli/main.py:1241` — `ControlError("unknown-command", …)`, `browser_control/cli/main.py:1269` — `fail("not-allowed", …)`
- evidence:
  - `errors.py` is 21 lines total and defines only the exception, `__init__(self, code, message)` (14) and `fail` (20) — there is no list of valid codes and no validation point.
  - cdp alone spells 10 distinct codes inline: `cdp-not-local` (80, 384, 386), `cdp-unreachable` (114, 121, 142), `result-too-large` (123, 490), `cdp-error` (~20 sites), `no-websockets` (445, 644, 660, 686), `js-error` (478, 870), `blocked` (534, 548, 726, 794, 802), `eval-timeout` (590, 597), `no-page-tab`/`no-frame` (355).
  - `fail("...")` vs `raise ControlError("...")` are used interchangeably for the same codes (cdp.py:426 `fail("cdp-error"...)` inside a coroutine that otherwise `raise ControlError("cdp-error"...)`).
- proposal: add a registry to `errors.py` — `CODES: frozenset[str]` (or a `Code` `StrEnum`) plus named constants (`ERR_CDP_ERROR = "cdp-error"` …) and use those constants at the raise sites. Keep `ControlError.__init__` behaviour unchanged (do not validate, or the frozen codes would become a runtime failure surface).
- dependencies: none
- risk: low — pure substitution of string literals with constants; codes themselves are frozen.
- acceptance:
  - `errors.CODES` lists every code currently raised (grep of `fail(`/`ControlError(` literal first args ⊆ `CODES`)
  - no bare code literal remains in `lib/` and `cli/` raise sites
  - `tests/test_unit.py` green (supervisor to run)

### F-1.6 (P2) Tests patch module-level `cdp.*` names and globals; the intended refactor will move those seams
- category: test-coupling
- locations: `tests/test_unit.py:596,727` — `browser.cdp.page_rows_at = page_rows_at`, `tests/test_unit.py:803,819,830,834` — `cdp.port_of = lambda …`, `cdp.browser_call = lambda …`, `tests/test_unit.py:1423-1424` — `cli_main.cdp.websockets = None`, `tests/test_unit.py:1901-1903` — `cdp.evaluate/port_of/target_ws = lambda …`, `tests/test_unit.py:2011` — `patched = cdp.evaluate`, `tests/test_unit.py:1697,1844` — `capabilities.POLICY.update(original)`
- evidence:
  - The hermetic suite drives the transport by assigning module attributes directly (e.g. `cdp.target_ws = lambda port, target, kind="page": f"ws://{kind}/{target}"` at test_unit.py:1903).
  - `capabilities.POLICY` is mutated and restored by hand in two tests (1697, 1844) because the state has no instance handle.
  - `cdp.websockets` is a rebindable module global (cdp.py:33-38) that tests flip to `None` (test_unit.py:1424).
- proposal: this is a consequence of F-1.1 and F-1.4, not an independent defect. While doing those, expose the seam tests need rather than a module attribute — e.g. `Session(..., connect=websockets.connect)` and a `Policy` object — so tests patch one injected handle. No behaviour change.
- dependencies: F-1.1, F-1.4
- risk: low — test-only churn, but it must be planned with F-1.1/F-1.4 or those refactors will break ~15 assertions.
- acceptance:
  - no test assigns `browser.cdp.<fn>` or `capabilities.POLICY`
  - `tests/test_unit.py` green (supervisor to run)

## Proposed target layout for this slice
- `browser_control/lib/errors.py` — refusal type + the one code registry (`ControlError`, `fail`, `CODES`, code constants).
- `browser_control/lib/cdp/__init__.py` — facade re-exporting the current `cdp.*` public surface so no call site changes.
- `browser_control/lib/cdp/endpoint.py` — port file, capped HTTP JSON, kernel listener lookup (`port_of`, `get_json`, `listener_of`, `answers`, `version_at`, `reachable`).
- `browser_control/lib/cdp/targets.py` — `/json` row shaping, target→ws resolution, host checks (`page_rows`, `frame_targets`, `target_ws`, `rows_to_tabs`, `_checked_ws`, `browser_ws`, `browser_call`).
- `browser_control/lib/cdp/rpc.py` — one RPC framing/demux path and untrusted-text handling (`_await_reply`, `_foreign`, `_value_of`, cap constants).
- `browser_control/lib/cdp/session.py` — `class Session` as the only connection owner, plus `call`/`evaluate`/`evaluate_until` wrappers.
- `browser_control/lib/capabilities.py` — declaration only (`CLASSES`, `ACTIONS`, `class Surface`).
- `browser_control/lib/policy.py` — the gate as a value object (`class Policy`, `allowed`, `describe`).

## Open questions
- Is `capabilities.POLICY` deliberately process-global (env vars, one CLI invocation) or may the gate become a per-invocation `Policy` instance? This decides whether F-1.4 is a value-object refactor or just a move into a second module.
- For F-1.3: keep `browser_control.lib.cdp` as a re-export facade (zero call-site churn) or require explicit `cdp.endpoint`/`cdp.session` imports and update the 55 `cdp.X` references?
- For F-1.5: is centralising ~10 codes worth a registry, or should codes stay literals and only a test-side `CODES` inventory be added?
- For F-1.1: is reconciling `_evaluate`'s `eval-timeout` (cdp.py:590) with `Session._call`'s `startswith("Runtime.evaluate")` mapping (cdp.py:797) intended to produce identical codes for the same failure — i.e. must the folded path keep exactly one of them?