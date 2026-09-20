# Lane 6: cross-cutting sweep — `browser_control/**`, `plugins/**`

## Summary
- Slices inspected in full: `lib/{errors,audit,capabilities,cdp,browser,dom,profile,plugins}.py` (21 / 224 / 302 / 887 / 2455 / 2779 / 522 / 205 lines), `cli/main.py` (1321), `__init__.py`, `__main__.py`, `plugins/x_reader.py`, `browser-control-cli`; `tests/*.py` read only for coupling evidence. Artifacts ignored per instruction.
- The import graph is **already acyclic and one-directional** (`errors` ← `cdp` ← `browser` ← {`dom`, `profile`} ← `cli`; `audit`/`capabilities`/`plugins` a separate branch). There is no cycle to break and no `lib`→`cli` edge. The problems are **fan-in on `browser.py`** (imported by `dom`, `profile`, `cli`, and the shipped plugin) and **cross-module use of private names** (`profile.py` imports `_is_managed`, `dom.py` calls `tabs._one_tab`/`tabs._brief`, `cli/main.py` calls `dom._resolve`).
- Ranked structural problems: (1) **six module-level mutable globals** carry per-invocation state, mutated from up to four modules (`browser.SCOPE`, `dom.FRAME`, `browser._OWNER_CACHE`, `audit._SCRATCH`, `capabilities.PLUGIN_ACTIONS`/`POLICY`, `cli.PLUGINS`); (2) the **same helper copied 2–4 times** in four clusters (untrusted-text sanitiser, `/proc` reads, numeric coercion/path canonicalisation, argv flag readers); (3) **26 hand-rolled deadline/sleep loops** with four different sleep intervals; (4) `browser.py` (2455) and `dom.py` (2779) are **packages in one file**, each with six or more responsibilities; (5) `attach`/`detach`/`_named_browser` hand-write the **same "which browser did the caller name" resolution three times**; (6) the test suites pin the current seams by **monkeypatching module globals by name**, so any fold must move names and tests in the same commit.
- Already fine and worth keeping: `errors.ControlError`/`fail` as the single refusal type with exactly one printer (`cli/main.py:1285-1320`); `dom.PRELUDE` (`dom.py:179`) as **one** shared page prelude for every matcher; `capabilities.ACTIONS` + `unclassified()` checked against the CLI's own handler tables at runtime *and* in `tests/test_unit.py:1567`; the profile lock (`browser.py:1225-1333`) as one deliberate implementation; `cdp.Session` as the only connection owner (lane 1, F-1.1).
- Verdict: **OK with notes** — this is a structure-only review; nothing here is a defect in the frozen contract. P1 below = "must happen in this refactor", P2 = "report-only / cheap while you are there".

## Findings

### F-6.1 Nine module-level mutable globals hold state that no object owns
- category: hidden-state
- locations: `browser_control/lib/browser.py:121` — `SCOPE: dict[str, str]`; `browser_control/lib/dom.py:86` — `FRAME: dict[str, Any]`; `browser_control/lib/browser.py:812` — `_OWNER_CACHE`; `browser_control/lib/audit.py:38` — `_SCRATCH`; `browser_control/lib/audit.py:219` — `LOG = ActionLog()`; `browser_control/lib/capabilities.py:138` — `PLUGIN_ACTIONS`; `browser_control/lib/capabilities.py:167` — `POLICY`; `browser_control/cli/main.py:1159` — `PLUGINS`
- evidence:
  - `browser.py:121 SCOPE: dict[str, str] = {"profile": ""}`, written only by `scope()` (`browser.py:134`), and **read from 10 sites**: `147, 580, 641, 648-651, 654, 675, 678, 1034, 2077`. It is set once per invocation from the CLI (`cli/main.py:1230 browser_lib.scope(flags["profile"] or "")`), so two modules share one process-wide instance.
  - `dom.py:86 FRAME: dict[str, Any] = {"wanted": "", "resolved": None}` — written by `frame()` (`dom.py:99, 102`), written *again* by `_document_ws` (`dom.py:664`), read at `660, 866, 1525-1528`; set by `cli/main.py:1231` and read back at `1271-1277` to decorate the reply. `tests/test_unit.py:2043-2045` pokes `dom.FRAME["resolved"]` directly.
  - `browser.py:812 _OWNER_CACHE` is a correctness-bearing memo: the docstring says "One /proc walk per (profile, port) per process", and two call sites must remember to evict it (`browser.py:1081`, `browser.py:1360`) or a stale "unverified" verdict outlives the listener it described.
  - `audit.py:53 global _SCRATCH` (memoised `mkdtemp`) plus the `LOG` singleton whose per-invocation state (`_secret`) is mutated from **two modules**: `cli/main.py:1235 audit.LOG.begin(verb)`, `cli/main.py:1319 audit.LOG.write(...)`, and `dom.py:2541 audit.LOG.mark_secret(text)` inside `_preflight`. `browser.py` does not import `audit`, `dom.py` does — so the "the COMMAND writes the line" rule in `audit.py:5-9` already has an exception.
  - `capabilities.py:147 PLUGIN_ACTIONS.clear()` inside `set_plugins` and `capabilities.py:262 POLICY.update({...})` inside `policy()` mutate two module dicts that `by_class()`/`allowed()` read; `cli/main.py:1176-1178 global PLUGINS` rebinds the plugin registry per invocation.
- proposal: introduce one per-invocation object and delete every global:
  `browser_control/lib/scope.py :: class Scope` (`profile: str`, `frame_wanted: str`, `frame_resolved: dict | None`) created in `cli/main.main`, threaded to `lib/browser/*` and `lib/page/*` (or, minimally, kept as one instance held by a `Context` object rather than four module dicts); `_OWNER_CACHE` → an instance field of `lib/browser/owners.py :: class EndpointOwners` (`owner(profile, port)`, `evict(...)`), created per invocation; `audit._SCRATCH` → an instance field of `ActionLog`; the `LOG` module singleton stays only as the default for `main()`'s own `ActionLog`; `PLUGIN_ACTIONS`/`POLICY` become fields of lane 1's `Surface`/`Policy` (F-1.4) and `PLUGINS` a local in `main()`.
  Caller migration: `browser.py:134/136/147/580/641/648/675/678/1034/2077`, `dom.py:99/102/660/664/866/1525/1528`, `cli/main.py:1176-1178, 1230-1231, 1271-1277`, `profile.py:262-264/299-300` (scope read).
