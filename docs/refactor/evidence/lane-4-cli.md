# Lane 4: `browser_control/cli/main.py` (1320 lines) + `browser_control/lib/audit.py` (224 lines)

## Summary
- **What this slice is.** `cli/main.py` is the whole argv adapter: a 122-line `USAGE` literal (50–171), argv primitives (`_one`/`_none`/`_urls`/`_selector`/`_pop`/`_pop_all`/`_switch`/`_tab_flag`/`_int`/`_float`, 174–460), 39 `cmd_*` handlers (265–943), the three verb tables (945–995), the global-flag reader (997–1048), the gate's own argv re-reader (`_modes`/`resolved_mode`/`action`, 1070–1157), and one 150-line `main` (1171–1320). `lib/audit.py` is one `ActionLog` class (96–217), a module singleton `LOG` (219), and a dead `reset_redaction` (222–224).
- **Biggest structural problem (ranked):** (1) `main` owns six jobs in one function and hides per-invocation state in five module globals; (2) the gate re-parses argv independently of the handlers (`resolved_mode`/`action`), which is the exact seam that already produced the security bug recorded in `docs/progress.md:905-910`; (3) 15 hand-copied "unknown flag" loops + 8 `_int(x) if x is not None else None` dances + 6 near-identical element-verb handlers; (4) the surface is declared in four places (`capabilities.ACTIONS`, the three tables, `USAGE`, `dom.FRAME_VERBS`), with `capabilities.unclassified()` existing only to catch the drift; (5) `cli.main` is monkeypatched by 20+ test sites, so any split must keep its public names or migrate the suite.
- **Already fine — do not "fix":** output discipline is genuinely single-sited (every `print`/`ERR[...]`/exit-2 in the tree is in `cli/main.py`; `lib` never prints, per `errors.py`'s "lib raises it, cli prints it"). There is **no** `browser_control` ⇄ `browser_control/cli` cycle: `lib` never imports `cli`, and the only package-root import (`from browser_control import __version__`, main.py:23) hits an `__init__.py` with no imports. `ActionLog` is already a class that owns its one piece of state (`_secret`); what it needs is a sink seam, not a rewrite.
- **What is not worth doing:** moving ERR/JSON formatting into `lib` (it would invert a documented boundary), and replacing the hand-written argv readers with `argparse` — every refusal message is contract-visible and asserted by substring across the hermetic suite.
- **Verdict: OK with notes.** Nothing here is a behaviour bug and no proposal changes the frozen contract; sequence F-4.1/F-4.2/F-4.3/F-4.6 first, then F-4.4/F-4.5, and keep F-4.8 optional.

## Findings

### F-4.1 `main()` is a six-job orchestrator holding call state in five module globals
- category: class-candidate
- locations: `browser_control/cli/main.py:1171-1320` — `main`; `browser_control/cli/main.py:1176-1177` — `global PLUGINS`; `browser_control/cli/main.py:1190-1213` — the duplicated no-verb refusal; `browser_control/cli/main.py:1246-1279` — frame rule + reply decoration; `browser_control/cli/main.py:1288-1320` — three `except` arms + audit write
- evidence:
  - 150 lines in one function: plugin load (1176–1179), help (1180–1184), two refusal paths (1190–1199 and 1201–1213), global application (1229–1235), handler lookup incl. plugins (1236–1242), `--frame` rules (1246–1258), the gate (1259–1269), dispatch (1270), reply mutation (1271–1279), print+flush+`return 0` (1280–1287), then `except ControlError` (1288), `except BrokenPipeError` with the `dup2(/dev/null)` dance (1292–1306), `except Exception` → `internal` (1307–1314), `finally` → `audit.LOG.write` (1315–1320).
  - The identical three-statement refusal (`print(USAGE, file=sys.stderr)` / `print("ERR[bad-args]: a verb is required …")` / `return 2`) is written twice, ten lines apart (1195–1199, 1205–1213).
  - Per-call state is written into five globals owned by four other modules: `browser_lib.scope(...)` (1230 → `browser.py:121 SCOPE`), `dom.frame(...)` (1231 → `dom.py:86 FRAME`), `capabilities.policy(...)` (1234 → `capabilities.py:167 POLICY`), `audit.LOG.begin(...)` (1235), and the rebound `PLUGINS` (1177 → `cli/main.py:1159`), the last needing `global` (1176).
  - The dispatcher mutates a verb's return value: `reply["frame"]`/`reply["frame_resolved"]` (1276, 1279) — the library layer has no counterpart for this decoration.
- proposal: new `browser_control/cli/runner.py :: class Invocation` holding one call's `browser`, `profile`, `frame`, `policy`, `plugins`, `log`, with methods `load_plugins()`, `start()` (the two refusal paths, on one code path), `gate()`, `dispatch()`, `decorate(reply)`, `emit(reply) -> int`; `browser_control/cli/emit.py :: class Reporter` owns stdout/stderr + the three `except` arms and returns the exit code. `cli/main.py` keeps a `main(argv) -> int` façade of ≤ 10 lines (the console script `browser_control.cli.main:main` and `__main__.py:10` must keep resolving). Caller migration: `browser_control/cli/main.py:1171-1320` only; `__main__.py:10`, `browser-control-cli:11` unchanged.
- dependencies: F-4.5 (hosting), F-4.6 (name compatibility)
- risk: medium — the exception arms are load-bearing (the `dup2` fix and the 120-exit bug behind `print` are pinned by `tests/test_unit.py:2575-2590`); behaviour must move, not change.
- acceptance: `python3 tests/test_unit.py` green; `grep -c 'def main(' browser_control/cli/main.py` == 1 and that body ≤ 15 lines; `grep -rn '^ *global ' browser_control/cli/` == 0 matches; `python3 -m browser_control selftest` and the console script return the same JSON keys and exit codes as before.

### F-4.2 Argument plumbing is copy-pasted per verb, not owned by one argv reader
- category: duplication
- locations: `browser_control/cli/main.py:243-252` — `_selector`'s flag loop; `browser_control/cli/main.py:403-470` — `_pop`/`_switch`/`_tab_flag`/`_pop_all`; the 15 in-handler unknown-flag loops; the 8 `--index` conversions
- evidence:
  - `if str(arg).startswith("-"): fail("bad-args", f"{verb}: unknown flag {arg!r}")` is repeated at main.py:177, 206 (inside helpers) **and** inside 13 handlers: 515, 525, 569, 595, 615, 651, 690, 715, 766, 795, 812, 829, 872, 889 (15 call sites total).
  - The `--index` dance `_int(index, "tab X --index") if index is not None else None` appears 8× (584, 604, 628, 782, 802, 821, 880, 897), each carrying its own copy of the verb's name as a literal.
  - `one TEXT at most, got {len(rest)}` appears 8× (572, 598, 618, 718, 769, 798, 815, 847); `give TEXT or --selector CSS, not both` 4× (601, 621, 721, 818); `give TEXT, --selector CSS, or --at X,Y` 2× (581, 779).
  - `cmd_tab_hover` (562–586), `cmd_tab_check` (588–606), `cmd_tab_select` (608–630), `cmd_tab_find` (709–726), `cmd_tab_click` (759–784), `cmd_tab_focus` (806–823) are six copies of the same shape: `_tab_flag` → `_pop --selector` → `_pop --index` → flag loop → `len(rest) > 1` → `needle` → mutual-exclusion check → call.
- proposal: `browser_control/cli/argv.py :: class VerbArgs(verb, rest)` with `.pop(flag)`, `.pop_all(flag)`, `.switch(flag)`, `.int(flag)`, `.float(flag)`, `.tab()`, `.one()`, `.none()`, `.target(allow_at=False)`, `.selector(allow)`, `.done()`; `_flags` becomes `VerbArgs.globals(argv)`. Migration: every `cmd_*` in `main.py:265-943` plus helpers 174–460, moved to `cli/verbs/{browser,tab,profile}.py`.
- dependencies: F-4.1, F-4.5
- risk: medium — the refusal text is the contract (asserted by substring in dozens of checks); the class must emit the current strings verbatim.
- acceptance: `grep -c 'startswith("-")' browser_control/cli/verbs/*.py` == 0; `grep -rc 'one TEXT at most' browser_control/cli/` == 1; `grep -rn '_pop(\|_int(' browser_control/cli/verbs/` == 0; `python3 tests/test_unit.py` green with no test edits.

### F-4.3 The gate re-reads argv with its own parser, in lockstep with the handlers by comment
- category: duplication
- locations: `browser_control/cli/main.py:1070-1082` — `_modes`; `browser_control/cli/main.py:1084-1125` — `resolved_mode`; `browser_control/cli/main.py:1127-1157` — `action`; `browser_control/cli/main.py:1259-1269` — the gate; `browser_control/cli/main.py:740-757` — `cmd_tab_wait`
- evidence:
  - `resolved_mode`'s own comment admits the coupling: "`--tab` FIRST, exactly as `cmd_tab_wait` pops it: a `--tab` value that is literally `--for` must not be read as the mode" (1104–1106), then re-does `_pop(args, "--tab", …)`, `_pop(args, "--for", …)` (1099–1103) and the equivalent for `dialog`/`media` (1108–1116).
  - `action` builds the capability key by string formatting (`f"tab {head} {resolved_mode(...) or 'state'}"`, 1148–1151) that must equal a key in `capabilities.ACTIONS` (capabilities.py:42-98) — a third spelling of the verb's identity.
  - This seam already shipped a real hole: `docs/progress.md:905-910` records "`--for JS`, `--for \" js\"` and `--for load --for js` were authorised as reads and then ran caller code" as one of the review campaign's measured findings; the guard against it is now `tests/test_unit.py:1734-1762`.
  - `_modes` reads the declared actions back out of `capabilities.ACTIONS` (1078–1082) so the valid-mode list is not kept twice — the right instinct, applied to one of the two things the gate needs.
- proposal: declare the action with the verb: `browser_control/cli/registry.py :: class Verb.action_of(rest) -> str` (default `self.name`; `tab wait`/`dialog`/`media` supply their mode reader — the same reader their handler uses). The gate then calls `VERBS.lookup(verb, rest).action_of(rest)`, and `resolved_mode`/`_modes`/`action` collapse into that per-verb reader. Migration: `main.py:1265` (`wanted = action(verb, rest)`); tests at `test_unit.py:1704-1761` keep calling `cli.main.action`.
- dependencies: F-4.4
- risk: medium — the gate is security-relevant and fail-closed; the mode refusal must stay `bad-args` (not `not-allowed`) so a typo is not reported as a policy verdict.
- acceptance: exactly one function in `browser_control/cli/` derives a call's action; `grep -rn 'resolved_mode' browser_control/` == 1 definition + 1 use; `tests/test_unit.py::t_policy_gate` and `t_gate_and_argv_hardening` green unmodified; `run_cli(["tab","wait","--for","JS","--allow","read"])` → exit 2, `ERR[not-allowed]`.

### F-4.4 One verb record instead of four declarations
- category: class-candidate
- locations: `browser_control/cli/main.py:945-995` — `TAB_SUBCOMMANDS`/`PROFILE_SUBCOMMANDS`/`HANDLERS`; `browser_control/lib/capabilities.py:42-98` — `ACTIONS`; `browser_control/cli/main.py:50-171` — `USAGE`; `browser_control/lib/dom.py:141-145` — `FRAME_VERBS`; `browser_control/lib/plugins.py:180-187` — the plugin action record
- evidence:
  - The same surface is declared four times: names→callables (`HANDLERS`, main.py:982-995), names→classes (`ACTIONS`, capabilities.py:42-98), built-in prose (`USAGE`, main.py:50-171), and frame eligibility (`FRAME_VERBS`, dom.py:141-145 — read at main.py:1250, 1276 and pinned by `test_unit.py:2106-2110`).
  - `capabilities.unclassified()` (capabilities.py:100-124) exists **only** because `ACTIONS` and the tables can drift; it is called from `selftest` (main.py:382-385) and from `test_unit.py:1568-1573`.
  - Plugin actions already are records — `{"run", "classes", "usage", "plugin", "path"}` (plugins.py:180-187), validated in `_register` (plugins.py:150-186) and rendered into help by main.py:1182-1183 — while the built-ins carry no `classes`/`usage` metadata at all.
  - Per-verb applicability is hand-written in four places: `attach --list` vs scope (main.py:270-275), `list` vs `--profile` (324-331), `profile info` vs `--browser` (905-910), every verb vs `--frame` (1246-1258), each with its own message.
- proposal: `browser_control/cli/registry.py :: @dataclass class Verb(name, handler, classes, usage, takes_scope=True, takes_browser=True, frame_ok=False, action_of=None)` + `class VerbTable` exposing `.handlers`, `.subcommands`, `._verb_names()` and `.lookup(verb, rest)`; `capabilities.ACTIONS` stays the gate's declaration, and one hermetic check asserts `Verb.classes == ACTIONS[action]` for every verb. Keep hand-written parsing (F-4.2) — the refusal strings are the contract, so no `argparse`.
- dependencies: F-4.2, F-4.6
- risk: medium — the drift check must not be weakened while the four declaration sites are consolidated; `selftest`'s `verbs`/`capabilities` output must stay byte-identical given the same environment.
- acceptance: every entry in the three tables carries non-empty `classes` and `usage`; `capabilities.unclassified(HANDLERS, {"tab":…, "profile":…}) == []`; `USAGE`'s first line is still `usage: browser-control-cli VERB [ARGS]` (`test_unit.py:1406`) and the help body is unchanged; `selftest` JSON keys unchanged.

### F-4.5 `cli/` needs a package split to host the above, plus a packaging line
- category: package-split
- locations: `browser_control/cli/` — `__init__.py` (1 line) + `main.py` (1320 lines); `pyproject.toml:31` — `packages = ["browser_control", "browser_control.lib", "browser_control.cli"]`; `pyproject.toml:27` — console script
- evidence:
  - `cli/` is the only package in the tree with a single module; `ls browser_control/cli` → `__init__.py`, `main.py` only.
  - The natural seams are already contiguous line ranges in one file: USAGE 50–171 (122 lines), argv primitives 174–460, browser/profile handlers 265–337 + 901–943, tab handlers 462–900, tables 945–995, flag reader 997–1048, action resolution 1050–1157, `main` 1171–1320.
  - `[tool.setuptools] packages` is an explicit list, so a new `browser_control.cli.verbs` subpackage is **not** picked up until it is added — an install-time failure the test suite (run from the checkout) would not catch.
  - `browser-control-cli` (repo-root script) and `browser_control/__main__.py:10` both import `browser_control.cli.main:main`, so the entry point name must not move.
- proposal: layout in "Proposed target layout" below; the only packaging edit is adding `"browser_control.cli.verbs"` to `pyproject.toml:31`.
- dependencies: hosts F-4.1, F-4.2, F-4.3, F-4.4
- risk: low — pure layout, provided `cli/main.py::main` remains the entry point.
- acceptance: `pip install -e .` (or `python -m build`) still installs `browser-control-cli`; `python -c "import browser_control.cli.verbs.tab"` succeeds; `python3 -m browser_control selftest` exits 0; `grep -c browser_control.cli.verbs pyproject.toml` == 1.

### F-4.6 The suite patches `cli.main` internals — the split's binding constraint
- category: test-coupling
- locations: `tests/test_unit.py:28-44` — imports `cli_main`; `tests/test_unit.py:391-406, 441-465, 486-502, 522-553, 648-679, 1027-1061, 1071-1090, 1383-1409` — monkeypatching; `tests/test_unit.py:1423-1430, 1872-1883, 2084-2096` — module-attribute patching; `browser_control/cli/main.py:28-44` — the 15 from-imported names
- evidence:
  - All 15 names from-imported at main.py:29-44 are monkeypatched by the suite: `activate` (test_unit.py:233/235/279), `launch` (391-406), `new_tab`/`list_tabs`/`tab_info`/`close_tabs` (441-465), `list_browsers`/`browser_info` (486-502), `attach`/`attachments`/`detach` (522-553), `nav`/`history`/`reload` (648-679), `close_tabs` (1027-1061), `stop` (1071-1090), and all four again in the `internal` test (1383-1409).
  - Module attributes are patched too: `cli_main.cdp.websockets` (1423-1424), `cli_main.dom.click` (2084-2085), and the table itself — `cli_main.HANDLERS["list"] = explode` (1872-1883).
  - The tables and the action resolver are read directly by tests: `cli_main.HANDLERS`/`TAB_SUBCOMMANDS`/`PROFILE_SUBCOMMANDS` (1568-1573, 2106-2110) and `cli_main.action(...)` (1704-1761).
  - Patching works today only because handlers call the *globally bound* names; if `cmd_open` moves to `cli/verbs/browser.py` and calls `browser_lib.launch`, `cli_main.launch = fake_launch` stops taking effect.
- proposal: keep `browser_control/cli/main.py` as the façade that re-exports `HANDLERS`, `TAB_SUBCOMMANDS`, `PROFILE_SUBCOMMANDS` and `action`, and have verb modules call the library through module attributes (`browser_lib.launch(...)`, `dom.click(...)`) so the suite's patch target is stable; then migrate the 15 name patches to `browser_control.lib.browser.<name>` in a follow-up commit (the tests already patch `dom.*` this way).
- dependencies: constrains F-4.1, F-4.4, F-4.5
- risk: medium — a mechanically clean split silently disables 20 test doubles, turning real coverage into green-but-empty checks (the suite's own history in `docs/progress.md:896-904` records this class of "assertion that cannot fail").
- acceptance: `python3 tests/test_unit.py` green **with no test edits** (façade path), or, if tests migrate, `grep -c 'cli_main\.\(launch\|new_tab\|nav\|stop\|attach\)' tests/test_unit.py` == 0 and the number of PASS lines is not lower than today.

### F-4.7 Dead code and an unreset secret in the verb seam
- category: dead-or-unreachable
- locations: `browser_control/cli/main.py:243-252` — `_selector`'s `--profile` branch; `browser_control/cli/main.py:838-850` — `_text_arg(…, tab)`; `browser_control/lib/audit.py:102-104` — `begin(action)`; `browser_control/lib/audit.py:222-224` — `reset_redaction`; `browser_control/cli/main.py:1235` vs `1190-1213`
- evidence:
  - `_selector` accepts `--profile` and stores `out[key] = value` (243–252), but `_flags` (`main.py:1001-1047`, `FLAG_KEY` at 997) strips `--profile` from argv *before* dispatch, so the branch cannot fire through `main`; and no caller reads the value — `cmd_attach` passes `browser_lib.scope()` (279), `cmd_detach` too (289), `cmd_close` too (313). `out["profile"]` is read only as a boolean in the conflict checks (258, 260). `test_unit.py:2551-2552` proves the path: `attach --list --profile DIR` is refused at main.py:270-275, from the global scope, never from `_selector`.
  - `_text_arg(rest, verb, tab)` (838) never uses `tab`; both callers pass `spec` (854, 861).
  - `ActionLog.begin(action)` (102) ignores its argument — the CLI passes `verb` (main.py:1235) and passes the verb *again* at write time (main.py:1319), so two call sites must agree about "what is running" while one of them is a no-op.
  - `reset_redaction()` (audit.py:222-224) has zero callers in the tree (already noted as dead in `docs/review-2026-09-20.md:236`), which is why the reset gap below went unnoticed: `begin` is the only resetter and is reached at 1235, *after* the two early refusals return at 1199/1213; the `finally` (1319) then writes with the previous call's `_secret` still set, so an in-process second call can stamp `redacted: true` on a refusal that carried no secret. Fail-closed (over-redaction, never a leak) and unreachable from the installed one-shot command — `t_cli_no_args_is_logged` (test_unit.py:2560-2572) asserts only `code`.
  - `ActionLog.write(args: Any)` (audit.py:134) is looser than its use (`for a in (args or [])`, 144-147): a bare string iterates per character (noted in `docs/review-2026-09-20.md:237`).
- proposal: delete `reset_redaction`; drop `begin`'s parameter (keep the name) or fold it into the `Invocation.start()` of F-4.1 and call it on every path (move it above the early returns); delete `_selector`'s `--profile` branch and its two `out["profile"]` mentions; drop `_text_arg`'s third parameter; type `write(args=...)` as `Sequence[str] | None`.
- dependencies: F-4.1 (single start path)
- risk: low — all four changes are contract-neutral; the `--profile` deletion is behaviour-identical for every argv reachable through `main`.
- acceptance: `grep -rn 'reset_redaction' browser_control tests` == 0; `grep -c 'def begin(self, action' browser_control/lib/audit.py` == 0; `grep -c '"--pid", "--profile"' browser_control/cli/main.py` == 0; new hermetic check: `LOG.begin("tab"); LOG.mark_secret("x"); run_cli([])` leaves `redacted` absent from the refusal row; `python3 tests/test_unit.py` green.

### F-4.8 `audit.py` mixes the record model with filesystem policy
- category: lib-module
- locations: `browser_control/lib/audit.py:96-165` — `ActionLog`; `browser_control/lib/audit.py:41-62` — `scratch_dir`; `browser_control/lib/audit.py:167-208` — `_append`; `browser_control/lib/audit.py:210-217` — `_scratch_copy`; `browser_control/lib/audit.py:219` — `LOG`
- evidence:
  - Three responsibilities in 224 lines: the record + redaction model (`ActionLog.begin/mark_secret/write/_censor`, 96–165), the file and permission policy (`_append`: `makedirs(0o700)`, `os.open(..., 0o600)`, `fchmod` narrowing, the short-write loop, 167–208), and the scratch fallback (`scratch_dir` via `mkdtemp`, 41–62, 210–217).
  - The module-level `_SCRATCH` cache (38, mutated under `global` at 53) is process-wide state the CLI never touches but both suites do: `tests/live_test.py:2295`, `tests/test_unit.py:1497`, `tests/test_unit.py:2611` assert `stat.S_IMODE(os.stat(audit.scratch_dir()).st_mode) == 0o700`.
  - `LOG = ActionLog()` (219) is the singleton the CLI drives twice per call (`begin` 1235, `write` 1319) and `dom.py:2544` writes into once (`audit.LOG.mark_secret(text)`) — a lib-to-lib coupling through a module global, with the writer living in another layer.
  - The permissions are the security-relevant part and are well pinned: 0600 file / 0700 dir (`test_unit.py:2601-2611`), redaction-before-truncation (`2256-2278`), short write ≠ success (`2293-2298`).
- proposal: keep `browser_control/lib/audit.py :: ActionLog` as record + redaction with an injected sink (`def __init__(self, sink: Sink | None = None)`); move the file/permission/scratch policy to `browser_control/lib/logfile.py :: class FileSink` (`write(line) -> bool`, `scratch_dir()`); re-export `scratch_dir` from `audit` so `tests/live_test.py:2295` keeps working, or update those three call sites.
- dependencies: none (independent of the CLI split)
- risk: medium — the mode/fchmod/scratch behaviour is load-bearing (a mis-split loses the 0600 narrowing or the scratch fallback) and the tests around it are precise.
- acceptance: `tests/test_unit.py` cases `t_audit_redaction`, `t_action_log_lands_on_disk`, `t_a_working_log_makes_no_scratch_dirs`, `t_audit_redaction_beats_truncation`, `t_audit_short_write_is_not_success` green unmodified; `browser_control/lib/audit.py` contains no `os.open`/`os.fchmod`/`mkdtemp`; `grep -c 'def scratch_dir' browser_control/lib/logfile.py` == 1.

### F-4.9 `cmd_tab_frames` is the only handler that reaches into `dom`'s private resolver
- category: lib-module
- locations: `browser_control/cli/main.py:701-706` — `cmd_tab_frames`; `browser_control/lib/dom.py:646-648` — `_resolve`; `browser_control/lib/dom.py:761-764` — `frames`
- evidence:
  - `cmd_tab_frames` calls `dom._resolve(spec, browser, for_write=False)` with `# noqa: SLF001` (main.py:705) and feeds the pair to `dom.frames(row, tab_row)` (706), while all 18 other `dom` verbs take `text/selector/index/tab=/browser=` and resolve internally (`js` dom.py:1128→1138, `text` 2417→2427, `click` 1532→1552, `media` 2731→2750, …).
  - `dom.frames` already has the resolution one line away (dom.py:761-764) and the other 18 call sites show the internal `_resolve(tab, browser, for_write=False)` form it needs — the only reason the CLI holds the private call is that `frames`'s signature predates the convention.
- proposal: give `dom.frames` the shared shape — `def frames(tab: str = "", browser: str = "") -> dict` resolving internally — and reduce the handler to `return dom.frames(tab=spec, browser=browser)`. Migration: `browser_control/cli/main.py:705-706`.
- dependencies: none
- risk: low — one handler, one call, same resolved inputs.
- acceptance: `grep -c 'noqa: SLF001' browser_control/cli/main.py` == 0; `dom.frames(tab=…)` exists with the `(tab, browser)` shape; `tests/test_unit.py::t_frames_and_points` and the live `tab frames` checks green.

## Proposed target layout for this slice
- `browser_control/cli/__init__.py` — unchanged (docstring only).
- `browser_control/cli/main.py` — the entry point and compatibility façade: `main(argv) -> int` (≤ 15 lines) + re-exports `HANDLERS`, `TAB_SUBCOMMANDS`, `PROFILE_SUBCOMMANDS`, `action`, `USAGE`.
- `browser_control/cli/runner.py` — `class Invocation`: one call's browser/profile/frame/policy/plugins/log; `start`, `gate`, `dispatch`, `decorate` (F-4.1, F-4.7).
- `browser_control/cli/argv.py` — `class VerbArgs` + `globals(argv)`, `selector()`: argv shapes and refusals only (F-4.2).
- `browser_control/cli/registry.py` — `@dataclass Verb` (`name`, `handler`, `classes`, `usage`, `takes_scope`, `takes_browser`, `frame_ok`, `action_of`) + `VerbTable`; owns the three tables and the action resolution (F-4.3, F-4.4).
- `browser_control/cli/emit.py` — `class Reporter`: one JSON object on stdout, `ERR[code]` on stderr, exit codes, the broken-pipe path (F-4.1).
- `browser_control/cli/usage.py` — the `USAGE` text as data (`SUMMARY: dict[str, str]` + the fixed preamble/SPEC/flags/gate/reads/out blocks) so the help body stays byte-identical (F-4.4).
- `browser_control/cli/verbs/__init__.py` — empty.
- `browser_control/cli/verbs/browser.py` — `open`, `close`, `list`, `info`, `attach`, `detach`, `selftest`.
- `browser_control/cli/verbs/tab.py` — the 27 `tab` subcommands.
- `browser_control/cli/verbs/profile.py` — `profile info|seed|reset`.
- `browser_control/lib/audit.py` — `ActionLog` (record, redaction) with an injectable sink (F-4.8).
- `browser_control/lib/logfile.py` — `FileSink` + `scratch_dir`: 0600/0700, `fchmod` narrowing, short-write loop, scratch fallback (F-4.8).
- `pyproject.toml` — add `"browser_control.cli.verbs"` to `[tool.setuptools] packages` (F-4.5).

## Open questions
1. **Test migration or façade?** Must `browser_control.cli.main` keep re-exporting the 15 library names forever, or is a one-time test migration to `browser_control.lib.browser.<name>` patches acceptable? This decision gates F-4.1/F-4.2/F-4.4 (F-4.6).
2. **Who owns the lib globals?** `browser_lib.SCOPE` (browser.py:121), `dom.FRAME` (dom.py:86) and `capabilities.POLICY` (capabilities.py:167) are set per invocation by `main` (1230–1234). Does `Invocation` become their owner (a cross-lane change in `lib`), or does the CLI keep writing module globals for now?
3. **Single declaration of classes:** should `capabilities.ACTIONS` be *derived* from the `Verb` records (one source, `lib` reading `cli` — which would invert the current import direction), or stay a separate declaration guarded by a drift check plus `unclassified()` (current shape, F-4.4)?
4. **Per-verb applicability:** move the four hand-written scope refusals (main.py:270-275, 324-331, 905-910, 1246-1258) onto `Verb` flags, or leave the messages where they are and only record the metadata?
5. **Is the audit split worth it?** 224 lines with the security-relevant parts correctly separated (F-4.7) — is `lib/logfile.py` warranted, or should the work stop at deleting the dead code and typing `args`?
6. **`dom.frames` shape (F-4.9)** lands in the DOM lane's file; is that lane already planning the internal-resolution convention for it, or should this lane's owner raise it there?