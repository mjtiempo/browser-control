# browser-control — refactor plan (agent-executable)

```yaml
doc: browser-control refactor plan
version: 1
status: ready-for-execution
branch: refactor/fold-into-classes-libs-packages
base_commit: cf252aa
base_describe: "Extraction as a verb, a plugin system, and the first plugin: read-only X search"
created: 2026-09-20
method: >
  Six read-only reviewer subagents (one per slice + one cross-cutting) analysed the
  codebase end-to-end; the parent verified load-bearing claims by re-reading the cited
  lines and running the hermetic suite. Raw findings:
  docs/refactor/evidence/lane-{1..6}-*.md (61 findings total).
source_docs: [README.md, docs/plan.md, docs/progress.md, docs/review-2026-09-20.md]
baseline:
  hermetic: "python3 tests/test_unit.py -> 63 passed, 0 failed"
  live: "python3 tests/live_test.py -> needs a browser; run before/after phases 3-6"
  pyright: "/home/mark/.npm-global/bin/pyright (basic mode, 0 errors was the pre-review baseline)"
  ruff: "in project caches; not on PATH in this shell"
frozen_contract:
  - verb/subcommand names, flags, --help text semantics
  - stdout: exactly one JSON object; stderr: ERR[code]: message; refusal exit status 2
  - every JSON key/value type currently emitted (incl. absence of keys where tests assert absence)
  - every ControlError code currently raised
  - lib never prints; cli/main.py is the only printer
  - security invariants: loopback-only websockets, kernel ownership guard on every write,
    payload caps, redaction-before-truncation, log 0600 / dir 0700, fail-closed policy
  - entry points: browser-control-cli -> browser_control.cli.main:main, python -m browser_control
decisions:
  D-1 scope_globals:
    question: Do browser.SCOPE / dom.FRAME / capabilities.POLICY become per-invocation objects?
    default: "yes — one lib/scope.py::Scope + lib/policy.py::Policy built by cli.main, with
              browser.scope()/dom.frame() kept as shims so lib callers need no edit"
    owner: human architect
  D-2 layout:
    question: Facade split (A) vs leaves-only (B) vs clean-break packages (C)?
    default: "A — packages with re-export facades; every step ships green, contract cannot move"
    owner: human architect
  D-3 proc_owner:
    question: Who owns /proc primitives — lib/proc.py or lib/cdp/endpoint.py?
    default: "lib/proc.py is the single owner (incl. listener_of); cdp imports it, never the reverse"
    owner: human architect
  D-4 plugin_contract:
    question: Keep import-anything plugin contract or add a named seam?
    default: "add browser_control/plugin_api.py with __all__; keep run(rest, browser) signature
              and api:1 untouched; a context object is a separate api:2 decision"
    owner: human architect
  D-5 package_names:
    question: lib/dom/ package (keep name) vs rename to lib/page/?
    default: "keep lib/dom/ (tests and CLI import the name in ~60 places); lib/browser/ for the
              browser tier; no lib/page/"
    owner: human architect
  D-6 test_churn:
    question: How much test editing is allowed?
    default: "allowed: import paths and monkeypatch targets in the SAME commit as the move;
              NOT allowed: changing recorded call-shape tuples or weakening assertions"
    owner: human architect
```

## How an executing agent must use this document

1. Read this file completely, then the evidence file(s) named by the task. The evidence
   contains the full argument and line anchors; the task below is the instruction.
2. Work tasks in dependency order. One task (or one small coherent group) per commit.
3. After **every** commit: `python3 tests/test_unit.py` must be green (baseline 63 passed,
   0 failed). Phases 3-6 additionally require `python3 tests/live_test.py` (needs a real
   browser; it runs the repo-root `browser-control-cli`).
4. The frozen contract above is absolute. A refactor that changes an emitted string, key,
   code, or exit status is a failure even if tests were updated to match.
5. When adding a subpackage, add it to `[tool.setuptools] packages` in `pyproject.toml`
   (the list is explicit; a missing entry ships a broken wheel while checkout tests pass).
6. When a task moves a symbol that tests monkeypatch, update the patch target in the same
   commit. A re-export facade keeps `import` working but does NOT keep patching working.
7. Update this file's task `status:` line and the JSON index at the end as tasks land:
   `status: done (commit <sha>)`.
8. Non-goals: no behavior changes, no new features, no argparse rewrite, no `BrowserService`
   (docs/plan.md:135 is stale — no caller exists), no code formatting sweeps.

### Repo facts (at `cf252aa`)

| File | Lines | Note |
| --- | --- | --- |
| `browser_control/lib/dom.py` | 2778 | 0 classes; 18 `tab` verbs + 40 helpers |
| `browser_control/lib/browser.py` | 2454 | 0 classes; 105 top-level functions; the hub |
| `browser_control/cli/main.py` | 1320 | argv adapter + 39 `cmd_*` + gate |
| `browser_control/lib/cdp.py` | 887 | endpoint + targets + websocket session |
| `browser_control/lib/profile.py` | 521 | 11 `# noqa: SLF001` into browser privates |
| `browser_control/lib/capabilities.py` | 301 | declaration + gate in 2 mutable globals |
| `browser_control/lib/audit.py` | 224 | ActionLog + file policy + scratch fallback |
| `browser_control/lib/plugins.py` | 195 | Registry + discovery + validation |
| `browser_control/lib/errors.py` | 21 | exception + fail; no code registry |
| `plugins/x_reader.py` | 216 | reference plugin; re-implements argv helpers |
| `tests/test_unit.py` | 2956 | 63 hermetic checks; patches module globals by name |
| `tests/live_test.py` | 2327 | live battery; imports `browser_control.lib.{browser,cdp,audit}` by path |

Ignore `build/`, `dist/`, `browser_control.egg-info/`, `__pycache__` — artifacts.

## Target layout (Layout A, D-2 default)

```text
browser_control/
  __init__.py              # __version__ only (UNCHANGED)
  __main__.py              # python -m shim (UNCHANGED)
  plugin_api.py            # RF-34: named plugin seam, __all__ (D-4)
  lib/
    __init__.py            # docstring only (UNCHANGED)
    errors.py              # + CODES registry (RF-06)
    text.py                # one untrusted-text flattener/sanitiser (RF-01)
    coerce.py              # as_int/as_float/as_ints/as_list (RF-02)
    proc.py                # /proc reads incl. listener_of (RF-03, D-3)
    poll.py                # poll()/deadline() for every deadline loop (RF-04)
    paths.py               # expand/norm/root/profile_dir/is_managed/pid_file/lock_path (RF-05)
    images.py              # PNG header/pixels/path validation/atomic write (RF-07)
    scope.py               # Scope: profile + frame for ONE invocation (RF-08, D-1)
    capabilities.py        # Surface declaration only (RF-09)
    policy.py              # Policy gate as a value object (RF-09)
    attachments.py         # AttachmentStore, sole writer of attached.json (RF-10)
    locks.py               # LockState + profile lock + root->profile choreography (RF-11)
    instance.py            # Instance: profile identity/liveness (RF-12)
    audit.py               # ActionLog + injectable sink (RF-14)
    logfile.py             # FileSink + scratch_dir (RF-14)
    cdp/                   # endpoint.py targets.py rpc.py session.py + facade (RF-16..18)
    browser/               # machine.py owners.py selector.py lifecycle.py readback.py
                           # tabs.py nav.py + facade (RF-19..22)
    dom/                   # tab.py scripts.py pagedata.py frames.py queries.py actions.py
                           # scroll.py text_input.py keys.py media.py dialog.py
                           # screenshot.py result.py + facade (RF-23..30)
    plugins/               # discovery.py spec.py + facade Registry/PluginSet (RF-33)
    profile/               # info.py seed.py + facade info/seed/reset; seedtree.py stays
                           # lib/seedtree.py (RF-36)
  cli/
    __init__.py            # docstring only (UNCHANGED)
    main.py                # entry point + compatibility facade (RF-35)
    runner.py              # Invocation: one call's state (RF-35)
    argv.py                # VerbArgs + globals(); public to plugins (RF-31)
    registry.py            # Verb records + VerbTable + action_of (RF-32)
    emit.py                # Reporter: stdout/stderr/exit codes (RF-34)
    usage.py               # USAGE as data (RF-32)
    verbs/                 # browser.py tab.py profile.py (RF-31)
```

Layering (must stay acyclic): `errors -> text/coerce/proc/poll/paths/images -> audit/locks/
attachments/scope/capabilities/policy -> cdp -> browser -> dom -> instance/seedtree/profile ->
plugins -> cli`. `lib` never imports `cli`.

---

## Phase 0 — decisions (no code)

Answer D-1..D-6 (front matter). The defaults are chosen so execution can start without
blocking; if the architect overrides one, record the override here before the affected task.

---

## Phase 1 — foundation leaves (no behaviour change)

### RF-01 — `lib/text.py`: one untrusted-text flattener
- status: done (commit 28c06d4)
- priority: P1
- category: dedup
- findings: lane-6 F-6.2
- files: `lib/cdp.py:451 _foreign`, `lib/browser.py:234 _flat`, `lib/dom.py:995 _safe`, `lib/audit.py:90 _oneline`
- target: `browser_control/lib/text.py :: foreign(text, cap=200) -> str` (+ `flat(text, cap=60)` alias if needed)
- action: split whitespace, slice to cap, then drop control chars (`< " "`, DEL/C1) — the
  `cdp._foreign` order; migrate the four callers; delete the private copies. `dom._safe`
  currently filters BEFORE slicing (different result); adopt the one order and re-run tests.
