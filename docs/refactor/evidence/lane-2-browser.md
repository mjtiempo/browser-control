# Lane 2: `browser_control/lib/browser.py` (2455 lines)

## Summary
- This slice is the whole browser tier: it resolves the binary/profile (`binary` 90, `profile_dir` 110, `instance_dir` 139), owns the launch/stop lifecycle (`launch` 1367, `stop` 1512), the attach registry (`attach` 708, `detach` 768, `attachments` 693), the machine census (`browsers` 476), the `/proc`+kernel endpoint identity guard (`endpoint_owner` 815, `_verify_profile_endpoint` 1171), the profile/root `flock` (`_lock` 1305), tab addressing and the tab verbs (`resolve_tab` 247, `tab_info` 1706, `close_tabs` 1862, `new_tab` 2031), and the page verbs (`nav` 2265, `history` 2336, `activate` 2394, `reload` 2434).
- **Ranked 1st — no object owns any of the state.** 2455 lines contain **zero classes, dataclasses or TypedDicts**; state lives in two module-level mutable dicts (`SCOPE` 121, `_OWNER_CACHE` 812) and three on-disk stores (`.pid` 76/422/1590, `attached.json` 530/533/550, `.browser-control.lock` 1224/1228) whose read-modify-writes are written out longhand in three modules (browser.py:752-755, 790-803; profile.py:503-505).
- **Ranked 2nd — the "browser row" is an untyped dict that is simultaneously the internal struct and the frozen JSON**, with 8 keys plus a nested `cdp` dict of 6 more, indexed 30 times as `["cdp"]` and re-derived by four separate projection functions (`_brief` 951, `_row` 958, `_endpoint_details` 965, `_foreign_row` 1716).
- **Ranked 3rd — the module is 12 separable clusters** (paths, spec matching, `/proc` identity, census, attach registry, selection, endpoint guard, read-back waiters, lock, lifecycle, tab verbs, page verbs). `docs/plan.md` §2 already named two of them as separate modules (`tabs.py`, `nav.py`) and `docs/progress.md:142` records that they were folded in at 1281 lines — the boundary claim has not been re-checked since.
- **Ranked 4th — the same rule is written three times**: the `--port|--pid|--profile` "name one browser" validator (716-718, 781-783, 1534-1536 with two different arities) and its row lookup (719-731, 1486-1495); and `row["managed"] or row["attached"]` — the "may this CLI write here" predicate — is spelled out at 8 sites (612, 911, 1023, 1661, 1685, 1743, 1765, 1836).
- **Already fine, do not "fix":** the refusal discipline is uniform (every refusal is `fail(code, msg)`; `browser.py` never prints — `cli/main.py` owns stdout/stderr); the endpoint guard is a genuine single choke point applied on **every** write path (`_open_tabs` 1201, `close_tabs` 2003) and never drives an unverified row; the `flock` design is correct and documented (kernel-released, no stale lock, fail-open-with-warning when the filesystem cannot lock at all); and the deliberate split between exact (`_exact_spec_match` 1772) and substring (`_match_spec` 211) tab specs is documented behaviour, not drift.
- Verdict: **OK with notes.** This is a whole-file structural review (no diff); every finding is contract-preserving and none is a correctness bug reachable today.

## Findings

### F-2.1 No type owns the browser row — one untyped dict is both the internal struct and the frozen JSON
- category: class-candidate
- locations: `browser_control/lib/browser.py:476` — `browsers`, `browser_control/lib/browser.py:495-517` — the endpoint dict and the row literal, `browser_control/lib/browser.py:815` — `endpoint_owner`, `browser_control/lib/browser.py:951` — `_brief`, `browser_control/lib/browser.py:958` — `_row`, `browser_control/lib/browser.py:965` — `_endpoint_details`, `browser_control/lib/browser.py:1716` — `_foreign_row`
- evidence:
  - `grep '^class \|dataclass\|TypedDict' browser_control/lib/browser.py` is empty across 2455 lines; the row is built as a literal at 509-517 (`{"pid", "exe", "path", "profile", "profile_from", "managed", "attached", "cdp"}`) with the nested endpoint literal at 495-506 (`port`, `reachable`, `verified`, `listener{pid,exe}`, `tabs`, `reason`).
  - The same field is read two ways in the same file: `row["cdp"].get("verified")` (738, 884-885, 907-908, 941, 1000-1001) versus `row["cdp"]["verified"]` (526, 519, 674, 1504) — 30 sites index `["cdp"]` in total.
  - `_to_int(row["cdp"]["port"])` is re-derived at 730, 743, 746, 888, 945, 955, 1494; the sort keys re-read three of the flags (`518`, `916`, `1023`).
  - Four projections of the one dict exist — `_brief` (951), `_row` (958), `_endpoint_details` (965), `_foreign_row` (1716) — and `_brief` is called 24 times from `lib/dom.py` (`dom.py:785`, `1124`, … `2701`, each with `# noqa: SLF001`).
  - The row is returned **verbatim** as JSON (`list_browsers` 522-527, `list_tabs` groups at 991-993), so the dict is simultaneously the internal representation and the frozen wire contract.
