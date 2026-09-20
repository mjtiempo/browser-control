# Lane 5: profile + plugin tier (`lib/profile.py`, `lib/plugins.py`, `plugins/x_reader.py`, package `__init__`/entry points)

## Summary

- **What this slice does:** `lib/profile.py` implements `profile info|seed|reset` (tree walk → manifest → filtered copy → size read-back, plus per-instance locking and liveness refusal); `lib/plugins.py` discovers/imports/validates `PLUGIN = {…}` files and holds a `Registry`; `plugins/x_reader.py` is the shipped reference plugin (X search on top of `tab extract`); `browser_control/__init__.py`, `lib/__init__.py`, `cli/__init__.py` and `__main__.py` are the packaging/entry-point shims.
- **Ranked structural problems:** (1) there is no owner object for a *profile instance* — identity, liveness and locking are reached through the `SCOPE` global plus **11 `# noqa: SLF001` sites** over 7 private `browser.py` symbols (F-5.1); (2) the root-lock→profile-lock choreography is hand-rolled twice with a synthetic stub lock, and the lock handle is an untyped dict (F-5.2, F-5.3); (3) the plugin surface is carried in two module globals and re-derived by the CLI from raw dicts (F-5.4, F-5.5, F-5.7); (4) plugin-facing core access is "import anything in `lib`" with no named seam, which the test suite proves by rebinding module globals (F-5.6).
- **Already fine, keep it:** `lib/plugins.py` is small and single-purpose-ish (195 lines, one `Registry` class at `plugins.py:63`, four functions at `78/94/123/140`); loading really is fail-open with errors recorded rather than raised (`plugins.py:108-112`, `plugins.py:133-135`); `x_reader.py` only touches *public* names (`browser_lib.nav` at `x_reader.py:174`, `dom.wait` at `178`/`183`, `dom.extract` at `185`, `fail`/`ControlError`), so it is not reaching into privates; the class surface is already contract-tested against the verb tables (`tests/test_unit.py:1567-1582`); the two entry shims (`browser-control-cli:10`, `browser_control/__main__.py:14`) are 3 lines each and correct.
- **Contract posture:** verb names, flags, JSON keys, exit codes, `ERR[...]` codes and the stdout/stderr split are untouched by every proposal below; two proposals (F-5.6, F-5.8) touch *documented* behaviour questions and are therefore split into "structure now / decision later" in Open questions.
- **Merge verdict:** OK with notes — no contract-affecting defect was found; all findings are structural debt that this lane exists to schedule.

## Findings

### F-5.1 No value owns "the profile instance": identity, liveness and locking live in a global + private reaches

- category: class-candidate | hidden-state
- locations: `browser_control/lib/profile.py:40-47` — module imports (`_is_managed` at `:42`, plus `browser as browser_lib` at `:40`), `browser_control/lib/profile.py:222-250` — `_target(profile: str = "", browser: str = "")`, `browser_control/lib/profile.py:252-267` — `_live_pid(profile: str) -> int`, `browser_control/lib/profile.py:269-276` — `_refuse_live(profile: str, verb: str) -> None`, `browser_control/lib/browser.py:121` — `SCOPE: dict[str, str] = {"profile": ""}`, `browser_control/lib/browser.py:139` — `instance_dir(binary_path)`, `browser_control/lib/browser.py:627` — `_writable_profile`, `browser_control/lib/browser.py:669` — `managed_profile`
- evidence:
  - `grep -c "noqa: SLF001" browser_control/lib/profile.py` = **11** (`profile.py:205, 262, 300, 398, 400, 482, 484, 503, 504, 505, 510`), reaching 7 private names of `browser.py`: `_norm` (`browser.py:174`), `_pid_file` (`:268`), `_is_managed` (`:461`), `_attached` (`:533`), `_write_attached` (`:550`), `_lock_path` (`:1228`), `_lock` (`:1305`). Line `profile.py:205` even carries the noqa for the *public* constants `browser_lib.LOCK_FILE` / `PID_FILE`, i.e. the public/private boundary is not legible.
  - `_target` (`profile.py:222`) is a **fourth** instance resolver beside `instance_dir` (`browser.py:139`), `_writable_profile` (`browser.py:627`) and `managed_profile` (`browser.py:669`): `wanted = str(profile or "").strip() or scope()` (`profile.py:230`) → `_is_managed` → `fail("not-managed", …)` (`:233-238`), else `path = profile_dir(binary(browser))` (`:239`), plus a symlink refusal (`:240-245`). The refusal codes differ by path (`browser.py:150` uses `bad-args`, `profile.py:236` uses `not-managed`) and are frozen, so a merge must be parameterised, not unified blindly.
  - Identity-by-normalised-path is re-implemented inline three times instead of once: `profile.py:262-264` (`_live_pid`), `:299-300` (`info`'s live lookup), `:504` (`reset`'s record removal).
  - The instance is read from the global at two levels: `scope()` in `_target` (`profile.py:230`) and again in `info()` (`profile.py:288`), while the browser row's fields are hand-copied at `profile.py:317-321` (`row["live"] = {"pid": live["pid"], "exe": live["exe"], "port": live["cdp"]["port"], "verified": live["cdp"]["verified"]}`).
  - `info()` also re-derives `managed`/`attached`/`default_for` per row (`profile.py:306-316`), so the same instance questions are answered in three places with different guards.