- migration: `cdp.py:427-428,478,814-815,870`; `browser.py:258,262`; `dom.py:1011-1012,1568,1650,1742,1826,1833`; `audit.py:141,151,153`
- deps: none
- risk: low (visible refusal-text change only where `dom._safe` order differed; re-run suite)
- acceptance:
  - `grep -rn "def _flat\|def _foreign\|def _safe\|def _oneline" browser_control/` returns nothing
  - `grep -rln $'\x7f' browser_control/` returns exactly 1 file (`lib/text.py`)
  - `python3 tests/test_unit.py` green
- evidence: `docs/refactor/evidence/lane-6-crosscutting.md#F-6.2`

### RF-02 — `lib/coerce.py`: one numeric/sequence coercer
- status: done (commit f724849)
- priority: P1
- category: dedup
- findings: lane-6 F-6.5 (int half), lane-3 F-3.1 (scattered helpers)
- files: `browser.py:445 _to_int`, `dom.py:610 _int`, `dom.py:617 _num`, `dom.py:2251 _ints`, `dom.py:2267 _list`, `profile.py:190 _int`
- target: `browser_control/lib/coerce.py :: as_int/as_float/as_ints/as_list`
- action: keep `cli/main.py:219 _int` (argv validation; its message names a flag). The three
  library coercers collapse to `coerce.as_int`; keep per-site defaults.
- migration: all callers of the deleted private names (dom ~40 sites, browser 14 sites, profile)
- deps: none
- risk: low
- acceptance:
  - `grep -rn "def _to_int\|def _int(value: object" browser_control/` returns nothing
  - `python3 tests/test_unit.py` green (covers `_int`/`_ints` at test_unit.py:936-943)
- evidence: `docs/refactor/evidence/lane-6-crosscutting.md#F-6.5`

### RF-03 — `lib/proc.py`: one `/proc` reader (dedupes `cdp` vs `browser`)
- status: done (commit df385db)
- priority: P1
- category: lib
- findings: lane-6 F-6.3, lane-2 F-2.3
- files: `cdp.py:148 _proc_text`, `cdp.py:163 _exe_basename`, `cdp.py:171 _listening_inodes`, `cdp.py:195 listener_of`; `browser.py:272 _pid_alive`, `:283 _proc_text`, `:305 _cmdline_value`, `:323 _main_processes`, `:353 exe basename inline`, `:510 realpath inline`, `:268 _pid_file`, `:375 _find_pid`, `:389 _pid_on_profile`, `:422 _record_pid`, `:428 _spawn`
- target: `browser_control/lib/proc.py` — `proc_text`, `exe_name`, `exe_path`, `pid_alive`, `cmdline_value`, `main_processes`, `listener_of`, `pid_file`, `find_pid`, `pid_on_profile`, `record_pid`, `spawn`
- action: D-3: `lib/proc.py` is the owner, `cdp/endpoint.py` and `browser/` import it. Preserve
  the NUL-preserving read and identical refusal codes. `browser.py:853` re-implements the
  `--user-data-dir` marker test inline — call `pid_on_marker`.
- migration: `cdp.py:148-167` deleted; `browser.py:268-443` deleted; `browser/` imports `proc`
- deps: none (do before RF-16 so `cdp/` is split once)
- risk: medium (`/proc` parsing is the identity guard; fake-endpoint tests cover it)
- acceptance:
  - `grep -rln '"/proc' browser_control/lib/` returns exactly `lib/proc.py`
  - `grep -rn "def _proc_text" browser_control/` returns nothing
  - `python3 tests/test_unit.py` green incl. endpoint-ownership checks
- evidence: `docs/refactor/evidence/lane-6-crosscutting.md#F-6.3`, `lane-2-browser.md#F-2.3`

### RF-04 — `lib/poll.py`: one deadline loop
- status: done (commit 7fff291)
- priority: P1
- category: dedup
- findings: lane-6 F-6.4, lane-3 F-3.9
- files: 26 loops — `browser.py:1059,1077,1089,1105,1126,1158,1285,1357,1576,1583,2169,2201,2229,2255,2306,2412`; `dom.py:1760,1876,1994,2307,2396,2716`; `cdp.py:419,528,556,615,779`
- target: `browser_control/lib/poll.py :: poll(probe, *, timeout, interval, accept=bool, on_error=None) -> (attempts, last)` + `deadline(timeout)`
- action: convert the 20 non-transport loops one verb at a time; keep each verb's timeout and
  refusal text at the call site; do NOT touch `cdp.Session`'s per-sample budget arithmetic.
  Name the surviving intervals (four literals today: 0.15/0.2/0.25/0.3).
- migration: per-site; the loops that swallow refusals (`browser.py:1088 _wait_rows`,
  `:1102 _wait_tabs`, `:1150 _wait_ids_gone`) need `on_error` to keep their judgement.