- proposal: `browser_control/lib/browser/machine.py :: @dataclass(frozen=True) class Endpoint` (`port, reachable, verified, listener_pid, listener_exe, tabs, reason`) and `:: @dataclass(frozen=True) class BrowserRow` (`pid, exe, path, profile, profile_from, managed, attached, endpoint`) with `as_reply()`, `as_brief()`, `as_endpoint_reply()` built by the *existing* projection bodies so the JSON keys cannot drift, plus `may_write`/`may_read` (F-2.7). Callers to migrate: `browsers` 476, `_narrow` 571, `_drivable` 892, `_tabs_of` 933, `list_tabs` 976, `browser_info` 1010, `_resolve_across` 1640, `_one_tab` 2089, `_named_browser` 1473, `dom.py:648`/`785`+.
- dependencies: F-2.2 (row construction), F-2.3 (verdict source), F-2.6, F-2.7
- risk: medium — `list`, `info` and `tab list` JSON must stay key-identical; `tests/live_test.py:1340-1350`, `1800-1808`, `1992-2003` assert the exact nested `cdp.listener`/`cdp.tabs`/`cdp.reason` keys, including the *absence* of `listener` when unreachable.
- acceptance:
  - `grep -c '\["cdp"\]' browser_control/lib/browser/` == 0
  - `list`, `info` and `tab list` on a fixed fake census produce byte-identical JSON to the pre-refactor output
  - `python3 tests/test_unit.py` green (supervisor to run)

### F-2.2 Two process-global mutable dicts hold per-call state and a kernel memo
- category: hidden-state
- locations: `browser_control/lib/browser.py:121` — `SCOPE`, `browser_control/lib/browser.py:124` — `scope`, `browser_control/lib/browser.py:812` — `_OWNER_CACHE`, `browser_control/lib/browser.py:815` — `endpoint_owner`
- evidence:
  - `SCOPE: dict[str, str] = {"profile": ""}` (121) is written by the CLI (`cli/main.py:1230 browser_lib.scope(flags["profile"] or "")`) and read at 147, 580, 641, 648-654, 675, 1034, 2077 — and it leaks into another module: `profile.py:230`, `288` (`str(profile or "").strip() or scope()`) and `325` (`"scope": scope()`).
  - `_OWNER_CACHE: dict[tuple[str, int], dict] = {}` (812) memoises `endpoint_owner` (836 read, 861 write) purely as a perf shortcut ("One /proc walk per (profile, port) per process", 810-811), but invalidation is manual and only two of the five call sites do it: `_OWNER_CACHE.pop(...)` at 1081 (`_wait_own_port`) and 1360 (`_await_owner`), while `_verify_profile_endpoint` (1186) and `_page_count` (1465) read the memo with no eviction at all.
  - The key is `(profile, port)`, so a verdict cached for a port that a *different* process took later is still returned inside the same process — the docstring at 815-835 argues the rule at length, which is exactly the kind of invariant that belongs on an object with one `forget()` method rather than on two hand-written pops.
  - Both globals are the package-wide idiom (the same shape as `dom.FRAME`, dom.py:86, and `capabilities.POLICY`), which lanes 3 and 4 both flag (F-3.6, lane-4 open question 2) — this is the place to settle it.