- proposal: add `browser_control/lib/instance.py :: class Instance` (constructed as `Instance.resolve(profile, browser)` — one place that applies the scope→named→default precedence and the root boundary) exposing `.path`, `.managed`, `.live_pid()`, `.refuse_live(verb)`, `.browsers_row()`, `.attached`. Then `_target`/`_live_pid`/`_refuse_live` (`profile.py:222-276`) become thin delegations that keep their **existing refusal codes and texts**, and `profile.info/seed/reset` construct one `Instance` each. Caller migration: `profile.py:230-245`, `:288-291`, `:379`, `:390`, `:470-471`, `:490`, `:501-505`; `cli/main.py:901-935` (`cmd_profile_info/seed/reset`) stay unchanged because the module-level `info/seed/reset` signatures are preserved.
- dependencies: F-5.3 (same reach-through motivation), F-5.2 (locking belongs to the same object)
- risk: medium — `_target` and `_live_pid` encode review-fixed behaviour (symlink refusal, normalised comparison) whose regressions are `not-managed` / `profile-live` contract changes.
- acceptance:
  - `grep -c "noqa: SLF001" browser_control/lib/profile.py` == 0.
  - `tests/test_unit.py` green, including `t_profile_symlink_target_is_refused`, `t_seed_lands_where_chrome_reads`, `t_reset_guard_counts_skipped_content` (`test_unit.py:2424-2515`).
  - `profile_lib.info/seed/reset` keep their exact signatures (`info(profile="")`, `seed(source="", profile="", browser="", force=False, dry=False)`, `reset(profile="", browser="", force=False)`).

### F-5.2 Lock choreography duplicated in `seed`/`reset`; the lock handle is an untyped dict with a synthetic stub

- category: class-candidate | duplication | hidden-state
- locations: `browser_control/lib/profile.py:391-402` — `held = True` + `contextlib.nullcontext({"held": True, "warning": ""})`, `browser_control/lib/profile.py:398-402` and `:482-485` — the `_lock(root)` + `_lock(target)` nesting, `browser_control/lib/profile.py:449-453` and `:517-521` — identical warning merge, `browser_control/lib/browser.py:1305-1333` — `def _lock(path: str, verb: str, wait: float = LOCK_WAIT_S)` yielding `{"held": bool, "warning": str}`
- evidence:
  - `_lock`'s documented yield is a bare dict — `browser.py:1310` docstring: “Yields ``{"held": bool, "warning": str}``”; the two consumers in `profile.py` read `lock["warning"]`/`profile_lock["warning"]` (`profile.py:449`, `:517`), and 5 more consumers do `if lock["warning"]: reply["warning"] = lock["warning"]` (`browser.py:763-764`, `:778-779`, `:805-806`, `:1551-1552`, `:1595-1596`) — 7 copy-pasted “lock warning → reply warning” sites over 2 modules.
  - `profile.py:396` fabricates a *lock-shaped dict* to keep `--dry` from creating the lock file: `contextlib.nullcontext({"held": True, "warning": ""})` — a stub that must stay in sync with `browser.py:1310`'s undocumented shape.
  - The identical 4-line merge appears at `profile.py:449-453` and `:517-521`; the second copy is byte-identical, which is what made the `--dry` and re-check-under-lock fixes (see `docs/review-2026-09-20.md` items 13/14) have to be applied twice.
  - The ordering invariant (“root lock orders the attach records; the profile lock orders the instance; re-check liveness UNDER the profile lock”) is expressed only in comments at `profile.py:403-409` and `:486-489`, not in code, so a third profile verb would have to re-derive it.