- deps: none
- risk: medium (must not shift any single verb's timeout)
- acceptance:
  - `grep -rc "deadline = time.time() +" browser_control/` total <= 3
  - `python3 tests/test_unit.py` green + live battery nav/wait/scroll checks green
- evidence: `docs/refactor/evidence/lane-6-crosscutting.md#F-6.4`, `lane-3-dom.md#F-3.9`

### RF-05 — `lib/paths.py`: one path/root/profile resolver
- status: done (commit 4eb04cc)
- priority: P1
- category: lib
- findings: lane-6 F-6.5 (paths half), lane-5 F-5.1 (private reaches)
- files: `browser.py:86,134,174-176,457` (`root`, `_norm`, `profile_dir`, `is_managed`, `instance_dir`), `audit.py:127`, `plugins.py:88,91`, `profile.py:232,290,376`
- target: `browser_control/lib/paths.py` — `expand`, `norm`, `root`, `profile_dir`, `is_managed`, `pid_file`, `lock_path`
- action: public names (D-1/D-6); `browser.py` re-exports for the facade. Delete the 10 open-coded
  `abspath(expanduser(...))` sites and `profile.py`'s private imports of `_norm`/`_is_managed`.
- migration: the 10 sites above; `profile.py:41-48` import block
- deps: none
- risk: low
- acceptance: `grep -rc "os.path.abspath(os.path.expanduser" browser_control/` sums <= 2;
  `grep -rn "from browser_control.lib.browser import" browser_control/ | grep "_"` returns nothing
- evidence: `docs/refactor/evidence/lane-6-crosscutting.md#F-6.5`

### RF-06 — `errors.CODES`: register the refusal vocabulary (P2)
- status: done (commit 4101928)
- priority: P2
- category: naming
- findings: lane-1 F-1.5
- files: `lib/errors.py:11,20`; ~160 raise sites
- target: `CODES: frozenset[str]` + named constants (`ERR_CDP_ERROR = "cdp-error"`, ...) in `lib/errors.py`
- action: enumerate every code currently passed to `fail()`/`ControlError(...)`; do NOT validate
  at raise time (a typo becoming a runtime failure is a new surface). Replace literals in
  `lib/` and `cli/` with constants.
- deps: none
- risk: low
- acceptance: a new hermetic check asserts every `fail(`/`ControlError(` first arg is in `CODES`;
  `python3 tests/test_unit.py` green
- evidence: `docs/refactor/evidence/lane-1-transport.md#F-1.5`

### RF-07 — `lib/images.py`: the screenshot file layer, CDP-free
- status: done (commit a47722a)
- priority: P1
- category: lib
- findings: lane-3 F-3.7
- files: `dom.py:2019 _png_size`, `:2032 _pixels`, `:2053 _shot_target`, `:2077 _write_shot`
- target: `browser_control/lib/images.py` — `png_size`, `expected_pixels`, `output_path`, `write_atomic`
- action: pure move; imports only `math`, `os`, `contextlib`, `lib.errors`; must not import
  `cdp`, `browser`, `dom`. Keep refusal codes (`bad-args`, `file-exists`, `write-failed`).
- migration: `dom.screenshot` calls `images.*`; test import paths in `test_unit.py:348-366`
- deps: none
- risk: low
- acceptance: `python3 -c "import browser_control.lib.images"` succeeds with `cdp` absent from
  `sys.modules`; `grep -rn "O_NOFOLLOW" browser_control/lib/dom/` returns nothing
- evidence: `docs/refactor/evidence/lane-3-dom.md#F-3.7`

### RF-08 — `lib/scope.py::Scope`: own the per-invocation profile + frame (D-1)
- status: done (commit 4e8cddf)
- priority: P1
- category: state
- findings: lane-6 F-6.1, lane-2 F-2.2 (`SCOPE`), lane-3 F-3.6 (`FRAME`), lane-4 open-q 2
- files: `browser.py:121 SCOPE` (+10 reads), `dom.py:86 FRAME` (+ writes in `_document_ws`), `cli/main.py:1230-1231,1271-1277`
- target: `browser_control/lib/scope.py :: class Scope` (`profile`, `frame_wanted`, `frame_resolved`)
- action: one instance built by `cli.main`; thread it (or keep `browser.scope()`/`dom.frame()`
  as process-wide shims delegating to the instance, D-1 default). `dom.FRAME` must stop being
  written inside `_document_ws`; frame resolution becomes a method on the future `Tab`/`Frames`
  (RF-27).
- migration: `browser.py:147,580,641,648-654,675,678,1034,2077`; `dom.py:99,102,660,664,866,1525-1528`;
  `profile.py:230,288,325`; `cli/main.py:1230-1231,1271-1277`; test `test_unit.py:2043-2045`
- deps: none (do before RF-19/RF-23 so moved code carries the object)
- risk: high (every verb reads the scope; frame checks are asserted)
- acceptance:
  - `grep -rn "^SCOPE\|^FRAME" browser_control/` returns nothing
  - frame checks `t_frames_and_points`, `t_frames_bind_to_their_tab` pass unchanged
  - `python3 tests/test_unit.py` green
- evidence: `docs/refactor/evidence/lane-6-crosscutting.md#F-6.1`, `lane-3-dom.md#F-3.6`

### RF-09 — capabilities: `Surface` (declaration) + `Policy` (gate) value objects
- status: done (commit f4f88fe)
- priority: P1
- category: state/class
- findings: lane-1 F-1.4, lane-6 F-6.1, lane-5 F-5.5 (gate half)
- files: `capabilities.py:138 PLUGIN_ACTIONS`, `:167 POLICY`, `:120 by_class`, `:211 policy`, `:280 allowed`, `:100 unclassified`
- target: `capabilities.py :: class Surface` (CLASSES/ACTIONS/plugin classes; `unclassified`, `by_class`);
  `lib/policy.py :: class Policy` (`from_sources(allow, deny, env)`, `allowed(action)`, `describe()`)
- action: no module-level mutable dicts left; `cli.main` builds one `Policy` per invocation and
  passes it to the gate. Keep "no policy set -> no gate" and fail-closed behaviour.
- migration: `cli/main.py:378-386,1080,1178,1234,1267`; `plugins.py:175,180`; tests
  `test_unit.py:1649,1697,1784,1844`
- deps: none
- risk: medium (security-relevant gate)
- acceptance:
  - `grep -rn "^PLUGIN_ACTIONS\|^POLICY" browser_control/` returns nothing
  - `capabilities.unclassified(HANDLERS, {...}) == []` still holds (test_unit.py:1567)
  - `python3 tests/test_unit.py` green incl. `t_policy_gate`, `t_gate_and_argv_hardening`
- evidence: `docs/refactor/evidence/lane-1-transport.md#F-1.4`

### RF-10 — publish the seams, delete the dead, fix the naming
- status: done (commit 5196cf2)
- priority: P1
- category: dead-code/naming/tests
- findings: lane-2 F-2.10 + F-2.9 (publish half), lane-6 F-6.12 + F-6.13 + F-6.11 (publish half),
  lane-3 F-3.11, lane-4 F-4.7 (dead half), lane-5 F-5.1 (boundary legibility)
- files: `browser.py:359 _profile_marker`, `:1057 _wait_port`, `:169 live_profiles`, `:951 _brief`,
  `:958 _row`, `:1716 _foreign_row`, `:2089 _one_tab`, `:2434 reload`; `dom.py:59 import ... as tabs`;
  `cli/main.py:243-252 _selector --profile branch`, `:838 _text_arg` 3rd param; `audit.py:222 reset_redaction`
- action:
  1. delete `_profile_marker` (no callers), `reset_redaction` (no callers), `_selector`'s
     `--profile` branch (unreachable through `main`), `_text_arg`'s unused `tab` parameter.
  2. move `_wait_port` + `live_profiles` to the tests if their only caller stays a test.
  3. promote public names: `brief`, `one_tab`, `norm`, `is_managed`, `lock`, `lock_path`,
     `tabs_of`, `rows`, `verify_profile_endpoint`, `resolve_across`; keep `_name` aliases for
     one release. `import browser as tabs` becomes `import browser as browser_lib`.
  4. `reload` -> library-side `reload_page` (CLI verb name frozen); `# noqa: A001` disappears.
- migration: `profile.py` 11 `SLF001` sites; `dom.py` 23 `SLF001` sites; `cli/main.py:705`
- deps: none (must land before RF-19/RF-23 or the moves break tests silently)
- risk: medium (alias lifetime; silent patch no-ops are the failure mode)
- acceptance:
  - `grep -rn "_profile_marker\|reset_redaction" browser_control/ tests/` returns nothing
  - `grep -rn "import browser as tabs" browser_control/` returns nothing
  - `python3 tests/test_unit.py` green with no assertion weakened
- evidence: `docs/refactor/evidence/lane-2-browser.md#F-2.10`, `lane-6-crosscutting.md#F-6.12`,
  `lane-6-crosscutting.md#F-6.13`, `lane-3-dom.md#F-3.11`

---

## Phase 2 — state owners (classes that own what globals/private reaches own today)

### RF-11 — `lib/locks.py`: typed lock + one root→profile choreography
- status: done (commit 1b88aba)
- priority: P1
- category: class/dedup
- findings: lane-2 F-2.5, lane-5 F-5.2
- files: `browser.py:1224 LOCK_FILE`, `:1228 _lock_path`, `:1305 _lock` (yields bare dict), 6 warning-merge
  sites (`browser.py:763,778,805,1551,1595`; `profile.py:449,517`), `profile.py:396` fake stub,
  `profile.py:398-402,482-485` hand-rolled nesting
- target: `lib/locks.py :: @dataclass LockState(held, warning)` + `profile_lock(path, verb, wait=...)`
  + `instance_locks(root, profile, verb, *, skip_profile_lock=False)`
- action: `fcntl` import at module top; `profile.py`'s `nullcontext({"held": True, ...})` becomes
  `LockState(held=True, warning="")`; warning merge becomes one method; the ordering invariant
  (root lock -> re-check liveness -> profile lock) lives in `instance_locks`, not a comment.
- migration: the 8 sites above; `test_unit.py:1624` literal becomes the dataclass
- deps: none
- risk: medium (locking is the most delicate invariant; `--dry` no-write asserted)
- acceptance:
  - `grep -n '{"held"' browser_control/lib/profile.py` returns nothing
  - `ERR[profile-busy]` text byte-identical; `t_seed_dry_writes_nothing` green
- evidence: `docs/refactor/evidence/lane-5-profile-plugins.md#F-5.2`, `lane-2-browser.md#F-2.5`

### RF-12 — `lib/attachments.py`: one owner for `attached.json`
- status: done (commit 5b1d51e)
- priority: P1
- category: class/dedup
- findings: lane-2 F-2.4, lane-5 F-5.3
- files: `browser.py:530 ATTACH_FILE`, `:533 _attached`, `:550 _write_attached`, `:566 is_attached`,
  `:693 attachments`, `:708 attach` (751-755), `:768 detach` (774-803); `profile.py:501-505`
- target: `lib/attachments.py :: class AttachmentStore` — `records()`, `get`, `put`, `drop`, `drop_all`, `rows()`,
  one private `_locked_update(fn)` taking the root lock once per RMW
- action: preserve the on-disk JSON shape, temp+`os.replace` atomicity, and `attach --list` reply keys.
  `attach` must resolve identity and write the record inside one critical section; if closing the
  race is a behaviour change, keep current order and file it (see Open question Q-2).
- migration: `browser.attach/detach/attachments`; `profile.reset`; `profile.py:503-505` `SLF001` sites disappear
- deps: RF-11 (root lock), RF-05 (`norm`)
- risk: medium (contract-visible through `attach --list` and every `list` row)
- acceptance:
  - `grep -rn "_write_attached" browser_control/` matches only `lib/attachments.py`
  - `attach --list` JSON keys unchanged; `python3 tests/test_unit.py` green
- evidence: `docs/refactor/evidence/lane-2-browser.md#F-2.4`, `lane-5-profile-plugins.md#F-5.3`

### RF-13 — `Selector`: one "which browser did the caller name" value object
- status: done (commit 4ece341)
- priority: P1
- category: class/dedup
- findings: lane-2 F-2.6, lane-6 F-6.7
- files: `browser.py:716-731 attach`, `:781-798 detach`, `:1473-1511 _named_browser`, `:1534-1543 stop`;
  `cli/main.py:226-261 _selector`
- target: `browser/selector.py :: @dataclass Selector(port, pid, profile)` + `named()` +
  `require_one(verb)` + `find(rows)`; CLI `_selector` returns it plus its own `list`/`all` booleans
- action: keep both arities (`attach`/`detach` require exactly one; `stop` allows zero) as
  parameters of `require_one`. Refusal codes/texts byte-identical.
- migration: the four sites above; `cli/main.py:278,288,312`
- deps: RF-05, RF-19 (find returns a `BrowserRow`)
- risk: medium (consent gate; texts asserted by substring)
- acceptance:
  - `grep -c '"--pid", pid' browser_control/lib/browser/` == 1
  - `attach`/`detach`/`close --pid|--port|--profile` refusal texts byte-identical
- evidence: `docs/refactor/evidence/lane-2-browser.md#F-2.6`, `lane-6-crosscutting.md#F-6.7`

### RF-14 — `lib/instance.py::Instance`: the profile instance as a value
- status: done (commit 29d809e)
- priority: P1
- category: class
- findings: lane-5 F-5.1
- files: `profile.py:222 _target`, `:252 _live_pid`, `:269 _refuse_live`, `:279 info`, `:288 scope read`,
  `:306-321 row hand-copy`; `browser.py:139 instance_dir`, `:627 _writable_profile`, `:669 managed_profile`
- target: `lib/instance.py :: class Instance` — `resolve(profile, browser)`, `.path`, `.managed`,
  `.live_pid()`, `.refuse_live(verb)`, `.attached`, `.browsers_row()`
- action: one resolver applying scope→named→default precedence and the root boundary; keep the
  two distinct refusal codes (`bad-args` from browser.py:150, `not-managed` from profile.py:236)
  parameterised, not unified.
- migration: `profile.py:222-276,288-291,379,390,470-505`; `profile.py` `SLF001` count reaches 0
- deps: RF-05, RF-11, RF-12
- risk: medium (symlink refusal + normalised comparison are review-fixed behaviour)
- acceptance:
  - `grep -c "noqa: SLF001" browser_control/lib/profile.py` == 0
  - `profile_lib.info/seed/reset` signatures unchanged
  - `t_profile_symlink_target_is_refused`, `t_seed_lands_where_chrome_reads`,
    `t_reset_guard_counts_skipped_content` green
- evidence: `docs/refactor/evidence/lane-5-profile-plugins.md#F-5.1`

### RF-15 — audit: `ActionLog` with a sink; `lib/logfile.py` owns file policy
- status: done (commit 5feb828)
- priority: P1
- category: lib
- findings: lane-4 F-4.8, lane-4 F-4.7 (begin reset), lane-6 F-6.1 (`_SCRATCH`)
- files: `audit.py:96-165 ActionLog`, `:167-208 _append`, `:41-62 scratch_dir`, `:38 _SCRATCH`,
  `:102 begin(action)`, `:219 LOG`; `cli/main.py:1235,1319`
- target: `audit.ActionLog(sink=None)` (record + redaction only); `lib/logfile.py :: class FileSink`
  (`write`, `scratch_dir`); `_SCRATCH` becomes a `FileSink` field; re-export `scratch_dir`
- action: preserve 0600/0700, fchmod narrowing, short-write != success, scratch fallback.
  Fix the reset gap: `begin()` loses its unused parameter; ensure redaction state resets on
  every path, including the two early refusals (`cli/main.py:1190-1213`).
- migration: tests asserting `audit.scratch_dir()` mode (`test_unit.py:1497,2601-2611`, `live_test.py:2295`)
- deps: none
- risk: medium (security-relevant modes)
- acceptance: `grep -n "os.open\|os.fchmod\|mkdtemp" browser_control/lib/audit.py` returns nothing;
  `t_audit_redaction*`, `t_action_log_lands_on_disk` green; new check: refusal after a
  secret-bearing call does not carry stale `redacted`
- evidence: `docs/refactor/evidence/lane-4-cli.md#F-4.8`, `lane-4-cli.md#F-4.7`

### RF-16 — `lib/cdp/` package behind a facade (pure move)
- status: done (commit 7faca8f)
- priority: P1
- category: package
- findings: lane-1 F-1.3
- files: `cdp.py` (887): endpoint 54-267, targets 269-406, session 408-891
- target: `lib/cdp/{__init__,endpoint,targets,rpc,session}.py`; `__init__` re-exports the current
  `cdp.*` surface (D-2), including `websockets` and `PORT_FILE` (read by tests/CLI)
- action: move whole; `proc` primitives come from RF-03; no file > ~350 lines. Update
  `pyproject.toml` packages list.
- migration: 55 `cdp.X` references stay untouched thanks to the facade
- deps: RF-03
- risk: medium (facade must cover `cdp.websockets`, `cdp.PORT_FILE`)
- acceptance:
  - `python -c "from browser_control.lib import cdp; cdp.Session; cdp.get_json; cdp.page_rows"` succeeds
  - `pip install .` from outside the repo can import the package
  - `python3 tests/test_unit.py` green
- evidence: `docs/refactor/evidence/lane-1-transport.md#F-1.3`

### RF-17 — one RPC send/await/demux path
- status: done (commit 7bb0930)
- priority: P1
- category: dedup
- findings: lane-1 F-1.2
- files: `cdp.py:408 _call`, `:512 _page_enable`, `:551 _sample`, `:776 Session._call`
- target: `cdp/rpc.py :: _await_reply(ws, rid, budget) -> dict` owning send, recv, id demux,
  non-JSON guard and error mapping; the four loops call it
- action: unify while preserving each message; `Session._call` keeps the parked/dialog branch on top
- deps: RF-16
- risk: medium (error text and `fail` vs `raise` differ per copy)
- acceptance: one `json.dumps({"id":` send site and one `msg.get("id")` demux site in `cdp/`;
  `Page.enable` refusal still `blocked` + `BLOCKED_HINT`; `python3 tests/test_unit.py` green
- evidence: `docs/refactor/evidence/lane-1-transport.md#F-1.2`

### RF-18 — `Session` is the only connection owner
- status: done (commit 20b3190)
- priority: P1
- category: class
- findings: lane-1 F-1.1
- files: `cdp.py:441 call`, `:635 evaluate`, `:651 evaluate_until`, `:667 Session`, three private coroutines
- target: `call`/`evaluate`/`evaluate_until` are thin wrappers over one `Session`; exactly one
  `websockets.connect` site (`Session._connect`)
- action: reconcile the `eval-timeout` mapping (`_evaluate` at :590 vs `Session._call` at :797)
  deliberately — same failure must yield the same code. Signatures/return types unchanged.
- deps: RF-17
- risk: high (every CDP path; frozen error codes)
- acceptance: `grep -rn "websockets.connect" browser_control/lib/cdp/` == 1 site;
  `python3 tests/test_unit.py` green; `selftest` + live `tab js`/`tab click` JSON identical
- evidence: `docs/refactor/evidence/lane-1-transport.md#F-1.1`

---

## Phase 3 — browser tier package

### RF-19 — `lib/browser/` package behind a facade (pure move)
- status: done (commit d729d27)
- priority: P1
- category: package
- findings: lane-2 F-2.8, lane-6 F-6.8
- files: `browser.py` (2454, 12 clusters) — cluster ranges in lane-2 summary
- target: `lib/browser/{__init__,machine,owners,selector,lifecycle,readback,tabs,nav}.py`; `__init__`
  re-exports every current public name AND the privates the suites touch (F-6.11 list), so no
  call site changes in the move. Procedural leaves already extracted: `lib/paths.py`, `lib/proc.py`,
  `lib/locks.py`, `lib/attachments.py`.
- action: pure move, one cluster per commit if desired; update `pyproject.toml`; no file > ~400 lines.
  Do NOT change any behaviour in this task.
- migration: 5 import sites (`cli/main.py:28-45`, `profile.py:41-47`, `dom.py:648+`, `plugins/x_reader.py:174`),
  ~30 test patch sites (they keep working via facade; patch-target migration is RF-37/per-move)
- deps: RF-05, RF-08, RF-10, RF-11, RF-12, RF-14
- risk: high (facade list is load-bearing; silent patch no-ops)
- acceptance:
  - the long `python3 -c "from browser_control.lib.browser import ..."` list from lane-2 F-2.8 succeeds
  - `python3 tests/test_unit.py` green **with no test edits**
  - `pip install .` outside the repo succeeds
- evidence: `docs/refactor/evidence/lane-2-browser.md#F-2.8`, `lane-6-crosscutting.md#F-6.8`

### RF-20 — `BrowserRow` + `Endpoint` dataclasses; kill `["cdp"]` indexing
- status: done (commit 7740e55)
- priority: P1
- category: class
- findings: lane-2 F-2.1
- files: `browser.py:476 browsers`, `:495-517 row literal`, `:951 _brief`, `:958 _row`, `:965 _endpoint_details`, `:1716 _foreign_row`
- target: `browser/machine.py :: @dataclass(frozen=True) Endpoint` and `BrowserRow` with
  `as_reply()/as_brief()/as_endpoint_reply()` built from the existing projection bodies;
  `may_write`/`may_read` land here too (RF-21)
- action: the four projections keep producing byte-identical JSON, key presence included
  (absent `listener` when unreachable is contract — live_test.py:1340-1350).
- deps: RF-19
- risk: medium (30 `["cdp"]` sites; JSON equality pinned by live tests)
- acceptance:
  - `grep -c '\["cdp"\]' browser_control/lib/browser/` == 0
  - `list`, `info`, `tab list` on a fixed fake census produce identical JSON
- evidence: `docs/refactor/evidence/lane-2-browser.md#F-2.1`

### RF-21 — one named write/read authorisation predicate
- status: done (commit 7740e55)
- priority: P1
- category: dedup
- findings: lane-2 F-2.7
- files: `browser.py:597 _writable`, `:615 _readable`, `:892 _drivable`, predicate at `:612,911,1023,1661,1685,1743,1765,1836`
- target: `machine.writable(rows)` / `readable(rows)` / `drivable(rows, strict=...)` and
  `BrowserRow.may_write`/`.may_read`; one named `strict` parameter
- action: provably-equivalent extraction; the docstring at 601-611 records why the fallback
  must stay narrow — keep it.
- deps: RF-19, RF-20
- risk: low
- acceptance: `grep -rc '"managed"\] or ' browser_control/lib/browser/` == 0;
  `t_one_tab_addressing` passes unmodified
- evidence: `docs/refactor/evidence/lane-2-browser.md#F-2.7`

### RF-22 — `EndpointGuard`: the owner memo becomes an object with `forget()`
- status: done (commit 7740e55)
- priority: P1
- category: state/class
- findings: lane-2 F-2.2 (`_OWNER_CACHE`), lane-6 F-6.1
- files: `browser.py:812 _OWNER_CACHE`, `:815 endpoint_owner`, `:1081/:1360` hand-written pops,
  `:1171 _verify_profile_endpoint`, `:1335 _await_owner`, `:889 _drive_refusal`
- target: `browser/owners.py :: class EndpointGuard` — `verdict(profile, port, refresh=False)`,
  `forget(profile, port)`; the two manual pops become `guard.forget(...)`
- action: one memo field, one eviction API; keep the verification policy and codes identical.
- deps: RF-19, RF-03
- risk: medium (a wrong refresh default is a safety bug, not perf)
- acceptance: `grep -rc "_OWNER_CACHE" browser_control/lib/browser/` == 1 (inside owners.py);
  `t_endpoint_ownership`, `t_wait_own_port_requires_a_verified_owner` green unmodified
- evidence: `docs/refactor/evidence/lane-2-browser.md#F-2.2`

---

## Phase 4 — DOM tier package

### RF-23 — `lib/dom/` package behind a facade (pure move)
- status: todo
- priority: P1
- category: package
- findings: lane-3 F-3.1 + F-3.12
- files: `dom.py` (2778) — subsystem ranges in lane-3 summary
- target: `lib/dom/{__init__,tab,scripts,pagedata,frames,queries,actions,scroll,text_input,keys,media,dialog,screenshot,result}.py`;
  `__init__` re-exports the frozen surface (18 verbs, `frame`, `frame_resolved`, `mode_of`,
  `FRAME_VERBS`, caps, `WAIT_EXPRS`, `FIND_EXPR`, `KEYS`, `PRELUDE`) plus the privates tests touch
- action: pure move; no file > ~400 lines; procedural extractions (RF-24..RF-30) land inside
  this package afterwards. Keep `lib/dom.py` importable? With `lib/dom/` the path is identical.
- migration: `cli/main.py` ~30 `dom.X` reads unchanged; 12 test patch sites keep working via
  `__init__` re-export; the four `dom._resolve/_session/_matches_in/_under_point` patches
  migrate with RF-27 (they patch names the verbs look up — see acceptance).
- deps: RF-10
- risk: medium (60 test imports; re-export list load-bearing)
- acceptance:
  - `python3 tests/test_unit.py` green with no test edits
  - no file under `lib/dom/` > ~400 lines; `__init__.py` is imports + `__all__` only
  - `grep -rn "lib import dom\|lib\.dom" browser_control/lib/` (outside dom/) returns nothing
- evidence: `docs/refactor/evidence/lane-3-dom.md#F-3.1`, `lane-3-dom.md#F-3.12`

### RF-24 — `dom/scripts.py`: one home for page-side JS + `fill()`
- status: done (commit 6e142f9)
- priority: P1
- category: lib/dedup
- findings: lane-3 F-3.4, lane-3 F-3.8 (probes), lane-6 F-6.9 (inline literals)
- files: `dom.py:181 PRELUDE`, 18 constants (256..1206), inline literals `:1431,:1494,:1953,:1997,:604`
- target: `dom/scripts.py` — `PRELUDE`, all named expressions (promote the four inline literals),
  `WAIT_EXPRS`, and ONE `fill(expression, **values)` that refuses unknown/leftover placeholders
- action: `__MODE__` collision between matcher (`selector|text`) and media (`play|pause`) is
  resolved by distinct placeholder names. Test `dom.FIND_EXPR.count("__SELECTOR__") == 1` stays.
- deps: RF-23
- risk: medium (a missed placeholder currently ships as a runtime `js-error`)
- acceptance: `grep -rn '\.replace("__' browser_control/lib/dom/` matches only inside `scripts.py`;
  a hermetic check asserts `fill()` refuses a leftover; no `document.`/`elementFromPoint`
  literal outside `scripts.py`
- evidence: `docs/refactor/evidence/lane-3-dom.md#F-3.4`, `lane-3-dom.md#F-3.8`

### RF-25 — `Verdict` + `PageState`: one reply/verdict convention
- status: done (commit 789331f)
- priority: P1
- category: class
- findings: lane-3 F-3.5, lane-3 F-3.8 (`changed`)
- files: `dom.py:1120 _reply`, `:2548 _text_reply`, `:2684 _media_reply`, `:1080 _playback_verdict`,
  `:1101 _text_verdict`, `:1881` inline select verdict, `:1465` vs `:1597` changed
- target: `dom/result.py :: class PageState` (`changed(other)`) + `class Verdict(verified, why)`;
  `Tab.reply()` (RF-27) builds `{ok,tab,browser,url,title,visibility,...}`
- action: thin wrappers, not re-encoders — JSON keys (`verified`, `changed`, `note`,
  `length_before/after`) frozen.
- deps: RF-23
- risk: medium
- acceptance: `grep -rc '"browser": tabs._brief(row)' browser_control/lib/dom/` == 0;
  one `changed` comparison in the package; `python3 tests/test_unit.py` green
- evidence: `docs/refactor/evidence/lane-3-dom.md#F-3.5`

### RF-26 — one page-session factory (+ browser connection reuse)
- status: done (commit 35489ff)
- priority: P1
- category: class/dedup
- findings: lane-6 F-6.10
- files: `dom.py:679 _session`, `:651 _document_ws`, `:715,763,869,1184,1984`; `browser.py:2147 _eval`, `:2276 nav`, `:2400 activate`, `:1204/:1989` per-item `browser_call`
- target: `dom/` (or `browser/readback.py`) `page_session(...)` + `evaluate(...)`; a
  `BrowserConnection` owner so bulk loops reuse one websocket
- action: frame resolution included in the factory; keep `dialog`'s `page_domain=False` exact.
- deps: RF-18, RF-23
- risk: medium
- acceptance: `grep -rn "cdp.target_ws(cdp.port_of" browser_control/` returns nothing;
  `--frame 1` / `frame-unattributable` / `dialog accept` checks green
- evidence: `docs/refactor/evidence/lane-6-crosscutting.md#F-6.10`

### RF-27 — `Tab`: one per-verb prelude instead of 21 copy-pastes
- status: done (commit fe8ab46)
- priority: P1
- category: class
- findings: lane-3 F-3.2, lane-6 F-6.9
- files: 21 `_resolve`+`_session` preludes (dom.py:1138..2750); guard block triplicated at
  `:1558-1584, :1640-1661, :1732-1754`; click triple at `:1438-1444, :1584-1590, :1754-1760`
- target: `dom/tab.py :: class Tab` constructed inside each verb (signatures and recorded call
  tuples unchanged): `Tab.open`, `.facts()`, `.target(needle,css,index)`, `.node(expr)`,
  `.aim(element)`, `.reply(**fields)`; `dom/actions.py :: click_point(session,x,y)`
- action: migrate verbs one at a time (find/text, then element verbs, then scroll, then
  text_input/media/dialog/screenshot); after each, update the corresponding monkeypatch target
  (`test_unit.py:2221-2240` patch `dom._resolve/_session/_matches_in/_under_point`) in the SAME commit.
- deps: RF-24, RF-25, RF-26
- risk: high (guard codes/messages asserted by the live battery)
- acceptance:
  - `grep -c "with _session(row, tab_row)" browser_control/lib/dom/` == 0
  - `grep -rn "scroll it into view first" browser_control/lib/dom/` matches once
  - live battery click/hover/check/select/occluded/off-screen checks green
- evidence: `docs/refactor/evidence/lane-3-dom.md#F-3.2`, `lane-6-crosscutting.md#F-6.9`

### RF-28 — hover/changed dedup through `Tab` + `PageState`
- status: done (commit fe8ab46)
- priority: P2
- category: dedup
- findings: lane-3 F-3.8
- files: `dom.py:1493` inline hover literal, `:1465` vs `:1597` changed
- target: both hover paths go through `Tab.hovered(x,y) -> Verdict`; both `changed` through `PageState`
- action: `--at` hover refuses on `hovered == False` only (not on `chain`), preserving today's tolerance.
- deps: RF-24, RF-25, RF-27
- risk: low-medium
- acceptance: `grep -rn "elementFromPoint" browser_control/lib/dom/` matches only `scripts.py`;
  live `--at` hover check unchanged
- evidence: `docs/refactor/evidence/lane-3-dom.md#F-3.8`

### RF-29 — `dom.frames(tab=…, browser=…)` resolves internally
- status: done (commit 6506468)
- priority: P1
- category: lib
- findings: lane-4 F-4.9, lane-6 F-6.15, lane-3 F-3.11
- files: `cli/main.py:701-706 cmd_tab_frames`, `dom.py:646 _resolve`, `:761 frames`
- target: `frames(tab="", browser="") -> dict` with the same shape as every other verb;
  keep an internal `frames_of_rows(row, tab_row)` for `_frame_summary`
- action: removes the CLI's only `dom._` access (`# noqa: SLF001` count in `cli/main.py` -> 0)
- deps: RF-23
- risk: low
- acceptance: `grep -rn "dom\._" browser_control/cli/` returns nothing; `tab frames` JSON unchanged
- evidence: `docs/refactor/evidence/lane-4-cli.md#F-4.9`, `lane-6-crosscutting.md#F-6.15`

### RF-30 — `dom/keys.py`: one key-triple source
- status: done (commit 6506468)
- priority: P2
- category: lib/dedup
- findings: lane-3 F-3.10
- files: `dom.py:159 KEYS`, `:2606` hardcoded Enter in `type_text`, `:1864` arrow keys in `select`
- target: `dom/keys.py :: KEYS` + `key_event(name, kind) -> dict`
- action: preserve the measured difference (keyDown without `text` does not submit); do not
  "fix" the two Enter spellings into one accidentally.
- deps: RF-23
- risk: medium
- acceptance: `grep -rn "windowsVirtualKeyCode" browser_control/lib/dom/` matches only `keys.py`;
  `dom.KEYS["enter"][3] == "\r"` still holds; live press/type checks unchanged
- evidence: `docs/refactor/evidence/lane-3-dom.md#F-3.10`

---

## Phase 5 — CLI package + plugins/profiles

### RF-31 — `cli/` package + `VerbArgs` argv reader
- status: done (commit 188907c)
- priority: P1
- category: package/dedup
- findings: lane-4 F-4.2 + F-4.5, lane-6 F-6.6
- files: `cli/main.py` (1320): USAGE 50-171, primitives 174-460, handlers 265-943, tables 945-995;
  `plugins/x_reader.py:74 _take`, `:80 _value`
- target: `cli/{argv,usage,registry?…}` + `cli/verbs/{browser,tab,profile}.py`; `cli/main.py`
  keeps `main()` and re-exports `HANDLERS`/`TAB_SUBCOMMANDS`/`PROFILE_SUBCOMMANDS`/`action`/`USAGE`
- action: `class VerbArgs` owns `pop/pop_all/switch/int/float/tab/one/none/target/selector/done`;
  the class emits today's refusal strings verbatim. Move handlers in groups; keep
  `cli_main.launch`-style patch targets working (handlers call `browser_lib.X` through the
  module attribute). `cli/argv.py` becomes the supported plugin parser; delete `x_reader._take/_value`.
- migration: every `cmd_*`; `cli/main.py:28-44` from-imports stay as re-exports
- deps: RF-10
- risk: medium (refusal text is contract; 20+ test patch sites)
- acceptance: `grep -c 'startswith("-")' browser_control/cli/verbs/*.py` == 0;
  `grep -rc 'one TEXT at most' browser_control/cli/` == 1; `python3 tests/test_unit.py` green
  with no assertion changes; `pyproject.toml` gains `browser_control.cli.verbs`
- evidence: `docs/refactor/evidence/lane-4-cli.md#F-4.2`, `lane-4-cli.md#F-4.5`, `lane-6-crosscutting.md#F-6.6`

### RF-32 — one `Verb` record and one action resolver
- status: done (commit 0c89bbd)
- priority: P1
- category: class/dedup
- findings: lane-4 F-4.3 + F-4.4, lane-5 F-5.7
- files: `cli/main.py:945-995` tables, `:1070-1157` `_modes/resolved_mode/action`, `:50-171 USAGE`;
  `capabilities.py:42-98 ACTIONS`
- target: `cli/registry.py :: @dataclass Verb(name, handler, classes, usage, takes_scope,
  takes_browser, frame_ok, action_of)` + `VerbTable`; `usage.py` holds the prose as data
- action: the gate asks the verb for its action (`action_of`), the handler uses the same reader —
  the two stop re-parsing argv independently (this seam already shipped the `--for JS` hole,
  `docs/progress.md:905-910`). Keep `capabilities.unclassified()` as the drift check.
- deps: RF-31, RF-09
- risk: medium (security-relevant)
- acceptance: `run_cli(["tab","wait","--for","JS","--allow","read"])` -> exit 2 `ERR[not-allowed]`;
  `t_policy_gate`, `t_gate_and_argv_hardening` green unmodified; help body byte-identical
- evidence: `docs/refactor/evidence/lane-4-cli.md#F-4.3`, `lane-4-cli.md#F-4.4`

### RF-33 — `lib/plugins/` package, typed actions, no module globals
- status: done (commit 74d5149)
- priority: P1
- category: package/class
- findings: lane-5 F-5.4 + F-5.5, lane-6 F-6.14, lane-5 F-5.9 (plugins half)
- files: `plugins.py` (195), `capabilities.py:138` (via RF-09), `cli/main.py:1159 PLUGINS`, `:1176-1179`
- target: `lib/plugins/{__init__,discovery,spec}.py`; `PluginAction`/`PluginInfo`/`PluginError`
  dataclasses; `PluginSet.load(reserved)` classmethod, `classes()`, `verb_names()`, `usages()`, `report()`
- action: CLI asks the registry instead of indexing raw dicts; no `global PLUGINS`; plugin call
  stays `run(rest, browser)` (api:1 frozen). `selftest` `plugins`/`plugin_errors` keys unchanged.
- deps: RF-09
- risk: low-medium
- acceptance: `grep -rn "PLUGIN_ACTIONS" browser_control/` returns nothing;
  `grep -c "^global " browser_control/cli/main.py` == 0; plugin fixture checks green
- evidence: `docs/refactor/evidence/lane-5-profile-plugins.md#F-5.4`, `lane-6-crosscutting.md#F-6.14`

### RF-34 — `browser_control/plugin_api.py`: the named plugin seam (D-4)
- status: done (commit 9df582b)
- priority: P2
- category: lib
- findings: lane-5 F-5.6
- files: `plugins/README.md` contract section, `lib/plugins.py:1-46`, `plugins/x_reader.py:26-29`
- target: top-level `plugin_api.py` with `__all__` (`fail`, `ControlError`, `nav`, `wait`,
  `extract`, `PLUGIN_API`) re-exporting today's functions
- action: migrate `x_reader.py` to import from it; point the README/docs at it. A context object
  carrying tab/browser/frame is a separate `api: 2` proposal — do not change the signature now.
- deps: RF-33
- risk: medium (documented contract; the seam module is the low-risk half)
- acceptance: `python -c "import browser_control.plugin_api as m; assert m.__all__"` succeeds;
  `grep -c "from browser_control.lib" plugins/x_reader.py` == 0; `t_x_plugin_offline` green
- evidence: `docs/refactor/evidence/lane-5-profile-plugins.md#F-5.6`

### RF-35 — `Invocation` + `Reporter`: split `main()`; drop the last globals
- status: done (commit c258ca0)
- priority: P1
- category: class
- findings: lane-4 F-4.1, lane-5 F-5.5 (CLI half), lane-6 F-6.1 (PLUGINS)
- files: `cli/main.py:1171-1320 main`, `:1176-1177 global PLUGINS`, `:1190-1213` duplicated refusal,
  `:1288-1320` except arms + audit write
- target: `cli/runner.py :: class Invocation` (load_plugins/start/gate/dispatch/decorate/emit);
  `cli/emit.py :: class Reporter` (stdout/stderr/exit codes/broken-pipe path); `main()` becomes
  a <= 15-line facade
- action: keep the `dup2(/dev/null)` broken-pipe behaviour and the `internal` arm exactly;
  one start path so audit reset happens on every path (RF-15); no `global` statements.
- deps: RF-31, RF-32, RF-33, RF-09, RF-08
- risk: medium (exception arms pinned by tests)
- acceptance: `grep -rn "^ *global " browser_control/cli/` == 0;
  `grep -c 'def main(' browser_control/cli/main.py` == 1 and body <= 15 lines;
  `python3 -m browser_control selftest` same JSON keys/exit codes
- evidence: `docs/refactor/evidence/lane-4-cli.md#F-4.1`

### RF-36 — `lib/profile/` package + `seedtree.py`
- status: done (commit 70e2074)
- priority: P2
- category: package
- findings: lane-5 F-5.8 + F-5.9 (profile half)
- files: `profile.py:67 _tree`, `:55-64 SEED_SKIP`, `:146 _copy`, `:278 info`, `:354 seed`, `:461 reset`
- target: `lib/seedtree.py :: TreeFacts + walk(source, *, skips) + copy_verified(...)`;
  `lib/profile/{__init__,info,seed}.py` whose `__init__` re-exports `info/seed/reset`
- action: every call site names its skip policy explicitly; `SEED_SKIP` remains the seed/copy
  policy. Do NOT change what `profile info`'s `bytes`/`files` mean (document the existing
  semantics instead — see Q-1).