- dependencies: F-6.12 (the tests patch `dom.FRAME`), lane-1 F-1.4.
- risk: high — every verb reads the scope, and `dom.FRAME` is asserted by a hermetic check.
- acceptance:
  - `python3 tests/test_unit.py` green (supervisor to run) after each of the two steps (scope, then caches).
  - `grep -rn "^SCOPE\|^FRAME\|^_OWNER_CACHE\|^_SCRATCH\|^PLUGIN_ACTIONS\|^POLICY" browser_control/` returns 0 lines.
  - `grep -rn "global " browser_control/` returns 0 lines.

### F-6.2 The untrusted-text sanitiser exists four times, in three different orders
- category: duplication
- locations: `browser_control/lib/audit.py:90` — `_oneline`; `browser_control/lib/cdp.py:451` — `_foreign`; `browser_control/lib/browser.py:234` — `_flat`; `browser_control/lib/dom.py:995` — `_safe`
- evidence:
  - `cdp.py:460-462` and `browser.py:242-245` are **byte-identical bodies**:
    `line = " ".join(str(text or "").split())[:cap]` then `"".join(ch for ch in line if ch >= " " and not "\x7f" <= ch <= "\x9f")` — only the default cap differs (200 vs 60).
  - `browser._flat`'s own docstring (`browser.py:239-240`) admits the derivation: *"The recipe is `audit._oneline`'s, plus a cap and the C0/DEL/C1 filter `cdp._foreign` uses."*
  - `dom._safe` (`dom.py:1005-1008`) does it in the **opposite order** — filter first, then `[:limit]` — and never calls `.split()`, so it collapses nothing: the same page text yields a different string depending on which helper a caller reached for.
  - `audit._oneline` is the odd one out: it flattens but does **not** strip C0/DEL/C1, and it is the helper used for the audit line (`audit.py:141, 151, 153`).
- proposal: one leaf module `browser_control/lib/text.py :: def foreign(text: object, cap: int = 200) -> str` (+ `def flat(text, cap=60)` if the shorter default is wanted as an alias). Delete `cdp._foreign`, `browser._flat`, `dom._safe`; `audit._oneline` becomes `text.foreign(text, cap=...)` or a documented thin wrapper.
  Callers: `cdp.py:427-428, 478, 814-815, 870`; `browser.py:258, 262`; `dom.py:1011-1012, 1568, 1650, 1742, 1826, 1833`; `audit.py:141, 151, 153`.