- proposal: introduce `browser_control/lib/locks.py :: @dataclass(frozen=True) class Lock` (`held: bool`, `warning: str`) and a context manager `instance_locks(root_path, profile_path, verb, *, skip_profile_lock=False)` that owns the root→profile order and exposes `.warnings`. Migrate `profile.py:396-402` (`skip_profile_lock=dry`), `:482-485`, and replace both merge loops with `reply |= locks.warning_field()`-style helper; `browser.py:1305` returns a `Lock` and its 5 call sites (`:763`, `:778`, `:805`, `:1551`, `:1595`) read `.warning`.
- dependencies: F-5.1 (`Instance.lock()` becomes the only caller); cross-lane with the `browser.py` owner
- risk: medium — locking is the module's most delicate invariant; the `--dry` no-write promise is asserted by `t_seed_dry_writes_nothing` (`test_unit.py:2489-2503`).
- acceptance:
  - exactly one place constructs the root+profile lock nesting (`grep -c "_lock_path(root())" browser_control/lib/**/*.py` == 1 outside `locks.py`).
  - `grep -n '{"held"' browser_control/lib/profile.py` returns nothing.
  - `python3 tests/test_unit.py` green, including the `--dry` and `profile-busy` cases.

### F-5.3 Attachment records: three hand-rolled read-modify-writes, one of them in `profile.py`

- category: duplication | lib-module
- locations: `browser_control/lib/browser.py:530-563` — `ATTACH_FILE`, `_attached()`, `_write_attached(records)`, `browser_control/lib/browser.py:751-757` (`attach`), `:774-791` (`detach`), `browser_control/lib/profile.py:501-505` (`reset`)
- evidence:
  - `attach` (`browser.py:751-755`): `with _lock(_lock_path(root()), "attach") as lock: records = _attached(); …; _write_attached(records)`.
  - `detach --all` (`browser.py:774-777`) and `detach` (`browser.py:789-798`) repeat the same three lines with the same root lock.
  - `profile.py:501-505` performs a fourth copy **from another module**: `records = browser_lib._attached()  # noqa: SLF001` → `records.pop(browser_lib._norm(target), None)` → `browser_lib._write_attached(records)`; it depends on the record file's key discipline (`_norm`, `browser.py:535-537`) and the atomic-replace format (`browser.py:550-563`) without owning either.
  - `is_attached` (`browser.py:566`) and the record store are also the only reason `profile.reset` imports `_norm` at all, which is what earns the SLF001 at `profile.py:504`.
- proposal: `browser_control/lib/attachments.py :: class AttachmentStore` with `rows()`, `get(profile)`, `put(record)`, `drop(profile)`, `drop_all()` over `ATTACH_FILE`; `browser.attach/detach/attachments` and `profile.reset` (already inside the root lock at `profile.py:482`) call `store.drop(instance.path)`. The reply keys (`detached`, `count`, `already`, `attached`) stay exactly as built today.
- dependencies: F-5.2 (the root lock the store relies on), F-5.1 (`Instance.path`)
- risk: low — pure relocation of an existing JSON file; the format is unchanged.
- acceptance:
  - `grep -c "_write_attached" browser_control/lib/*.py` == 1 (inside `attachments.py`).
  - `tests/test_unit.py` attach/detach cases green; `python3 tests/live_test.py` attach checks green.

### F-5.4 `Registry` stores raw dicts, so the CLI re-derives the whole plugin surface