- deps: RF-12, RF-14
- risk: low-medium
- acceptance: no module-level set consulted inside the walker; `profile info` keys unchanged;
  `tests/test_unit.py:2431-2511` green; `pip install .` outside the repo imports
  `browser_control.lib.profile`
- evidence: `docs/refactor/evidence/lane-5-profile-plugins.md#F-5.8`, `lane-5-profile-plugins.md#F-5.9`

### RF-37 — test-seam sweep (final audit)
- status: done (commit 188907c)
- priority: P1
- category: tests
- findings: lane-6 F-6.11, lane-2 F-2.9, lane-3 F-3.12, lane-4 F-4.6
- files: `tests/test_unit.py`, `tests/live_test.py`
- target: no test patches a name through a facade that the moved code no longer looks up;
  no test asserts on source text; `SLF001` count in tests does not grow
- action:
  1. `test_unit.py:845-850` source-text assertion (`browser_control/lib/browser.py` path) becomes
     behavioural (the test already owns the behaviour at 784-792).
  2. migrate remaining `browser.*`/`dom.*`/`cli_main.*` patch targets to the owning module.
  3. prove patch efficacy: for each migrated patch site, a mutation (e.g. patch calls a callable
     that records itself) still intercepts — a green-but-unused patch is the failure mode.
