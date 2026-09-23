# Hard operations in CDP-driven browser control — what breaks, and how mature clients survive it

Method note: every claim below was verified by **fetching the source in this run**
(`curl` of the CDP JSON protocol, `chromium.googlesource.com` `.cc` files, Puppeteer
and Playwright TypeScript on `raw.githubusercontent.com`, MDN's `content` repo, and
the docs sites). A URL appears inline next to each finding. Two sources could not be
fetched (the Chromium issue tracker returned a Google sign-in page; SearXNG's Google
/DDG/Brave backends were rate-limited and returned nothing for later queries) — those
items are marked **[courier gap]** and their claim is carried by a source that *was*
fetched, or stated as my read of it (**inference**). Claims about this repo's own
measured behaviour cite `docs/progress.md` and are marked **project-measured**.

This supersedes the earlier revision of this file, which was written with no web tool
and therefore carried no fetched URLs.

Protocol JSON fetched this run:
`https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/json/browser_protocol.json`
and `.../json/js_protocol.json` (the `Runtime` domain lives in the `js_` file, not the
`browser_` one — worth knowing if you grep).

---

## Findings

### 1. JavaScript dialogs (alert/confirm/prompt/beforeunload)

**What makes it hard.** A dialog is modal to the *renderer*, not to CDP. The protocol
surfaces it as one event (`Page.javascriptDialogOpening`) and resolves it with one
command (`Page.handleJavaScriptDialog`), but the browser only routes the dialog to a
client while the **Page domain is enabled** and only holds **one pending dialog at a
time**. So a dialog that opens with nobody listening is not merely unanswered — it is
never *seen*, and the renderer blocks on it.