- category: class-candidate | lib-module
- locations: `browser_control/lib/plugins.py:183-187` — `registry.actions[verb] = {"run": …, "classes": …, "usage": …, "plugin": name, "path": path}`, `browser_control/lib/plugins.py:63-76` — `class Registry`, `browser_control/cli/main.py:1164` — `return [*HANDLERS, *PLUGINS.actions]`, `browser_control/cli/main.py:1182-1183`, `browser_control/cli/main.py:1178-1179`, `browser_control/cli/main.py:1238-1239`
- evidence:
  - four different consumers poke the same untyped dict with string keys: `spec['usage']` (help, `main.py:1183`), `tuple(spec["classes"])` (gate wiring, `main.py:1178-1179`), `plugin["run"]` (dispatch, `main.py:1238-1239`), `PLUGINS.actions` as a name map (`main.py:1164`).
  - `Registry.errors` is `list[str]` of pre-formatted messages (`plugins.py:69`, `:75`) that `selftest` prints verbatim (`main.py:1168` → `main.py:388`), so a report consumer cannot tell a bad API version from a collision without parsing text (`plugins.py:150-153` vs `:166-168`).
  - validation is spread across one 50-line function: `_register` (`plugins.py:140-195`) does schema checks (`:143-157`), verb-name regex (`:159-164`), collision (`:165-168`), callable check (`:170-173`), capability-vocabulary check (`:174-181`) and the two registrations (`:182-194`).
  - the built-in side of the same concept is declarative and per-subcommand (`capabilities.ACTIONS["tab dialog accept"]`, asserted at `test_unit.py:1592`), while a plugin can only declare classes for a whole top-level verb — the spec shape is weaker than the table it must feed (`capabilities.py:127`).
- proposal: `browser_control/lib/plugins/spec.py :: @dataclass(frozen=True) class PluginAction(verb, run, classes, usage, plugin, path)` and `@dataclass class PluginInfo(name, path, description, verbs)` plus `@dataclass class PluginError(path, message, kind)`; `Registry.actions: dict[str, PluginAction]`, and add `Registry.verb_names(reserved)`, `Registry.classes()`, `Registry.usages()`, `Registry.report()` so `main.py` asks the registry instead of indexing dicts. Migrate `cli/main.py:1164`, `:1168`, `:1178-1179`, `:1182-1183`, `:1238-1239`, `:388`.
- dependencies: F-5.5 (same wiring), F-5.9 (the package split that gives `spec.py` a home)
- risk: low — internal representation only; `selftest`'s `plugins`/`plugin_errors` JSON must keep its current shape (`main.py:1167-1168`).
- acceptance:
  - no `["usage"]` / `["classes"]` / `["run"]` string indexing of registry entries outside `lib/plugins/`.
  - `selftest` output for the fixture plugins in `test_unit.py:2738-2793` is byte-identical.
  - `python3 tests/test_unit.py` green (plugin system + x plugin cases).

### F-5.5 The plugin surface is carried by two module globals, rebound per invocation

- category: hidden-state
- locations: `browser_control/cli/main.py:1159` — `PLUGINS = plugins_lib.Registry()`, `browser_control/cli/main.py:1176-1179` — `global PLUGINS` + `plugins_lib.load(...)` + `capabilities.set_plugins(...)`, `browser_control/lib/capabilities.py:138` — `PLUGIN_ACTIONS: dict[str, tuple[str, ...]] = {}`, `browser_control/lib/capabilities.py:141-149` — `set_plugins`, `browser_control/lib/capabilities.py:284` — `classes = ACTIONS.get(action) or PLUGIN_ACTIONS.get(action)`
- evidence:
  - `main()` rebinds a module global on every call (`main.py:1176-1177`) and `_verb_names`/`_plugins_report` read it from module scope (`main.py:1164`, `:1168`), so anything calling into `cli.main` in-process shares one mutable registry — the comment at `main.py:1157-1158` exists only because of that.
  - the class declarations take a second hop through a global that the *consumer* module owns: `main.py:1178-1179` pushes a dict into `capabilities.PLUGIN_ACTIONS` (`capabilities.py:147-149`), and the gate reads it back at `capabilities.py:284`. The producer (`plugins.py`) never sees `capabilities` state; a mediator exists only inside `main`.
  - `grep -c "^global " browser_control/cli/main.py` == 1 today, and it exists solely for plugins.
  - the same information is already in one place (`Registry.actions`) but is *copied* into a second mutable owner, so the two can disagree if `load` is ever called without `set_plugins`.