- dependencies: lane-1 F-1.2/F-1.3 (`_foreign` is named in lane 1's `cdp/rpc.py` target — pick **one** home; `lib/text.py` is the leaf and `cdp` should import it).
- risk: low — pure move, but the merge of `_safe`'s order is a visible change of refusal text (assertions in the suite must be re-run).
- acceptance: `grep -rn $'\x7f' browser_control/` returns exactly 1 file; `grep -rn "def _flat\|def _foreign\|def _safe\|def _oneline" browser_control/` returns 0 lines; `python3 tests/test_unit.py` green.

### F-6.3 `/proc` is read by two modules, and the exe basename is re-inlined twice
- category: duplication
- locations: `browser_control/lib/cdp.py:148` — `_proc_text`; `browser_control/lib/browser.py:283` — `_proc_text`; `browser_control/lib/cdp.py:163` — `_exe_basename`; `browser_control/lib/browser.py:353, 510`; `browser_control/lib/cdp.py:195` — `listener_of`; `browser_control/lib/browser.py:272` — `_pid_alive`; `browser_control/lib/browser.py:323` — `_main_processes`
- evidence:
  - The two `_proc_text` are the same function with different `pid` types: `cdp.py:148 def _proc_text(pid: str, name: str)` vs `browser.py:283 def _proc_text(pid: int, name: str)`; both open `f"/proc/{pid}/{name}"` in binary and `rstrip("\0\n")`, and both carry the same "NULs are PRESERVED" rationale (`cdp.py:150-155`, `browser.py:286-293`).
  - `cdp._exe_basename` (`cdp.py:163-167`) is open-coded as `os.path.basename(os.path.realpath(f"/proc/{pid}/exe"))` at `browser.py:353` and as `os.path.realpath(f"/proc/{pid}/exe")` at `browser.py:510`.
  - Three distinct `/proc` walks exist: `cdp.py:171 _listening_inodes` + `cdp.py:195 listener_of` (`/proc/net/tcp{,6}` → inode → `/proc/<pid>/fd`), `browser.py:323 _main_processes` (every pid's cmdline + exe), `browser.py:272 _pid_alive` (`/proc/<pid>/stat`).
- proposal: `browser_control/lib/proc.py` — `proc_text(pid, name)`, `exe_name(pid)`, `exe_path(pid)`, `pid_alive(pid)`, `main_processes(exes)`, `listener_of(port)` (moved out of `cdp`). `cdp/endpoint.py` (lane 1) imports `proc.listener_of`; `browser/proc.py` (F-6.9) imports the rest. `cdp.py:148-167` and `browser.py:272-353` are deleted.
- dependencies: lane-1 F-1.3 (`listener_of` is currently assigned to `cdp/endpoint.py`; `lib/proc.py` must be the *owner* and `cdp` the importer, or the leaf→hub direction inverts).
- risk: medium — `/proc` parsing is the identity guard for stop/attach; the fake-endpoint checks in `tests/test_unit.py` cover it.
- acceptance: `grep -rln '"/proc' browser_control/` returns exactly 1 file; `grep -rn "def _proc_text" browser_control/` returns 0 lines; endpoint-ownership test green.

### F-6.4 Twenty-six hand-rolled deadline loops and four unnamed sleep intervals
- category: duplication
- locations: `browser_control/lib/browser.py:1059, 1077, 1089, 1105, 1126, 1158, 1285, 1357, 1576, 1583, 2169, 2201, 2229, 2255, 2306, 2412`; `browser_control/lib/dom.py:1760, 1876, 1994, 2307, 2396, 2716`; `browser_control/lib/cdp.py:419, 528, 556, 615, 779`
- evidence:
  - Every one is the same shape: `deadline = time.time() + <timeout>` … `while time.time() < deadline:` … `time.sleep(<literal>)` — 24 `time.sleep` sites with four different literals (`0.15` ×5, `0.2` ×3, `0.25` ×9, `0.3` ×1) chosen per call site, plus named constants elsewhere (`dom.TYPE_PAUSE_S`, `dom.WAIT_POLL_S`).
  - The loops that swallow and re-interpret refusals are the risky ones: `browser.py:1088 _wait_rows` (`except Exception: rows = []`), `browser.py:1102 _wait_tabs`, `browser.py:1150 _wait_ids_gone` (which delegates the "was that absence or a failure?" decision to `_require_tab_list_readable`, `browser.py:1136`). Each hand-rolls its own version of that judgement.
  - `cdp.py:651 evaluate_until` already implements "one connection, many samples, bounded budget" correctly; the browser/dom loops each re-invent it over HTTP.
- proposal: `browser_control/lib/poll.py :: def poll(probe, *, timeout, interval, accept=bool, on_error=...) -> tuple[attempts, last]` plus `def deadline(timeout) -> float`. Convert the 20 non-transport loops; keep `cdp.Session`'s own in-loop budget arithmetic (it is per-sample, not per-poll). Refusal text stays at the call site.
- dependencies: F-6.1 (the loops are also where `_OWNER_CACHE` is evicted).
- risk: medium — a shared poll helper must not change any single verb's timeout; each conversion needs its own check.
- acceptance: `grep -rc "deadline = time.time() +" browser_control/ | awk -F: '{s+=$2} END {print s}'` ≤ 3; named interval constants for the surviving literals; `python3 tests/test_unit.py` and the live battery's nav/wait/scroll checks green.

### F-6.5 "Coerce a page value" and "canonicalise a path" are copied into every module
- category: duplication
- locations: `browser_control/lib/browser.py:445` — `_to_int`; `browser_control/lib/dom.py:610, 617, 2251, 2267` — `_int`, `_num`, `_ints`, `_list`; `browser_control/lib/profile.py:190` — `_int`; `browser_control/lib/browser.py:86, 134, 176, 457`, `audit.py:127`, `plugins.py:88, 91`, `profile.py:232, 290, 376` — `os.path.abspath(os.path.expanduser(...))`
- evidence:
  - Three int coercers with **three signatures for one job**: `browser.py:445 def _to_int(value: object) -> int`, `dom.py:610 def _int(value: object, default: int = 0) -> int`, `profile.py:190 def _int(value: object) -> int`; and a fourth, differently-purposed `cli/main.py:219 def _int(value: str, what: str) -> int` that raises `bad-args`.
  - The path pair is open-coded 10 times (list above). `browser.py:174 _norm` is exactly that pair, is **private**, and is nonetheless used by another module: `profile.py:262, 264, 299-300, 504` all read `browser_lib._norm(...)  # noqa: SLF001`.
  - `profile.py:41-48` contains a module-level `from browser_control.lib.browser import _is_managed, …` — a **private name imported across a module boundary**, which is the single sharpest symptom that the profile-path rules have no public owner.
- proposal: `browser_control/lib/coerce.py` (`as_int`, `as_float`, `as_ints`, `as_list`) and `browser_control/lib/paths.py` (`expand`, `norm`, `root`, `profile_dir`, `is_managed`, `pid_file`, `lock_path`), with `browser.py` re-exporting them so `browser.norm`/`browser.is_managed` become the stable public names. Delete the private copies; keep `cli.main._int` (it is argv validation, and its message names the flag).
  Callers: the 10 path sites above; `browser.py:445` used at `493, 723, 728-730, 743-746, 795-798, 840, 888, 945, 955, 969, 1182, 1482-1494`; `dom.py:610` used at ~40 sites.
- dependencies: none (leaf).
- risk: low — mechanical, and `tests/test_unit.py` covers `_int`/`_ints`/`_norm` behaviour directly (`test_unit.py:936-943`, `1359`).
- acceptance: `grep -rn "def _to_int\|def _int(value: object" browser_control/` returns 0 lines; `grep -rn "from browser_control.lib.browser import" browser_control/ | grep "_"` returns 0 lines; `grep -rc "os.path.abspath(os.path.expanduser" browser_control/` sums to ≤ 2.

### F-6.6 The CLI's flag readers are duplicated, and the plugin tier had to write its own
- category: duplication
- locations: `browser_control/cli/main.py:403` — `_pop`; `browser_control/cli/main.py:429` — `_switch`; `browser_control/cli/main.py:471` — `_pop_all`; `browser_control/cli/main.py:1001` — `_flags`; `plugins/x_reader.py:74` — `_take`; `plugins/x_reader.py:80` — `_value`
- evidence:
  - `_pop` (`main.py:403-427`) and `_pop_all` (`main.py:471-496`) differ only in whether they stop at the first match; `_pop_all`'s docstring says it: *"A repeatable flag needs its own reader: `--except a --except b` is two exceptions, and `_pop` would leave the second one in argv."*
  - `_flags` (`main.py:1001-1047`) is a **fourth** scanner for `--flag VALUE` / `--flag=value`, with its own duplicate-detection rules.
  - Because all of these are private, `plugins/x_reader.py:74-95` re-implements two of them (`_take`, `_value`) so a plugin verb can parse `--latest` / `--cap N`. A plugin author following the `lib.plugins` docstring has no public argv tool.
  - `_pop` is called **~50 times** across `cli/main.py` (`305, 444, 508-513, 523, 539, 545, 552, 564-567, 590-593, 610-613, 634-636, 646-649, 669-676, 688, 703, 711-713, 730-733, 742-748, 761-764, 788-793, 808-810, 827, 854, 861, 868-870, 886-887, 914-916, 924, 1102-1111`).
- proposal: `browser_control/cli/argv.py` — `pop(rest, flag, verb)`, `pop_all(rest, flag, verb)`, `switch(rest, flag)`, `take_bool(rest, flag)`, `globals_(args, spec)`; document it in `lib/plugins.py`'s module docstring as the supported way for a `run(rest, browser)` to parse its own flags; delete `x_reader._take`/`_value`.
- dependencies: none.
- risk: low — behaviours are asserted by the CLI-grammar checks (`test_unit.py:391-1090`).
- acceptance: one `--flag=value` scanner in the repo (`grep -rn '"=", 1' browser_control/ cli plugins/` bounded to argv.py); `tab close --like a --like b` behaviour unchanged; `python3 tests/test_unit.py` green.

### F-6.7 "Which browser did the caller name" is written three times and must agree
- category: class-candidate
- locations: `browser_control/lib/browser.py:708-747` — `attach`; `browser_control/lib/browser.py:1473-1511` — `_named_browser`; `browser_control/lib/browser.py:768-799` — `detach`
- evidence:
  - Both build the identical one-selector check: `given = [name for name, value in (("--port", port), ("--pid", pid), ("--profile", profile)) if value]` (`browser.py:716-717` and, transposed, `browser.py:1478-1484`), then refuse `bad-args` when it is not exactly one.
  - Both then look the row up the same three ways — `next((r for r in rows if r["pid"] == _to_int(pid)), None)` (`browser.py:723` vs `1487`), by `_norm(profile)` (`725-726` vs `1490-1491`), by port (`728-730` vs `1493-1495`).
  - Both refuse `no-browser` with the same sentence shape (`731-732` vs `1496-1497`) and `cdp-unreachable` when the endpoint does not answer (`736-740` vs `1498-1502`); `_named_browser` adds the `verified`→`cdp-not-local` step (`1503-1507`) that `attach` spells out inline as `not_local_refusal(...)` (`741-744`).
  - `detach` builds the `given` list a third time (`browser.py:785-790`) and then filters the attach records by the same three keys (`792-798`).
- proposal: `browser_control/lib/browsers.py :: @dataclass class BrowserSelector(port, pid, profile)` with `def named(self, require_verified: bool) -> Row` and `def one_of(self) -> str` (the `bad-args`/"name ONE browser" rule); `attach`, `detach` and `stop` (`browser.py:1512-1602`, which calls `_named_browser` at `1538`) all take one. The three hand-written lookups collapse to one.
- dependencies: F-6.9 (`stop`/`launch` move too), F-6.5 (`_to_int`).
- risk: medium — this is the consent gate for signalling another tool's browser; the codes must stay byte-identical.
- acceptance: `grep -c 'for r in rows if r\["pid"\] == _to_int(pid)' browser_control/lib/browser.py` == 1; `close --pid/--port/--profile` and `attach --list` error codes unchanged; `tests/live_test.py` foreign-browser checks green.

### F-6.8 `lib/browser.py` (2455 lines) is six modules wearing one name
- category: package-split
- locations: `browser_control/lib/browser.py:84-176`, `268-443`, `533-568`, `693-810`, `1225-1333`, `1367-1602`, `211-266 + 1571-2030`, `2053-2455`
- evidence:
  - One file owns: (a) root/binary/profile-path resolution (`root`, `binary`, `profile_dir`, `scope`, `instance_dir`, `profiles`, `_norm` — `84-176`); (b) `/proc` process identity (`_pid_alive` … `_spawn` — `268-443`); (c) the attach records (`_attached`, `_write_attached`, `is_attached`, `attachments`, `attach`, `detach` — `533-568`, `693-810`); (d) the profile lock (`LOCK_FILE`, `_acquire`, `_lock`, `_await_owner` — `1225-1333`); (e) tab resolution and bulk close (`_match_spec`, `resolve_tab`, `_resolve_across`, `_spec_matches`, `close_tabs` — `211-266`, `1571-2030`); (f) page navigation (`_wait_*`, `nav`, `history`, `activate`, `reload` — `2053-2455`); plus lifecycle (`launch`, `stop`, `browser_info`, `list_browsers` — `1052-1602`).
  - It is the repo's hub: imported by `dom.py:59` (aliased `as tabs`), `profile.py:40-48`, `cli/main.py:25/28`, and `plugins/x_reader.py:26`.
  - `profile.py` needs six of its privates (`_is_managed` via a module-level import, `_norm`, `_lock`, `_lock_path`, `_attached`, `_write_attached`, `_pid_file` — `profile.py:41-48, 262, 264, 398, 400, 482, 484, 503-505, 510`).
- proposal: `browser_control/lib/browser/` package with `__init__.py` as a **re-export facade** so `browser_control.lib.browser.<name>` keeps resolving for every current name:
  `paths.py` → `lib/paths.py` (F-6.5), `proc.py` → `lib/proc.py` (F-6.3), `owners.py` → `EndpointOwners` (F-6.1), `attach.py` → records + `attach`/`detach`/`attachments`, `lock.py` → `lock`/`lock_path` (public), `resolve.py` → `browsers`/`drivable`/`writable`/`readable`/`narrow`/`one_tab`/`resolve_across`/`tab_info`, `tabs.py` → `list_tabs`/`new_tab`/`close_tabs`/spec matching, `nav.py` → `nav`/`history`/`reload`/`activate` + the nav waits, `lifecycle.py` → `launch`/`stop`/`browser_info`/`list_browsers`/`managed_profile`.
- dependencies: F-6.1, F-6.3, F-6.5, F-6.7, F-6.12.
- risk: high — 40+ public and 8 private names are referenced by tests; the facade is what keeps it one commit per step.
- acceptance: every module under `lib/` ≤ 800 lines; `python3 -c "import browser_control.lib.browser as b; [getattr(b, n) for n in (...)]"` resolves all names used by `tests/test_unit.py` and `tests/live_test.py`; both suites green.

### F-6.9 `lib/dom.py` (2779 lines): one element-targeting sequence copy-pasted into four verbs
- category: class-candidate
- locations: `browser_control/lib/dom.py:1554-1590` — `click`; `dom.py:1636-1667` — `hover`; `dom.py:1718-1765` — `check`; `dom.py:1805-1808` — `select`; `dom.py:1438-1444` — `_at_point_click`; `dom.py:2472-2476 + 2640-2644` — `focus`, `upload`
- evidence:
  - The same five-call prologue — `_query_args` → `_resolve` → `_session` → `_matches_in(session, needle, css, FIND_CAP)` → `_pick(...)` — appears at `1554-1557` (click), `1636-1639` (hover), `1718-1722` (check), `1805-1808` (select), and in modified form in `focus`/`upload`.
  - The 26-line guard block is **verbatim in three places**, including its comment: `if not element.get("in_viewport"): fail("no-viewport-target", …)` / `if not element.get("hit"): fail("occluded", …)` / `point = _ints(element.get("hit_at") or element.get("point"), 2)` / `if len(point) < 2: fail("no-viewport-target", … "reports no usable point…")` at `1558-1584`, `1640-1661`, `1732-1754` (`no-viewport-target` at `1560/1580`, `1642/1658`, `1734/1750`).
  - The three-event click dispatch loop `for kind, buttons in (("mouseMoved", 0), ("mousePressed", 1), ("mouseReleased", 0)): session.call("Input.dispatchMouseEvent", …)` is written three times: `1438-1444`, `1584-1590`, `1754-1760`.
  - `_matches_in(session, "", "", 1)` is used purely as a "page facts" read in `_click_at` (`1457`), `_hover_at` (`1487`) and `scroll` (`2229`) — a matcher call standing in for a base evaluation.
- proposal: `browser_control/lib/page/target.py :: class Target` — a context manager built from `(tab_spec, browser, for_write)` that owns `session`, `data`, `.element(index)`, `.point()` and `.require_hittable(verb)` (raising `no-viewport-target` / `occluded` / `missing-point` once); plus `refuse("no-viewport-target", …)` used by all four verbs. The eleven dom verbs then read as one prologue + one action + one read-back.
- dependencies: F-6.12 (tests patch `dom._resolve`/`_session`/`_matches_in`/`_under_point` as module globals), F-6.8.
- risk: high — the guard block's exact codes/messages are asserted by the live battery (occluded / off-screen checks).
- acceptance: `grep -c 'reports no usable point' browser_control/lib/page/*.py` == 1; `grep -c "mouseReleased" browser_control/lib/page/*.py` == 2 (the shared loop + its callers); `python3 tests/live_test.py` click/hover/check/select checks green.

### F-6.10 "Profile → port → target websocket → session" is resolved at eight sites
- category: class-candidate
- locations: `browser_control/lib/dom.py:679-680` — `_session`; `dom.py:651-668` — `_document_ws`; `dom.py:715, 763, 869, 1984, 1184`; `browser_control/lib/browser.py:2147-2152` — `_eval`; `browser.py:2276, 2400` — `nav`, `activate`
- evidence:
  - `dom._session` does `port = cdp.port_of(str(row["profile"])); return cdp.Session(_document_ws(port, str(tab_row["id"])))` (`dom.py:679-680`); `browser._eval` does `ws = cdp.target_ws(cdp.port_of(profile), target_id); return cdp.evaluate(ws, …)` (`browser.py:2149-2151`); `nav` and `activate` open `cdp.Session(cdp.target_ws(cdp.port_of(profile), target_id))` inline (`browser.py:2276`, `2400`); `dialog` builds one by hand with `page_domain=False` (`dom.py:1983-1986`).
  - `cdp.browser_call` re-resolves the *browser* websocket per call (`cdp.py:403-406` → `browser_ws` → `get_json(profile, "/json/version")`), and the two bulk loops call it once per item: `browser.py:1204` inside `for url in urls` and `browser.py:1989-1991` inside `for target_id in ids` — one HTTP GET per tab created/closed.
  - The frame scope is applied only through `_document_ws` (`dom.py:660-668`), so any verb that opens its own connection bypasses it — the class of bug the docstring at `dom.py:653-658` records.
- proposal: one factory `browser_control/lib/page/session.py :: def page_session(scope, row, tab_row, *, frame=None, page_domain=True) -> cdp.Session` (frame resolution included), and one `browser_control/lib/page/eval.py :: def evaluate(scope, profile, target_id, expr, timeout) -> Any` for the polling verbs. `browser._eval`, `nav`, `history`, `activate`, `reload` and `dom` all use it; `cdp.browser_call` gains a `BrowserConnection` owner so a bulk loop reuses one. Choose it so that `_document_ws`'s frame resolution can be replaced by a test seam.
- dependencies: F-6.9, lane-1 F-1.1/F-1.3.
- risk: medium — the parked-dialog `page_domain=False` case (`dom.py:1984`) must keep its exact behaviour.
- acceptance: `grep -rn "cdp.target_ws(cdp.port_of" browser_control/` returns 0 lines; the frame checks (`--frame 1`, `frame-unattributable`) and `dialog accept` stay green.

### F-6.11 The tests pin the current seams by name — the fold must move code and tests together
- category: test-coupling
- locations: `tests/live_test.py:45-49`; `tests/test_unit.py:31-42, 77-80, 233-235, 392, 443-444, 487-488, 523-524, 649-650, 1028, 1072, 1423-1424, 1567-1571, 2221-2240, 2396-2403`; `tests/test_unit.py:2043-2045`
- evidence:
  - Module **paths** are hard-coded outside the package: `live_test.py:45-47` `import_module("browser_control.lib.browser" | "browser_control.lib.cdp" | "browser_control.lib.audit")`; `live_test.py:49` runs `REPO / "browser-control-cli"` (the 13-line checkout script).
  - The hermetic suite calls the CLI **in process** and monkeypatches **names bound in `cli.main`'s namespace**: `cli_main.launch` (`:392`), `cli_main.new_tab/list_tabs/tab_info/close_tabs` (`:443-444`), `cli_main.list_browsers/browser_info` (`:487-488`), `cli_main.attach/attachments/detach` (`:523-524`), `cli_main.nav/history/reload` (`:649-650`), `cli_main.close_tabs/:1028`, `cli_main.stop/:1072`, `cli_main.activate` (`:233-235`), `cli_main.cdp.websockets` (`:1423-1424`), and reads `cli_main.HANDLERS`, `cli_main.TAB_SUBCOMMANDS`, `cli_main.PROFILE_SUBCOMMANDS` (`:1567-1571`).
  - It patches **`dom` internals as module globals**: `dom._resolve/session/_matches_in/_under_point = …` (`:2221-2240`) with `dom.click` resolving those globals; `dom.FRAME["resolved"]` (`:2043-2045`); `browser._tabs_of` (`:784-790`), `browser._verify_profile_endpoint` (`:800-824`), `browser._rows` (`:2396-2403`), `cdp.port_of`/`cdp.target_ws`/`cdp.browser_call` (`:803, 1902-1903`).
  - `live_test.py` uses `browser_lib._pid_alive` (`:529, 1671, 1722-1724, 2013, 2111`), `_pid_of` (`:1605, 1611`), `_pid_file` (`:1609`), and `managed_profile` (`:1604`).
  - **The load-bearing consequence**: a re-export facade keeps `import` working but does **not** keep monkeypatching working. `dom.click` looks `_resolve`/`_matches_in` up in *its own* module globals; once `click` lives in `lib/page/input.py`, patching `lib.dom._resolve` no longer reaches it. These checks fail loudly (the fake row would hit a real websocket and raise), not silently — but they must be migrated in the same commit as the move.
- proposal: (a) freeze a seam before any move — `lib/browser/__init__.py` and `lib/page/__init__.py` re-export every name (public **and** the 12 privates the suites touch: `browser._pid_alive/_pid_of/_pid_file/_tabs_of/_rows/_verify_profile_endpoint/_wait_port/_tab_count/_same_page/_exact_spec_match/_resolve_across/_one_tab`, `dom._resolve/_session/_matches_in/_under_point/_png_size/_pixels/_shot_target/_checkable/_frame_target/_frame_summary/_no_dialog/_text_verdict/_is_secret/_playback_verdict/_at_point/_point/_int/_ints/_list/_extract_schema/_extract_records`, `cdp._get_bytes/_checked_ws/_value_of/_foreign`); (b) `cli/main.py` keeps binding the 15 service names plus `HANDLERS`/`TAB_SUBCOMMANDS`/`PROFILE_SUBCOMMANDS`; (c) as each verb moves, update the **patch target** in `tests/` in the same commit (`dom.X = …` → `page.input.X = …`), and prefer patching the owning module to patching a facade.
- dependencies: F-6.1, F-6.8, F-6.9, lane-1 F-1.6.
- risk: high — this is the gate on every other finding; the facade is not sufficient by itself.
- acceptance: `python3 tests/test_unit.py` green after **every** migration step (supervisor to run); `python3 tests/live_test.py` green after the browser/dom steps; `grep -rn "SLF001" tests/*.py | wc -l` does not increase; no test imports a name from `browser_control.lib.browser` that the facade does not export.

### F-6.12 Dead and test-only functions in the hub module
- category: dead-or-unreachable
- locations: `browser_control/lib/browser.py:359-366` — `_profile_marker`; `browser_control/lib/browser.py:1057-1065` — `_wait_port`; `browser_control/lib/browser.py:169-172` — `live_profiles`
- evidence:
  - `_profile_marker` is defined and documented but **called nowhere**: the only occurrences of the name in the repo are `browser.py:359` (the definition) and its own docstring. `_pid_on_marker` (`browser.py:369-372`) and `_pid_on_profile` (`browser.py:389-395`) do the work through `_cmdline_value`.
  - `_wait_port` has no production caller: `launch` uses `_wait_own_port` (`browser.py:1078`), whose docstring explains why `_wait_port` was not enough (`browser.py:1069-1074`). Its only caller is `tests/test_unit.py:2364`.
  - `live_profiles` has no production caller either — `list` and `profile info` walk `browsers()` — and its only caller is `tests/test_unit.py:133`.
- proposal: delete `_profile_marker`; move `_wait_port` and its check into `tests/` (or keep it with a comment naming the test as the caller); drop `live_profiles` unless a verb adopts it.
- dependencies: F-6.8 (do it as part of the move so the deletion lands once).
- risk: low.
- acceptance: `grep -rn "_profile_marker" browser_control/ tests/` returns 0 lines; the 63 hermetic checks still pass after `_wait_port` moves.

### F-6.13 Naming: three vocabularies for two ideas, and a module imported under a false alias
- category: naming
- locations: `browser_control/lib/dom.py:59` — `from browser_control.lib import browser as tabs`; `browser_control/lib/browser.py:951, 958, 1716` — `_brief`, `_row`, `_foreign_row`; `browser_control/lib/browser.py:2434` — `reload` with `# noqa: A001`; `browser_control/lib/dom.py:995, 946` — `_safe`, `_matches_in`
- evidence:
  - `dom.py:59` imports the browser tier `as tabs` and then calls `tabs._one_tab` (`dom.py:648`) and `tabs._brief` (`dom.py:1122, 1136, 1146, 1197, …`) — the alias hides that a page verb depends on the lifecycle module, and the `# noqa: SLF001` on every call marks it.
  - Three "shrink a row" helpers with overlapping fields: `_brief` (`951`: pid/exe/profile/managed/attached/port), `_row` (`958`: pid/exe/path/profile/profile_from/managed/attached), `_foreign_row` (`1716`: id/title/url/pid/exe/profile). `_tabs_or_fail` (`919`) and `_tabs_of` (`933`) are another such pair, one wrapping the other only to re-raise.
  - `reload` shadows the builtin and carries `# noqa: A001`; `dom.type_text` vs the frozen verb `tab type` shows the same collision already worked around once.
- proposal: internal renames only (verb/flag/JSON names stay frozen): `tabs._one_tab` → `browser.one_tab` (public, F-6.8 makes it public anyway), `dom._matches_in(session, "", "", 1)` → a named `page_facts(session)`, and one `browser.browser_row(row, shape=...)` replacing `_brief`/`_row`. Keep `reload` as the frozen verb name at the CLI boundary but name the library function `reload_page`.
- dependencies: F-6.8, F-6.9.
- risk: low.
- acceptance: `grep -rn "import browser as tabs" browser_control/` returns 0 lines; `grep -rn "as tabs\." browser_control/` returns 0 lines; `# noqa: A001` count under `lib/` is 1.

### F-6.14 The plugin surface is three pieces of process state and no argv tool
- category: class-candidate
- locations: `browser_control/lib/plugins.py:60-71` — `class Registry`; `browser_control/lib/plugins.py:84-98` — `load`; `browser_control/lib/capabilities.py:143-149` — `set_plugins`; `browser_control/cli/main.py:1159, 1176-1178` — `PLUGINS`; `plugins/x_reader.py:74, 80`
- evidence:
  - `Registry` holds `actions`/`plugins`/`errors` (`plugins.py:66-68`) but loading is a **free function** `load(reserved)` (`plugins.py:84`) that constructs it, so the class has no construction policy of its own.
  - The declared classes are then copied into a second module dict by `capabilities.set_plugins` (`capabilities.py:143-149`, called at `cli/main.py:1178`) so that the gate can see them — the same fact in two places.
  - `cli/main.py:1159 PLUGINS = plugins_lib.Registry()` is rebound per invocation under `global PLUGINS` (`cli/main.py:1176-1177`) and read by `_verb_names` (`1162`) and `_plugins_report` (`1167`).
  - A plugin that needs to parse its own flags must re-implement private CLI helpers (`x_reader.py:74 _take`, `:80 _value`) — see F-6.6.
- proposal: `lib/plugins.py :: class PluginSet` with `PluginSet.load(reserved)` as a classmethod and one `classes()` method returning the `{verb: (classes…)}` map; `cli.main` holds one instance per invocation; `capabilities` takes the plugin classes as a value (`Surface.from(ACTIONS, plugins.classes())`, lane-1 F-1.4) instead of keeping `PLUGIN_ACTIONS`.
- dependencies: lane-1 F-1.4, F-6.6, F-6.11.
- risk: low-medium — `selftest` must keep printing `plugins` and `plugin_errors` with the same keys/new key for `plugin_errors` (see the CLAUDE.md agent-rule about `plugin_errors`).
- acceptance: `grep -rn "PLUGIN_ACTIONS" browser_control/` returns 0 lines; `browser-control-cli selftest` output keys `plugins`/`plugin_errors` unchanged; the plugin containment checks in `tests/test_unit.py` green.

### F-6.15 (P2) `admin`: the CLI imports the vendor tier for one private call
- category: lib-module
- locations: `browser_control/cli/main.py:701-706` — `cmd_tab_frames`; `browser_control/lib/dom.py:646-648` — `_resolve`; `browser_control/lib/dom.py:761` — `frames`
- evidence:
  - Every page verb takes a `tab` spec and resolves it themselves, but `frames(row, tab_row)` takes **pre-resolved rows**, so the CLI reaches into the page tier's private resolver: `cli/main.py:705 row, tab_row = dom._resolve(spec, browser, for_write=False)  # noqa: SLF001` then `main.py:706 return dom.frames(row, tab_row)`.
  - That is the only `dom._` access from the CLI, and the only verb whose signature breaks the `(text, selector, index, tab, browser)` shape the other eleven share.
- proposal: `dom.frames(tab: str = "", browser: str = "") -> dict` resolving internally like `find`/`text`/`media`; `cmd_tab_frames` (`main.py:701-706`) becomes `return dom.frames(tab=spec, browser=browser)`. Keep an internal `frames_of_rows(row, tab_row)` for `_frame_summary`'s existing caller.
- dependencies: F-6.9 (do it as part of the `page/` split).
- risk: low.
- acceptance: `grep -rn "dom\._" browser_control/cli/` returns 0 lines; `tab frames` JSON unchanged (live battery `tab frames` check green).

## Proposed target layout for this slice
Three candidates for the whole repo; the trees are full paths. `[L1]` marks a file whose scope is lane 1's (kept here only to show where it lands).

**A. “Facade split” — recommended.** Keep the two-package shape, add leaves, turn the two giants into packages with re-export facades so no call site and no test path changes at once.
```
browser_control/
  __init__.py                — __version__ only
  __main__.py                — python -m shim
  lib/
    __init__.py              — package docstring
    errors.py                — ControlError, fail, CODES [L1]
    text.py                  — one untrusted-text flattener/sanitiser        (F-6.2)
    coerce.py                — as_int/as_float/as_ints/as_list              (F-6.5)
    poll.py                  — poll(probe, timeout, interval) + deadline()  (F-6.4)
    proc.py                  — /proc reads: proc_text, exe_name, pid_alive, listener_of (F-6.3)
    paths.py                 — expand/norm/root/profile_dir/is_managed/pid_file/lock_path (F-6.5)
    scope.py                 — class Scope: profile + frame for ONE invocation (F-6.1)
    lock.py                  — profile flock: lock(), lock_path(), hold()   (F-6.8)
    attach.py                — the attach records: read/write/list/revoke   (F-6.8)
    audit.py                 — class ActionLog; no module-level mutable global
    capabilities.py          — declaration only [L1]
    policy.py                — the gate as a value object [L1]
    plugins.py               — class PluginSet (load/classes/describe)      (F-6.14)
    cdp/                     — endpoint.py, targets.py, rpc.py, session.py [L1]
    browser/
      __init__.py            — re-export facade: every current browser.* name  (F-6.8)
      resolve.py             — browsers/drivable/writable/readable/one_tab/resolve_across
      owners.py              — class EndpointOwners (the memo, with eviction)  (F-6.1)
      lifecycle.py           — launch/stop/browser_info/list_browsers/managed_profile
      tabs.py                — list_tabs/tab_info/new_tab/close_tabs/spec matching
      nav.py                 — nav/history/reload_page/activate + the nav waits
    page/
      __init__.py            — re-export facade (lib/dom.py re-exports it too)  (F-6.9)
      target.py              — class Target: session + element + point + hittable guard
      match.py               — PRELUDE, expressions, queries, page_facts()
      reads.py               — js/wait/text/extract/find/frames
      input.py               — click/hover/check/select/scroll/focus/press/insert/type/upload
      media.py, shot.py, dialog.py  — the three verbs with their own read-back oracles
    dom.py                   — compatibility facade re-exporting page/* (tests import it)
    profile/                 — __init__.py (facade), info.py, seed.py, reset.py
  cli/
    __init__.py
    main.py                  — dispatch + the frozen stdout/stderr/exit contract
    argv.py                  — pop/pop_all/switch/globals (public to plugins)  (F-6.6)
    usage.py                 — the USAGE text
    commands.py              — the cmd_* handlers
plugins/x_reader.py          — unchanged except it uses cli.argv
```
Tradeoffs: largest total diff, but every step ships behind a facade, so the suite stays green throughout and the frozen contract cannot move. Layering is `errors → text/coerce/poll/proc/paths → audit/lock/attach/scope → cdp → browser → page → profile → capabilities/policy → plugins → cli`, still acyclic. Cost: three facades (`lib/browser/__init__`, `lib/page/__init__`, `lib/dom.py`) to keep alive, and the monkeypatch caveat in F-6.11 (a facade is not enough for the 4 `dom._*` patch sites).

**B. “Leaves only” — cheapest.** Add `lib/{text,coerce,poll,proc,paths,scope}.py`, `cli/argv.py`, publish the private seams (`norm`, `is_managed`, `one_tab`, `brief`, `lock`), and stop there: `browser.py` stays ~2100 lines, `dom.py` ~2500. Tradeoffs: F-6.2/F-6.3/F-6.4/F-6.5/F-6.6 all land with near-zero risk and no test churn; F-6.7–F-6.10 and F-6.15 remain, so the hub file and the duplicated guard block survive. Good as a first slice, insufficient as the answer.

**C. “Layer-first packages” — cleanest, most churn.** `browser_control/{core,transport,dom,browser,cli}` with `core` = the leaves above, `transport` = `cdp`, `dom` = the page tier, `browser` = lifecycle/tabs/nav/attach, `cli` = adapters, and **no** back-compat facades. Tradeoffs: the import story becomes obvious and every file has one reason to change, but `tests/live_test.py:45-47` (hard-coded `browser_control.lib.browser|cdp|audit`), the ~45 `# noqa: SLF001` test sites, and any external plugin's imports break in one commit. Recommend only if the team wants to update the suites as part of the same change.

**Recommended migration order (Layout A):**
1. Leaves with no dependencies: `lib/text.py`, `lib/coerce.py`, `lib/paths.py`, `lib/poll.py`, `lib/proc.py` (F-6.2–F-6.5). No behaviour change, no test churn.
2. Publish the seams: `browser.norm/is_managed/one_tab/brief/lock/lock_path`, `dom.resolve/session/matches_in`, and re-export the 12 privates the suites use (F-6.11). Delete `_profile_marker` and the dead `_wait_port`/`live_profiles` here (F-6.12).
3. `cli/argv.py`, and re-point `plugins/x_reader.py` at it (F-6.6).
4. `browser.py` → `lib/browser/` behind the facade, then `BrowserSelector` (F-6.7) and `EndpointOwners` (F-6.1a).
5. `dom.py` → `lib/page/` behind `lib/dom.py`, then `Target` (F-6.9), the session factory (F-6.10) and F-6.15 — moving the 4 monkeypatch sites in `tests/test_unit.py:2221-2240` in the same commit.
6. `cli/main.py` → `usage.py`/`commands.py`/`main.py`, keeping the re-exported service names, then `Scope` and `PluginSet`/`Policy` objects (F-6.1b, F-6.14).

## Open questions
- **Who owns `/proc`?** Lane 1 assigns `listener_of` to `lib/cdp/endpoint.py`; F-6.3 wants one `lib/proc.py` that both `cdp` and `browser` import. The direction matters (a leaf may not import a package that imports it) — the human architect must pick `lib/proc.py` as the owner, or accept two `/proc` readers.
- **May the per-invocation scope become an object?** `browser.SCOPE` and `dom.FRAME` are process-global and are set from the CLI once per invocation (`cli/main.py:1230-1231`). If any embedder (not just the CLI) is meant to call `lib` twice in one process, `Scope` must be threaded or contextvar-backed; if the only entry point is the CLI, a per-`main()` instance is enough. This decides how large F-6.1's diff is.
- **How much test churn is acceptable?** Layout A keeps every module path and name importable but *cannot* keep the four module-global monkeypatches working (`tests/test_unit.py:2221-2240`). Is updating those patch targets — and the ~45 `# noqa: SLF001` sites — in scope for this refactor, or must the private names survive as facade attributes *and* remain the resolution path for the verbs? The latter constrains the split (the verbs must keep looking these helpers up in one shared namespace).
- **Is `plugins/x_reader.py` a supported artifact or a fixture?** It is the only in-repo consumer of the plugin API and one hermetic check drives it (`tests/test_unit.py:2820-2890`). If it is a fixture, F-6.6/F-6.14 can move it under `tests/`; if it is a shipped example, `cli/argv.py` becomes part of the public plugin surface and must be documented in `lib/plugins.py`'s docstring.