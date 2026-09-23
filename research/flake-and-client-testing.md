# Flake elimination and client self-testing: how production browser-automation frameworks do it

Scope: what Playwright, Puppeteer, Selenium, Chromium and Google-scale CI actually
implement (docs + source, fetched — not snippets), mapped to `browser-control`'s
modules (`lib/cdp/*`, `lib/browser/*`, `lib/dom/*`, `lib/poll.py`,
`lib/policy.py`, `tests/test_unit.py`, `tests/live_test.py`).

Convention below: **direct evidence** = I fetched the file above. **Interpretation**
= my read applied to this CLI.

---

## Findings

### 1. Actionability and auto-waiting

**Why it is hard.** "The element exists" and "the element's protocol call returned"
are both near-worthless predictors of "the user-visible effect happened". An element
can be present but `display:none`, mid-animation, covered by an overlay, disabled, or
read-only. A raw CDP `Input.dispatchMouseEvent` to a point succeeds regardless —
the ack is L1 evidence that proves nothing about the effect.

**Direct evidence — Playwright's pre-action checks.** Playwright "performs a range of
actionability checks on the elements before making actions … and only then performs
the requested action. If the required checks do not pass within the given timeout,
action fails with the TimeoutError" (https://playwright.dev/docs/actionability).
For `locator.click()` it requires: resolves to **exactly one** element; **Visible**;
**Stable** ("maintained the same bounding box for at least two consecutive animation
frames"); **Receives Events** ("the hit target of the pointer event at the action
point" — i.e. nothing else captures the click there); **Enabled**. Per-action table
(also fetched): `check/click/dblclick/setChecked/tap/uncheck` → Visible+Stable+
ReceivesEvents+Enabled; `fill/clear/selectOption` → Visible+Enabled+Editable;
`screenshot` → Visible+Stable; `setInputFiles/dispatchEvent/focus/press` → none.
Definitions worth copying verbatim into this CLI's verdicts: **Visible = non-empty
bounding box and not `visibility:hidden`** — "*Elements with `opacity:0` are
considered visible*"; **Editable = enabled and not readonly**; **Enabled** is false
for `[disabled]` on form controls, controls inside a `[disabled]` `<fieldset>`, or a
descendant of `[aria-disabled=true]`.

**Direct evidence — Playwright's stable-check implementation** (fetched
https://raw.githubusercontent.com/microsoft/playwright/main/packages/injected/src/injectedScript.ts):
`_checkElementIsStable` runs inside a `requestAnimationFrame` loop, tracks
`getBoundingClientRect()` across frames, requires `stableRafCount` **consecutive equal
rects**, and *drops frames shorter than 15 ms* with the explicit comment "Drop frames
that are shorter than 16ms - WebKit Win bug." It re-reads the box each frame and
returns `'error:notconnected'` if the element detaches. This is the concrete algorithm
behind "stable".

**Direct evidence — Puppeteer's `waitFor*` family.** `Frame.waitForFunction` /
`waitForSelector` delegate to a `WaitTask` whose `polling` option is `'raf' |
'mutation' | number` and **defaults to `'raf'`**
(https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/common/WaitTask.ts).
Under the hood `RAFPoller` loops on `requestAnimationFrame`, `MutationPoller` uses a
`MutationObserver`, `IntervalPoller` uses a millisecond timer
(https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/injected/Poller.ts).

**Direct evidence — fixed sleeps are explicitly banned by the reference framework.**
Selenium's waiting-strategies page: "The first solution many people turn to is adding
a sleep statement … Because the code can't know exactly how long it needs to wait,
this can fail when it doesn't sleep long enough. Alternately, if the value is set too
high and a sleep statement is added in every place it is needed, the duration of the
session can become prohibitive." It then warns: **"Do not mix implicit and explicit
waits. Doing so can cause unpredictable wait times. For example, setting an implicit
wait of 10 seconds and an explicit wait of 15 seconds could cause a timeout to occur
after 20 seconds."** (https://www.selenium.dev/documentation/webdriver/waits/).

**Direct evidence — retry with jitter.** AWS's canonical post: naive capped
exponential backoff still produces synchronized "clusters of calls"; adding **jitter**
spreads them and "reduce[s] our call count by more than half … significantly improved
the time to completion". "Full Jitter" and "Decorrelated Jitter" are named as the two
competitive formulas (equal jitter is worse). "The return on implementation
complexity of using jittered backoff is huge, and it should be considered a standard
approach for remote clients."
(https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/).

**Concrete failure modes for this CLI.** (a) `tab click` hit-tests before but not
after bring-to-front, so a stale layout click lands on an overlay — overclaim.
(b) `tab find` returns rects without the hit test, so it reports a hidden/covered
element as actionable. (c) A hidden or animating element passes "exists" and the
read-back ("url changed") is attributed to the click when a pending navigation
changed it. (d) A single pre-action read without the two-frame stability rule clicks a
moving element at its old rect. **Interpretation:** the project's L4 `tab click`
(hit-test before, url/title/focus/scroll before-after; `occluded` /
`no-viewport-target`) is the Playwright model; the gap is the **stability** check
(two consecutive equal rects, ≥ one paint apart), which `lib/dom/*` should own, and
the `force`-style escape hatch must be declared `verified:false` exactly as
`lib/plan.md §4 rule 12` already requires for `js`/`press`/`--at`.

**Established best practice to name:** Playwright *auto-waiting / actionability
checks* (visible, stable, receives-events, enabled, editable) with a documented
`force` opt-out; Puppeteer `waitForFunction` polling modes; Selenium's
no-fixed-sleep / no-mixed-waits rules; AWS full/decorrelated jitter for any retry
loop.

---

### 2. Event-driven waiting

**Why it is hard.** You must pick a signal that means "the state I care about
settled" without a clock, and every in-page timer-based signal is subject to
browser throttling that the page (and the CLI) cannot see. Polling latency and
background throttling pull in opposite directions.

**Direct evidence — polling modes and defaults.** Playwright: "If `polling` is
`'raf'`, then pageFunction is constantly executed in `requestAnimationFrame`
callback. If `polling` is a number, then it is treated as an interval in milliseconds
… **Defaults to `raf`**." The same API grew a `signal AbortSignal` (v1.62) and a
`timeout` that "Defaults to 0 - no timeout"
(https://playwright.dev/docs/api/class-frame). Puppeteer's `WaitTask` implements the
three pollers above.

**Direct evidence — the throttle failure mode is real and bitten.** Playwright issue
#40568 ("Clarify that `waitForFunction` uses in-browser timers that can be throttled"):
the reporter's `waitForFunction` "is timing out at 1 minute and only seemingly called
**once** … since I'm running many tests in parallel, the browser might be throttling
all these polls (which seem to be implemented as simple in-browser `setTimeout`s) and
thus neglecting to run them." The issue was closed as completed
(https://github.com/microsoft/playwright/issues/40568).

**Direct evidence — why it is throttled.** MDN Page Visibility API: "Most browsers
**stop sending `requestAnimationFrame()` callbacks to background tabs or hidden
`<iframe>`s**"; "Timers such as `setTimeout()` are throttled in background/inactive
tabs"; browsers implement **budget-based background timeout throttling** (a time
budget that regenerates at ~10 ms/s; Chrome throttles after 10 s), and only tabs
"playing audio" or "using real-time network connections (WebSockets and WebRTC) go
unthrottled" (https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API).
Note the irony for this CLI: *the CDP control socket* keeps the tab unthrottled only
while a WebSocket is live, and the page's own rAF loop will still stall in a hidden
window.

**Direct evidence — MutationObserver mechanics.** Puppeteer's `MutationPoller`
observes `{childList:true, subtree:true, attributes:true}`, and — important, and easy
to get wrong — walks **shadow roots** ("`subtree` observation does not cross shadow
boundaries, so every shadow root needs to be observed on its own"), attaching
observers to newly-added shadow hosts, and prunes a mutation batch to its top-most
added nodes
(https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/injected/Poller.ts).

**Direct evidence — network-idle heuristics and their exact definitions.** Puppeteer's
`LifecycleWatcher` documents them literally: `networkidle0` = "no more than **0**
network connections for at least **500 ms**"; `networkidle2` = "no more than **2**
… for at least **500 ms**", mapped to the protocol lifecycle events `networkIdle`
and `networkAlmostIdle`
(https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/cdp/LifecycleWatcher.ts).
`page.waitForNetworkIdle` takes `idleTime` (default `500`) and `concurrency`
(default `0`) (https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/api/Page.ts;
docs https://pptr.dev/api/puppeteer.page.waitfornetworkidle). Playwright labels the
same heuristic **DISCOURAGED**: `'networkidle'` = "consider operation to be finished
when there are no network connections for at least 500 ms. **Don't use this method for
testing, rely on web assertions to assess readiness instead**"
(https://playwright.dev/docs/api/class-page).

**Direct evidence — execution-context readiness.** CDP delivers
`Runtime.executionContextCreated` / `executionContextDestroyed` /
`executionContextsCleared` (https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/json/js_protocol.json).
`Page.addScriptToEvaluateOnNewDocument` "Evaluates given script in every frame upon
creation **before loading frame's scripts**" (same file). Puppeteer's `WaitTask`
turns context churn into a retry rule: an error containing "Execution context was
destroyed" is *swallowed and re-run* ("When the page is navigated, the promise is
rejected. We will try again in the new execution context"), while "Execution context
is not available in detached frame" becomes `Waiting failed: Frame detached` and an
error (WaitTask.ts).

**Concrete failure modes for this CLI.** (a) A wait implemented with a page-side
`setTimeout`/`rAF` predicate stalls in a hidden/minimized window and times out — the
CLI must not depend on the page's timers; the host-side `poll()` in `lib/poll.py`
(RPC round-trips at `POLL_FAST=0.15s` … `POLL_LOAD=0.3s`) is the right layer.
(b) A MutationObserver-only wait misses shadow-DOM insertions (Puppeteer's shadow
walk above). (c) `networkidle`-style "browser is calm" gating hangs forever on a page
with a websocket — Playwright's own docs reject it. (d) `tab wait --for` polling at
0.15 s adds up to ~0.15 s latency per sample; the interval is a latency budget that
must be named (the four `POLL_*` constants already are).

**Established best practice to name:** host-side polling with a bounded deadline and a
named interval (Puppeteer `IntervalPoller`, Playwright numeric `polling`); rAF/mutation
polling only inside the page when the wait is genuinely frame-bound, with the throttle
caveat documented; **never** `networkidle` as a readiness gate — assert on a state
instead (Playwright's "DISCOURAGED" note); re-arm the wait in the new execution
context after navigation (Puppeteer `WaitTask.rerun`).

---

### 3. Testing the automation client itself

**Why it is hard.** The client's inputs are a real browser, a real socket, real time
and real `/proc`; every one of those is a nondeterminism source. The framework can
only be trusted if it is testable *without* the browser, and its browser-dependent
tests are graded, not silently retried.

**Direct evidence — Puppeteer's two tiers.** "Default `npm test` runs
`test:{chrome,firefox}:headless`"; and separately, "**Unit tests** — Tests that only
test code (without the running browser) are put next to the classes they test and run
using the Node test runner (requires Node 22+): `npm run unit`". The browser tier is
driven by "a custom test runner on top of Mocha that consults the
`TestExpectations.json` to see if a given test result is expected or not"
(https://raw.githubusercontent.com/puppeteer/puppeteer/main/docs/contributing.md).

**Direct evidence — the expectations file is the quarantine mechanism.** Each entry is
`{testIdPattern, platforms:[darwin|linux|win32], parameters:[...], expectations:[PASS|
FAIL|TIMEOUT|SKIP], comment}`, matched with `*` globs; "the latest expectation that is
set will take precedence"; the file's own comments link each SKIP/FAIL to an issue
(https://raw.githubusercontent.com/puppeteer/puppeteer/main/test/TestExpectations.json
and https://raw.githubusercontent.com/puppeteer/puppeteer/main/tools/mocha-runner/README.md).
Known-unstable tests are therefore *named, platform-scoped, and commented* — not
silently retried inside a helper.

**Direct evidence — the live harness fails on leaked browsers.** Puppeteer's live
tier, in `afterEach`, records `browserNotClosedError` on the test if anything remains
in `browserCleanups`, and in `afterAll` "if a browser was launched and not closed it
throws `Browser was not closed in ${lastTestFile}`"; `afterAll` also stops the HTTP
and HTTPS test servers and closes the browser
(https://raw.githubusercontent.com/puppeteer/puppeteer/main/test/src/mocha-utils.ts).

**Direct evidence — golden/snapshot outputs.** Puppeteer compares screenshots
byte-for-byte first, then pixel-by-pixel: `compareImages` asserts **equal dimensions
or throws** ("Sizes differ: expected image WxH, but got WxH") and then runs
`pixelmatch` with `{threshold: 0.1}`, producing a diff PNG on mismatch
(https://raw.githubusercontent.com/puppeteer/puppeteer/main/test/src/golden-utils.ts).

**Direct evidence — Playwright's tiers and hermeticity rule.** "The two most important
[suites] … **Library tests** cover APIs not related to the test runner … `npm run
ctest` (fast path runs all tests in Chromium) / `npm run test` (slow path runs all
tests in three browsers)" and "**Test runner tests** … `npm run ttest`". And: "**tests
should be *hermetic*, and not depend on external services.** Tests should work on all
three platforms" (https://raw.githubusercontent.com/microsoft/playwright/main/CONTRIBUTING.md).

**Direct evidence — retries and quarantine policy.** Playwright classifies results as
"passed"/"flaky"/"failed", where flaky = "failed on the first run, but passed when
retried" (https://playwright.dev/docs/test-retries). Crucially, retries are safe
because the environment is reset: "Should any test fail, Playwright Test will
**discard the entire worker process along with the browser** and will start a new one"
("Workers are always shutdown after a test failure to guarantee pristine environment")
(https://playwright.dev/docs/test-parallel). Google's account of the policy at scale:
a tool "monitors the flakiness of tests and if the flakiness is too high, it
**automatically quarantines** the test. Quarantining removes the test from the
critical path and files a bug for developers"; a test can be marked flaky so it "report[s]
a failure only if it fails **3 times in a row**". Context numbers: ~1.5 % of all runs
flaky, "Almost 16 % of our tests have some level of flakiness", "about 84 % of the
transitions we observe from pass to fail involve a flaky test"
(https://testing.googleblog.com/2016/05/flaky-tests-at-google-and-how-we.html).
Chromium's own version is a checked-in expectations file: `TestExpectations` "contains
the list of all known web test failures", with `[ Skip ]` tests "not run by default",
`[ Slow ]` merged in, and flag-specific override files
(https://chromium.googlesource.com/chromium/src/+/refs/heads/main/docs/testing/web_tests.md).

**Concrete failure modes for this CLI.** (a) `tests/live_test.py` flakes on a slow
machine and is re-run until green, hiding a real `wait-timeout` bug. (b) A live test
that starts a browser but asserts before that browser exits leaves a process behind
for the *next* test to trip on (the Puppeteer `Browser was not closed` pattern).
(c) A test that monkeypatches `dom.click` to a fake asserts the *fake* was called,
not that the verb's read-back logic is right — the hermetic tier must still exercise
`lib/dom` verdict functions. (d) An "expected failure" with no owner/issue rots
forever (Google's warning).

**Established best practice to name:** a graded, comment-bearing expectations file
keyed by test id + platform (Puppeteer `TestExpectations.json`, Chromium
`TestExpectations`); hermetic tier with no browser and no network (Playwright
"tests should be hermetic"); live tier that fails on leaked resources (Puppeteer
`afterEach`/`afterAll`); quarantine with an owner (Google testing blog); report
flaky separately from failed (Playwright retries).

---

### 4. Verification oracles

**Why it is hard.** For every DOM verb in this CLI the oracle is the page's own
JavaScript — `hit`, `:hover`, geometry, `checked`, `currentTime`, `document
.visibilityState` can all be shadowed by the page. An oracle that shares a failure
domain with the thing it checks is **tautological**: re-reading `element.value` after
typing proves the field's getter returned what you set, not that bytes reached the
frame.

**Direct evidence — the page-independent PNG oracle.** The PNG spec fixes the first 8
bytes of any PNG datastream to the signature `89 50 4E 47 0D 0A 1A 0A`, and "the
first chunk in a PNG datastream" must be `IHDR` ("IHDR: image header"), whose width
and height dimension the image ("the output buffer … dimensions from the IHDR
chunk"). Every chunk carries a CRC ("The CRC is always present, even for chunks
containing no data").
(https://www.w3.org/TR/png-3/). **Interpretation:** a written screenshot is verified
from its own bytes (signature valid, IHDR parses, dimensions match the page's claimed
CSS size × `devicePixelRatio`) — this is the one oracle in `lib/dom/screenshot.py`
that a lying page cannot forge, short of a byte mismatch that the check itself
detects.

**Direct evidence — the protocol's own numbers.** `Page.captureScreenshot` returns
`data: Base64-encoded image data … defaults to png`; `Page.getLayoutMetrics` returns
`cssLayoutViewport` / `cssVisualViewport` / `cssContentSize` "in CSS pixels"
(https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/json/browser_protocol.json).
So the screenshot check is a genuine *second* oracle: bytes-vs-CDP-metrics, not
bytes-vs-page-JS.

**Direct evidence — snapshot as a second oracle.** Puppeteer's golden comparison
asserts equal image dimensions and a per-pixel diff at threshold 0.1 (golden-utils.ts,
URL above). Pixdiff is an independent check of the image, not of the page's DOM.

**Direct evidence — frameworks insulate their own measurements from page-controlled
globals.** Playwright's `UtilityScript` binds `setTimeout/clearTimeout/setInterval/
clearInterval/requestAnimationFrame/cancelAnimationFrame/requestIdleCallback/
performance/Intl/Date/AbortSignal` up front, with the comment "**Builtins protect
injected code from clock emulation**", and falls back to the pre-emulation copies when
the page has a `__pwClock` shim
(https://raw.githubusercontent.com/microsoft/playwright/main/packages/injected/src/utilityScript.ts).
Playwright's own automation runs in a named world (`world: 'utility'`) with the binding
`__playwright__binding__`
(https://raw.githubusercontent.com/microsoft/playwright/main/packages/playwright-core/src/server/page.ts).
**Interpretation:** this is the concrete answer to "the page's JS is a lying oracle":
run the *measurement* (timing, rAF cadence, bindings) in a context the page cannot
shadow. It does **not** rescue DOM properties — an isolated world shares the DOM, so
`element.getBoundingClientRect`/`visibilityState` remain page-influenced unless read
from the browser side.

**Direct evidence — isolated worlds exist as a first-class CDP primitive.**
`Page.createIsolatedWorld(frameId, worldName, grantUniveralAccess, contentSecurityPolicy)`
returns an `executionContextId`; `Runtime.evaluate` accepts `contextId` (mutually
exclusive with the default page context) and a **`uniqueContextId`** that "Compared to
contextId that may be reused across processes … is guaranteed to be system-unique";
`Runtime.callFunctionOn` requires "All call arguments must belong to the same
JavaScript world as the target object"
(https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/json/js_protocol.json).
**Interpretation:** the CLI can read geometry state in a named isolated world so page
monkeypatching of its *own* convenience globals cannot reach the CLI's script — but
shadowing of DOM properties is still possible, so `verified:true` still means "the
page's DOM says so", exactly as README states.

**Direct evidence — `verified:false` for unclear oracles is not novel.** Playwright's
`utilityScript`/`injectedScript` return sentinel `'error:notconnected'` rather than
guessing; Puppeteer turns unknown context errors into either a retry or a named
failure ("Waiting failed: Frame detached") instead of a false success. This mirrors
`lib/plan.md §4 rule 7` (`unclear oracle ≠ absent effect`).

**Concrete failure modes for this CLI.** (a) `tab click` verifies "ok" via
`document.activeElement === el` — a page script can set focus itself (tautology).
(b) `tab check` reads `checked` after dispatching `Input.dispatchMouseEvent`; a page
can flip `checked` without the click landing (same-domain lie) — the L4 hit test is the
separation. (c) A screenshot is declared good because the page reports size X·DPR when
the PNG IHDR actually says something else. (d) `tab dialog state` is reported as
"no dialog" when the renderer merely did not answer — the project already prevents
this with `open: null, verified:false`. (e) An `eval`-based oracle is used inside a
page world with an overridden `setTimeout`, so a timed wait never advances.

**Established best practice to name:** verify mutations from an *independently
derived* source — PNG bytes (W3C PNG spec signature + IHDR), CDP metrics, `/proc`, the
protocol's own error envelopes — never from the same page JS that performed the
action; bind framework globals before the page runs (Playwright `UtilityScript`); run
framework measurement in a named isolated world (CDP `Page.createIsolatedWorld`);
compare snapshots byte/size-first then pixel-wise (Puppeteer `golden-utils`).

---

### 5. Timeouts and races

**Why it is hard.** A single operation crosses layers (CLI flag → verb → `lib/cdp`
RPC → socket → browser → renderer). A per-layer timeout that stacks instead of
propagating turns a 5 s user budget into 5 s × N; a wait that ignores navigation or a
tab close hangs or verifies the wrong document; a socket that dies mid-command must
fail every in-flight command, not just the new ones.

**Direct evidence — layered, each with its own budget (Playwright).** Defaults:
**test timeout 30 000 ms**, **expect timeout 5 000 ms**, **action timeout no timeout**
(config `actionTimeout`), **navigation timeout no timeout** (config `navigationTimeout`),
**global run timeout no timeout** (config `globalTimeout`), `beforeAll/afterAll`
30 000 ms, fixture timeout configurable per fixture (`{timeout: 60_000}`). A test's
time budget includes "the test function, fixture setups, and `beforeEach` hooks", and
"Additional separate timeout, of the same value, is shared between fixture teardowns
and `afterEach` hooks"; `test.slow()` triples, `testInfo.setTimeout()` extends
(https://playwright.dev/docs/test-timeouts). The docs' own guidance for flakiness:
"If you happen to be in this section because your tests are flaky, it is very likely
that you should be looking for the solution elsewhere."

**Direct evidence — Puppeteer's budgets.** Launch `timeout` default `30_000` ms for
browser start, and `protocolTimeout` as the RPC-layer budget
(https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/node/LaunchOptions.ts).
`WaitTask` sets its own timer: on expiry `terminate(TimeoutError('Waiting failed: Nms
exceeded'))`, and `terminate()` clears the timer and disposes the in-page poller
(WaitTask.ts).

**Direct evidence — cancellation is a first-class option.** Playwright's
`waitForFunction` accepts `signal: AbortSignal` (v1.62): "If the signal is aborted,
the operation will be aborted and throw an error. **Note that providing a signal does
not disable the default timeout**" (https://playwright.dev/docs/api/class-frame).
Puppeteer's `WaitTask` and `LifecycleWatcher` both accept `signal?: AbortSignal` and
hook `abort`; `LifecycleWatcher` even re-parents the reason
(`signal.reason.cause = this.#error`) so the cancellation carries the wait's context
(WaitTask.ts, LifecycleWatcher.ts).

**Direct evidence — waits that race navigation.** `LifecycleWatcher`:
`#onFrameDetached(frame)` → if it is *our* frame, `this.#error.message = 'Navigating
frame was detached'` and the deferred resolves with that error immediately; the
navigation has a `Deferred.create({timeout: this.#timeout, message: 'Navigation timeout
of N ms exceeded'})`; and it explicitly handles both `FrameNavigatedWithinDocument`
(same-document) and `FrameNavigated`/`FrameSwapped`/`FrameSwappedByActivation`.
`WaitTask` distinguishes the two races: "Execution context was destroyed" (navigation)
→ **retry in the new context**, vs detached frame → fail
(https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/cdp/LifecycleWatcher.ts).

**Direct evidence — a socket closing during an in-flight command.** Puppeteer
`Connection.#onClose()` flips `#closed = true`, clears the transport handlers, calls
`this.#callbacks.clear()`, calls `session.onClosed()` on every session, clears the
session map, and emits `CDPSessionEvent.Disconnected`. `CallbackRegistry.clear()`
rejects **every pending callback** with `TargetCloseError('Target closed')`; a *new*
`send()` on a closed connection is rejected up front with
`ConnectionClosedError('Connection closed.')`
(https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/cdp/Connection.ts
and https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/common/CallbackRegistry.ts).
The CDP events that signal this at the source: `Target.detachedFromTarget`
("Issued when detached from target for any reason … Can be issued multiple times per
target if multiple sessions have been attached to it"), `Target.targetCrashed`,
`Page.frameDetached`
(https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/json/browser_protocol.json).

**Direct evidence — a monotonic deadline is not optional.** `lib/poll.py` already
solves this ("Monotonic: a budget is a duration, and an NTP step or a VM resume must
not move it (a wall-clock step pushed a `tab wait` past its timeout, or expired it on
the first sample — a review flagged the deadline source)"). This matches the
framework behavior: both Puppeteer's `Deferred` timeout and Playwright's runner use
monotonic timers, not wall clock.

**Concrete failure modes for this CLI.** (a) `--timeout 5` is applied at the verb but
each `lib/cdp` RPC adds its own 30 s socket budget → the CLI can hang ~150 s. (b) A
`tab wait` that starts before a `nav` sees the old document, gets a valid read-back,
and reports success for the wrong page. (c) A websocket dies mid-`tab click`; if only
new sends are guarded, the in-flight future hangs forever. (d) A `tab close` of the
target tab during a `--for` wait is treated as "not yet true" and polls to the
deadline instead of refusing. (e) A retry loop re-issues a non-idempotent mutation
after a timeout whose effect actually landed.

**Established best practice to name:** one deadline per operation that *propagates*
into every layer (Playwright's test/expect/action/navigation/fixture/global ladder);
bounded waits with an explicit `timeout` and an `AbortSignal` for cancellation
(Playwright `signal`); a wait re-armed or failed explicitly on navigation/frame-detach
(Puppeteer `LifecycleWatcher`/`WaitTask`); all in-flight RPCs failed on socket close
(Puppeteer `Connection`/`CallbackRegistry`); monotonic deadlines everywhere.

---

### 6. Harness and process hygiene

**Why it is hard.** A browser is a multi-process tree (zygote + renderers + GPU) that
outlives a naive `kill`; ports and profile directories are global mutable state; and a
failed test is exactly the moment cleanup is skipped. Every published framework has
been bitten by all four.

**Direct evidence — kill the whole process group (Playwright).** `processLauncher`
spawns the browser with `detached: process.platform !== 'win32'` and the exact comment:
"On non-windows platforms, `detached: true` makes child process a leader of a new
**process group**, making it possible to kill child process tree with `.kill(-pid)`
command." Graceful close is attempted first; on failure or on a second signal it runs
`process.kill(-spawnedProcess.pid, 'SIGKILL')` (Windows: `killProcessTree`). It installs
`exit`/`SIGINT`/`SIGTERM`/`SIGHUP` handlers, and the comment on graceful close is
explicit that handlers stay installed until the process is dead "**to prevent zombie
processes**"; a second SIGINT "resort[s] to default handler … just in case we
hang/stall"; `gracefullyProcessExitDoNotHang` force-exits "after 30 seconds"
(https://raw.githubusercontent.com/microsoft/playwright/main/packages/utils/processLauncher.ts).

**Direct evidence — signal handling and profile cleanup (Puppeteer).** Launch options
`handleSIGINT`, `handleSIGTERM`, `handleSIGHUP` — each "Close the browser process on
…", all defaulting to `true` (LaunchOptions.ts). `BrowserLauncher` registers a
*process-exit* cleanup for a temporary user-data-dir, with the ordering comment:
"Register **after** @puppeteer/browsers has installed its process-exit dispatcher.
That dispatcher kills the browser **before** this synchronous fallback removes the
profile directory", and it removes the listener when the entry set empties
(https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/node/BrowserLauncher.ts).

**Direct evidence — the port-allocation race and its elimination.** Chrome launched
with `--remote-debugging-port=0` writes the *actual* chosen port to a file:
`content/browser/devtools/devtools_http_handler.cc` appends `kDevToolsActivePortFileName`
to the output (user-data) directory and writes `"%d\n%s", ip_address->port(),
browser_guid` (port then browser GUID)
(https://chromium.googlesource.com/chromium/src/+/refs/heads/main/content/browser/devtools/devtools_http_handler.cc?format=TEXT).
Playwright then launches with `--remote-debugging-port=0` and resolves the endpoint by
"reading `${userDataDir}/DevToolsActivePort`", parsing the first line as the port, and
building `ws://localhost:${port}/devtools/browser`; both a missing file and a
non-numeric file are typed errors. It *also* matches `/DevTools listening on (.*)/`
from the browser's own stderr as a fallback
(https://raw.githubusercontent.com/microsoft/playwright/main/packages/playwright-core/src/server/chromium/chromium.ts).
**Interpretation:** the established answer to "port allocation races" is *do not pick
a port* — let the browser pick one and read it from `DevToolsActivePort`, keyed to the
profile the browser was launched with. This subsumes the project's `cdp-not-local`
guard: the file lives in *that profile's* directory, so a stale file from a dead
instance is detectably stale.

**Direct evidence — profile locking.** Chrome holds a `ProcessSingleton` lock on the
user-data dir; Playwright's `profileInUseError()` recognizes the Chromium log markers
`"Failed to create a ProcessSingleton for your profile directory."` and `"Opening in
existing browser session."` and rewrites them to "This usually means that the profile
is already in use by another instance of Chromium"
(https://raw.githubusercontent.com/microsoft/playwright/main/packages/playwright-core/src/server/chromium/chromium.ts).
The user-facing symptom is Puppeteer #4860: "**The profile appears to be in use by
another Chromium process on another computer** … Chromium has locked the profile so
that it doesn't get corrupted" (https://github.com/puppeteer/puppeteer/issues/4860).
This is the same failure class as the CLI's `profile-busy` flock.

**Direct evidence — parallel isolation and the per-suite vs per-test browser
tradeoff.** Playwright: "All tests run in worker processes … each starts **its own
browser**"; "By default, test files are run in parallel. Tests in a single file are
run in order, in the same worker process"; "**Workers are always shutdown after a test
failure** to guarantee pristine environment"; each *test* gets its own
`BrowserContext` ("Each test has its own Browser Context … fast and cheap to create
and … completely isolated, even when running in a single browser") — so the established
split is **one browser per worker (expensive, reused across tests), one context per
test (cheap, fresh)**. For genuinely shared resources Playwright offers named locks
(`{ lock: 'user-settings' }`) that "work across files, worker processes and projects"
(https://playwright.dev/docs/test-parallel,
https://playwright.dev/docs/browser-contexts).

**Concrete failure modes for this CLI.** (a) `close --force` SIGTERMs only the parent
PID; Chrome's zygote/renderers reparent to init and keep the profile/profile lock —
same class as killing only the process, not the group. (b) A `--remote-debugging-port=9222`
hard-code races another process and drives a stranger (the `cdp-not-local` case).
(c) A crashed `live_test.py` leaves `cdp-profiles/…` behind with a live or stale
`DevToolsActivePort`, so the next run "attaches" to nothing or to the wrong browser.
(d) Two live tests select the same profile → the second hangs on the lock or opens a
window against the first's tab list. (e) A temp dir is removed before the (still
running) browser releases it, so the browser re-creates it and the profile is
half-there.

**Established best practice to name:** spawn the browser detached (its own process
group) and kill the group on teardown, graceful-close-then-SIGKILL, with exit/SIGINT/
SIGTERM/SIGHUP handlers (Playwright `processLauncher`); close the browser on those
signals and remove temp profiles via a process-exit hook (Puppeteer `handleSIG*` +
`registerProcessExitCleanup`); let the browser pick the debug port and read
`DevToolsActivePort` (`--remote-debugging-port=0`, Chromium `devtools_http_handler.cc`,
Playwright `chromium.ts`); rely on the profile lock for mutual exclusion and report it
as a typed, named error; one browser per worker + one context per test, with named
locks for shared resources (Playwright parallelism/isolation).

---

## Sources

Docs / specs (fetched):
- https://playwright.dev/docs/actionability
- https://playwright.dev/docs/best-practices
- https://playwright.dev/docs/test-parallel
- https://playwright.dev/docs/browser-contexts
- https://playwright.dev/docs/test-timeouts
- https://playwright.dev/docs/test-retries
- https://playwright.dev/docs/api/class-frame
- https://playwright.dev/docs/api/class-page
- https://www.selenium.dev/documentation/webdriver/waits/
- https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API
- https://www.w3.org/TR/png-3/
- https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
- https://testing.googleblog.com/2016/05/flaky-tests-at-google-and-how-we.html
- https://pptr.dev/api/puppeteer.page.waitfornetworkidle

Source (fetched, raw):
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/docs/contributing.md
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/tools/mocha-runner/README.md
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/test/TestExpectations.json
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/test/src/mocha-utils.ts
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/test/src/golden-utils.ts
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/cdp/LifecycleWatcher.ts
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/common/WaitTask.ts
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/injected/Poller.ts
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/cdp/Connection.ts
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/common/CallbackRegistry.ts
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/node/LaunchOptions.ts
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/node/BrowserLauncher.ts
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/api/Page.ts
- https://raw.githubusercontent.com/microsoft/playwright/main/CONTRIBUTING.md
- https://raw.githubusercontent.com/microsoft/playwright/main/packages/playwright-core/src/server/chromium/chromium.ts
- https://raw.githubusercontent.com/microsoft/playwright/main/packages/injected/src/injectedScript.ts
- https://raw.githubusercontent.com/microsoft/playwright/main/packages/injected/src/utilityScript.ts
- https://raw.githubusercontent.com/microsoft/playwright/main/packages/playwright-core/src/server/page.ts
- https://raw.githubusercontent.com/microsoft/playwright/main/packages/utils/processLauncher.ts
- https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/json/browser_protocol.json
- https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/json/js_protocol.json
- https://chromium.googlesource.com/chromium/src/+/refs/heads/main/content/browser/devtools/devtools_http_handler.cc?format=TEXT
- https://chromium.googlesource.com/chromium/src/+/refs/heads/main/docs/testing/web_tests.md?format=TEXT

Issues (fetched):
- https://github.com/microsoft/playwright/issues/40568
- https://github.com/puppeteer/puppeteer/issues/4860

## Test implications

Concrete input→output contracts for **this** CLI. Hermetic unless marked `[live]`.
"Fake peer" = the in-process CDP WebSocket fake already in `tests/test_unit.py`;
"fake `/json`" = the existing `http.server` fake endpoint.

1. **Bounded RPC, no hang.** Fake peer accepts the socket and never replies to a
   command → `tab js --expression 1` (or any verb) exits **2** with `ERR[<timeout
   code>]` on stderr, writes **no** JSON to stdout, and the process's wall time is
   ≤ the declared deadline + one poll interval (assert with `time.monotonic`).
2. **Deadline propagates once, not per layer.** Run a verb with `--timeout 0.4`
   against a fake peer that delays every reply by 0.3 s; assert total elapsed
   `< 1.0 s` (i.e. the budget is not multiplied by the number of layered waits) and
   the refusal names the deadline.
3. **Monotonic deadline.** Monkeypatch `time.time` to jump backward (or forward) mid-wait
   while `time.monotonic` is unaffected; run `tab wait --for selector` against a fake
   peer that never satisfies it → the wait still expires exactly once, at the monotonic
   deadline (it must neither expire on the first sample nor run long). Mirrors
   `lib/poll.py`'s documented invariant.
4. **Socket close fails in-flight commands.** Fake peer reads one command frame then
   closes the socket without replying → the verb exits **2** with the non-verified
   refusal (`ERR[nav-not-verified]` / `ERR[cdp-unreachable]` as applicable), never
   `ok:true`, and does not wait for the full deadline (the close is the signal).
5. **Ack ≠ effect.** Fake peer returns `{ok:true}` to a mutation but the subsequent
   read-back probe returns the pre-state → exit **2** with the `*-not-verified` code
   and the reply reports the *last sample seen* (not an absence claim).
6. **Unclear oracle ≠ absence.** Fake peer answers the read-back with a malformed /
   non-conforming value for `tab dialog state` → stdout `{ok:true, open:null,
   verified:false, …}` with a note, exit **0**; a test asserts the pair `open:null` +
   `verified:false` never becomes `open:false, verified:true`.
7. **Actionability: occlusion.** Fake peer returns a hit-test at the click point that
   resolves to a different node than the target → `tab click` exits **2** with
   `ERR[occluded]` naming the covering element, and **no** input dispatch is sent
   (assert the fake peer saw zero `Input.*` command frames).
8. **Actionability: stability.** Fake peer returns two *different* bounding rects on
   successive rAF read-backs then a third stable pair → `tab click` does not act until
   two consecutive equal rects (assert order of command frames); with alternating rects
   forever it refuses at the stability deadline and never dispatches.
9. **Reads never traceback.** Fake `/json` returns HTML (200 + `text/html`) or truncated
   JSON → `tab list` exits **2** with `ERR[cdp-unreachable]` (shape refusal), **not**
   exit 1 / traceback; stderr is exactly one `ERR[code]: message` line.
10. **PNG oracle is independent.** Fake peer returns base64 of a PNG whose IHDR width×
    height is 800×600 while the page's fake size response is `innerWidth=400,
    devicePixelRatio=2` → `tab screenshot` writes the file, `ok:true`, exit **0**.
    Same setup with an IHDR of 512×384 → exit **2** `ERR[screenshot-not-verified]`
    and **no file written** (assert the target path does not exist).
11. **PNG signature gate.** Fake peer returns bytes not beginning `89 50 4E 47 0D 0A
    1A 0A` (e.g. JPEG magic `FF D8 FF`) → `tab screenshot` refuses
    `ERR[screenshot-not-verified]`, writes nothing.
12. **Detached execution context is a retry, not a failure.** Fake peer returns the CDP
    error `"Execution context was destroyed"` for the first poll sample and a valid
    value for the second → the verb succeeds, and the fake peer recorded exactly one
    retry of that one sample (the `retries=1`-per-sample rule). With an error containing
    `detached frame` it fails immediately with the named code.
13. **Navigation race is detected.** Fake peer answers a pre-nav read with the old
    document's `readyState`/URL while a `FrameNavigated`-equivalent is queued → the
    verb refuses with `ERR[nav-not-verified]` rather than reporting the stale document
    as the destination.
14. **Endpoint ownership guard.** Fake `/json` advertises a port; the `/proc` owner of
    the listening socket (faked at the `lib/browser/owners.py` boundary) has a cmdline
    naming a *different* profile → every drive verb refuses `ERR[cdp-not-local]` naming
    the holder, and the fake peer records **zero** WebSocket connections.
15. **Port race elimination.** Assert that `open` passes `--remote-debugging-port=0`
    and reads the port from `<profile>/DevToolsActivePort` (first line), not from a
    hard-coded/scan value; given a stale `DevToolsActivePort` whose owner cannot be
    verified, `open` reports `started:false` with the refusal (never attaches to the
    stranger).
16. **Profile lock = exactly one start.** Two concurrent `open` calls on the same
    profile, with the lock held by a probe process → exactly one reply has
    `started:true`; the other exits **2** `ERR[profile-busy]` naming the holder; on a
    filesystem that cannot lock, the reply carries a `warning` (not silence).
17. **Process-group teardown `[live]`.** Launch the browser with `setsid`/detached so
    the child is a process-group leader; after `close --force`, assert (a) the
    negative-pgid signal path is taken, (b) `os.killpg` shows the whole group gone
    within the deadline, (c) `waitpid` reaps so no zombie remains, (d) the profile dir
    (and its `DevToolsActivePort`/lock) is gone or explicitly reported.
18. **Leaked-browser detection `[live]`.** The live battery installs an `afterEach`
    that fails the test when a browser it launched is still alive, mirroring
    Puppeteer's `Browser was not closed in <file>`; assert the check fires when a test
    deliberately skips `close`.
19. **Graded expectations file.** `tests/expected_failures.json` (or equivalent) maps
    `{test_id, platform}` → `{expect: PASS|FAIL|TIMEOUT, issue: URL}`; the runner fails
    when an entry's test **unexpectedly passes** (forcing review) and refuses entries
    with no `issue` field. Hermetic self-test of the runner.
20. **Policy fails closed.** With `--allow read`, `tab click` exits **2** with
    `ERR[not-allowed]` naming the rule; an action whose capability class is unknown is
    refused, not ignored; `selftest` is never gated (runs under `--deny '*'`).
21. **Audit redaction.** Run a verb whose fake peer exchanges a secret (cookie/token
    value); assert the JSONL line in `lib/audit.py`'s log contains the redaction
    placeholder and **never** the raw secret substring.
22. **Byte-stable stdout.** Run `tab extract`/`tab list` twice against an identical fake
    endpoint/profile fixture → stdout bytes are identical (no timestamps, PIDs, or port
    numbers in fields that the contract does not declare as volatile); any volatile
    field is enumerated in the test.
23. **Hermetic tier is truly hermetic.** Run `python3 tests/test_unit.py` with
    `PATH` stripped of every browser binary and with a blocker that fails on any
    outbound connection to a non-loopback address → the suite passes with **zero**
    network and **zero** browser launches, and completes under a fixed time budget.
24. **Interval is a named latency budget.** For `tab wait` with `POLL_FAST=0.15`, a fake
    peer that satisfies the predicate on the 3rd sample records a sample count and
    inter-sample spacing within `[interval, interval+ε]`, so a regression that changes
    the interval is asserted, not observed.

## Residual risks / gaps

- **Two claims are inference, not fetched doc:** (1) that an *isolated world* leaves
  DOM properties page-spoofable (I verified the mechanism — `Page.createIsolatedWorld`,
  `Runtime.evaluate contextId` — but found no vendor doc stating the DOM-sharing
  caveat); (2) that Playwright's runner uses monotonic timers (I verified Puppeteer's
  `WaitTask` timeout semantics and this CLI's own rationale, but did not locate
  Playwright's timer source).
- **Puppeteer "protocol-level fakes" for its own client** was not confirmed: I fetched
  `test/src/cdp/*` existence and `TestExpectations.json`/`mocha-utils.ts`, but Puppeteer
  does not ship a public `MockCDPServer` I could cite; its hermetic tier is the
  "unit tests … without the running browser" alongside source classes. The concrete
  fake-endpoint pattern used here is closer to this repo's own `http.server`/fake-WS
  approach than to a named upstream helper.
- `MDN/requestAnimationFrame` page did not render its prose on fetch; the throttling
  claim is cited to the Page Visibility API page instead, which states it directly.
- Chromium's `web_tests.md` was fetched via base64 `?format=TEXT`; the `[ Skip ]` /
  `[ Slow ]` semantics quoted are from that page and the sibling
  `TestExpectations` file, not re-verified in the file itself.