- proposal: introduce `browser_control/cli/invocation.py :: @dataclass class Invocation(verb, rest, flags, browser, plugins)` built in `main()` and passed to handlers, so `_verb_names(invocation)`/`_plugins_report(invocation)` need no global; `cmd_selftest` (`main.py:351-390`) reads it from the argument. Plugin dispatch keeps the documented two-argument call — `invocation.plugins.actions[verb].run(rest, browser)` at `main.py:1270` — so `plugins/README.md`'s contract is untouched. Cross-lane alternative (better, larger): a gate object constructed with the registry instead of `capabilities.PLUGIN_ACTIONS`.
- dependencies: F-5.4 (typed actions make the hand-off trivial)
- risk: medium — the built-in handler signature `Callable[[list[str], str], dict]` (`main.py:400`) is used by `HANDLERS`, `TAB_SUBCOMMANDS` (`main.py:945`), `PROFILE_SUBCOMMANDS` (`main.py:976`) and ~60 `cmd_*` functions; a signature change is broad and must not leak into the plugin call.
- acceptance:
  - `grep -c "^global " browser_control/cli/main.py` == 0 and `grep -c "PLUGINS = " browser_control/cli/main.py` == 0.
  - `test_unit.py:2788-2790` ("a fresh invocation has no plugin and no ghost") still passes, and `BROWSER_CONTROL_PLUGIN_PATH` still fully replaces the search path (`plugins.py:78-92`).

### F-5.6 No named seam between plugins and core verbs: the documented contract is "import anything in `lib`"

- category: lib-module | naming | test-coupling
- locations: `plugins/README.md` — “## The contract” (“Everything in `browser_control.lib` is importable”), `browser_control/lib/plugins.py:8-19` and `:43-46` (the `PLUGIN` example and the TRUST paragraph), `plugins/x_reader.py:26-29` (imports), `plugins/x_reader.py:174, 178, 183, 185` (core calls), `tests/test_unit.py:2827-2829`, `tests/test_unit.py:2848-2850`
- evidence:
  - `x_reader.py` imports `browser_control.lib.browser as browser_lib`, `browser_control.lib.dom`, `browser_control.lib.errors` and calls `browser_lib.nav(...)`, `dom.wait("idle"|"element", …)`, `dom.extract(...)` — public names only, so the *problem is breadth, not privacy*: every one of `browser.py`'s ~120 top-level functions and `dom.py`'s surface is promised to plugin authors.
  - the plugin is not handed the call context it needs: `run(rest, browser)` (`plugins.py:14`, `x_reader.py:144`) receives argv-after-globals and the `--browser` value, but the **instance scope and frame are only reachable through globals** (`browser.SCOPE` read inside `_writable_profile` at `browser.py:637`, `dom.frame()` read at `main.py:1271`), and `--frame` is refused for plugin verbs (`main.py:1245-1257`).
  - the coupling is visible in the test suite: `t_x_plugin_offline` rebinds `browser.nav`, `dom.wait`, `dom.extract` as module attributes to drive the plugin (`test_unit.py:2801`, `:2827-2829`, restored `:2848-2850`) — the only way to substitute the core verbs.
  - the contract prose is duplicated in three places: `plugins.py:1-46`, `plugins/README.md`, `README.md:153-172`, so a signature change requires three edits and can still leave a lesson in a docstring.
- proposal: add `browser_control/plugin_api.py` (top-level module, no packaging change needed) with `__all__` naming the supported seam (`PLUGIN_API`, `fail`, `ControlError`, `nav`, `wait`, `extract`, and — if the architect accepts it — a `PluginContext` carrying `tab`, `browser`, `frame`), re-exporting today's functions unchanged; migrate `plugins/x_reader.py:26-29` to import from it; point `plugins/README.md`'s contract section at it. Keeping `run(rest, browser)` means no installed plugin breaks; a context object would ride the existing `api` counter (`plugins.py:57`, refusal at `:150-153`).
- dependencies: F-5.4; Open questions 2
- risk: medium — anything that changes the plugin call signature is a documented-contract change, which is why the seam module is the low-risk half of this proposal.
- acceptance:
  - `python3 -c "import browser_control.plugin_api as m; assert m.__all__"` succeeds.
  - `grep -c "from browser_control.lib" plugins/x_reader.py` == 0.
  - `tests/test_unit.py::t_x_plugin_offline` still green with `BROWSER_CONTROL_PLUGIN_PATH=$PWD/plugins`.