- deps: all move tasks
- risk: medium
- acceptance: `grep -rc 'browser_control/lib/browser.py' tests/` == 0;
  `python3 tests/test_unit.py` pass count >= 63; `python3 tests/live_test.py` green
- evidence: `docs/refactor/evidence/lane-6-crosscutting.md#F-6.11`

---

## Coverage matrix (every finding has a task)

| Finding | Task | Finding | Task |
| --- | --- | --- | --- |
| F-1.1 | RF-18 | F-3.9 | RF-04 |
| F-1.2 | RF-17 | F-3.10 | RF-30 |
| F-1.3 | RF-16 | F-3.11 | RF-10, RF-29 |
| F-1.4 | RF-09 | F-3.12 | RF-23, RF-37 |
| F-1.5 | RF-06 | F-4.1 | RF-35 |
| F-1.6 | RF-18, RF-37 | F-4.2 | RF-31 |
| F-2.1 | RF-20 | F-4.3 | RF-32 |
| F-2.2 | RF-08, RF-22 | F-4.4 | RF-32 |
| F-2.3 | RF-03 | F-4.5 | RF-31 |
| F-2.4 | RF-12 | F-4.6 | RF-37 |
| F-2.5 | RF-11 | F-4.7 | RF-10, RF-15 |
| F-2.6 | RF-13 | F-4.8 | RF-15 |
| F-2.7 | RF-21 | F-4.9 | RF-29 |
| F-2.8 | RF-19 | F-5.1 | RF-14 |
| F-2.9 | RF-10, RF-37 | F-5.2 | RF-11 |
| F-2.10 | RF-10 | F-5.3 | RF-12 |
| F-3.1 | RF-23 | F-5.4 | RF-33 |
| F-3.2 | RF-27 | F-5.5 | RF-33, RF-35 |
| F-3.3 | RF-27 | F-5.6 | RF-34 |
| F-3.4 | RF-24 | F-5.7 | RF-32, RF-33 |
| F-3.5 | RF-25 | F-5.8 | RF-36 |
| F-3.6 | RF-08 | F-5.9 | RF-33, RF-36 |
| F-3.7 | RF-07 | F-6.1 | RF-08, RF-09, RF-15, RF-22, RF-33, RF-35 |
| F-3.8 | RF-24, RF-28 | F-6.2 | RF-01 |
| F-6.3 | RF-03 | F-6.9 | RF-27 |
| F-6.4 | RF-04 | F-6.10 | RF-26 |
| F-6.5 | RF-02, RF-05 | F-6.11 | RF-37 |
| F-6.6 | RF-31 | F-6.12 | RF-10 |
| F-6.7 | RF-13 | F-6.13 | RF-10 |
| F-6.8 | RF-19 | F-6.14 | RF-33 |
| | | F-6.15 | RF-29 |