- proposal: `browser_control/lib/browser/scope.py :: class Scope` (one instance, `profile` attribute, `set(value)`/`get()`), kept behind the existing `browser.scope()` shim so `cli/main.py:270,279,287,307,324,392,1230` and `profile.py:230,288,325` need no edit; each verb reads it **once** at entry into a local and passes it down to `instance_dir` (147), `_narrow` (580), `_writable_profile` (641-654), `managed_profile` (675), `browser_info` (1034), `_no_drive` (2077). `browser_control/lib/browser/endpoint.py :: class EndpointGuard` owns `_OWNER_CACHE` with `verdict(profile, port, refresh=False)` and `forget(profile, port)`; the two hand-written pops become `guard.forget(...)`.
- dependencies: F-2.3, F-2.6; cross-lane: F-3.6 (dom's `FRAME`), lane-4 open question 2 (who owns the globals)
- risk: medium — `scope()` is CLI-facing and must keep its set/clear/read semantics (`None` reads, `""` clears), and the guard's verdicts gate every write, so a wrong `refresh` default becomes a safety bug rather than a perf bug.
- acceptance:
  - `grep -rn 'SCOPE\[' browser_control/lib/` matches one module only
  - `grep -c '_OWNER_CACHE' browser_control/lib/browser/` == 1 (inside `endpoint.py`) and the two `.pop(` sites are gone
  - `tests/test_unit.py::t_endpoint_ownership` (1503) and `t_wait_own_port_requires_a_verified_owner` (2355) green unmodified

### F-2.3 `/proc` and endpoint verification have no module of their own, and `_proc_text` is duplicated with `cdp.py`
- category: lib-module
- locations: `browser_control/lib/browser.py:268-443` — `_pid_file`, `_pid_alive`, `_proc_text`, `_cmdline_value`, `_main_processes`, `_profile_marker`, `_pid_on_marker`, `_find_pid`, `_pid_on_profile`, `_pid_of`, `_record_pid`, `_spawn`; `browser_control/lib/browser.py:815-889` — `endpoint_owner`, `not_local_refusal`, `_drive_refusal`; `browser_control/lib/browser.py:1171` — `_verify_profile_endpoint`; `browser_control/lib/browser.py:1335` — `_await_owner`; `browser_control/lib/cdp.py:148` — `_proc_text`; `browser_control/lib/cdp.py:163` — `_exe_basename`; `browser_control/lib/cdp.py:195` — `listener_of`
- evidence:
  - `_proc_text` exists twice with the same body and the same rationale: `browser.py:283-303` ("One /proc file, as text — read to the END, not to 4096 bytes … NULs are PRESERVED") and `cdp.py:148-161` ("One /proc file of a pid as text, or "" — argv NULs PRESERVED … Flattening here made `--user-data-dir` containing a space lose its tail"). The only difference is the `pid: int` vs `pid: str` annotation.
  - `os.path.basename(os.path.realpath(f"/proc/{pid}/exe"))` is inline at `browser.py:353` and is `cdp._exe_basename` at `cdp.py:163-166`.
  - `endpoint_owner` (815) is a *policy* layered on cdp's kernel primitive `listener_of` (cdp.py:195): it re-reads `/proc` for the profile's own pid (`_find_pid` 375 → `_main_processes` 323) and re-implements the marker test inline at 853 (`elif profile and not _pid_on_marker(pid, cmd, profile)`) instead of using `_profile_marker` (359, dead — F-2.10).
  - `docs/plan.md:34` already assigns "Ownership guard: port → inode → pid → exe; Chromium family only → `cdp-not-local`" to the transport module, and lane 1's F-1.3 puts `listener_of`/`_proc_text`/`_exe_basename` in `lib/cdp/endpoint.py`.
  - This cluster is ~180 lines of Linux process introspection with no browser semantics: `_pid_alive` reads `/proc/<pid>/stat` for zombie state (272-281), `_cmdline_value` (305) parses argv, `_spawn` (428) is a bare `Popen`.
- proposal: `browser_control/lib/proc.py` — `pid_alive`, `proc_text`, `exe_basename`, `cmdline_value`, `main_processes`, `pid_file`, `pid_of`, `find_pid`, `pid_on_profile`, `record_pid`, `spawn`, `to_int`, `is_managed`, `default_profile`; and `browser_control/lib/browser/endpoint.py :: EndpointGuard` + `not_local_refusal` (or fold the guard into lane 1's `cdp/endpoint.py` per plan.md:34 — see Open questions). `cdp.py:148/163` become `from browser_control.lib.proc import proc_text, exe_basename`.
- dependencies: F-1.3 (cdp package, the dedup target), F-2.2 (memo owner)
- risk: low-medium — pure move; refusal codes (`cdp-not-local`, `no-browser`, `launch-failed`) travel with the functions, and `_pid_alive`/`_cmdline_value`/`endpoint_owner` are directly asserted (`tests/test_unit.py:2112-2113`, `1359-1368`, `1503-1545`).
- acceptance:
  - `grep -rn '/proc' browser_control/lib/browser/` == 0
  - `grep -rc 'def _proc_text\|def proc_text' browser_control/lib/` == 1
  - `python3 -c "import browser_control.lib.proc"` succeeds with no `cdp` import in that module
  - `python3 tests/test_unit.py` green (supervisor to run)

### F-2.4 The attach registry has three writers and no owner
- category: class-candidate
- locations: `browser_control/lib/browser.py:530` — `ATTACH_FILE`, `browser_control/lib/browser.py:533` — `_attached`, `browser_control/lib/browser.py:550` — `_write_attached`, `browser_control/lib/browser.py:566` — `is_attached`, `browser_control/lib/browser.py:693` — `attachments`, `browser_control/lib/browser.py:708` — `attach` (751-755), `browser_control/lib/browser.py:768` — `detach` (789-803), `browser_control/lib/profile.py:503-505` — the third writer
- evidence:
  - The read-modify-write is written out three times: `records = _attached(); records[record["profile"]] = record; _write_attached(records)` (752-755), `records = _attached(); …; records.pop(key, None); _write_attached(records)` (790-803), and `records = browser_lib._attached(); records.pop(browser_lib._norm(target), None); browser_lib._write_attached(records)` (`profile.py:503-505`).
  - `attach` resolves the browser identity at 719-731 — **outside** the lock it takes at 751 — so the row it authorises and the record it writes are not covered by one critical section; the row lookup itself is the first of three copies (F-2.6).
  - `profile.py` reaches into four private names of `browser` for this one file: `_attached` (503), `_write_attached` (505), `_norm` (504), plus the root lock (482-484) — 6 of the 7 cross-module private reaches from `profile.py` (the other is `_pid_file` 510).
  - Only `_write_attached` knows the on-disk shape (a JSON list, sorted by profile, temp+`os.replace`, 550-564) — and it is the only writer, so the shape has no owner either.
  - `browsers()` reads the file on every census (`attached = _attached()` 487) and `attachments()` re-reads it again at 695, so the same file is parsed twice in one `attach --list`.
- proposal: `browser_control/lib/browser/registry.py :: class AttachRegistry(root)` with `records()`, `is_attached(profile)`, `add(record)`, `remove(profile=None, pid=0, port=0)`, `clear()`, `rows()`, and one private `_locked_update(fn)` that takes the root lock once for the whole RMW; `attach`/`detach`/`attachments` (693-807) and `profile.reset` (`profile.py:501-505`) all call it, and `_norm` becomes a `proc`/`paths` import.
- dependencies: F-2.5 (the same root lock), F-2.6 (selector matching for `detach`)
- risk: medium — the file is contract-visible through `attach --list` (`attachments` 693-706) and the `attached` flag on every `list` row (515); `_write_attached`'s atomicity and its `attach-failed` refusal must be preserved.
- acceptance:
  - `grep -rn '_attached\|_write_attached' browser_control/lib/` matches only in `registry.py`
  - `grep -c 'SLF001' browser_control/lib/profile.py` == 0
  - `attach --list` JSON keys and the `list` row's `attached` flag unchanged (`tests/test_unit.py:561-627`, `tests/live_test.py:2066-2087`)

### F-2.5 The lock's payload is an untyped dict that `profile.py` has to fake
- category: class-candidate
- locations: `browser_control/lib/browser.py:1224` — `LOCK_FILE`, `browser_control/lib/browser.py:1228` — `_lock_path`, `browser_control/lib/browser.py:1305` — `_lock`, `browser_control/lib/browser.py:1275` — `_acquire`, `browser_control/lib/profile.py:396` — the faked payload, `browser_control/lib/profile.py:398-400` / `482-484` — private lock imports
- evidence:
  - `_lock` yields a bare dict (`yield {"held": False, "warning": …}` 1324-1325, `yield {"held": bool(not warning), "warning": warning}` 1329) documented only in prose (`Yields ``{"held": bool, "warning": str}```, 1308).
  - Because of that, `profile.py:396` must hand-rebuild the shape to stand in for the lock: `contextlib.nullcontext({"held": True, "warning": ""})` — a second definition of the same struct, in another module, with no shared type.
  - The decoration `if lock["warning"]: reply["warning"] = lock["warning"]` is copy-pasted 6× in browser.py (763, 778, 805, 1551, 1595; `launch` joins it by hand at 1440-1450) and 2× in profile.py (449, 517).
  - `import fcntl` is done *inside two functions* (1283, 1316) rather than at module top; `_expired` (1265) is a one-line wrapper whose own docstring says "A call, so a refusal handler reads as one"; `_lock_holder` (1232) and `_holder_text` (1270) differ only by an `or "no details"`.
  - The dict shape is pinned by a test that asserts equality with a literal: `assert first == {"held": True, "warning": ""}` (`tests/test_unit.py:1624`).
- proposal: `browser_control/lib/browser/lock.py :: @dataclass class LockState(held: bool, warning: str)` with `warn(reply) -> dict`, `@contextmanager def profile_lock(path, verb, wait=LOCK_WAIT_S)`, and `def lock_path(profile)`; `fcntl` imported at module top; `_expired`/`_holder_text` inlined. Callers: browser.py 751, 774, 789, 1397, 1543; profile.py 396-400, 482-484, 449, 517.
- dependencies: F-2.4 (shares the root lock), F-2.6
- risk: low — the `profile-busy` message text (1294-1299) is contract-visible and must not change; `tests/test_unit.py:1624` needs a one-line update to the new type.
- acceptance:
  - `grep -rc 'if lock\["warning"\]' browser_control/lib/` == 0
  - `grep -c 'nullcontext({"held"' browser_control/lib/profile.py` == 0
  - `grep -c 'import fcntl' browser_control/lib/browser/lock.py` == 1 and it is at module level
  - `tests/test_unit.py::t_lock_serializes_a_check_then_act` (1613) green; `ERR[profile-busy]` text unchanged

### F-2.6 The `--port|--pid|--profile` selector is validated three times and resolved three times, with two different arities
- category: duplication
- locations: `browser_control/lib/browser.py:716-731` — `attach`, `browser_control/lib/browser.py:781-798` — `detach`, `browser_control/lib/browser.py:1486-1495` — `_named_browser`, `browser_control/lib/browser.py:1534-1543` — `stop`, `browser_control/cli/main.py:226-261` — `_selector`
- evidence:
  - The validator is the same comprehension three times: `given = [name for name, value in (("--port", port), ("--pid", pid), ("--profile", profile)) if value]` at 716-717, 781-782, 1534-1535 — but `attach`/`detach` require `len(given) != 1` (718, 783) while `stop` allows zero and refuses only `> 1` (1536), i.e. the same flag set has two arity contracts.
  - The row lookup is three times: `if pid: … elif profile: … else: port` at 723-731, again at 1486-1495 (which additionally builds the `what = f"--pid {pid}"` label used in the refusal), and a *fourth* variant over the attach records at 791-798.
  - `_named_browser` takes an untyped `selector: dict` (1473) and `stop` constructs it inline: `_named_browser({"port": port, "pid": pid, "profile": profile})` (1540) — a dict literal as an ad-hoc struct.
  - The CLI has its own, wider version of the same struct: `_selector` (`cli/main.py:226-261`) returns `{"port", "pid", "profile", "list", "all"}`, and the three handlers destructure it field by field (`main.py:278-279`, `288-289`, `312-313`). It has already drifted: `_selector` stores `--profile`, but `cmd_attach` passes `browser_lib.scope()` instead (`main.py:278-279`), so `selector["profile"]` is only ever read as a boolean (258, 260).
- proposal: `browser_control/lib/browser/selector.py :: @dataclass class Selector(port: int = 0, pid: int = 0, profile: str = "")` with `from_flags(...)`, `named() -> bool`, `require_one(verb) -> None` (the 3 refusals), and `machine.find(rows, selector) -> BrowserRow | None` (the 3 lookups, returning the row so `_named_browser`'s verification stays in one place). `cli/main.py::_selector` returns `Selector` plus its own `list`/`all` booleans and passes it, so `main.py:278/288/312` stop re-listing fields.
- dependencies: F-2.1 (`find` returns a `BrowserRow`), F-2.7
- risk: medium — the refusal texts are asserted by substring (`name ONE browser`, `no running Chromium-family browser matches --pid N`) in `tests/test_unit.py:505-553` and `tests/live_test.py`.
- acceptance:
  - `grep -c '"--pid", pid' browser_control/lib/browser/` == 1
  - `grep -rc 'elif profile:' browser_control/lib/browser/` == 1
  - `attach`/`detach`/`close --pid|--port|--profile` refusal codes and texts byte-identical (`tests/test_unit.py:505-553`, `1071-1090`)
  - `python3 tests/test_unit.py` green (supervisor to run)

### F-2.7 "May this CLI write here" is an inline predicate at 8 sites over three overlapping filters
- category: duplication
- locations: `browser_control/lib/browser.py:597` — `_writable`, `browser_control/lib/browser.py:615` — `_readable`, `browser_control/lib/browser.py:892` — `_drivable`, and the predicate sites `browser_control/lib/browser.py:612, 911, 1023, 1661, 1685, 1743, 1765, 1836`
- evidence:
  - `r["managed"] or r["attached"]` — the write authorisation — appears at 612, 911, 1023, 1661, 1685, 1743, 1765, 1836, plus the sort key at 518 and the `(attached)` label at 658/1026; eight of those are the same predicate with no name.
  - Three filters overlap: `_writable` = `_drivable(strict=False)` filtered to managed-or-attached (611-612); `_readable` = `_writable(browser) or _drivable(browser)` (624) — a read may fall back to a browser a write may not; `_drivable`'s `strict` flag (892) flips the same condition between "refuse `cdp-not-local`" and "silently omit", and callers choose differently: `strict=False` at 611, 991, 1703, versus strict at 1650, 1725, 2115.
  - The docstring at 601-611 records the bug this predicate caused once already: the old "every drivable browser" fallback "sent `tab press` and `tab about:blank` into a stranger's session — measured against a throwaway Chrome".
- proposal: one named predicate — `machine.py :: def writable(rows) -> list[BrowserRow]`, `def readable(rows)`, and `BrowserRow.may_write` / `.may_read`; `_tab_count` (1703), `list_tabs` (991), `_closeable` (1725), `_split` (1743), `_exact_matches` (1765), `_spec_matches` (1836), `_resolve_across` (1661, 1685) and `_one_tab` (2106) call it instead of re-spelling it. Keep `strict` as a named argument of the one filter.
- dependencies: F-2.1, F-2.6
- risk: low — every site spells the identical expression today, so the extraction is provably equivalent; the only judgement is which of the three filters becomes the primitive.
- acceptance:
  - `grep -rc '"managed"\] or ' browser_control/lib/browser/` == 0
  - `grep -c 'def writable' browser_control/lib/browser/machine.py` == 1
  - `tests/test_unit.py::t_one_tab_addressing` (703-853) passes unmodified

### F-2.8 2455 lines, 105 top-level functions, 0 classes across 12 separable clusters — the plan already named two of the modules
- category: package-split
- locations: `browser_control/lib/browser.py:1-80` — constants + docstring, `:83-209` — resolution/paths, `:210-266` — spec matching, `:267-443` — `/proc` identity, `:444-531` — census, `:530-569`+`:693-808` — attach registry, `:570-692` — selection, `:810-889` — endpoint guard, `:890-1050` — drivable/projections/listing, `:1051-1221` — read-back waiters, `:1222-1366` — lock, `:1367-1601` — lifecycle, `:1602-2066` — tab verbs, `:2067-2455` — page verbs
- evidence:
  - `grep '^def '` returns 105 definitions, 73 of them private, and `grep '^class '` returns nothing; the largest public surface in the package is also the least typed (every row, record, selector and lock payload is a bare `dict`).
  - Constants for four unrelated concerns are declared at four places in the file: `BROWSER_BINS`/`DEFAULT_PROFILES` (48, 61), `SCOPE` (121), `ATTACH_FILE` (530), `LOCK_FILE`/`LOCK_WAIT_S` (1224-1225), `ACTIVE_SPEC` (1600), `NAV_TIMEOUT_S`/`READY_EXPR` (2058, 2065).
  - `docs/plan.md:74-96` names `tabs.py` (list/new_tab/close_tab/activate/info) and `nav.py` (nav/back/forward/reload) as modules of their own, and `docs/plan.md:374-385` puts them in the build order; `docs/progress.md:142` records the deviation — "folded into `lib/browser.py` (1281 lines) for the tabs … the boundary is now where it should be" — a claim that was plausible at 1281 lines and is not at 2455, where those two clusters alone are 1602-2066 and 2067-2455.
  - Three modules now import this one: `cli/main.py:28-45` (15 names), `profile.py:41-47` (6 names), `dom.py:648/785`+ (2 privates, 25 sites), plus `plugins/x_reader.py:174` (`browser_lib.nav`).
- proposal: `browser_control/lib/browser/` package — `__init__.py` (frozen surface + `__all__`), `paths.py`, `proc.py`, `endpoint.py`, `registry.py`, `lock.py`, `selector.py`, `machine.py`, `lifecycle.py`, `readback.py`, `tabs.py`, `page.py` (layout below). Add `"browser_control.lib.browser"` to `[tool.setuptools] packages` in `pyproject.toml` (lane 4's F-4.5 makes the same point for `cli`).
- dependencies: F-2.1…F-2.7 all land inside this split; F-4.6 (cli's 15 from-imports must keep resolving), F-3.11 (dom's 25 private reaches)
- risk: medium-high — the re-export list is load-bearing for 5 import sites and ~30 test patch sites, and a package `__init__` re-export does **not** keep `browser.browsers = fake` effective inside the submodules: the patch would silently stop taking effect rather than fail (F-2.9).
- acceptance:
  - `python3 -c "from browser_control.lib.browser import launch, stop, attach, detach, attachments, list_browsers, browser_info, list_tabs, new_tab, close_tabs, tab_info, nav, history, activate, reload, resolve_tab, safe_url, flags, root, scope, binary, profile_dir, profiles, managed_profile, ensure_up, endpoint_owner, not_local_refusal, is_attached, BROWSER_BINS, ACTIVE_SPEC"` succeeds
  - no file under `browser_control/lib/browser/` exceeds ~400 lines
  - `grep -c browser_control.lib.browser pyproject.toml` == 1 and `pip install -e .` still installs `browser-control-cli`
  - `python3 tests/test_unit.py` green (supervisor to run) and `browser-control-cli selftest` exit 0

### F-2.9 The suite patches `browser.*` module attributes and asserts on `browser.py`'s source text
- category: test-coupling
- locations: `tests/test_unit.py:572-853` — attribute patching, `tests/test_unit.py:845-850` — source-text assertion, `tests/test_unit.py:1624` — lock payload literal, `tests/test_unit.py:2380-2403` — more patching, `tests/test_unit.py:2827` — `browser.nav`
- evidence:
  - The suite drives the library by rebinding module globals: `browser.browsers = rows` (595, 726, 752, 780, 852), `browser.cdp.page_rows_at = page_rows_at` (596, 727), `browser._tabs_of = lambda row: ([], …)` (785), `browser.endpoint_owner = lambda …` (801, 817, 832), `browser._verify_profile_endpoint = lambda profile: …` (817, 824), `browser._rows = boom` (2397), `browser.nav = fake_nav` (2827).
  - That only works because every internal caller looks the name up as a module global; moving `browsers`/`_tabs_of`/`_verify_profile_endpoint` into a submodule turns these patches into no-ops while the suite stays green — the failure mode lane 4 calls out for `cli.main` (F-4.6) and `docs/progress.md:896-904` records for this project.
  - One test asserts on the **source text** of the file: `source = (Path(__file__).resolve().parent.parent / "browser_control/lib/browser.py").read_text(encoding="utf-8")` then `assert "_tabs_of(row)[0]" not in source` and `assert "_tabs_or_fail(row)" in source` (`tests/test_unit.py:845-850`). A package split makes that path a directory.
  - `tests/test_unit.py:1624` pins the lock payload as a dict literal, so F-2.5's type change requires that one line to move with it.
- proposal: expose one patchable seam and patch that — `machine.browsers` / `guard.verdict` / `registry.records()` (F-2.1, F-2.2, F-2.4) instead of module attributes; replace the source-text assertion with a behavioural one (the test already has the behaviour in hand at 784-792: a `_tabs_of` returning an error must make the verb refuse `cdp-error`).
- dependencies: F-2.1, F-2.2, F-2.4, F-2.5, F-2.8
- risk: medium — the source-text check fails loudly (`IsADirectoryError`), but the attribute patches fail silently, which is the more expensive outcome; the migration must be done in the same change as the split.
- acceptance:
  - `grep -c 'browser\.browsers =' tests/test_unit.py` == 0 and `grep -c 'browser\._tabs_of =' tests/test_unit.py` == 0
  - `grep -rc 'browser_control/lib/browser.py' tests/` == 0
  - the number of PASS lines in `python3 tests/test_unit.py` is not lower than today (supervisor to run)

### F-2.10 `_profile_marker` is dead; `resolve_tab` and `live_profiles` are reachable only from tests
- category: dead-or-unreachable
- locations: `browser_control/lib/browser.py:359` — `_profile_marker`, `browser_control/lib/browser.py:247` — `resolve_tab`, `browser_control/lib/browser.py:169` — `live_profiles`, `browser_control/lib/browser.py:1640` — `_resolve_across`
- evidence:
  - `_profile_marker` (359-366, `return f"--user-data-dir={_norm(profile)}"`) has **no call site anywhere in the repo** (`grep -rn '_profile_marker' browser_control tests` returns the definition only); its body is inlined at `_pid_on_marker` (371-372) and again in `endpoint_owner` (853).
  - `resolve_tab` (247-265) is called only from `tests/test_unit.py:101-110` and `2411`; production paths use `_resolve_across` (1640), which re-implements its exact refusals inline — `f"no tab matches {spec!r} (have: {have or 'none'})"` at 1675 versus 260, `fail("tab-ambiguous", …)` at 1682 versus 263.
  - `live_profiles` (169-171) is called only from `tests/test_unit.py:133`; no verb or CLI path consumes it.
  - The same inversion runs the other way: the names that *are* cross-module seams are private — `_is_managed` (`profile.py:42`), `_norm` (262), `_lock`/`_lock_path` (398), `_attached`/`_write_attached` (503-505), `_pid_file` (510), `_one_tab`/`_brief` (`dom.py:648/785`+) — 9 private symbols with outside callers, while `endpoint_owner`, `not_local_refusal`, `browsers`, `managed_profile` and `ensure_up` are public by name and used only inside this module.
- proposal: delete `_profile_marker`; make `resolve_tab` the single per-browser rule and have `_resolve_across` call it (keeping the name, which the tests already exercise) or move it to the tests-only surface; keep `live_profiles` only if a verb consumes it, else drop it. State the surface explicitly with `__all__` in the new package's `__init__.py` (F-2.8), and promote `_one_tab`/`_brief` per lane 3's F-3.11.
- dependencies: F-2.6 (the same resolution rule), F-2.8 (`__all__`)
- risk: low — no production caller for any of the three, and the resolution rule it would unify is already asserted by `t_resolve_tab` (94-110).
- acceptance:
  - `grep -rn '_profile_marker' browser_control tests` == 0
  - either `_resolve_across` calls `resolve_tab` or `resolve_tab` is gone; `grep -c 'no tab matches' browser_control/lib/browser/` == 1
  - `tests/test_unit.py::t_resolve_tab` (94) still green

## Proposed target layout for this slice
- `browser_control/lib/browser/__init__.py` — the frozen surface only: the 15 verb functions `cli/main.py:28-45` imports, the 6 names `profile.py:41-47` imports, `resolve_tab`/`safe_url`/`flags`/`profiles`/`managed_profile`/`ensure_up`/`endpoint_owner`/`not_local_refusal`/`is_attached`, the constants `BROWSER_BINS`/`ACTIVE_SPEC`, and `__all__` (F-2.8, F-2.10).
- `browser_control/lib/browser/paths.py` — one resolver for root/binary/profile/scope: `root`, `binary`, `profile_dir`, `Scope`, `scope`, `instance_dir`, `profiles`, `flags`, `safe_url`, `is_managed` (F-2.2).
- `browser_control/lib/proc.py` — Linux process identity, no browser semantics: `pid_alive`, `proc_text`, `exe_basename`, `cmdline_value`, `main_processes`, `pid_file`, `pid_of`, `find_pid`, `pid_on_profile`, `record_pid`, `spawn`, `to_int`, `default_profile` (F-2.3; `cdp.py:148/163` fold in here).
- `browser_control/lib/browser/endpoint.py` — `EndpointGuard` (memo + `verdict()`/`forget()`), `endpoint_owner`, `not_local_refusal`, `_drive_refusal`, `_await_owner`, `_verify_profile_endpoint` (F-2.2, F-2.3).
- `browser_control/lib/browser/registry.py` — `AttachRegistry` over `attached.json`: `records`, `is_attached`, `add`, `remove`, `clear`, `rows`; one locked RMW (F-2.4).
- `browser_control/lib/browser/lock.py` — `LockState`, `profile_lock`, `lock_path` (F-2.5).
- `browser_control/lib/browser/selector.py` — `Selector` value object, `Selector.require_one`, `Selector.find` (F-2.6).
- `browser_control/lib/browser/machine.py` — `BrowserRow` + `Endpoint` value objects, `browsers`, `list_browsers`, `narrow`, `writable`, `readable`, `drivable`, `tabs_of`/`tabs_or_fail`, `as_brief`/`as_row`/`as_endpoint_reply`, `list_tabs`, `browser_info` (F-2.1, F-2.7).
- `browser_control/lib/browser/lifecycle.py` — `launch`, `stop`, `_page_count`, `_named_browser`, `_open_tabs`, `_spawn` wiring (the open/close state machine: pid file, port file, adoption).
- `browser_control/lib/browser/readback.py` — the bounded waiters shared by lifecycle and the tab/page verbs: `rows`, `wait_port`, `wait_own_port`, `wait_rows`, `wait_tabs`, `wait_url`, `wait_ids_gone`, `require_tab_list_readable`.
- `browser_control/lib/browser/tabs.py` — tab addressing and the tab verbs: `match_spec`, `resolve_tab`, `flat`, `visible`, `spec_hits`, `resolve_across`, `tab_count`, `tab_info`, `closeable`, `split`, `exact_matches`, `exact_spec_match`, `spec_matches`, `TabQuery` (built inside `close_tabs` so the signature and the 8 keyword call sites stay), `close_tabs`, `new_tab`, `one_tab`.
- `browser_control/lib/browser/page.py` — the page verbs and their oracles: `nav`, `history`, `activate`, `reload`, `same_page`, `eval_in`, `href`, `wait_document`, `ready`, `wait_move`, `wait_url_change`, `time_origin`, `wait_new_document`, `no_drive`.
- `pyproject.toml` — add `"browser_control.lib.browser"` to `[tool.setuptools] packages`.

## Open questions
1. **Package or flat siblings?** `lib/browser/` (recommended: the import path stays stable, `__init__.py` carries the frozen surface) versus `lib/browser_machine.py` + `lib/browser_tabs.py` + … — a human call, because five modules and ~30 test sites import `browser_control.lib.browser`.
2. **Where does the endpoint guard live?** `docs/plan.md:34` assigns the ownership guard to the transport module, lane 1's F-1.3 puts `listener_of`/`_proc_text` in `lib/cdp/endpoint.py`, and the guard's *policy* half needs `find_pid`/`pid_on_profile` from this lane. Options: (a) `lib/proc.py` (primitives) + `lib/browser/endpoint.py` (policy), or (b) everything under `lib/cdp/endpoint.py` per plan.md. This is a cross-lane decision with F-1.3 and must be made once.
3. **Who owns the per-invocation scope?** `browser.SCOPE` (121), `dom.FRAME` (`dom.py:86`) and `capabilities.POLICY` are three globals set by `main` (`cli/main.py:1230-1234`). Lane 3 (F-3.6) and lane 4 (open question 2) both ask this; this lane's answer is "one `Scope` object owned by the invocation, with `browser.scope()`/`dom.frame()` kept as shims", but it is a cross-lane change and cannot be decided inside lane 2.
4. **Is `browser_control.lib.browser` a permanent façade?** If it must stay a re-export surface forever, `dom.py`'s 25 private reaches (`_one_tab`, `_brief`) and `profile.py`'s 7 private imports need a decision too (promote to public names, per F-3.11), otherwise the "package" is cosmetic.
5. **`resolve_tab` — promote or drop?** It is the cleanest statement of the per-browser spec rule and is exercised by tests (94-110), but nothing in production calls it while `_resolve_across` duplicates its refusals. Make it the one rule, or delete it and keep the tests pointed at `_resolve_across`?
6. **Is the `attach` identity race in scope?** `attach` resolves the row at 719-731 and writes the record at 755 under a lock taken at 751, so a browser that stops in between still gets an authorization record. A structural-only refactor should not change behaviour, but the registry owner (F-2.4) is the natural place to close it — is that a separate ticket?