### F-5.7 Help and dispatch for plugins are assembled in the CLI from registry internals (and built-in help has no drift test)

- category: duplication | lib-module
- locations: `browser_control/cli/main.py:50-172` — the hand-written `USAGE` block, `browser_control/cli/main.py:945` `TAB_SUBCOMMANDS`, `:976` `PROFILE_SUBCOMMANDS`, `:982` `HANDLERS`, `browser_control/cli/main.py:1162-1168` `_verb_names`/`_plugins_report`, `browser_control/cli/main.py:1181-1183`
- evidence:
  - plugin usage lines are generated from the spec (`main.py:1182-1183` reads `spec['usage']`) while ~120 lines of built-in usage are prose in `USAGE` (`main.py:50-172`) that restates `HANDLERS`/`TAB_SUBCOMMANDS`/`PROFILE_SUBCOMMANDS` by hand — two mechanisms for one user-facing table, and the only test is `out.startswith("usage: browser-control-cli")` (`test_unit.py:1406`).
  - `_verb_names()` (`main.py:1164`) merges built-ins and plugin verbs only for *error text* (`main.py:1197`, `:1209`, `:1242`); dispatch does a second, separate lookup (`main.py:1235-1239`), so there are three views of "what verbs exist" (`HANDLERS`, `_verb_names`, dispatch).
  - the gate resolves plugin actions to the bare verb (`main.py:1147-1158` returns `verb` for anything that is not `tab`/`profile`), so a plugin's subcommands cannot carry their own classes while built-ins can (`capabilities.ACTIONS["tab dialog accept"]`, `test_unit.py:1592`).
- proposal: give `Registry` the surface API from F-5.4 (`verb_names()`, `usages()`, `classes()`, `report()`), have `main` print built-in help from a generated table keyed off `HANDLERS`/`TAB_SUBCOMMANDS`/`PROFILE_SUBCOMMANDS` with the existing prose as the short help strings, and add a contract test mirroring `test_unit.py:1567-1582`: every handler and subcommand has a usage line. No output text needs to change to make this testable.
- dependencies: F-5.4; the exact help text is user-visible (freeze it if it is considered contract)
- risk: low if the generated text is diffed against the current `USAGE` before landing.
- acceptance:
  - `USAGE` is produced from the tables (no second copy of a verb name in a string literal).
  - a new test asserts `set(HANDLERS) ⊆ usage_verbs` and `set(TAB_SUBCOMMANDS) ⊆ usage_tab_subcommands`; `--help` output byte-identical to today.

### F-5.8 `_tree` conflates the manifest with the seed skip policy; `profile info` reports a weight that excludes the skips

- category: hidden-state | lib-module
- locations: `browser_control/lib/profile.py:55-64` — `SEED_SKIP`, `browser_control/lib/profile.py:67` — `def _tree(source: str, count_skips: bool = False) -> dict`, `:96-100` skip branch, `:297` — `facts = _tree(path)` inside `info`, `:310-311` — `"bytes"/"files"`, `:146-148` — `_copy` calls `_tree(source)`, `:494` — `_tree(target, count_skips=True)` in `reset`
- evidence:
  - `_tree`'s skip test is against the global `SEED_SKIP` (`profile.py:96`), and its docstring states the coupling as intended: “`seed` keeps the default: it copies nothing from there, so those bytes are not part of its manifest” (`profile.py:81-82`).
  - `info` calls `_tree(path)` with the default (`profile.py:297`) and publishes `facts["bytes"]`/`facts["files"]` (`:310-311`) although `info`'s docstring promises “its weight” (`profile.py:279-283`) — `Cache`, `Code Cache`, `GPUCache`, `Crashpad`… (`profile.py:58-63`) are invisible, and unlike `seed`'s reply (`profile.py:436` `"skipped"`) the row has no field saying so.
  - `reset` had to opt *in* (`profile.py:494`, `count_skips=True`) to stop wiping a 500 MB cache as “0 files, 0 bytes” — the fix recorded as item 13 in `docs/review-2026-09-20.md:184` — which shows the flag is really “which policy applies to this verb”, not a walker option.
  - the accumulator is an untyped 9-key dict (`profile.py:84-86`) mutated from two functions (`:93-119` in the walk, `:172` `facts["links_planted"] += 1` in the copy), and read by three verbs (`:310-311`, `:434-441`, `:494-496`, `:515`).
