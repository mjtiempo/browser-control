I have completed the review. Note: no write-capable tool is available in this role, so the artifact is returned in full below (the runtime should persist it to `/home/mark/Documents/pidev/mark-browser-control/research/current-tests-review.md`). No file was edited.

---

# Review — test suites of `browser-control-cli`

**Targets.** `tests/test_unit.py` (4846 lines, ~102 `check()` entries) and `tests/live_test.py` (2483 lines, 60 `CHECKS`). Contract sources read: `README.md`, `docs/progress.md`, and the code under test (`browser_control/lib/**`, `plugins/**`).

**Method.** Every finding below is grounded in (a) a raise site in the library with its line, and (b) the absence of any assertion for it in either suite. Coverage claims were checked by grepping both suites for each refusal code and by reading the checks that claim to cover each verb. No test was executed (no run access); commands a supervisor should run are listed at the end.

## Correct (what is already good — with evidence)

- **The page-independent oracles are genuinely used.** PNG IHDR bytes vs the page's geometry (`tests/live_test.py:1111-1132`), `/proc/<pid>/cmdline` for headless/extensions (`tests/live_test.py:1316-1410`), raw socket + direct `/json` reads (`tests/live_test.py:296-322`), and the on-disk action log for redaction (`tests/live_test.py:958-977`) are all real oracles, not the reply talking about itself.
- **Negative controls exist where they matter most.** `_no_dialog` both ways (`tests/test_unit.py:446-449`), `_checkable` three ways (`tests/test_unit.py:450-453`), the tri-state text verdict (`tests/test_unit.py:1395-1404`), the media clock-verdict (`tests/test_unit.py:1524-1552`), the strictness tripwire (`tests/test_unit.py:1634-1672`), the fatal-handler tripwire (`tests/test_unit.py:4491-4517`), and the held-lock/free-lock pair (`tests/test_unit.py:1957-1984`).
- **The argv layer is pinned as thoroughly as the service layer** (every `tab` subcommand's flags, arity, and refusals), and `t_error_codes_are_registered` (`tests/test_unit.py:4065-4097`) closes the "typo'd code" class by scanning the tree.

## Fixed

None — this is a read-only review.

---

## Findings

### P0-1 — The *refusal half* of the mutation read-back is untested for the whole text/form family: a lost verification would pass the suite

**Location (test seam):** `tests/test_unit.py:1383-1406` (`t_keys_and_verdicts`) tests only the *pure* `dom._text_verdict`; nothing calls the verb-level decision. **Location (code):** `browser_control/lib/dom/text_input.py:136-147` is the only place the verdict becomes a refusal, and nothing in `tests/test_unit.py` or the six live insert/type checks (`tests/test_unit.py:1455-1522`; `tests/live_test.py:908-924`) ever drives `dom.insert`/`dom.type_text` with a read-back that says "no".

**Evidence.** `tests/test_unit.py:1395-1404` asserts `_text_verdict` returns `False` for an unchanged field, but the code that consumes it — `fail(code, …)` at `text_input.py:144-146` — is never reached by any check. The one test that asserts `ERR[type-not-verified]` (`tests/test_unit.py:4039-4042`) exercises the *google plugin's* own guard (`plugins/google_search.py:139-143`), not the core verb. The same shape holds for `check` (`lib/dom/actions.py:354-356`), `hover` (`actions.py:289-291`), `upload` (`actions.py:571-574`) and `select` (`actions.py:446-448`): all five are driven only on the success path.

**Consequence.** A regression that made `_text_reply` always report `verified: true` (or that swallowed the `fail`) leaves both suites green — the stance "a mutation is never `ok: true` on a bare acknowledgement" (`README.md`, "The stance") breaks silently for the verbs that carry passwords.

**Smallest fix.** In `tests/test_unit.py`, call the shared reply builders directly with a read-back that says "unchanged" and require the refusal: `refusal(lambda: dom._text_reply(row, tab_row, before, dict(before), "hello", "insert"), "insert-not-verified")` and the `"type"` rung for `type-not-verified`, plus the analogous `_checkable`/hover-probe/`upload` probe with a mismatch.

### P0-2 — `tab screenshot`'s verification refusals and its "nothing was written" promise are untested

**Location (code):** `browser_control/lib/dom/screenshot.py:59-72` — three refusal branches (`dpr` non-finite, `png_size` empty, `size != expected`) each claim "nothing was written". **Location (test seam):** only the *pure helpers* are pinned (`tests/test_unit.py:428-443` for `png_size`/`expected_pixels`, `tests/test_unit.py:3535-3549` for `write_atomic`), and live only exercises the success path (`tests/live_test.py:1111-1132`).

**Evidence.** Grepping both suites for `screenshot-not-verified` matches only the pure-helper calls at `tests/test_unit.py:437-443`. No check asserts that a mismatch leaves *no file on disk* — the exact behaviour the README calls one of the two page-independent oracles ("the screenshot's own PNG bytes", `README.md` §"The stance").

**Smallest fix.** A hermetic check with a fake session (like `tests/test_unit.py:2668-2706`) whose `Page.captureScreenshot` returns bytes that disagree with the reported metrics; assert `ERR[screenshot-not-verified]` **and** `not os.path.exists(target)`.

### P0-3 — `profile seed`'s read-back refusals are untested: a partial copy could report `verified: true`

**Location (code):** `browser_control/lib/profile/seed.py:169-180` (`seed-not-verified` for an unreadable directory and for a file that did not land, via `seedtree.missing`, `lib/seedtree.py:101-118`). **Location (test seam):** the four hermetic seed checks (`tests/test_unit.py:3408-3517`) and live `c_profile_verbs` (`tests/live_test.py:1988-2080`) all assert *success*, including `reply["verified"] is True`.

**Evidence.** `grep -n "seed-not-verified" tests/` → no match. The README's rule for this verb is exactly "what landed is read back … a mismatch refuses `seed-not-verified`" and `docs/progress.md` §5.22 repeats it as a headline ("Every file the source has (minus the skips) is checked for in the target by size"). A regression that skipped `missing()` would be invisible.

**Smallest fix.** Hermetic: build a source whose file is unreadable (`chmod 0o000` on a subdirectory) and require `seed-not-verified`; separately call `seedtree.missing(entries, src, target)` with a truncated target and assert the relative path is returned.

---

### P1-1 — A missing `node` turns the JavaScript-compile check into a PASS (`skip ≠ pass`)

**Location:** `tests/test_unit.py:492-494` returns normally when `shutil.which("node") is None`; `check()` (`tests/test_unit.py:87-96`) records a normal return as `PASS`. The summary (`tests/test_unit.py:4838-4841`) then reads "N passed, 0 failed".

**Evidence.** Contrast with the live suite, which records `SKIP` and exits 2 (`tests/live_test.py:282-293`, `tests/live_test.py:2470-2476`) precisely because `docs/progress.md` §2 says "skip ≠ pass". The hermetic docstring at `tests/test_unit.py:486-491` even says the suite "stays hermetic everywhere" — i.e. a machine without node reports a green run for a check that did not run.

**Smallest fix.** Make the absent-node case raise (or register the check as SKIP and make the process exit non-zero), and add the `node` prerequisite to `docs/progress.md` §7 / the CI file.

### P1-2 — The live battery inherits `BROWSER_CONTROL_ALLOW`/`DENY`/`PLUGIN_PATH` and asserts an exact policy dict

**Location:** `tests/live_test.py:226-232` — `env()` returns `{**os.environ, …}` and never pops `BROWSER_CONTROL_ALLOW`, `BROWSER_CONTROL_DENY` or `BROWSER_CONTROL_PLUGIN_PATH`. `tests/live_test.py:1556-1560` then asserts `caps["policy"] == {"allow": [], "deny": ["read"], …}` exactly.

**Evidence.** The hermetic suite explicitly pops those two variables (`tests/test_unit.py:4694-4695`) and pins the plugin path (`tests/test_unit.py:4692-4693`) for exactly this reason; the live suite does neither. On a host that exports `BROWSER_CONTROL_ALLOW=read` (the case the project's own README advertises as a session-wide policy) every write check fails spuriously, and `c_policy_gate`'s exact-dict assertion fails even before that.

**Smallest fix.** Pop both policy variables in `env()` (or in `main()`) and pass policy only through `env_extra`, and set `BROWSER_CONTROL_PLUGIN_PATH` to an empty temp dir the way `tests/test_unit.py:4692` does.

### P1-3 — `cdp.evaluate_until` has no test at all

**Location:** `browser_control/lib/cdp/session.py:306-341`. The one-connection poll loop, its `blocked`-when-parked refusal (`session.py:322-325`) and its "a sample that did not answer is what the loop is FOR" branch (`session.py:333-338`) are never exercised. `tab wait`'s live check (`tests/live_test.py:814-835`) covers only success and `wait-timeout`.

**Smallest fix.** Reuse the fake websocket peer from `tests/test_unit.py:2578-2654`: have it answer a `Runtime.evaluate` with an error envelope on the first frame and a truthy value on the second, and assert `(value, samples)`; then have it answer only `Page.enable` with the blocked error and assert `ERR[blocked]`.

### P1-4 — Twenty-plus refusal codes are never asserted anywhere; for most, only the success path is covered

**Evidence (grep over both suites).** The following codes appear in no assertion in `tests/test_unit.py` or `tests/live_test.py`, while each has a reachable raise site:

| code | raise site |
| --- | --- |
| `activate-not-verified` | `lib/browser/nav.py:336` |
| `nav-not-verified` | `lib/browser/nav.py:237,242,290,294` |
| `reload-not-verified` | `lib/browser/nav.py:361,366` |
| `scroll-not-verified` | `lib/dom/scroll.py:162,185,225` |
| `dialog-not-verified` | `lib/dom/dialog.py:129` |
| `check-not-verified` | `lib/dom/actions.py:355` |
| `hover-not-verified` | `lib/dom/actions.py:193,290` |
| `upload-not-verified` | `lib/dom/actions.py:572` |
| `ambiguous-element` | `lib/dom/queries.py:110` |
| `ambiguous-option` | `lib/dom/actions.py:447` |
| `no-viewport` | `lib/dom/pagedata.py:39` |
| `eval-timeout` | `lib/cdp/session.py:185` |
| `write-failed` | `lib/images.py:103,114,125,129,137` |
| `browser-not-stopped` | `lib/browser/lifecycle.py:721,730,742,747,756` |
| `profile-unusable` | `lib/browser/lifecycle.py:577` |
| `attach-failed` | `lib/attachments.py:132,136,146` |
| `seed-not-verified` | `lib/profile/seed.py:171,178` (P0-3) |
| `reset-failed` / `reset-not-verified` | `lib/profile/reset.py:89,92`; `lib/profile/seed.py:158,162` |
| `close-tab-not-verified` (survivor) | only the unreadable-list case is tested, `tests/test_unit.py:3195-3211` |
| `media-not-verified` | only inside an either/or at `tests/live_test.py:1019` |

Note the pattern: for `activate`, `nav`, `reload`, `scroll`, `dialog`, `check`, `hover`, `upload` the *success* assertion exists (`tests/live_test.py:1027,652,687,871,1134,1068,1049,940`) but the "the read-back said NO" branch — the branch that makes the verb a verification rather than an acknowledgement — does not. Several are trivially reachable live: `tab nav` to a page the browser ignores, `tab scroll --by` on a document with no overflow, `tab click` on a selector that matches two visible elements (the DOM fixture has no such pair, so `ambiguous-element` is unreachable as written, `tests/live_test.py:73-125`).

**Smallest fix.** Add one hermetic check per branch with a fake session (the pattern at `tests/test_unit.py:2668-2706` already exists), prioritising the nav/scroll/check/hover/upload/activate family; add a two-visible-match element and a second matching `<option>` to `DOM_FIXTURE` (`tests/live_test.py:74-125`) for `ambiguous-element`/`ambiguous-option`.

### P1-5 — Plugin failure modes are only half covered; the fail-closed classes rule is not tested

**Location:** `browser_control/lib/plugins/spec.py:64-104`. **Test seam:** `tests/test_unit.py:3759-3814` covers exactly three of the eight `PluginError` kinds: import failure, name collision with a built-in, and `api 99`.

**Evidence.** Untested branches: missing `PLUGIN` dict (`spec.py:65-68`), empty name (`spec.py:72-75`), no actions (`spec.py:76-79`), an invalid verb name (`spec.py:83-88`), a spec with no callable `run` (`spec.py:92-95`), and — the security-relevant one — an action declaring **no** classes or an unknown class (`spec.py:96-104`, "a plugin action is refused unless it declares at least one capability class the gate knows", `spec.py:9-11`). Two plugins colliding with *each other* (`taken` grew by the built-in table only in the existing check) is also untested. `--help` appending plugin usage (`docs/progress.md` §5.27) has no assertion either.

**Smallest fix.** Add one plugin file per branch to the fixture directory at `tests/test_unit.py:3774-3783` and assert the `plugin_errors` kind string (`schema`, `verb-name`, `run`, `classes`) appears.

### P1-6 — `AttachmentStore` (the file that opens the tab-write gate) is only touched through the happy path

**Location:** `browser_control/lib/attachments.py:40-48` (`records()` returns `{}` for a malformed/absent file — "an attachment that cannot be read is not an authorization"), `:124-136` (the `attach-failed` `.new` scratch guard), `:66-80` (`drop` by pid/port).

**Evidence.** `tests/test_unit.py:741-809` (`t_attach_bookkeeping`) exercises `attach`/`detach(profile=…)`/`is_attached` only; `grep -n "attached.json\|attach-failed" tests/` finds nothing. The "malformed file is not an authorization" property is a security boundary with no test.

**Smallest fix.** Hermetic: write `{"not": "a list"}` and `[]` at `<root>/attached.json`, assert `browser.is_attached(profile)` is `False` and a `tab` write still refuses `not-managed`; plant a leftover `<root>/attached.json.new` and assert `attach-failed`.

---

### P2-1 — Weak assertion: the media refusal accepts two codes and a word-match

`tests/live_test.py:1019`: `assert "ERR[media-blocked]" in err or "ERR[media-not-verified]" in err, err` and `assert "nothing to play" in err or "refused" in err, err`. The specific cause→code mapping (`lib/dom/media.py:150` vs `:156`) can regress unnoticed. Assert the code that matches the state the fixture is actually in (the source-less `<video>` page, `tests/live_test.py:214-218`) and drop the `"refused"` alternative.

### P2-2 — Weak assertion: the planted-symlink screenshot check swallows the refusal

`tests/test_unit.py:3543-3548`: `with contextlib.suppress(ControlError): images_lib.write_atomic(…)` with the comment "the refusal is the fix's answer". The `write-failed` code is never asserted, and the leftover-temp branch (`lib/images.py:124-128`) is not reached at all (a symlink at the temp name raises `ELOOP`, not `FileExistsError`). Use `refusal(lambda: …, "write-failed")` and add a second case with a *real* file at `f"{target}.bc-{os.getpid()}.part"`.

### P2-3 — Weak assertion: two `ControlError` sites assert the message but not the code

`tests/test_unit.py:2529-2536` (`t_frames_and_points`, `dom._point`) and `tests/test_unit.py:4255-4263` (`t_frame_refusal_flattens_the_url`) both `try/except ControlError` and assert text only. Replace with the `refusal()` helper plus a message assertion, so the code cannot drift to `bad-args`/`internal` unnoticed.

### P2-4 — Vacuous-by-construction check

`tests/test_unit.py:3180-3193` (`t_tab_count_does_not_refuse_on_a_stranger`) asserts `browser._tab_count() == 0`. A `_tab_count` that returned `0` unconditionally would pass. Add the positive half: one verified row with 2 tabs ⇒ `2`, then the stranger row ⇒ `0`.

### P2-5 — Order dependence / masked flakiness: the hover retry

`tests/live_test.py:1049-1066` catches `AssertionError` and re-attempts with a two-step move, because "a previous check's click can leave [the pointer] there". That is a real cross-check state leak (the pointer is global to the browser); the check documents it rather than fixing the ordering (the fixture already has `tests/live_test.py:1057` as the workaround). Prefer moving the pointer in a `#dom` navigation helper before any hover/click check, or make the retry explicit in the check name/state so the tolerance cannot silently absorb a genuine first-attempt failure.

### P2-6 — Wall-clock assertion inside a live check

`tests/live_test.py:1151`: `assert took < 8` after a click that raises a dialog. It is a timing bound on a loaded machine (the suite's own §5.2 records flakes from assuming an idle machine). The refusal's own text already proves the grace path; drop the bound or raise it well above the refusal budget.

### P2-7 — Missed oracle: `tab upload` asserts the CLI's reply, not the page's own file list

`tests/live_test.py:940-951` asserts `reply["input"]["files"]`. The fixture page is reachable (`tests/live_test.py:74-125`), so `tab js "document.querySelector('#upload').files[0].size"` reads the same fact through a different expression path and cannot be satisfied by a read-back bug. Same family: `c_dom_media_state_play_pause` (`tests/live_test.py:989-1006`, clock via the reply; `tab js "document.querySelector('#v').currentTime"` is independent) and `c_dom_scroll_moves_the_document_and_nested` (`tests/live_test.py:871-894`, `window.scrollY`).

### P2-8 — Missed oracle: the pid record on disk is never read in the live flow

`tests/live_test.py:577-603` (`c_open_starts_a_browser`) verifies pid/port/`/proc` but never `<profile>/.pid`; the record is only touched in-process in `c_close_ignores_a_recycled_pid` (`tests/live_test.py:1697-1729`). Assert `Path(profile, ".pid").read_text()` equals the reported pid after `open`, and is gone after `close` — a disk oracle for record_pid/`_remove`.

### P2-9 — Missed oracle: login-store counts come from the reply

`tests/test_unit.py:3408-3432` (`t_seed_reads_logins_back`) and `tests/live_test.py:2082-2123` (`c_profile_logins`) build a SQLite fixture and then assert the reply's counts. Re-opening the copied `Cookies` with `sqlite3` and counting rows is an independent read of the same fact and would catch a census regression that reads the reply's own bookkeeping.

### P2-10 — Test-quality: temp-root litter and a glob that can race the live suite

`tests/test_unit.py:4686-4687` creates `browser-control-hermetic-*` (and `:4692` a plugins dir) and never removes them; `tests/test_unit.py:1776-1810` (`t_a_working_log_makes_no_scratch_dirs`) globs `/tmp` for audit scratch dirs and would fail if a concurrent battery created one in the window (the comment acknowledges it). Remove the hermetic roots in a `finally`, and make the glob check compare within the suite's own scratch directory.

### P2-11 — Test-quality: the live suite's own HTTP oracle honours `http_proxy`

`tests/live_test.py:296-302` uses `urllib.request.urlopen`, whose default opener installs the environment-driven `ProxyHandler`; the suite that proves the *code under test* refuses a proxy (`tests/test_unit.py:2793-2819`) can itself be routed through one whenever `http_proxy` is exported without `no_proxy=127.0.0.1`. Use an opener with `ProxyHandler({})`, as the library now does.

### P2-12 — Gap (report-only): `seedtree.walk`'s `unreadable`/`special`/`links` counts and `has_content(ours=…)` have no direct test

`browser_control/lib/seedtree.py:66-133` and `:173-186`. `links_planted` is asserted (`tests/test_unit.py:3513-3516`) but `links`, `special`, `unreadable` and the `ours` filter (the reason a fresh profile does not refuse `profile-exists`, `seedtree.py:180-186`) are not. A fifo in a fixture plus a `chmod 0o000` subdirectory pins all four cheaply.

### P2-13 — Gap (report-only): `instance_locks` order and `skip_profile_lock` are not asserted

`browser_control/lib/locks.py:159-176`. The hermetic lock checks (`tests/test_unit.py:1957-2067`) exercise `profile_lock` only; the root→profile ordering and the `--dry` skip are only exercised indirectly through `profile.seed`. Assert the lock names actually held during a `seed --dry` (no profile lock file created) and during a real `seed` (both).

### P2-14 — Gap (report-only): `coerce.as_ints`' limit semantics

`browser_control/lib/coerce.py:52-63`: `count=0` means "no limit" (the documented footgun). `tests/test_unit.py:1120-1138` covers `count=2` and non-list inputs but never `count=0`/`count=None`, which is the exact behaviour `docs/progress.md` §5.28 says was fixed.

### P2-15 — Gap (report-only): `tab media --index` selection among several players

`tests/live_test.py:73-125` gives the media page exactly one `<video>`, so the "several players are picked by rule (the playing one, else the largest) with `--index` to choose" path (`docs/progress.md` §5.7, `lib/dom/media.py:138-144`) and an out-of-range `--index` are untested.

---

## Merge verdict

**OK with notes.** No P0 issue is a *bug in the product*; each P0 is a place where the suite would stay green if the verification the project is built on were removed, which is exactly the class this review was asked to find. The suite is well above average in oracle discipline; the concentrated weakness is that **success paths are tested and read-back failures are not**, and that a whole check can silently degrade to PASS (`node`). Recommend fixing P0-1…P0-3 and P1-1…P1-4 before the next release; the rest are report-only.

**Commands a supervisor should run** (I cannot execute them):
- `python3 tests/test_unit.py` — confirm the reported pass count and whether the node check actually ran (`PASS` vs the `SKIP` note at `tests/test_unit.py:493`).
- `BROWSER_CONTROL_ALLOW=read python3 tests/live_test.py` — demonstrates P1-2 (expected: many spurious failures, and `c_policy_gate`'s exact-dict assertion failing).
- `python3 tests/live_test.py` with the grep list above to confirm which refusal codes are reachable live.