## Open questions carried for the architect (do not silently decide)

- Q-1 **What does `profile info`'s `bytes`/`files` mean?** Currently "what seed would copy"
  (skips excluded) while the docstring promises total weight (`profile.py:279-283,297`).
  RF-36 makes the policy explicit and deliberately does not change the number.
- Q-2 **Attach identity race:** `attach` resolves the row before taking the lock it writes under
  (`browser.py:719-731` vs `:751`). RF-12 is the natural place to close it, but closing it is a
  behaviour change — decide whether it is in scope or a separate ticket.
- Q-3 **Plugin capability granularity:** plugins declare classes per top-level verb; built-ins
  per subcommand/mode (`capabilities.ACTIONS["tab dialog accept"]`). Extend the spec shape or
  document the asymmetry (RF-33)?
- Q-4 **Is `plugins/x_reader.py` shipped API or a fixture?** If fixture, RF-31/RF-34 may move it
  under `tests/`; if shipped, `cli/argv.py` becomes part of the public plugin surface.
- Q-5 **`lib/files.py` vs `lib/images.py`:** audit, images and profile each do atomic writes.
  RF-07 keeps them separate (narrow); revisit only if a fourth appears.

## Machine-readable task index

```json
[
 {"id":"RF-01","phase":1,"title":"lib/text.py: one untrusted-text flattener","status":"done (commit 28c06d4)","deps":[],"risk":"low","priority":"P1","findings":["F-6.2"],"files":["browser_control/lib/cdp.py","browser_control/lib/browser.py","browser_control/lib/dom.py","browser_control/lib/audit.py"],"target":["browser_control/lib/text.py"]},
 {"id":"RF-02","phase":1,"title":"lib/coerce.py: one numeric/sequence coercer","status":"done (commit f724849)","deps":[],"risk":"low","priority":"P1","findings":["F-6.5"],"files":["browser_control/lib/browser.py","browser_control/lib/dom.py","browser_control/lib/profile.py"],"target":["browser_control/lib/coerce.py"]},
 {"id":"RF-03","phase":1,"title":"lib/proc.py: one /proc reader","status":"done (commit df385db)","deps":[],"risk":"medium","priority":"P1","findings":["F-6.3","F-2.3"],"files":["browser_control/lib/cdp.py","browser_control/lib/browser.py"],"target":["browser_control/lib/proc.py"]},
 {"id":"RF-04","phase":1,"title":"lib/poll.py: one deadline loop","status":"done (commit 7fff291)","deps":[],"risk":"medium","priority":"P1","findings":["F-6.4","F-3.9"],"files":["browser_control/lib/browser.py","browser_control/lib/dom.py","browser_control/lib/cdp.py"],"target":["browser_control/lib/poll.py"]},
 {"id":"RF-05","phase":1,"title":"lib/paths.py: one path/root/profile resolver","status":"done (commit 4eb04cc)","deps":[],"risk":"low","priority":"P1","findings":["F-6.5","F-5.1"],"files":["browser_control/lib/browser.py","browser_control/lib/profile.py","browser_control/lib/audit.py","browser_control/lib/plugins.py"],"target":["browser_control/lib/paths.py"]},
 {"id":"RF-06","phase":1,"title":"errors.CODES: register the refusal vocabulary","status":"done (commit 4101928)","deps":[],"risk":"low","priority":"P2","findings":["F-1.5"],"files":["browser_control/lib/errors.py"],"target":["browser_control/lib/errors.py"]},
 {"id":"RF-07","phase":1,"title":"lib/images.py: CDP-free screenshot file layer","status":"done (commit a47722a)","deps":[],"risk":"low","priority":"P1","findings":["F-3.7"],"files":["browser_control/lib/dom.py"],"target":["browser_control/lib/images.py"]},
 {"id":"RF-08","phase":1,"title":"lib/scope.py::Scope: own profile+frame","status":"done (commit 4e8cddf)","deps":[],"risk":"high","priority":"P1","findings":["F-6.1","F-2.2","F-3.6"],"files":["browser_control/lib/browser.py","browser_control/lib/dom.py","browser_control/cli/main.py"],"target":["browser_control/lib/scope.py"]},
 {"id":"RF-09","phase":1,"title":"capabilities Surface + policy Policy","status":"done (commit f4f88fe)","deps":[],"risk":"medium","priority":"P1","findings":["F-1.4","F-6.1"],"files":["browser_control/lib/capabilities.py"],"target":["browser_control/lib/capabilities.py","browser_control/lib/policy.py"]},
 {"id":"RF-10","phase":1,"title":"publish seams, delete dead, fix naming","status":"done (commit 5196cf2)","deps":[],"risk":"medium","priority":"P1","findings":["F-2.9","F-2.10","F-3.11","F-4.7","F-6.11","F-6.12","F-6.13"],"files":["browser_control/lib/browser.py","browser_control/lib/dom.py","browser_control/cli/main.py","browser_control/lib/audit.py"],"target":["browser_control/lib/browser.py","browser_control/lib/dom.py","browser_control/cli/main.py"]},
 {"id":"RF-11","phase":2,"title":"lib/locks.py: typed lock + choreography","status":"done (commit 1b88aba)","deps":[],"risk":"medium","priority":"P1","findings":["F-2.5","F-5.2"],"files":["browser_control/lib/browser.py","browser_control/lib/profile.py"],"target":["browser_control/lib/locks.py"]},
 {"id":"RF-12","phase":2,"title":"lib/attachments.py: one attached.json owner","status":"done (commit 5b1d51e)","deps":["RF-11"],"risk":"medium","priority":"P1","findings":["F-2.4","F-5.3"],"files":["browser_control/lib/browser.py","browser_control/lib/profile.py"],"target":["browser_control/lib/attachments.py"]},
 {"id":"RF-13","phase":2,"title":"Selector: one named-browser value object","status":"done (commit 4ece341)","deps":["RF-05","RF-19"],"risk":"medium","priority":"P1","findings":["F-2.6","F-6.7"],"files":["browser_control/lib/browser.py","browser_control/cli/main.py"],"target":["browser_control/lib/browser/selector.py"]},
 {"id":"RF-14","phase":2,"title":"lib/instance.py::Instance","status":"done (commit 29d809e)","deps":["RF-05","RF-11","RF-12"],"risk":"medium","priority":"P1","findings":["F-5.1"],"files":["browser_control/lib/profile.py","browser_control/lib/browser.py"],"target":["browser_control/lib/instance.py"]},
 {"id":"RF-15","phase":2,"title":"audit ActionLog sink + lib/logfile.py","status":"done (commit 5feb828)","deps":[],"risk":"medium","priority":"P1","findings":["F-4.7","F-4.8","F-6.1"],"files":["browser_control/lib/audit.py","browser_control/cli/main.py"],"target":["browser_control/lib/audit.py","browser_control/lib/logfile.py"]},
 {"id":"RF-16","phase":2,"title":"lib/cdp/ package behind facade","status":"done (commit 7faca8f)","deps":["RF-03"],"risk":"medium","priority":"P1","findings":["F-1.3"],"files":["browser_control/lib/cdp.py"],"target":["browser_control/lib/cdp/"]},
 {"id":"RF-17","phase":2,"title":"one RPC send/await/demux path","status":"done (commit 7bb0930)","deps":["RF-16"],"risk":"medium","priority":"P1","findings":["F-1.2"],"files":["browser_control/lib/cdp/"],"target":["browser_control/lib/cdp/rpc.py"]},
 {"id":"RF-18","phase":2,"title":"Session is the only connection owner","status":"done (commit 20b3190)","deps":["RF-17"],"risk":"high","priority":"P1","findings":["F-1.1","F-1.6"],"files":["browser_control/lib/cdp/"],"target":["browser_control/lib/cdp/session.py"]},
 {"id":"RF-19","phase":3,"title":"lib/browser/ package behind facade","status":"done (commit d729d27)","deps":["RF-05","RF-08","RF-10","RF-11","RF-12","RF-14"],"risk":"high","priority":"P1","findings":["F-2.8","F-6.8"],"files":["browser_control/lib/browser.py"],"target":["browser_control/lib/browser/"]},
 {"id":"RF-20","phase":3,"title":"BrowserRow + Endpoint dataclasses","status":"done (commit 7740e55)","deps":["RF-19"],"risk":"medium","priority":"P1","findings":["F-2.1"],"files":["browser_control/lib/browser/"],"target":["browser_control/lib/browser/machine.py"]},
 {"id":"RF-21","phase":3,"title":"one write/read authorisation predicate","status":"done (commit 7740e55)","deps":["RF-19","RF-20"],"risk":"low","priority":"P1","findings":["F-2.7"],"files":["browser_control/lib/browser/"],"target":["browser_control/lib/browser/machine.py"]},
 {"id":"RF-22","phase":3,"title":"EndpointGuard object with forget()","status":"done (commit 7740e55)","deps":["RF-19","RF-03"],"risk":"medium","priority":"P1","findings":["F-2.2","F-6.1"],"files":["browser_control/lib/browser/"],"target":["browser_control/lib/browser/owners.py"]},
 {"id":"RF-23","phase":4,"title":"lib/dom/ package behind facade","status":"todo","deps":["RF-10"],"risk":"medium","priority":"P1","findings":["F-3.1","F-3.12"],"files":["browser_control/lib/dom.py"],"target":["browser_control/lib/dom/"]},
 {"id":"RF-24","phase":4,"title":"dom/scripts.py + fill()","status":"done (commit 6e142f9)","deps":["RF-23"],"risk":"medium","priority":"P1","findings":["F-3.4","F-3.8"],"files":["browser_control/lib/dom/"],"target":["browser_control/lib/dom/scripts.py"]},
 {"id":"RF-25","phase":4,"title":"Verdict + PageState reply convention","status":"done (commit 789331f)","deps":["RF-23"],"risk":"medium","priority":"P1","findings":["F-3.5","F-3.8"],"files":["browser_control/lib/dom/"],"target":["browser_control/lib/dom/result.py"]},
 {"id":"RF-26","phase":4,"title":"one page-session factory + connection reuse","status":"done (commit 35489ff)","deps":["RF-18","RF-23"],"risk":"medium","priority":"P1","findings":["F-6.10"],"files":["browser_control/lib/dom/","browser_control/lib/browser/"],"target":["browser_control/lib/dom/tab.py"]},
 {"id":"RF-27","phase":4,"title":"Tab: one per-verb prelude","status":"done (commit fe8ab46)","deps":["RF-24","RF-25","RF-26"],"risk":"high","priority":"P1","findings":["F-3.2","F-3.3","F-6.9"],"files":["browser_control/lib/dom/"],"target":["browser_control/lib/dom/tab.py","browser_control/lib/dom/actions.py"]},
 {"id":"RF-28","phase":4,"title":"hover/changed dedup","status":"done (commit fe8ab46)","deps":["RF-24","RF-25","RF-27"],"risk":"low","priority":"P2","findings":["F-3.8"],"files":["browser_control/lib/dom/"],"target":["browser_control/lib/dom/actions.py"]},
 {"id":"RF-29","phase":4,"title":"dom.frames resolves internally","status":"done (commit 6506468)","deps":["RF-23"],"risk":"low","priority":"P1","findings":["F-4.9","F-6.15","F-3.11"],"files":["browser_control/cli/main.py","browser_control/lib/dom/"],"target":["browser_control/lib/dom/frames.py"]},
 {"id":"RF-30","phase":4,"title":"dom/keys.py one key-triple source","status":"done (commit 6506468)","deps":["RF-23"],"risk":"medium","priority":"P2","findings":["F-3.10"],"files":["browser_control/lib/dom/"],"target":["browser_control/lib/dom/keys.py"]},
 {"id":"RF-31","phase":5,"title":"cli/ package + VerbArgs","status":"done (commit 188907c)","deps":["RF-10"],"risk":"medium","priority":"P1","findings":["F-4.2","F-4.5","F-6.6"],"files":["browser_control/cli/main.py","plugins/x_reader.py"],"target":["browser_control/cli/argv.py","browser_control/cli/verbs/"]},
 {"id":"RF-32","phase":5,"title":"Verb record + single action resolver","status":"done (commit 0c89bbd)","deps":["RF-31","RF-09"],"risk":"medium","priority":"P1","findings":["F-4.3","F-4.4","F-5.7"],"files":["browser_control/cli/"],"target":["browser_control/cli/registry.py","browser_control/cli/usage.py"]},
 {"id":"RF-33","phase":5,"title":"lib/plugins/ package, typed actions","status":"done (commit 74d5149)","deps":["RF-09"],"risk":"low","priority":"P1","findings":["F-5.4","F-5.5","F-5.9","F-6.14"],"files":["browser_control/lib/plugins.py","browser_control/cli/main.py"],"target":["browser_control/lib/plugins/"]},
 {"id":"RF-34","phase":5,"title":"plugin_api.py named seam","status":"done (commit 9df582b)","deps":["RF-33"],"risk":"medium","priority":"P2","findings":["F-5.6"],"files":["plugins/x_reader.py","plugins/README.md","browser_control/lib/plugins.py"],"target":["browser_control/plugin_api.py"]},
 {"id":"RF-35","phase":5,"title":"Invocation + Reporter; main() facade","status":"done (commit c258ca0)","deps":["RF-31","RF-32","RF-33","RF-09","RF-08"],"risk":"medium","priority":"P1","findings":["F-4.1","F-5.5","F-6.1"],"files":["browser_control/cli/main.py"],"target":["browser_control/cli/runner.py","browser_control/cli/emit.py"]},
 {"id":"RF-36","phase":5,"title":"lib/profile/ package + seedtree.py","status":"done (commit 70e2074)","deps":["RF-12","RF-14"],"risk":"low","priority":"P2","findings":["F-5.8","F-5.9"],"files":["browser_control/lib/profile.py"],"target":["browser_control/lib/profile/","browser_control/lib/seedtree.py"]},
 {"id":"RF-37","phase":5,"title":"test-seam sweep (final audit)","status":"done (commit 188907c)","deps":["RF-01","RF-02","RF-03","RF-04","RF-05","RF-06","RF-07","RF-08","RF-09","RF-10","RF-11","RF-12","RF-13","RF-14","RF-15","RF-16","RF-17","RF-18","RF-19","RF-20","RF-21","RF-22","RF-23","RF-24","RF-25","RF-26","RF-27","RF-28","RF-29","RF-30","RF-31","RF-32","RF-33","RF-34","RF-35","RF-36"],"risk":"medium","priority":"P1","findings":["F-2.9","F-3.12","F-4.6","F-6.11"],"files":["tests/test_unit.py","tests/live_test.py"],"target":["tests/"]}
]
```