- proposal: `browser_control/lib/seedtree.py :: @dataclass class TreeFacts` (bytes, files, dirs, links, special, links_planted, unreadable, skipped, entries) plus `walk(source, *, skips: frozenset[str])` and `copy_verified(source, target, dry, *, skips)`; every call site names its policy explicitly (`profile.py:148`, `:297`, `:494`). Structural only: `SEED_SKIP` stays the seed/copy policy.
- dependencies: F-5.1, F-5.9
- risk: low as a refactor; medium only if the architect also changes what `info.bytes` means (see Open questions 1).
- acceptance:
  - no module-level set is consulted inside the walker (`walk` receives `skips`).
  - `reset`'s guard case `t_reset_guard_counts_skipped_content` (`test_unit.py:2505-2515`) still green.
  - `profile info` JSON keys unchanged (`profiles[].bytes`, `files`, `managed`, `attached`, `default_for`, `modified`, optional `live`).

### F-5.9 Package split: `lib/profile.py` and `lib/plugins.py` should become packages, and `pyproject.toml` constrains how

- category: package-split | naming
- locations: `browser_control/lib/profile.py` (521 lines, 13 top-level functions at `:67, :123, :146, :190, :197, :212, :222, :252, :269, :278, :331, :354, :461`), `browser_control/lib/plugins.py` (195 lines), `pyproject.toml:28` — `packages = ["browser_control", "browser_control.lib", "browser_control.cli"]`, `browser_control/lib/__init__.py`, `browser_control/cli/__init__.py`, `browser_control/__init__.py:8` — `__version__ = "0.1.0"`, `pyproject.toml:31` — `version = { attr = "browser_control.__version__" }`, `docs/plan.md:135` — “`__init__.py` exposing `BrowserService`”
- evidence:
  - `lib/profile.py` holds five distinct responsibilities in one module: instance resolution (`:222-276`), liveness (`:252-276`), the tree walk/manifest (`:67-121`), the copy+verify engine (`:146-188`, `:123-144`), and three verb adapters (`:278`, `:354`, `:461`) — with the locking choreography interleaved (F-5.2).
  - `lib/plugins.py` mixes four: search-path policy (`_dirs`, `:78-92`), importing (`_import`, `:123-138`), spec validation (`_register`, `:140-195`) and registry state (`Registry`, `:63-76`).
  - packaging is an **explicit** list (`pyproject.toml:28`), not `find`/auto-discovery, so a new subpackage that is not added there is silently absent from an installed wheel while still working from a checkout — this is the constraint that makes the split orderable.
  - `lib/__init__.py` and `cli/__init__.py` are docstring-only (correct: no re-export cycles), and `browser_control/__init__.py` exports only `__version__` (`:8`), which is a packaging hook (`pyproject.toml:31`) plus one CLI read (`cli/main.py:23`). There is **no `__all__` anywhere in the package** (`grep -rn "__all__" browser_control` → none) and no `BrowserService`, so `docs/plan.md:135` and `:16` describe an `__init__` surface that does not exist; `plugins/README.md` nevertheless promises plugin authors the whole of `lib` (F-5.6).
- proposal: split into `browser_control/lib/profile/` (`__init__.py` re-exporting `info/seed/reset` so `cli/main.py:901-935` and `tests/test_unit.py:2431-2511` are untouched, plus `instance.py` (F-5.1), `seed.py` (F-5.8 engine), `records.py` (F-5.3 store if not placed package-wide)) and `browser_control/lib/plugins/` (`__init__.py` `Registry`+`load`, `discovery.py`, `spec.py`); add every new subpackage to `pyproject.toml:28`; keep `lib/__init__.py`, `cli/__init__.py` docstring-only; keep `browser_control/__init__.py` to `__version__` (do **not** add `BrowserService` re-exports unless a caller appears — the plan line is stale); add the named plugin seam as `browser_control/plugin_api.py` (F-5.6).
- dependencies: F-5.1, F-5.2, F-5.3, F-5.4, F-5.6
- risk: medium — a package split changes import paths used by tests (`tests/test_unit.py:32-41`) and by nothing else; forgetting `pyproject.toml:28` ships a broken install.
- acceptance:
  - `python3 -m pip install .` then `python3 -c "import browser_control.lib.profile, browser_control.lib.plugins"` from a directory outside the repo succeeds (proves `pyproject.toml:28` is complete).
  - `python3 tests/test_unit.py` green with `tests/test_unit.py:32-41` **unmodified** (the `profile as profile_lib` import and the `info/seed/reset` calls must keep working).
  - `browser-control-cli selftest` output unchanged.