**Concrete failure modes (all direct evidence).**
- **The suppressed-dialog wedge.** The `hasBrowserHandler` parameter of the opening
  event documents the trap in one sentence: *"When browser has no dialog handler for
  given target, calling `alert` while Page domain is engaged will stall the page
  execution."* (browser_protocol.json, `Page.javascriptDialogOpening`;
  <https://chromedevtools.github.io/devtools-protocol/tot/Page/#event-javascriptDialogOpening>).
  Chromium's handler makes it structural: `PageHandler::DidRunJavaScriptDialog` begins
  `if (!enabled_) { return; }` — if no client enabled the Page domain, the dialog is
  dropped and the callback that unblocks the renderer is never stored
  (<https://chromium.googlesource.com/chromium/src/+/main/content/browser/devtools/protocol/page_handler.cc>).
  Later `Page.handleJavaScriptDialog` cannot help: it returns
  `Response::InvalidParams("No dialog is showing")` whenever `pending_dialog_` is null
  (same file, `HandleJavaScriptDialog`). This is exactly the wedge the repo measured on
  Chrome 152 headed — `accept` answers `no-dialog`, every `Runtime.evaluate` times out
  (**project-measured**, `docs/progress.md` §4.9 / §5.13).
- **One dialog, not a queue.** The same handler does `CHECK(pending_dialog_.is_null())`
  before storing the callback, so a second dialog while one is pending is a protocol
  invariant violation, not a stack you can drain.
- **beforeunload needs *sticky* user activation.** Chromium gates it behind the
  `kEnforceUserActivationForBeforeUnload` feature and returns `success=true` (i.e.
  *proceed*, no dialog) when the frame never had a gesture: the intervention text is
  verbatim *"Blocked attempt to show a 'beforeunload' confirmation panel for a frame
  that never had a user gesture since its load."*
  (`renderer_host/render_frame_host_impl.cc`, `RunBeforeUnloadConfirm`,
  <https://chromium.googlesource.com/chromium/src/+/main/content/browser/renderer_host/render_frame_host_impl.cc>).
  The same function enforces *"Allow at most one attempt to show a beforeunload dialog
  per navigation"* and, while one is showing, `GetProcess()->SetBlocked(true)` — *"tabs
  in the same process shouldn't process input events."* So a stray beforeunload wedges
  the whole renderer process, not just the frame.
- **Dialogs race navigation.** The dialog belongs to a `frameId` + document; the event
  carries `url` and `frameId`, and `Page.javascriptDialogClosed` fires when it closes
  (protocol). A navigation that commits tears the document down; Playwright guards for
  exactly this with `if (!this._page.frameManager.frame(this._targetId)) return;` at the
  top of its dialog handler
  (<https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/chromium/crPage.ts>).

**Established practice (named to project + URL).**
- **Playwright** auto-dismisses and *blocks evaluations while a dialog is open*:
  `DialogManager.dialogDidOpen` first invalidates evaluations — comment: *"Any ongoing
  evaluations will be stalled until the dialog is closed."* — then, if no handler
  returned true, calls `dialog._close()` (auto-dismiss; `beforeunload` is auto-*accepted*,
  everything else dismissed)
  (<https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/dialog.ts>).
  Documented at <https://playwright.dev/docs/dialogs>: *"If there is no listener for
  `page.on('dialog')`, all dialogs are automatically dismissed"*, and a listener that
  does not `accept()`/`dismiss()` makes the triggering action *"stall"* forever. Closing
  with a beforeunload is opt-in: `page.close({ runBeforeUnload: true })`.
- **Puppeteer** surfaces the event and handles it with `Page.handleJavaScriptDialog`,
  and correlates close via `Page.javascriptDialogClosed`
  (<https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/cdp/Dialog.ts>,
  with the emit in `cdp/Page.ts` `#onDialog`). The handler is registered on connect, so
  Puppeteer never opens the "nobody listening" window.
- **Chromium/DevTools** frontend runs the same model: enable Page, receive
  `javascriptDialogOpening`, answer with `Page.handleJavaScriptDialog`
  (`page_handler.cc`, `DidRunJavaScriptDialog` → `frontend_->JavascriptDialogOpening`).

**Takeaway for a one-shot CLI.** Playwright/Puppeteer are long-lived, so they can hold
the Page domain open for the page's whole life. A one-shot CLI cannot: it must enable
the Page domain **before** the action that might raise a dialog, and, because a dialog
raised while detached is unrecoverable, treat *"Page domain was not enabled when the
dialog opened"* as a distinct, named refusal (`open: null`, **project-measured**) rather
than as "no dialog".

**beforeunload + CDP navigation.** Browser-side `Page.navigate`/`Page.reload` do not run
a *trusted* prompt path the way a renderer-initiated navigation would; combined with the
user-gesture gate above, a robot navigating away never gets a beforeunload panel. That is
the desired primitive here (no modal on teardown), and it is why `tab nav` recovery works
(**project-measured**, `docs/progress.md` §5.13).

---

### 2. Cross-origin iframes / out-of-process frames (OOPIF)

**What makes it hard.** Under Site Isolation a cross-origin (or otherwise site-isolated)
frame is a **separate CDP target** with its own renderer process. A flat page-level
session cannot see into it: `Runtime.evaluate` with the page's context, `DOM.querySelector`
from the page root, and `Input.*` all stay in the page's process. Chromium names the type
literally: `const char DevToolsAgentHost::kTypeFrame[] = "iframe";`
(`devtools_agent_host_impl.cc`,
<https://chromium.googlesource.com/chromium/src/+/main/content/browser/devtools/devtools_agent_host_impl.cc>).

**The routing model (all direct evidence).**
- `Target.setAutoAttach { autoAttach, waitForDebuggerOnStart, flatten }` — *"Controls
  whether to automatically attach to new targets which are considered to be directly
  related to this one (for example, iframes or workers)… When turned on, attaches to all
  existing related targets as well."* With `flatten:true`, *"Enables 'flat' access to the
  session via specifying sessionId attribute in the commands … We plan to make this the
  default"* (crbug.com/991325) (browser_protocol.json, `Target.setAutoAttach`;
  <https://chromedevtools.github.io/devtools-protocol/tot/Target/#method-setAutoAttach>).
- `Target.attachedToTarget` delivers `{sessionId, targetInfo, waitingForDebugger}`; every
  subsequent command is tagged with that `sessionId` in flat mode. `waitForDebuggerOnStart`
  pauses the new target until `Runtime.runIfWaitingForDebugger` (same command doc).
- **Frame identity vs loader identity.** `Page.Frame` carries `id`, `parentId`,
  **`loaderId`**, `url`, `securityOrigin` etc. `loaderId` is *"Identifier of the loader
  associated with this frame"* and `Page.frameNavigated` is *"Fired once navigation of the
  frame has completed. Frame is now associated with the new loader."* — so `frameId` is the
  stable node identity while `loaderId` changes per committed document
  (<https://chromedevtools.github.io/devtools-protocol/tot/Page/#type-Frame>,
  `#event-frameNavigated`). `Page.frameStartedNavigating`'s `loaderId` note states the
  same: *"the previously committed loaderId would not change unless the navigation changes…"*.
  Read a frame's freshness by `loaderId`, not by `frameId`.
- **Execution contexts are per-frame.** `Runtime.executionContextCreated` /
  `executionContextDestroyed` / `executionContextsCleared` are the lifecycle; the created
  context's `auxData` matches *"{isDefault: boolean, type: 'default'|'isolated'|'worker',
  frameId: string}"* (js_protocol.json, `Runtime.ExecutionContextDescription`). And
  `Runtime.evaluate.contextId` *"may be reused across processes"*, whereas
  **`uniqueContextId`** is *"guaranteed to be system-unique… so it can be used to prevent
  accidental [reuse]"* — the right handle when you must evaluate in a specific frame's
  context (<https://chromedevtools.github.io/devtools-protocol/tot/Runtime/#method-evaluate>).

**Established practice.**
- **Playwright** keeps a `FrameSession` **per target**: `readonly _sessions = new
  Map<Protocol.Target.TargetID, FrameSession>()`, sends
  `Target.setAutoAttach { autoAttach:true, waitForDebuggerOnStart:true, flatten:true }`,
  and in `_onAttachedToTarget` branches on `event.targetInfo.type === 'iframe'`, buffering
  attach events until the frame tree arrives
  (<https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/chromium/crPage.ts>).
- **Playwright** also documents a real platform bug in this path — a comment at the
  browser-level `setAutoAttach`: *"Target.setAutoAttach has a bug where it does not wait
  for new Targets being attached."*
  (<https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/chromium/crBrowser.ts>).
- **Puppeteer** models a frame as owning a `client` and **re-parents it** when the process
  topology changes: `#onFrameAttached` does `frame.updateClient(session)` on the comment
  *"If an OOP iframes becomes a normal iframe again it is first attached to the parent
  frame before the target is removed"*; and `Page.frameDetached` with `reason: 'swap'` is
  treated as an OOPIF transition, not a removal (`case 'remove'` removes; `case 'swap'`
  emits `FrameSwapped`)
  (<https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/cdp/FrameManager.ts>).

**This repo's approach.** It attaches to the iframe **target** with a fresh `Session` and
drives the unchanged verb set inside it — measured: a cross-origin frame appears as its own
row in `/json/list`, `cdp.Session` reads that frame's DOM, and a real
`Input.dispatchMouseEvent` at **frame-local** coordinates fires the frame's handler
(**project-measured**, `docs/progress.md` §5.24). Note the protocol text says mouse `x/y`
are *"relative to the main frame's viewport"* (see §6) — the repo's frame-local behaviour
works because the frame's *own* session treats that frame as its root. A same-process
frame has **no target**, so it refuses `frame-not-separate` rather than silently reading
the top document. **[courier gap]** on a "same-process frame has no target" upstream
citation; the repo's measurement is the support.

---

### 3. Shadow DOM (open vs closed)

**What makes it hard.** A shadow root is a separate node tree behind an element.
`document.querySelectorAll` does **not** cross that boundary: CSS selectors match within
a tree, and a shadow root's subtree is a different tree. `Element.shadowRoot` is non-null
only for `mode:'open'` — MDN: the `mode` property *"specifies its mode — either `open` or
`closed`. This defines whether or not the shadow root's internal features are accessible
from JavaScript."* (<https://developer.mozilla.org/en-US/docs/Web/API/ShadowRoot/mode>).
So **closed** roots are invisible to page JavaScript *by design* — but not to the browser.

**The CDP path (direct evidence).**
- `DOM.getDocument` and `DOM.describeNode` both take `pierce`: *"Whether or not iframes
  and shadow roots should be traversed when returning the subtree (default is false)."*
  `DOM.querySelector`/`querySelectorAll` have **no** `pierce` parameter — they operate on a
  given `nodeId`'s tree. So the recipe is: get/describe a node with `pierce:true`, walk
  `node.shadowRoots` (and `shadowRootType`), then `DOM.querySelector` **on the shadow root
  node**. (browser_protocol.json, `DOM.getDocument`/`DOM.describeNode`/`DOM.Node`;
  <https://chromedevtools.github.io/devtools-protocol/tot/DOM/#method-getDocument>.)
- This works for **closed** roots. A worked demonstration: `DOM.querySelector({nodeId: root,
  selector:'#inside'})` returns `nodeId: 0` for a closed root, but `DOM.getDocument({depth:-1,
  pierce:true})` returns the content under `shadowRoots`, and
  `DOM.describeNode({nodeId, depth, pierce:true})` returns `node.shadowRoots` you can then
  query (<https://yotam.net/posts/piercing-the-shadow-root-using-cdp/>). The repo's
  `DOM.setFileInputFiles` uses an **objectId** for the same reason — it works through a
  shadow root (**project-measured**, `docs/progress.md` §5.x upload).

**Piercing strategies and their history.**
- The old CSS combinators `>>>` and `/deep/` were removed from the platform; today the
  only *page-side* piercing is `element.shadowRoot.querySelector(...)` (open only) or
  recursive walking via `getRootNode()` / `Event.composedPath()`. **[courier gap]**: the
  removal is documented in the web components spec history / Chrome status, not fetched
  this run.
- **Playwright** does open-root piercing in its engine: *"CSS selectors pierce open shadow
  DOM"*, and it warns *"XPath does not pierce shadow roots"*; the closed case is
  explicitly out of scope — *"Closed-mode shadow roots are not supported."*
  (<https://playwright.dev/docs/other-locators>,
  <https://playwright.dev/docs/locators>).
- **Puppeteer** historically used the `pierce/` selector prefix; the modern engine relies
  on its own query handler over `DOM.describeNode`/`pierce` for the CDP path. **[courier
  gap]** on a fetched Puppeteer pierce doc.

**Takeaway.** For a CDP-native CLI, `DOM.getDocument`/`describeNode` with `pierce:true` is
strictly more capable than anything the page can do (it reaches **closed** roots). The
honest oracle caveat stands: once you are *inside* a root, `input.files`, geometry and
`:hover` are still the page's own JavaScript reading.

---

### 4. File upload and downloads

**Upload (direct evidence).** `DOM.setFileInputFiles` *"Sets files for the given file input
element"* and accepts **`files`, plus exactly one of `nodeId`, `backendNodeId`, or
`objectId`** (browser_protocol.json;
<https://chromedevtools.github.io/devtools-protocol/tot/DOM/#method-setFileInputFiles>).
`objectId` is the one that reaches through a shadow root (the node wrapper is a JS object).
Failure mode: `input.files` is the only read-back; a mutation that "succeeds" but leaves
`files` empty must refuse. Selenium's `sendKeys` to a file input and Playwright's
`setInputFiles` both ultimately use this primitive. **[courier gap]** on Playwright's
`setInputFiles` source; the primitive is the same.

**Downloads (direct evidence).**
- The modern command is **`Browser.setDownloadBehavior`**: `behavior` ∈
  `deny|allow|allowAndName|default`, `browserContextId`, `downloadPath` (**required** for
  `allow`/`allowAndName`), and `eventsEnabled` (**defaults to false**) (browser_protocol.json;
  <https://chromedevtools.github.io/devtools-protocol/tot/Browser/#method-setDownloadBehavior>).
  `Page.setDownloadBehavior` is marked **deprecated: true** in the same file — do not use it.
- Events live on the **Browser** domain: `Browser.downloadWillBegin {frameId, guid, url,
  suggestedFilename}` and `Browser.downloadProgress {guid, totalBytes, receivedBytes, state,
  filePath}` — with the load-bearing caveat that `filePath` *"If download is 'completed',
  provides the path… Depending on the platform, it is **not guaranteed to be set, nor the
  file is guaranteed to exist**."* (same URL). So a completion verifier must stat the file
  itself, not trust `filePath`.
- Chromium enforces the contract: `BrowserHandler::DoSetDownloadBehavior` returns
  `Response::InvalidParams("downloadPath not provided")` for `allow`/`allowAndName` without a
  path, and returns `Response::ServerError("Not allowed")` when `allow_set_download_behavior_`
  is false (embedder-controlled); `SetDownloadEventsEnabled(events_enabled.value_or(false))`
  confirms events are off unless asked. `DownloadProgress` translates `IN_PROGRESS→inProgress`,
  `COMPLETE→completed` (and only then fills `filePath`, only if non-empty), `CANCELLED|
  INTERRUPTED→canceled`
  (<https://chromium.googlesource.com/chromium/src/+/main/content/browser/devtools/protocol/browser_handler.cc>).

**Established practice.**
- **Playwright** downloads land in a temp dir per context, are **deleted when the context
  closes**, are surfaced as a `page.on('download')` event, and are persisted with
  `download.saveAs(path)`; the launch option `downloadsPath` sets the folder
  (<https://playwright.dev/docs/downloads>). The "wait for the download before clicking"
  guidance (`const p = page.waitForEvent('download'); await click; const d = await p`) is
  the race fix for `downloadWillBegin` firing before your click returns.
- **Puppeteer** wraps the same command as `browserContext.setDownloadBehavior` sending
  `Browser.setDownloadBehavior {behavior, downloadPath, browserContextId}`
  (<https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/cdp/BrowserContext.ts>,
  type in `common/DownloadBehavior.ts`). Its own issue #11871 — *"Browser.setDownloadBehavior
  breaks the links on chrome://downloads page and download bubble"* — was closed as
  **not planned**, i.e. the override has side effects on Chrome's own UI
  (<https://github.com/puppeteer/puppeteer/issues/11871>).

**Headless quirk.** The historic split was that downloads needed the **browser-level**
command (and often a browser-level session), not `Page.*`, and that `eventsEnabled` must be
turned on or no events arrive. **[courier gap]** on a "headless downloads disabled"
upstream note: the SearXNG-backed queries for it returned nothing after the engine pool was
suspended; the browser_handler.cc gate above is the fetched support for the behaviour class.

---

### 5. Navigation lifecycle races

**What makes it hard.** A navigation can happen at any instant, and it destroys the
JavaScript execution context out from under an in-flight `Runtime.evaluate`. The document
is only replaced at *commit*; `Runtime` reports the teardown as
`Runtime.executionContextDestroyed` (and `executionContextsCleared` when all go)
(js_protocol.json). `Runtime.evaluate` targeting a destroyed `contextId` fails — that is the
"execution context was destroyed" class of errors
(<https://github.com/puppeteer/puppeteer/issues/3323>). The right handle across a possible
navigation is `uniqueContextId`, which is system-unique and *"can be used to prevent
accidental [reuse]"* across processes
(<https://chromedevtools.github.io/devtools-protocol/tot/Runtime/#method-evaluate>).

**Concrete failure modes (direct evidence).**
- **Evaluate during navigation → context gone.** Puppeteer's canonical behaviour: after a
  click that navigates, the execution context is invalid and evaluation throws. Error type
  is ordinary (`Runtime.evaluate` protocol error), not special-cased away.
- **Renderer death / tab close → target closed.** Puppeteer models this as
  `TargetCloseError extends ProtocolError`, alongside `ConnectionClosedError` (thrown *"if
  underlying protocol connection has been closed"*)
  (<https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/common/Errors.ts>).
  So in-flight commands on a dead renderer surface as a target/connection-level error, not
  as a DOM error — a CLI must map both to a named refusal, never replay a mutation (§ the
  repo's "reads retry, mutations never replay", `docs/plan.md`).
- **`load` is not "ready".** `Page.loadEventFired` and `domContentEventFired` fire at
  document-parse/load; modern pages keep mutating after. Playwright documents the hydration
  trap: a click on an enabled-but-not-yet-wired button *"won't have any effect"*
  (<https://playwright.dev/docs/navigations>). The lifecycle events (`Page.lifecycleEvent`
  with `name`, plus frameId/loaderId) are the richer signal
  (browser_protocol.json).
- **BFCache bypasses the lifecycle.** Playwright: *"Because a BFCache restore skips the
  network fetch phase, the browser does not fire standard navigation lifecycle events (such
  as commit, domcontentloaded, or load)… resulting in timeouts and a completely
  desynchronized Page object"*; it disables BFCache by default
  (<https://playwright.dev/docs/navigations>). A CLI that waits on `load` after a
  back/forward restore can wait forever.
- **`frameNavigated` timing.** The event fires *after* commit — *"Fired once navigation of
  the frame has completed. Frame is now associated with the new loader."* (protocol). Read
  the new `loaderId` from it; do not assume the URL you asked for is the URL that
  committed (redirects).

**Waiting strategies, named.**
- **Puppeteer** `LifecycleWatcher` defines `networkidle0` = *"no more than 0 network
  connections for at least 500 [ms]"* and `networkidle2` = *"no more than 2 … for at least
  500"*, mapping to the protocol's `networkIdle`/`networkAlmostIdle`; on expiry it throws
  *"Navigation timeout of N ms exceeded"*
  (<https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/cdp/LifecycleWatcher.ts>).
- **Playwright** implements the 500 ms network-idle timer in code:
  `this._networkIdleTimer = setTimeout(() => { … }, 500)` in `frames.ts`, and its docs
  discourage it: *"'networkidle' — DISCOURAGED wait until there are no network connections
  for at least 500 ms. Don't use this method for testing."*
  (<https://playwright.dev/docs/api/class-page>; source
  <https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/frames.ts>).
  Docs also separate **navigation** (URL change → commit) from **loading**
  (commit → `domcontentloaded` → `load`), which is the mental model a `wait --for load|idle`
  should follow (<https://playwright.dev/docs/navigations>).

**Takeaway.** Wait on a *page-observable* condition with a wall-clock deadline and report
`samples` (the repo's `tab wait --for load|idle|element|js` → `{ok, waited_s, samples}` or
`wait-timeout`, **project-measured**). Never treat `loadEventFired` alone as readiness.
`networkidle` is a heuristic with a 500 ms poll and no upstream guarantee — label it as
such, don't sell it as truth.

---

### 6. Trusted input synthesis vs page JavaScript

**Why trusted matters (direct evidence).** MDN states it plainly: `Event.isTrusted` *"is a
boolean value that is `true` when the event was generated by the user agent… and `false`
when the event was dispatched via `EventTarget.dispatchEvent()`. **The `click` event fired
through `HTMLElement.click()` sets the `isTrusted` property to `false`.**"*
(<https://developer.mozilla.org/en-US/docs/Web/API/Event/isTrusted>). The user-activation
guide makes the consequence concrete: an *activation triggering input event* must have
`isTrusted == true` **and** be a `keydown` / `mousedown` / `pointerdown(pointerType:'mouse')`
/ `pointerup(non-mouse)` / `touchend` — so programmatic `element.click()` cannot grant user
activation, and every popup/fullscreen/clipboard/PiP API that needs transient activation
will refuse
(<https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/User_activation>).
`Input.dispatchMouseEvent`/`dispatchKeyEvent` inject at the browser input layer, producing
**trusted** events; page-side `.click()` does not. That is why Puppeteer's `page.click` and
Playwright's `locator.click` both route through `Input.*` rather than `element.click()`.

**Coordinates and buttons (direct evidence).** `Input.dispatchMouseEvent`:
`x`/`y` are *"relative to the main frame's viewport in CSS pixels. 0 refers to the top of
the viewport and Y increases as it proceeds towards the bottom"*; `button` (default `none`),
`buttons` is a bitfield (Left=1, Right=2, Middle=4, Back=8, Forward=16, None=0),
`clickCount` (default 0), `modifiers` bitfield (Alt=1, Ctrl=2, Meta=4, Shift=8), and
`type` ∈ `mousePressed|mouseReleased|mouseMoved|mouseWheel`
(browser_protocol.json;
<https://chromedevtools.github.io/devtools-protocol/tot/Input/#method-dispatchMouseEvent>).
Playwright's `crInput.ts` shows the working shape: `move` → `mouseMoved`, `down` →
`mousePressed`, `up` → `mouseReleased`, each carrying `button`, `buttons`
(`toButtonsMask`), `clickCount`, and (on move/down) `force`. Its `move` comment is a useful
race note: for a click it sends the move **synchronously** with down/up — *"click relies on
move-down-up protocol commands being sent synchronously"*
(<https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/chromium/crInput.ts>).

**Keyboard: key events vs `insertText` (direct evidence).** `Input.dispatchKeyEvent` has
`type`, `modifiers`, `key`, `code`, `text` (*"Text as generated by processing a virtual key
code with a keyboard layout. Not needed for `keyUp` and `rawKeyDown`"*), `unmodifiedText`,
`windowsVirtualKeyCode`, `autoRepeat`, `location`, `commands`. `Input.insertText` is the
separate primitive: *"This method emulates inserting text that doesn't come from a key
press, for example an emoji keyboard or an **IME**."* (browser_protocol.json;
<https://chromedevtools.github.io/devtools-protocol/tot/Input/#method-dispatchKeyEvent>,
`#method-insertText`). Consequences:
- **Non-US layouts / dead keys**: `text` is produced by *the active layout*; a raw keycode
  alone produces whatever that layout maps. Playwright passes both `text` and
  `unmodifiedText` and uses `type: text ? 'keyDown' : 'rawKeyDown'` (`crInput.ts`), i.e.
  non-printing keys (arrows, Shift) go as `rawKeyDown` with no text. A dead key needs the
  following key to compose — so a single raw keycode is not a character.
- **`insertText` has no keydown/keyup/keypress.** Playwright docs: *"Dispatches only `input`
  event, does not emit the keydown, keyup or keypress events"* and *"Modifier keys DO NOT
  effect `keyboard.insertText`"*
  (<https://playwright.dev/docs/api/class-keyboard>). So `insertText` is right for
  reliably getting *characters* into a field (including IME/emoji) but wrong when the page
  listens for keystrokes. The repo's `tab insert` (one atomic `Input.insertText`, verified
  by field length) and `tab press` (`Input.dispatchKeyEvent`, effect owned by the page,
  `verified:false`) are the correct two-branch split (**project-measured**, `docs/progress.md`).
- Playwright's `keyboard.type` *"takes raw characters and generates proper keydown,
  keypress/input, and keyup events"*, while lower-level `down`/`up`/`insertText` let you
  *"manually fire events as if they were generated from a real keyboard"*, including holding
  modifiers (`keyboard.down('Shift')` … `keyboard.up('Shift')`)
  (<https://playwright.dev/docs/api/class-keyboard>).

**Takeaway.** `element.click()` can never be a mutation verb for a "verify, or refuse" CLI:
it is untrusted, cannot grant user activation, and skips hit-testing. `Input.*` is the only
trusted path — and it moves the oracle: the page's own `checked`/`focused`/geometry is the
read-back, and a page can shadow all of it (the repo's stated limit, `README.md`).

---

### 7. Full-page screenshots

**The protocol.** `Page.captureScreenshot {format, quality, clip, fromSurface (default
true), captureBeyondViewport (default false), optimizeForSpeed}` (browser_protocol.json;
<https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-captureScreenshot>).

**What Chromium actually does (direct evidence, `page_handler.cc`).**
- `from_surface && capture_beyond_viewport && !clip` → the browser asks the renderer for the
  page size (`main_frame->GetFullPageSize`), then calls
  `CaptureFullPageScreenshot`, which synthesizes **`clip = {x:0, y:0, w:fullPageSize.w,
  h:fullPageSize.h, scale:1}`** and re-enters `CaptureScreenshot` with
  `capture_beyond_viewport=true`
  (<https://chromium.googlesource.com/chromium/src/+/main/content/browser/devtools/protocol/page_handler.cc>).
- **Size limit.** `CaptureFullPageScreenshot` rejects pages whose full size is
  `>= kMaxDimension = 128 * 1024` px with `Response::ServerError("Page is too large.")` — the
  comment names *"the limit of 16K of the headless mode"* (same file). Zero-size clips are
  rejected: *"Cannot take screenshot with 0 width/height."*
- **Beyond-viewport resizes the page.** The `capture_beyond_viewport` branch **mutates
  `WebPreferences`** (`hide_scrollbars = true`, `record_whole_document = true`) and drives
  **`DeviceEmulationParams`** — and it sets the emulated view size **twice** with a
  `(1,1)` placeholder as an explicit workaround: *"TODO(crbug.com/40727379): Remove the bug
  is fixed. Walkaround for the bug. Emulated `view_size` has to be set twice, otherwise the
  scrollbar will be on the screenshot present."* This *is* the root cause of the class of
  bugs where a full-page screenshot perturbs the live page (media queries, lazy loading,
  scroll position).
- **Hidden view stalls.** Under `features::kCDPScreenshotNewSurface` the code asserts
  `CHECK(wc->GetPageVisibilityState() != PageVisibilityState::kHidden)` with the comment
  *"When view is completely hidden, capturing a surface snapshot will stall because the
  surface is never presented."* — a screenshot of a background/hidden tab can hang. This
  is decision-relevant for a CLI whose "active" tab is chosen among many.

**Known bugs (direct evidence).**
- **Puppeteer #8690** (open): *"Taking a full page screenshot causes large page to reload.
  This in turn causes page to reset dimensions to 800x600 and throw away any lazy loaded
  content. Moreover, the width of screenshot is as expected… and therefore it is **not
  possible to detect if screenshot function failed and reloaded just by inspecting the size
  of screenshot**."* — the definitive statement that a correct-looking PNG is not proof of a
  correct capture (<https://github.com/puppeteer/puppeteer/issues/8690>). Chromium issue
  383465171 is an analogous report (<https://issues.chromium.org/issues/383465171>).
- **Chromium hotlist** entry: *"Page.captureScreenshot with captureBeyondViewport resets
  navigator.maxTouchPoints"* — proof that the beyond-viewport emulation path leaks page
  state (<https://issues.chromium.org/hotlists/5432472>). **[courier gap]**: the individual
  issue page returns a sign-in page to `curl`; the hotlist row is the fetched evidence.

**Established practice.**
- **Puppeteer** defaults `fullPage:false`, and `setDefaultScreenshotOptions` sets
  `captureBeyondViewport ??= true` (a de-facto default of *on*), while the option docs say
  its default is *"false if there is no clip. true otherwise."* When
  `fullPage && !captureBeyondViewport`, Puppeteer instead **measures the page and resizes the
  viewport**: `document.documentElement.scrollWidth/scrollHeight` → `setViewport`, capture,
  then restore — with the caveat comment *"Note this may be affected by on-page CSS and
  JavaScript"*, and it restores the viewport in a `dispose` deferral either way
  (<https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/api/Page.ts>).
  This is the **scroll/resize-and-capture alternative** to `captureBeyondViewport`, and it is
  the one Puppeteer falls back on when the beyond-viewport path is unwanted.
- **Verification that is page-independent.** The only oracle that does not go through the
  page is the file's own bytes: a PNG is a fixed **signature** (`89 50 4E 47 0D 0A 1A 0A`)
  followed by an **`IHDR`** chunk (image header, *"the first chunk in a PNG datastream"*)
  carrying width/height
  (<https://www.w3.org/TR/png-3/>, §5.2 PNG signature / §11.2.1 IHDR). A CLI can assert the
  header, the declared dimensions, and the byte size without trusting the page at all — which
  is what the repo does, checked against `dim × devicePixelRatio` (**project-measured**,
  `docs/progress.md` §5.13 item 5). But note Puppeteer #8690 cuts the other way too: matching
  dimensions do **not** prove the page was not reset mid-capture, so the header check proves
  *"a PNG of this size was written"*, not *"the page was stable"*.
- Other tools stitch by scrolling and compositing when `captureBeyondViewport` misbehaves.
  **[courier gap]** on a fetched tool that ships scroll-and-stitch; the technique is
  standard, but I did not fetch a canonical implementation this run.

---

## Sources

CDP protocol (fetched in this run):
- https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/json/browser_protocol.json
- https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/json/js_protocol.json
- https://chromedevtools.github.io/devtools-protocol/tot/Page/
- https://chromedevtools.github.io/devtools-protocol/tot/Target/
- https://chromedevtools.github.io/devtools-protocol/tot/DOM/
- https://chromedevtools.github.io/devtools-protocol/tot/Input/
- https://chromedevtools.github.io/devtools-protocol/tot/Runtime/
- https://chromedevtools.github.io/devtools-protocol/tot/Browser/

Chromium source (fetched):
- https://chromium.googlesource.com/chromium/src/+/main/content/browser/devtools/protocol/page_handler.cc
- https://chromium.googlesource.com/chromium/src/+/main/content/browser/devtools/protocol/browser_handler.cc
- https://chromium.googlesource.com/chromium/src/+/main/content/browser/devtools/devtools_agent_host_impl.cc
- https://chromium.googlesource.com/chromium/src/+/main/content/browser/renderer_host/render_frame_host_impl.cc

Puppeteer (fetched):
- https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/cdp/Page.ts
- https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/cdp/Dialog.ts
- https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/cdp/FrameManager.ts
- https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/cdp/LifecycleWatcher.ts
- https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/cdp/BrowserContext.ts
- https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/common/Errors.ts
- https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/common/DownloadBehavior.ts
- https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/api/Page.ts
- https://github.com/puppeteer/puppeteer/issues/8690
- https://github.com/puppeteer/puppeteer/issues/11871
- https://github.com/puppeteer/puppeteer/issues/3323

Playwright (fetched):
- https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/dialog.ts
- https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/chromium/crPage.ts
- https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/chromium/crBrowser.ts
- https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/chromium/crInput.ts
- https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/frames.ts
- https://playwright.dev/docs/dialogs
- https://playwright.dev/docs/downloads
- https://playwright.dev/docs/navigations
- https://playwright.dev/docs/other-locators
- https://playwright.dev/docs/locators
- https://playwright.dev/docs/api/class-keyboard

MDN (fetched via the `mdn/content` repo):
- https://raw.githubusercontent.com/mdn/content/main/files/en-us/web/api/event/istrusted/index.md
- https://raw.githubusercontent.com/mdn/content/main/files/en-us/web/security/defenses/user_activation/index.md
- https://raw.githubusercontent.com/mdn/content/main/files/en-us/glossary/transient_activation/index.md
- https://developer.mozilla.org/en-US/docs/Web/API/ShadowRoot/mode

Other:
- https://yotam.net/posts/piercing-the-shadow-root-using-cdp/
- https://www.w3.org/TR/png-3/
- https://issues.chromium.org/hotlists/5432472
- https://issues.chromium.org/issues/383465171 **[fetch blocked: sign-in page]**

Project docs: `README.md`, `docs/plan.md`, `docs/progress.md`.

---

## Test implications

Concrete, hermetic input→output contracts for THIS CLI. "fake CDP endpoint" = the
existing fake transport/socket in `tests/test_unit.py`; each is assertable without a
browser unless marked **live**.

1. **Suppressed dialog is a refusal, not an answer.** GIVEN a tab whose fake CDP never
   emits `Page.javascriptDialogOpening` but whose `Runtime.evaluate` hangs, WHEN
   `tab dialog state --tab active`, THEN `open: null` and `verified: false` (never
   `open: false`), exit 0. GIVEN the same with `tab dialog accept`, THEN
   `ERR[no-dialog]` on stderr, exit 2. *(Contract from `page_handler.cc`
   `if (!enabled_) return;` + `InvalidParams("No dialog is showing")`.)*
2. **Dialog open→accept verifies by renderer return.** GIVEN a fake that emits
   `Page.javascriptDialogOpening {type:'confirm', message:'…', frameId:'F'}`, WHEN
   `tab dialog accept`, THEN the CLI sends `Page.handleJavaScriptDialog {accept:true}`,
   THEN waits for `Page.javascriptDialogClosed`, and the reply is `ok:true` with the
   frame id echoed; WHEN `accept:dismiss` it sends `{accept:false}`.
3. **Prompt text only rides a prompt.** WHEN `tab dialog accept --text V` on a
   `type:'alert'`, THEN refuse `bad-args` (no `promptText` sent); WHEN on
   `type:'prompt'`, THEN `Page.handleJavaScriptDialog {accept:true, promptText:'V'}`.
4. **beforeunload is never navigated by a panel.** WHEN `tab nav <url>` on a page whose
   `window.onbeforeunload` returns a string, THEN the CLI issues `Page.navigate`
   (browser-side) and asserts the observed url + `readyState`; it must not emit any
   `Page.handleJavaScriptDialog` and must not hang. *(Gates: `kEnforceUserActivationFor…`,
   one-attempt-per-navigation.)*
5. **Two dialogs are not a queue.** GIVEN a fake that emits a second
   `javascriptDialogOpening` before the first is closed, WHEN `tab dialog accept`, THEN
   the CLI handles exactly the first and reports the second as unhandled — it must not
   issue two overlapping `handleJavaScriptDialog` calls (no `CHECK` violation upstream).
6. **OOPIF is a separate target, addressed by its own session.** GIVEN a tab whose
   `Page.getFrameTree` lists a child frame `F` that ALSO appears as a target with
   `type:'iframe'`, WHEN `tab frames`, THEN the reply lists `F` with `separate: true` and a
   `target` id; WHEN `tab text --frame F`, THEN the CLI opens a session on that target and
   the command stream contains no evaluation tagged with the top frame's sessionId.
7. **Same-process frame refuses rather than misreads.** GIVEN `Page.getFrameTree` with a
   child frame `F` that has NO corresponding `iframe` target, WHEN `tab text --frame F`,
   THEN `ERR[frame-not-separate]` naming the remedy (`tab js` reads it), exit 2 — never
   the top document's text.
8. **Frame freshness is keyed by loaderId.** GIVEN a `Page.frameNavigated {frame:{id:'F',
   loaderId:'L2'}}` after `L1`, WHEN `tab frames`, THEN `F`'s `loaderId` is `L2`; WHEN the
   reply is compared, a same-document change that reuses `L1` must not report a new
   document.
9. **Unique execution context across navigation.** WHEN the CLI evaluates after a
   possible navigation, THEN the request carries `uniqueContextId` (or is refreshed from
   `Runtime.executionContextCreated`); GIVEN a stale context that is destroyed, THEN the
   reply is `ERR[eval-timeout]` (reads) — a mutation verb (e.g. `tab click`) is never
   retried automatically.
10. **`insertText` for characters, key events for keys.** WHEN `tab insert "héllo🙂"`, THEN
    exactly one `Input.insertText {text:"héllo🙂"}` is sent and NO
    `Input.dispatchKeyEvent`; WHEN `tab press "Enter"`, THEN an
    `Input.dispatchKeyEvent` with `type:'keyDown'`/`'keyUp'` and `code:'Enter'` is sent.
    WHEN `tab press "A"` with Shift, THEN `modifiers` includes Shift (8).
11. **Non-printing key has no text.** WHEN `tab press "ArrowLeft"`, THEN the key event has
    `type:'rawKeyDown'` and empty/absent `text` (matches Playwright's
    `type: text ? 'keyDown' : 'rawKeyDown'`), so the page does not receive a stray
    character.
12. **Click is trusted input at the element's centre.** WHEN `tab click --selector "#b"`,
    THEN the stream is `mouseMoved` → `mousePressed {button:'left', buttons:1, clickCount:1}`
    → `mouseReleased`, all at the element's viewport-centre `x/y`, and `x/y` are CSS pixels
    relative to the main frame viewport (per the protocol text). GIVEN a point that reaches
    a different node, THEN `ERR[occluded]` naming the reaching element and no `mousePressed`
    is sent.
13. **Upload binds by objectId and proves by `input.files`.** WHEN
    `tab upload F --selector "#in"`, THEN `DOM.setFileInputFiles {files:[F], objectId:…}`
    (objectId, not nodeId, so a shadow root works), THEN the CLI reads back `input.files`
    and refuses `ERR[upload-not-verified]` unless exactly one file with the same **name and
    byte size** is present; WHEN the objectId is missing it falls back to
    nodeId/backendNodeId and that fallback is recorded.
14. **Download events are opt-in and completion is stat-verified.** WHEN the CLI enables
    downloads, THEN it sends `Browser.setDownloadBehavior {behavior:'allow', downloadPath:P,
    eventsEnabled:true}` (never `Page.setDownloadBehavior`); GIVEN a `Browser.downloadProgress
    {state:'completed', filePath:Q}` it must `stat(Q)` itself and refuse
    `ERR[download-not-verified]` if the file is absent (the protocol says `filePath` is not
    guaranteed and the file may not exist).
15. **`allow` without a path refuses locally.** WHEN `tab download` (or any verb that sets
    the download dir) is called with no path, THEN the CLI refuses `bad-args` **before**
    sending `Browser.setDownloadBehavior` (mirrors the browser's
    `InvalidParams("downloadPath not provided")`).
16. **Full-page size limit is named, not truncated.** GIVEN a fake
    `Page.captureScreenshot` that returns the browser's `ServerError("Page is too large.")`,
    WHEN `tab screenshot --full`, THEN `ERR[screenshot-not-verified]` naming the size limit
    (>=131072 px, Chromium `kMaxDimension`) and no file is written; GIVEN a 0-width/height
    clip response, same refusal with the "0 width/height" cause.
17. **Screenshot proof is the file's own bytes.** WHEN `tab screenshot P --full` succeeds,
    THEN the CLI asserts P starts with `89 50 4E 47 0D 0A 1A 0A`, that chunk 0 is `IHDR`,
    and that IHDR width/height equal reported `dim × devicePixelRatio`; if any check fails
    it writes nothing and refuses `ERR[screenshot-not-verified]`. GIVEN `--force` absent and
    P existing, `ERR[file-exists]` with no CDP `captureScreenshot` sent.
18. **Beyond-viewport side effects are acknowledged, not hidden.** WHEN `tab screenshot
    --full` is used, THEN the reply carries a note that the capture resized/emulated the
    page (Chromium mutates WebPreferences + DeviceEmulationParams), and a **live** test
    asserts the page's `navigator.maxTouchPoints`/`window.innerWidth` before vs after are
    restored (or, if not, the drift is reported). *(Live; from `page_handler.cc` and the
    maxTouchPoints hotlist row.)*
19. **Hidden tab is a named refusal.** GIVEN a tab whose page reports `visibilityState !==
    'visible'`, WHEN `tab screenshot`, THEN the CLI either activates it first or refuses
    `ERR[occluded]`/`screenshot-not-verified` naming the hidden state — because a hidden
    surface "will stall" (`kCDPScreenshotNewSurface` CHECK). *(Live or via a fake visibility
    read.)*
20. **Dead renderer / closed target is a refusal, not a retry.** GIVEN the fake transport
    drops the socket or answers a `Target closed`-class error mid-`tab click`, THEN the CLI
    emits `ERR[cdp-unreachable]` (or the target-closed code) and **sends the mutation
    exactly once** — the command log shows no replay (mutations never replay, `docs/plan.md`).
21. **Wait is bounded and sampled.** WHEN `tab wait --for load --timeout 2s` against a
    fake that never fires, THEN `ERR[wait-timeout]` naming `load` and ~2.0 s with `samples >
    0`, exit 2; and the whole poll uses one connection (assert connection-open count == 1).
22. **`networkidle` is labeled a heuristic.** WHEN `tab wait --for idle` resolves, THEN the
    reply field is named as an idle heuristic with the 500 ms rationale (matches Playwright
    `setTimeout(…, 500)`), not `verified: true` as a fact about the page.
23. **Every dialog/mutation result is a named code or a read-back.** Property test: for
    each of `click|check|select|insert|press|upload|dialog|screenshot|nav`, the JSON is
    `ok:true` **only** when a read-back field is present (`changed`, `checked`, `files`,
    `PNG header`, `url_read+moved`), else the reply is a refusal with a code from the
    registry — never `ok:true` with a bare acknowledgement.
24. **Secret redaction survives every one of the above.** GIVEN a password field typed into
    (or an upload/dialog path containing a secret), THEN `lib/audit.py`'s JSONL contains no
    secret value and the verb's reply does not echo it — asserted by scanning the whole log
    for the planted secret after each scenario.

---

## Notes / limits

- **Direct evidence vs inference.** All §1–§7 claims tied to a fetched URL are direct.
  The two items marked **[courier gap]** (removal of `>>>`/`/deep/`, a canonical
  scroll-and-stitch implementation, a "headless downloads disabled" upstream note, the
  Chromium issue 383465171 page, and a same-process-frame upstream citation) could not be
  fetched this run because the Chromium issue tracker served a sign-in page and SearXNG's
  Google/DDG/Brave backends returned `Suspended: too many requests / access denied` —
  note the engine pool was healthy for the first ~20 queries and then exhausted.
- **One conflict to record.** `Input.dispatchMouseEvent` documents `x/y` as *"relative to
  the main frame's viewport"* (protocol), yet this repo drives a cross-origin frame through
  its own target and measured that **frame-local** coordinates fire the frame's handler
  (**project-measured**, `docs/progress.md` §5.24). Reading: in flat/session mode the
  *root* of "viewport" is the target's own main frame, so the two are consistent when you
  attach to the iframe target; they are **not** consistent if you try to click a frame's
  element from the top-level page session.
- **The README's own honest limit stands up** against the sources: the page-independent
  oracles are the PNG's IHDR bytes (§7), `/proc`, and the browser's own protocol errors
  (§1's `"No dialog is showing"`, §4's `"downloadPath not provided"`, §7's
  `"Page is too large."`) — everything else a DOM verb reports is the page's JavaScript
  (§6: `isTrusted` is exactly the field a page can distinguish trusted input by, but
  `checked`/geometry/`:hover` remain page-owned).