## Proposed target layout for this slice

- `browser_control/lib/instance.py` — `Instance` value: scope/named/default resolution, `managed`, `live_pid()`, `refuse_live()` (F-5.1).
- `browser_control/lib/locks.py` — `Lock` dataclass + root→profile lock context manager with warning aggregation (F-5.2).
- `browser_control/lib/attachments.py` — `AttachmentStore` over `attached.json`; sole writer of the record file (F-5.3).
- `browser_control/lib/seedtree.py` — `TreeFacts` + `walk(...)` / `copy_verified(...)` with an explicit skip policy (F-5.8).
- `browser_control/lib/profile/__init__.py` — re-exports `info/seed/reset` (signatures and JSON unchanged).
- `browser_control/lib/plugins/__init__.py` — `Registry`, `PluginAction`, `load()` (F-5.4).
- `browser_control/lib/plugins/discovery.py` — search-path policy and candidate file ordering (`plugins.py:78-92`).
- `browser_control/lib/plugins/spec.py` — `PLUGIN` dict validation → `PluginAction` / `PluginError` (`plugins.py:140-195`).
- `browser_control/lib/argv.py` — `take_value` / `take_switch` / `take_int` shared by `cli/main.py` and `plugins/x_reader.py` (F-5.6/next lane seam).
- `browser_control/plugin_api.py` — the named, `__all__`-ed plugin seam (`fail`, `ControlError`, `nav`, `wait`, `extract`, `PLUGIN_API`).
- `browser_control/cli/invocation.py` — `Invocation(verb, rest, flags, browser, plugins)` replacing `global PLUGINS`.
- `pyproject.toml:28` — list every new subpackage; `packages` stays explicit.
- Unchanged on purpose: `browser_control/__init__.py` (`__version__`), `lib/__init__.py`, `cli/__init__.py` (docstring-only), `browser_control/__main__.py`, `browser-control-cli`, `plugins/x_reader.py`’s behaviour.

## Open questions

1. Does `profile info`’s `bytes`/`files` mean *weight* (all files, `Cache` included) or *what `seed` would copy*? `profile.py:297` currently answers the second while the verb’s docstring promises the first (`profile.py:279-283`); F-5.8 makes the policy explicit but deliberately does not change the number. Decide before the split lands.
2. Should the plugin contract stay import-based (`plugins/README.md`: “Everything in `browser_control.lib` is importable”) or move to a context object under a bumped `api: 2`, with the registry accepting both (`plugins.py:57`, refusal at `:150-153`)? This is the only proposal that changes a documented contract, so it needs an explicit decision.
3. `lib/profile/` as a package vs keeping `lib/profile.py` and extracting `lib/seedtree.py` only — the package is cleaner for F-5.1/F-5.3 but costs a `pyproject.toml:28` entry and an `__init__` re-export surface.
4. Should `capabilities` stop owning `PLUGIN_ACTIONS` as a module global (`capabilities.py:138-149`, read at `:284`) in favour of a gate object constructed with the registry? That is a cross-lane change (gate/argv lane owns `capabilities.py`); F-5.5 can be satisfied inside `cli/main.py` alone.
5. Plugins can declare capability classes only per top-level verb (`main.py:1147-1158` returns the bare verb), while built-ins declare per-subcommand/mode (`capabilities.ACTIONS["tab dialog accept"]`). Extend the spec shape or leave the asymmetry documented?
6. Where should the plugin seam live: `browser_control/plugin_api.py` (top level, installable, no packaging change) or `browser_control/lib/plugin_api.py` (inside the tier it wraps)?