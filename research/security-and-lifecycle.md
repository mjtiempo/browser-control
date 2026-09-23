# Security and process-lifecycle challenges for a CDP-driven CLI (browser-control, Topic 3)

**Provenance.** Every claim below is either **(M)** measured on this host in this run, **(S)** read in
the named source that I fetched (URL inline, in the Sources list), or **(I)** my interpretation —
marked as such. Measurement environment: Linux 7.2.6-cachyos, `google-chrome-stable 153.0.8010.52`
(`/opt/google/chrome/chrome`), Python 3.14.7, 2026-09-23, driving throwaway profiles under
`/tmp` with `--headless=new` + `--remote-debugging-port=0`, plus one page that self-reported via
a local HTTP server so that measurements could be taken with **no CDP client at all**. A real Chrome
(pid was live on `/opt/google/chrome/chrome`, profile `~/.config/google-chrome`) was running
throughout and was never touched. `[unv]`-style tags are not used: anything not fetched or measured
is listed under **Gaps**.

Scope note: the brief assumes the design in this repo — managed profile per binary under
`BROWSER_CONTROL_ROOT`, `/proc` endpoint guard, `ERR[code]` + exit 2 refusals, `attach` =
tab-writes-only, per-profile flock, JSONL action log with redaction, `--allow/--deny` gate.

## Findings

### 1. DevTools endpoint exposure

**What makes it hard.** CDP has no authentication, no session tokens and no scope: the whole
boundary is the transport plus who can reach it. **(M)** With nothing but a local TCP connection to
the port I read cookie *values* back (`Storage.getCookies` returned the `sekret` value I had set),
and two independent WebSocket clients drove the same page target concurrently (both got answers,
`2` and `4`, from separate sockets) — there is no arbitration verb anywhere in the protocol. So
"port open on loopback" is equivalent to "any process of this uid owns this browser".

**Binding.** **(S)** `chrome/browser/devtools/remote_debugging_server.cc` binds
`CreateLocalHostServerSocket()` → `ListenWithAddressAndPort("127.0.0.1", port, 10)` and falls back
to `"::1"` (fetched 2026-09-23). **(S)** `content/public/common/content_switches.cc` in current main
declares only `remote-debugging-port`, `remote-debugging-pipe`, `remote-debugging-socket-name`,
`remote-debugging-io-pipes` — **`--remote-debugging-address` is gone**. **(M)** Passing
`--remote-debugging-address=192.0.2.1` (TEST-NET, chosen so an honoured flag could not expose
anything) changed nothing: `ss -ltn` still showed `LISTEN 0 10 127.0.0.1:43617`. Practical
consequence: the loopback-only claim is now a *defence-in-depth* assertion about the *listener you
found*, not a property you must add; it still matters because the CLI may be pointed at a
non-Chromium listener, a browser on another build/OS, or a port forwarded by another tool.

**Host header / DNS rebinding.** **(S)** `RequestIsSafeToServe()` in
`content/browser/devtools/devtools_http_handler.cc` returns false — and the handler answers
`500 "Host header is specified and is not an IP address or localhost."` — unless
`url.HostIsIPAddress() || net::IsLocalHostname(host)`. **(M)** Verbatim on this build: forged
`Host: evil.example` → `HTTP/1.1 500 … Host header is specified and is not an IP address or
localhost.`; `Host: localhost` → `HTTP/1.1 200 OK`. This is the rebinding defence, and it is what
the VM/host case trips over — Chrome's own agent tooling documents the workaround as an SSH tunnel
rather than widening the check **(S)** `chrome-devtools-mcp/docs/troubleshooting.md`. Rebinding
against *other* localhost HTTP APIs remains a live attack class in exactly this market **(S)**
oligo CVE-2025-49596 writeup (MCP Inspector chained DNS rebinding to RCE, CVSS 9.4).

**WebSocket origin check.** **(S)** `DevToolsHttpHandler::OnWebSocketRequest` rejects an upgrade when
an `Origin` header is present, is not the server's own origin (`url::Origin` of
`http://<server_ip_address>`), and is not in `--remote-allow-origins` (lowercased commas, with `*`
as the wildcard escape hatch); the 403 body names the flag. **(M)** `Origin: http://evil.example` →
`HTTP/1.1 403 … "Rejected an incoming WebSocket connection from the http://evil.example origin. Use
the command line flag --remote-allow-origins=http://evil.example to allow connections from this
origin or --remote-allow-origins=* to allow all origins."`; `Origin: devtools://devtools` → also
**403** on this build; `Origin: http://127.0.0.1:<port>` → accepted. **Key reading (I):** the default
allowlist is effectively empty, so the only clients that pass are native ones sending **no** Origin
(the CLI) or ones sending the loopback origin. Adding `--remote-allow-origins=*` "to make CDP work"
re-opens the browser-originated hole; adding a narrow allowlist is unnecessary for this CLI since it
sends no Origin.

**Consent machinery inside Chrome.** **(S)** `chrome/browser/devtools/features.cc`
`kDevToolsAcceptDebuggingConnections` (enabled by default except ChromeOS), plus
`remote_debugging_server.cc`'s approval path: in that mode **each incoming connection must be
approved by the user**, a denied connection gets `403 "Connection rejected"`, and approvals are
counted in the `DevToolsRemoteDebuggingConnectionPermission` histogram. **(S)** The
`RemoteDebuggingAllowed` enterprise policy (default `true`, `chrome.*:93-`) is the administrative
kill switch; the pref is `devtools.remote_debugging.allowed` (`chrome/common/pref_names.h`). **(S)**
Chrome's own agent tooling now consumes this: Chrome ≥ 144, `chrome://inspect/#remote-debugging`
enables the server and the user must "allow or disallow incoming debugging connections"
(`chrome-devtools-mcp/docs/advanced-usage.md`, `configuration.md`). **(S)** Note the precedence in
`remote_debugging_server.cc`: `--remote-debugging-port`/`-pipe` take precedence over approval mode,
so a CLI that launches with a port gets **no** approval prompt.

**Other concrete failure modes.** Hard-coding 9222 collides with other tooling (the docs' own
examples use it) and makes "which browser is this" ambiguous. **(M)** `DevToolsActivePort` outlives
the browser: after a graceful exit *and* after `SIGKILL` the file still held a dead port
(`44781`), and `http://127.0.0.1:44781/json/version` raised `URLError` — a client that trusts the
file without probing reports `cdp-unreachable` (or worse, talks to whatever later binds that port).
**(S)** The file's content is `"%d\n%s"` = port + browser GUID path (`devtools_http_handler.cc`),
**(M)** e.g. `44501\n/devtools/browser/c081a127-b000-4d39-a5f0-2169a2f607b5`. That GUID is a bearer
capability against *remote* clients only: any local process can read the file or `GET /json/version`
for free, so it must not be logged as if logging it were harmless, and it must not be treated as
authentication.

**Established best practice, named.** Prefer the **pipe transport** (`--remote-debugging-pipe`,
fds 3/4) over a TCP port: Playwright hard-codes it (`chromium.ts`: `chromeArguments.push('--remote-debugging-pipe')`), Chrome's
own `chrome-devtools-mcp` launches with `pipe: true` (`src/BrowserManager.ts`), and Puppeteer uses it
whenever `pipe` is requested, otherwise `--remote-debugging-port=${debuggingPort || 0}`
(`packages/puppeteer-core/src/node/ChromeLauncher.ts`). When a port is unavoidable: `--remote-debugging-port=0`
plus `DevToolsActivePort`, never a fixed port, never `--remote-allow-origins=*`
(`devtools_http_handler.cc`), and never a flag that would publish the interface.

### 2. Headless detection and anti-bot

**What makes it hard.** There is no single "headless bit". There are (a) markers your own transport
turns on, (b) strings your own build emits, (c) CDP side effects that the *page* can observe, and
(d) behavioural/reputation signals you cannot fix at all. Fixing one and leaving the others is worse
than fixing none, because cross-signal inconsistency is itself a signal.

**Measured signals on this build (all (M), chrome 153.0.8010.52, `--headless=new`).**

| launch | how observed | `navigator.webdriver` | UA |
| --- | --- | --- | --- |
| `--headless=new`, **no** remote-debugging flag at all | page POSTed to a local HTTP server (no CDP) | **false** | `… HeadlessChrome/153.0.0.0 Safari/537.36` |
| `--headless=new` + `--remote-debugging-port=0`, **no client ever connects** | same page-report channel | **true** | same |
| `--headless=new` + port + a browser-level ws connected | same | **true** | same |
| `--headless=new` + port + page target attached + `Runtime.evaluate` | CDP | **true** | same |
| `--headless=new` + `--disable-blink-features=AutomationControlled` (+port) | CDP | **false** | same |
| `--headless=new` + `--enable-automation` (+port) | CDP | **true** | same |
| `--headless=new` + `--enable-automation` + `--disable-blink-features=AutomationControlled` | CDP | **false** | same |
| `--user-agent=…Chrome/153.0.0.0…` (+port) | CDP | **true** | the override is honoured verbatim |

Three things follow, and they matter for this repo's README:

1. **The `HeadlessChrome` token is present under `--headless=new`** on this build.
   **(S)** `components/embedder_support/user_agent_utils.cc` `GetUserAgentInternal()` inserts
   `"Headless"` in front of the product whenever the `headless` switch is present, and
   `GetProductAndVersion()` returns the *reduced* form under UA reduction — hence
   `HeadlessChrome/153.0.0.0`, not `153.0.8010.52`. This resolves the "conflict" in the older
   brief in favour of *present*; do not assume a build where it is absent.
2. **`navigator.webdriver` is turned on by the mere presence of the remote-debugging port**, not by
   any command the client sends: the page reported `w=true` with zero CDP clients, and `w=false`
   when the port was absent. This independently reproduces the README's "Chrome 153 sets
   `navigator.webdriver` when the DevTools port is 0" measurement, and it means the *pipe*
   transport is not just a hardening nicety — **(I)** it is the only transport that does not, by
   itself, mark the page.
3. **`--disable-blink-features=AutomationControlled` is the working lever** (measured to flip
   `true → false` with and without `--enable-automation`), and it is what the ecosystem uses
   **(S)** DataDome's write-up of Puppeteer/Selenium stealth practice. **(S)** The Blink mechanism
   matches: `Navigator::webdriver()` in `third_party/blink/renderer/core/frame/navigator.cc` returns
   `true` if `RuntimeEnabledFeatures::AutomationControlledEnabled()`, else the result of
   `probe::ApplyAutomationOverride(...)`; `AutomationControlled` in
   `runtime_enabled_features.json5` is `base_feature: "none"`, `settable_from_internals: true` —
   i.e. off unless something turns it on by string/internal.

**Source of the automation flag it must never pass.** **(S)**
`packages/puppeteer-core/src/node/ChromeLauncher.ts` includes `--enable-automation` in its default
arguments; **(S)** `Emulation.setAutomationOverride` ("Allows overriding the automation flag",
experimental) exists in the protocol and is implemented in
`third_party/blink/renderer/core/inspector/inspector_emulation_agent.cc::setAutomationOverride`.
**(M)** On a launch *without* `--enable-automation`, `setAutomationOverride(true)` flipped
`false → true` and `(false)` flipped it back — so a CLI can also assert the flag's value rather than
infer it, but **(M)** on a launch where the runtime feature is already on
(`--enable-automation`), `setAutomationOverride(false)` did **not** turn it off (the runtime-feature
branch short-circuits). Do not promise "turn webdriver off over CDP".

**The `Runtime.enable` side-channel.** **(S)** DataDome's research ("How New Headless Chrome & the
CDP Signal Are Impacting Bot Detection", 2024) documents the mechanism precisely: `Runtime.consoleAPICalled`
events are dispatched only after the client sends `Runtime.enable`; Chrome buffers console messages
otherwise; a page can therefore set a getter on `Error.prototype.stack` (which V8 formats lazily),
pass the object to `console.log`, and set a flag when CDP serialises it —
`Runtime.enable` present ⇒ `detected === true`. **(S)** `rebrowser-patches` states the same in
operational terms: "This technique is **used by all major anti-bot software** such as Cloudflare,
DataDome, and others", and lists three patches (addBinding, isolated worlds, enable+disable) as the
mitigations. **(M) Candidate correction for this repo:** I ran the DataDome snippet verbatim in a
page that the CLI-style client drove, and it did **not** fire — neither for the message logged before
`Runtime.enable` (read back `unset`) nor for a detector armed immediately after `Runtime.enable`
(flag `false` after 1.5 s and after a full round trip), while the same snippet loaded *before* any
CDP client reported `unset` too. My harness is not the vendors' integration, so **(I)** the honest
claim is: *the CLI never needs `Runtime.enable` (`Runtime.evaluate` works without it, and that
removes the documented vector by construction), but this run did not reproduce the detector's
positive control, so nobody should assert "we are undetectable" or "their detector no longer works"
from either result.*

**UA spoofing shape.** **(M)** With no override, `navigator.userAgentData.brands` was
`[{Google Chrome,153},{Not_A Brand,8},{Chromium,153}]` and the high-entropy `fullVersionList` still
carried `153.0.8010.52` — i.e. the reduced UA freezes the *low*-entropy version while the real build
is available to any origin that asks for entropy; **(M)** `--user-agent=…Chrome/153.0.0.0…` changed
`navigator.userAgent` verbatim while UA-CH brands stayed `Google Chrome/153`. **(S)** Chromium's
reduction rules live at chromium.org/updates/ua-reduction (fetched via the current source behaviour
above), and `--user-agent` is honoured only if `HttpUtil::IsValidHeaderValue` accepts it
(`components/embedder_support/user_agent_utils.cc`, with a `LOG(WARNING)` otherwise). **(I)** So the
only defensible override is "read the binary's own version, write the reduced form, never
de-hardcode the platform" — and it must be stated as cosmetic, since UA-CH is unaffected.

**Folklore to reject, with the measurement that rejects it.** "Headless is marked by
`navigator.webdriver`" — falsified here: headless with no debugging port reported `false`. "You need
a fake UA to get past a bot wall" — unverified in this run (see Gaps). "CDP injects `cdc_*`" — that
is ChromeDriver's document injection, not a CDP artifact. "Cloudflare blocks headless" — **(S/I)**
the in-repo README records the project's own measurement of a challenge *looping* rather than
blocking; treat that as in-repo evidence, not something I re-measured, and note its test consequence
below.

### 3. Profile isolation

**What makes it hard.** The user-data directory *is* the isolation boundary, and since Chrome 136
Chrome enforces part of that rule itself — but only under Google branding, and only for the
*default* directory, so a CLI cannot outsource the guarantee.

**Chrome 136+, the headline change.** **(S)** `developer.chrome.com/blog/remote-debugging-port`
(2025-03-17): from Chrome 136, `--remote-debugging-port` and `--remote-debugging-pipe` "will no
longer be respected if attempting to debug the default Chrome data directory", they must now be
accompanied by `--user-data-dir`, a non-standard directory "uses a different encryption key", and
browser-automation users are pointed at Chrome for Testing. **(S)** Source:
`remote_debugging_server.cc::IsRemoteDebuggingAllowed()` with
`NotStartedReason::kDisabledByDefaultUserDataDir`, gated on `chrome::IsUsingDefaultDataDirectory()`
and on `BUILDFLAG(GOOGLE_CHROME_BRANDING)` (`true` for Chrome; for Chromium branding the check
exists but is compiled to a testing-only hook). **(I)** The CLI must therefore refuse a vendor
default profile itself; on a Chromium-branded/older/forked build Chrome will *not* refuse.

**(M)** This host is exactly the hazard case: a real Chrome is running on `~/.config/google-chrome`,
and its `/proc/<pid>/cmdline` is bare (`/opt/google/chrome/chrome`), i.e. it was launched with the
default profile. Any verb that resolved a profile to that path, or launched without
`--user-data-dir`, would be inside the user's browser.

**Delegation and "profile in use".** **(M)** A second launch against a `--user-data-dir` a live
browser already owns **aborts**: exit code 21, stderr
`ERROR:process_singleton_posix.cc:347] Failed to create <dir>/SingletonLock: File exists (17)` then
`Failed to create a ProcessSingleton for your profile directory. … Aborting now to avoid profile
corruption.` **(S)** `chrome-devtools-mcp` surfaces the same state as *"The browser is already
running for <userDataDir>. Use --isolated to run multiple browser instances."* **(S)**
`chrome_browser_main.cc::ProcessSingletonNotificationCallback` drops the delegation request when
*either* command line carries `--enable-automation` or when headless is in effect — (I) meaning the
delegate-vs-start decision depends on flags, so the CLI must never infer "the browser I launched is
the browser now running" from its own argv; it must read the process.

**Profile internals, as they are on disk.** **(M)** A profile created by these launch flags contains
`Default/` (0700) with `Cookies` (0600) — Chrome's own mode discipline; the *root* mode is whatever
the creator set (0700 for `mkdtemp`), so a CLI that `os.makedirs()` without a mode gets 0755 and a
world-readable profile. **(S)** `net/extras/sqlite/sqlite_persistent_cookie_store.cc`
`CreateV24Schema` (kCurrentVersionNumber = **24**) and **(M)** the live DB's
`SELECT sql FROM sqlite_master WHERE name='cookies'` matches it verbatim:
`creation_utc, host_key, top_frame_site_key, name, value, encrypted_value BLOB, path, expires_utc,
is_secure, is_httponly, last_access_utc, has_expires, is_persistent, priority, samesite,
source_scheme, source_port, last_update_utc, source_type, has_cross_site_ancestor`;
**(M)** `meta` = `[('mmap_status','-1'),('version','24'),('last_compatible_version','24')]`.
**(S)** Saved logins are the `logins` table (kCurrentVersionNumber = **43**) in
`components/password_manager/core/browser/password_store/login_database.cc` — different file
(`Login Data`), different version counter, same "numbers move per milestone" caveat.
**(M)** `Local State` sits at the **profile root** and, under `--password-store=basic`, carried
**no** `os_crypt` keys at all. **(S)** That is consistent with
`components/os_crypt/async/browser/posix_key_provider.cc`, where the "basic" provider's key is a
hard-coded constant — `PBKDF2-HMAC-SHA1(1 iteration, key="peanuts", salt="saltysalt")` — and the tag
is `v10`. **(I)** Consequence for this CLI: a managed profile launched with `--password-store=basic`
(which is what Playwright and Puppeteer both pass by default) has cookies that are *decryptable by
anyone who can read the file*; the 0700/0600 discipline is not cosmetic, and `profile seed` should
say which key path it relies on. **(S)** The keyring path is the alternative: the freedesktop
provider stores/unlocks its key in kwallet/gnome-libsecret via the product name
(`freedesktop_secret_key_provider.cc`), so a profile copied to another machine or another keyring
holds ciphertext nobody can read — "rows present but undecryptable" is a real state that must not be
reported as "no logins".

**Windows app-bound encryption, for the portability claim.** **(S)** Google's own announcement
(2024-07-30): Chrome 127 introduced App-Bound Encryption on **Windows**, starting with cookies; the
key is verified by a privileged service, is bound to the machine, does not roam ("App-Bound
Encryption strongly binds the encryption key to the machine, so will not function correctly in
environments where Chrome profiles roam between multiple machines"), and can be disabled by policy
`ApplicationBoundEncryptionEnabled`. **(I)** Any "copy your profile and keep your logins" promise is
therefore OS- and path-dependent; on Linux the equivalent boundary is the keyring, and on both
platforms a *copy* is a copy of ciphertext plus a key reference.

**Live-read hazards measured.** **(M)** A cookie set over CDP (`Network.setCookie`) was **not** on
disk in the immediately following read, and `Cookies-wal`/`-shm` did not exist once the browser had
exited cleanly — i.e. with the browser running, the file lags the browser's state; a census verb may
under-report unless it snapshots the DB triple (or copies it immutable) and says so. **(S)** The
named mitigation set for third-party extension churn is Playwright's default switch list
(`chromium.ts`/`chromiumSwitches.ts`): `--disable-extensions`,
`--disable-component-extensions-with-background-pages`, `--disable-field-trial-config`,
`--no-first-run`, `--no-default-browser-check`, `--disable-component-update`,
`--disable-background-networking`, `--password-store=basic`, `--use-mock-keychain`,
`--disable-search-engine-choice-screen`, plus a long `--disable-features=` list. **(I)** The
in-repo README's own measurement (a wallet extension's background pages pinning the main thread at
~126% and making the endpoint answer in 7 s instead of 4 ms) is a project measurement, not one I
reproduced; it is nonetheless the strongest available argument for disabling extensions by flag
rather than by hoping the profile is clean.

**Established best practice, named.** A dedicated `--user-data-dir` per instance, always
(Puppeteer/Playwright: Playwright pushes `--user-data-dir=${userDataDir}` and refuses a caller that
tries to pass its own; `chrome-devtools-mcp` defaults to
`$HOME/.cache/chrome-devtools-mcp/chrome-profile[<channel>]` and offers `--isolated` for a temp dir,
documenting that "Only one browser can use it at a time"). To *reuse* state, copy the directory —
never share it.

### 4. Secrets hygiene in automation tooling

**What makes it hard.** Both channels a CLI naturally uses — argv and the CDP wire — are readable by
parties the CLI does not control, and the protocol does not redact anything.

**(S, measured)** `/proc/<pid>/cmdline` is mode 0444 and readable across uids; `/proc/<pid>/environ`
is mode 0400 and was **not** readable for a root-owned process
(`PermissionError: [Errno 13]`), while the calling user's own environ was (12542 bytes). **(M)**
Sources: `proc_pid_cmdline(5)` — "Think of this file as the command line that the process wants you
to see", it is empty for a zombie (measured: 0 bytes), and a process may rewrite its argv region
(measured below). **(I)** Env is strictly less exposed than argv, but neither is private from the
same uid. Concretely exposed in this CLI: `tab insert`/`tab type` passwords, tokens inside
`tab nav https://u:p@host/?token=…`, `tab js` expressions that embed credentials, and any future
`--token`-style flag.

**Measured argv surprise that also belongs to §6.** **(M)** Chromium rewrites its process title: the
browser process's `/proc/<pid>/cmdline` is **one blob with a single trailing NUL** —
`/opt/google/chrome/chrome --remote-debugging-port=0 --user-data-dir=/tmp/tmpj0e1qnb2 --no-first-run
--no-default-browser-check --headless=new --noerrdialogs --ozone-platform=headless
--ozone-override-screen-size=800,600 --use-angle=swiftshader-webgl about:blank\0`, NUL count 1 —
whereas Chrome's *non*-Chrome helper (`chrome_crashpad_handler`) keeps normal NUL separation
(12 NULs). **(I)** So (a) `cmdline.split("\0")` finds no `--user-data-dir=` element at all, (b) the
blob is a `setproctitle`-style rewrite that a hostile process could fabricate, and (c) any redaction
that operates on "the argv list" must operate on that blob too.

**The wire carries secrets in the clear.** **(M)** `Storage.getCookies` returned the cookie *value*
for a cookie I set, including one set HttpOnly and Secure; **(S)** the protocol's
`Network.getAllCookies`/`Storage.getCookies`, `Runtime.evaluate` results and
`Input.insertText`/`Input.dispatchKeyEvent` payloads are exactly what an audit log must not hold.
**(S)** DevTools *does* gate one channel: `browser_handler.cc` returns the browser command line only
when `--enable-automation` is set ("Command line not returned because --enable-automation not set.")
— not a secrecy feature the CLI can rely on for its own argv.

**Log and artifact hazards.** **(S)** `open(2)`: `O_NOFOLLOW` fails with `ELOOP` if the final
component is a symlink, and `O_CLOEXEC` is "essential in some multithreaded programs" because a
separate `fcntl(F_SETFD)` races `fork`+`execve`. **(S)** `pipe(7)`: Linux `PIPE_BUF` is 4096 and
writes ≤ `PIPE_BUF` are atomic, larger writes may interleave — the "one `write()` per JSONL record"
rule. **(S)** `docs.python.org/3/library/subprocess.html`: `close_fds` defaults true (only 0/1/2
survive), and passing `pass_fds` forces `close_fds=True`; `start_new_session=True` performs `setsid()`.
**(M)** Chrome's own discipline: `Default/` 0700, `Cookies` 0600. **(I)** Artifacts follow the same
rule: a screenshot may show a logged-in session, so the file must be 0600 and must not be written
through a pre-planted symlink (the repo's existing "refuse an existing path without `--force`" is
exactly that mitigation).

**Child environment.** **(S)** `@puppeteer/browsers`' `launch()` logs only the `PUPPETEER_*`-prefixed
env keys it forwards (an explicit acknowledgment that env is a thing to enumerate, not dump) and
spawns with `detached: true` on POSIX; **(I)** this CLI should pass a minimal allowlist
(`PATH, HOME, TMPDIR, XDG_*, DISPLAY, WAYLAND_DISPLAY, LC_*`) rather than `os.environ`, or the child
inherits the caller's `AWS_SECRET_ACCESS_KEY` into a `/proc/<pid>/environ` that any same-uid process
can read.

### 5. Process lifecycle

**What makes it hard.** A browser is a stateful process whose exit path writes to disk, a *tree* of
helpers, and a *recyclable* pid. `kill`, `wait` and "the pid in the file" are each wrong on their own.

**SIGTERM is the graceful path, in source and measured.** **(S)**
`chrome/browser/chrome_browser_main_posix.cc`: `InstallShutdownSignalHandlers(…)` is installed in
`PostCreateMainMessageLoop()` "Exit in response to SIGINT, SIGTERM, etc."; SIGHUP/SIGINT → `Exit()`,
SIGTERM → `chrome::SessionEnding()` (with the comment that the next signal is usually SIGKILL).
**(M)** SIGTERM on a `--headless=new` browser exited `rc=0` promptly and the endpoint stopped
answering (`URLError`). **(S)** The protocol's own graceful verb is `Browser.close` — "Close browser
gracefully" in `pdl/domains/Browser.pdl`, alongside `Browser.crash` ("Crashes browser on the main
thread") and `Browser.crashGpuProcess`. **(I)** Order for a `close` verb: CDP `Browser.close` while
the endpoint answers → SIGTERM to a pid whose identity and cmdline still match → never SIGKILL, and
always verify exit rather than trusting the signal.

**What a kill leaves behind (measured).** **(M)** After `SIGKILL`: `SingletonLock` still present with
its `hostname-pid` target, `SingletonSocket` still present, `DevToolsActivePort` still holding the
dead port. **(M)** After a graceful exit: `SingletonLock` and `DevToolsActivePort` are *also* still
there. **(M)** A fresh launch on that same directory **succeeded** (new port `33233`, file and lock
rewritten, old port dead) — so the lock *file* is not the lock. **(S)** `process_singleton_posix.cc`
confirms the design: `SingletonLock` is a symlink "to a non-existent destination … containing the
hostname and process id" (`"%s%c%u"`, delimiter `-`), locked with `O_RDWR|O_CREAT|O_SYMLINK`, and
liveness is decided by `IsChromeProcess(pid)` = compare `base::GetProcessExecutablePath(pid)`'s
basename with `chrome::kBrowserProcessExecutableName`. **(I)** That is an *exe-name* test with no
start-time — so it is exactly the check a recycled pid defeats when the new holder is also a Chrome
binary. The CLI's stronger rule ("never signal a pid whose own cmdline does not name that profile")
is right; add starttime to close the rest.

**Zombies lie.** **(M)** `os.kill(<zombie_pid>, 0)` **succeeded** with no exception; the same
process's `/proc/<pid>/stat` state was `Z` and its `cmdline` was empty. **(I)** Liveness by signal
therefore reports a dead, unreaped browser as alive — a `close` that waits forever, an `open` that
refuses because the profile "looks busy". Correct liveness: state ∉ {`Z`,`X`,`x`} **and**
identity match. **(S)** `proc_pid_stat(5)` field 22 `starttime` ("time the process started after
system boot", in clock ticks).

**Pid identity beyond starttime.** **(S)** `pidfd_open(2)` (Linux 5.3): the fd refers to the task,
`ESRCH` if it does not exist, closed-on-exec by default, and "Even if the child has already
terminated by the time of the `pidfd_open()` call, its PID will not have been recycled"; use
`pidfd_send_signal(2)` to signal through it. **(M)** This interpreter has both:
`os.pidfd_open` and `signal.pidfd_send_signal` → `True, True`. **(S)**
`docs.python.org/3/library/subprocess.html`: `start_new_session=True` calls `setsid()` in the child;
**(S)** `setsid(2)`: the caller becomes session and process-group leader (no controlling terminal),
which is what insulates the browser from the caller's Ctrl-C/terminal close. **(S)** Puppeteer's
practice: `detached: true` on POSIX explicitly "makes child process a leader of a new process group,
making it possible to kill child process tree with `.kill(-pid)`". **(I)** For this CLI: launch with
`start_new_session=True`, but signal the *browser* and let it reap its own helpers — delivery order
inside a group is unspecified, and killing a renderer mid-write is the corruption being avoided.

**flock.** **(S)** `flock(2)`: locks are attached to the **open file description**, so duplicates
(`fork`, `dup`) share it and the lock is released when *all* such descriptors are closed; `flock` is
advisory only; since Linux 2.6.12 NFS emulates it with `fcntl` byte-range locks (so it is not
strictly local anymore, and an exclusive lock over NFS requires the file opened for writing).
**(I)** Consequences for the CLI: the decision must be `flock(LOCK_EX|LOCK_NB)`'s return value, never
the file's existence (the classic pid-file TOCTOU); the recorded holder pid is advisory text for the
`profile-busy` message and must never drive a signal; the fd must be `O_CLOEXEC` and must **not** be
in the child's `pass_fds`, or the browser inherits the open file description and the lock outlives
the CLI as an immortal ghost lock. **(S)** `chrome-devtools-mcp` adds a second, non-security
serialization at the *service* level (a `Mutex` in `src/index.ts`, plus "Only one browser can use it
at a time"), which is the multi-client problem the protocol itself does not solve (§6).

**Pipe hygiene.** **(S)** `@puppeteer/browsers` `launch()` configures stdio as
`['pipe','pipe','pipe']` (or five pipes in pipe-transport mode), records the streams with
`#recordStream`, and only forwards them to the parent when `dumpio` is set (default `false`).
**(I)** A browser that logs to an undrained pipe blocks once the buffer fills — a launch that "hangs
for no reason". Use `DEVNULL`, or drain.

**Exit-status hygiene.** **(M)** Chromium itself exits **21** when it cannot take the profile's
singleton lock; **(I)** the CLI's exit 2 for refusals must stay distinct from that and from
`128+signal`, and — since `list` can reconstruct everything from `/proc` + a probe — no state file
may be authoritative.

### 6. Attach and consent models

**What makes it hard.** CDP has no ownership, no consent, no read-only scope, no locking. **(M)** Two
clients drove one page target concurrently and both got answers; nothing arbitrates.

**The port is a claim; the kernel is the fact.** **(M)** The reliable chain works on this host: the
listening row in `/proc/net/tcp` (`0100007F:9321` → `127.0.0.1`, inode as the 10th field) → scan
`/proc/*/fd` for `socket:[<inode>]` → `2754960` → `/proc/2754960/exe` = `/opt/google/chrome/chrome`,
state `S`, `starttime 15915179`. **(S)** `/proc/<pid>/cmdline` is not proof of anything
(`proc_pid_cmdline(5)`: it is "the command line that the process wants you to see"; a process may
rewrite it via `prctl(PR_SET_MM_ARG_START)`) — **(M)** and Chromium *does* rewrite it (one
space-separated blob, single trailing NUL; children additionally carry `--type=renderer` and a
`--user-data-dir=` of their own, and the browser keeps the literal `--remote-debugging-port=0` even
though the real port is in `DevToolsActivePort`). **(I)** Therefore:
- match on the browser process (the one holding the listening socket inode), not on "any process
  whose cmdline mentions the profile";
- accept `--remote-debugging-port=0` as consistent with a *discovered* port (the argv cannot
  contain the real one);
- parse the blob by tokenising, and treat a path containing whitespace as a match you cannot confirm;
- cross-check `Browser.getVersion` against the exe: **(M)** `product` = `Chrome/153.0.8010.52` while
  the UA was `HeadlessChrome/153.0.0.0` — comparing those two strings for equality would refuse every
  headless browser. Compare family and major version, not the whole string.

**Consent, honestly stated.** **(S)** The only in-browser consent primitive is Chrome's per-connection
approval (feature `kDevToolsAcceptDebuggingConnections`; `chrome://inspect/#remote-debugging`; deny →
`403 "Connection rejected"`), and it applies to the approval-mode server only — `--remote-debugging-port`
takes precedence. **(S)** The user-visible warning infobar exists but is tied to `--enable-automation`
(`chrome/browser/ui/startup/automation_infobar_delegate.cc`, message id `IDS_CONTROLLED_BY_AUTOMATION`,
priority `kCriticalSecurity`, no buttons); **(S)** Puppeteer passes `--enable-automation` by default,
so a CLI that suppresses the automation marker to avoid detection has removed the only in-browser
signal the user would get. **(I)** That trade-off has to be stated, not hidden — and the CLI's own
`attach` record (owner, profile, pid, starttime, port) is the substitute consent artifact.
**(S)** Playwright's `connectOverCDP` docs are the clearest published statement of the attached
model: "significantly lower fidelity than … `browserType.connect()`", a warning that the caller must
have launched with Playwright's curated argument list or "some Playwright functionality may be
broken", and `noDefaults` — "Useful when attaching to a user's daily-driver browser where these
overrides would interfere with existing browser state."

**What a write-but-never-stop grant should cover.** **(S)** Protocol surface to deny on an attached
browser: `Browser.close` ("Close browser gracefully"), `Browser.crash`, `Browser.crashGpuProcess`,
`Browser.setDownloadBehavior` (and its `Page.` sibling) — a silent file-write primitive into *their*
filesystem; `Fetch`/network interception; and anything that changes the argv-visible mode
(headless/windowed is the owner's decision). **(I)** `tab js` deserves its own capability class on an
attached browser: it is full user-impersonation inside an origin, not "reading the same page".
`profile seed`/`profile reset` must be denied against an attached profile — they write state that
outlives the session, in *their* profile. **In scope:** nav/back/forward/reload, DOM and input verbs,
tab create/close, screenshot/upload against the caller's own paths.

**Multi-client side effects are real.** **(I)** Another client enabling `Page` can cause a pending JS
dialog to be dismissed when its session goes away (this repo's `tab dialog state` may answer
`open: null`), and `tab activate` steals focus from the user in windowed mode. **(I)** `detach` is a
local statement: with no server-side tokens it can only make *this* CLI forget the authorization, so
documentation implying it locks others out would be false, and `attach --list` should keep proving
the browser is still running with `attached: false`.

## Sources

Fetched in this run (2026-09-23); source paths are `raw.githubusercontent.com` reads of `main`/
`master` unless noted.

Chromium source & docs:
- https://developer.chrome.com/blog/remote-debugging-port (Chrome 136 default-data-dir change)
- https://raw.githubusercontent.com/chromium/chromium/main/content/browser/devtools/devtools_http_handler.cc (Host validation, Origin/`--remote-allow-origins`, `DevToolsActivePort` write, GUID)
- https://raw.githubusercontent.com/chromium/chromium/main/chrome/browser/devtools/remote_debugging_server.cc (loopback bind, Chrome-136 gate, approval mode)
- https://raw.githubusercontent.com/chromium/chromium/main/chrome/browser/devtools/remote_debugging_server.h
- https://raw.githubusercontent.com/chromium/chromium/main/chrome/browser/devtools/features.cc (`kDevToolsAcceptDebuggingConnections`)
- https://raw.githubusercontent.com/chromium/chromium/main/content/public/common/content_switches.cc (no `--remote-debugging-address`)
- https://raw.githubusercontent.com/chromium/chromium/main/chrome/browser/process_singleton_posix.cc (SingletonLock scheme, `IsChromeProcess`)
- https://raw.githubusercontent.com/chromium/chromium/main/chrome/browser/chrome_browser_main_posix.cc (SIGTERM/SIGINT/SIGHUP → graceful exit)
- https://raw.githubusercontent.com/chromium/chromium/main/chrome/browser/chrome_browser_main.cc (delegation dropped under automation/headless)
- https://raw.githubusercontent.com/chromium/chromium/main/net/extras/sqlite/sqlite_persistent_cookie_store.cc (Cookies v24 schema)
- https://raw.githubusercontent.com/chromium/chromium/main/components/password_manager/core/browser/password_store/login_database.cc (Login Data v43 schema)
- https://raw.githubusercontent.com/chromium/chromium/main/components/os_crypt/async/browser/posix_key_provider.cc ("peanuts"/"saltysalt" v10 key)
- https://raw.githubusercontent.com/chromium/chromium/main/components/os_crypt/async/browser/freedesktop_secret_key_provider.cc (keyring key storage)
- https://raw.githubusercontent.com/chromium/chromium/main/components/embedder_support/user_agent_utils.cc (Headless product token, reduced UA, `--user-agent` validation)
- https://raw.githubusercontent.com/chromium/chromium/main/third_party/blink/renderer/core/frame/navigator.cc (`webdriver()`)
- https://raw.githubusercontent.com/chromium/chromium/main/third_party/blink/renderer/core/frame/navigator_automation_information.idl
- https://raw.githubusercontent.com/chromium/chromium/main/third_party/blink/renderer/platform/runtime_enabled_features.json5 (`AutomationControlled`)
- https://raw.githubusercontent.com/chromium/chromium/main/third_party/blink/renderer/core/inspector/inspector_emulation_agent.cc (`setAutomationOverride`)
- https://raw.githubusercontent.com/chromium/chromium/main/chrome/browser/ui/startup/automation_infobar_delegate.cc
- https://raw.githubusercontent.com/chromium/chromium/main/content/browser/devtools/protocol/browser_handler.cc (command line gated on `--enable-automation`)
- https://raw.githubusercontent.com/chromium/chromium/main/components/policy/resources/templates/policy_definitions/Miscellaneous/RemoteDebuggingAllowed.yaml
- https://raw.githubusercontent.com/chromium/chromium/main/chrome/common/pref_names.h (`kDevToolsRemoteDebuggingAllowed`)
- https://security.googleblog.com/2024/07/improving-security-of-chrome-cookies-on.html (App-Bound Encryption, Chrome 127, Windows)

CDP protocol:
- https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/json/browser_protocol.json
- https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/pdl/domains/Browser.pdl
- https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/pdl/domains/Emulation.pdl
- https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/pdl/js_protocol.pdl

Clients (Playwright / Puppeteer / Chrome's own agent tool):
- https://playwright.dev/docs/api/class-browsertype (`connectOverCDP` warnings, `noDefaults`)
- https://raw.githubusercontent.com/microsoft/playwright/main/packages/playwright-core/src/server/chromium/chromium.ts (`--remote-debugging-pipe`, `--user-data-dir`)
- https://raw.githubusercontent.com/microsoft/playwright/main/packages/playwright-core/src/server/chromium/chromiumSwitches.ts (default switch list)
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/node/ChromeLauncher.ts (`--enable-automation`, pipe vs port 0)
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/puppeteer-core/src/node/BrowserLauncher.ts
- https://raw.githubusercontent.com/puppeteer/puppeteer/main/packages/browsers/src/launch.ts (`detached`, stdio, env enumeration)
- https://raw.githubusercontent.com/ChromeDevTools/chrome-devtools-mcp/main/README.md , /docs/configuration.md , /docs/advanced-usage.md , /docs/troubleshooting.md , /src/BrowserManager.ts , /src/config/mcp-options.ts (pipe transport, dedicated profile, `--isolated`, profile busy, approval mode/CVE-adjacent docs)

Detection research & advisories:
- https://datadome.co/threat-research/how-new-headless-chrome-the-cdp-signal-are-impacting-bot-detection/ (read via `r.jina.ai`; `Runtime.enable` serialization leak, `--auto-open-devtools-for-tabs` counter-play)
- https://raw.githubusercontent.com/rebrowser/rebrowser-patches/main/README.md (same leak, "used by all major anti-bot software", patch strategies)
- https://www.oligo.security/blog/critical-rce-vulnerability-in-anthropic-mcp-inspector-cve-2025-49596 (read via `r.jina.ai`; rebinding → RCE, CVSS 9.4)

Kernel/POSIX references:
- https://man7.org/linux/man-pages/man5/proc_pid_cmdline.5.html
- https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html (field 22 `starttime`, `Z`)
- https://man7.org/linux/man-pages/man2/pidfd_open.2.html
- https://man7.org/linux/man-pages/man2/flock.2.html
- https://man7.org/linux/man-pages/man2/open.2.html (`O_NOFOLLOW`, `O_CLOEXEC`)
- https://man7.org/linux/man-pages/man7/pipe.7.html (`PIPE_BUF` = 4096)
- https://man7.org/linux/man-pages/man2/setsid.2.html
- https://docs.python.org/3/library/subprocess.html (`start_new_session`, `pass_fds`, `close_fds`)

Not fetchable in this run: `issues.chromium.org/issues/40090537` (sign-in/CAPTCHA wall; the rebinding
UXSS report), `grep.app`/`searchcode` (blocked), so the exact wording of those is *not* cited here.

## Test implications

Numbered input → output contracts. Hermetic (fake `/proc`, fake `/proc/net/tcp`, fake transports,
fake `Popen`, in-process CLI) unless a line says **(live)**. Codes are proposed where a new refusal
is needed; they follow the repo's `ERR[code]`/exit-2 convention.

1. **Loopback by construction.** Launch-argv assertions for `open`/`open --headless`: exactly one
   `--remote-debugging-port=<N>` with N > 0, **no** `--remote-debugging-address` (in any form), no
   `--remote-allow-origins` (not even a narrow one; the CLI sends no Origin), and no value starting
   `--remote-allow-origins=*`. Conversely, a fixture where the discovered listener's local address is
   `00000000:1F90` in fake `/proc/net/tcp` → every drive verb refuses `ERR[cdp-not-loopback]` and the
   fake transport records zero bytes; `0100007F`/`::1` → proceeds.
2. **Port comes from the profile, not from a constant.** A fake profile whose `DevToolsActivePort`
   is `39911\n/devtools/browser/<uuid>` → the CLI dials 39911 and never 9222 (assert no connect to
   9222 in the transport log). A one-line file (port only, no GUID) → retries then refuses, never
   crashes. The file's GUID is never written to the audit log (assert the log line contains no
   `/devtools/browser/`).
3. **Stale `DevToolsActivePort` is not evidence.** Fixture: file present with port P, nothing
   listening on P, a live browser on port Q → `info`/`tab list` report `cdp-unreachable` for P (no
   silent fallback to Q), exit 2, and `close` sends no signal. Second fixture: P is listening but the
   socket's owner is not the pid named in argv → `ERR[cdp-not-local]`.
4. **Host/Origin rejection is named, not misreported.** Fake handshake rejected with the exact
   Chromium bodies (`500 Host header is specified and is not an IP address or localhost.` /
   `403 Rejected an incoming WebSocket connection from the <origin> origin…`) → distinct codes
   (`host-not-local`, `ws-origin-rejected`) whose text names the cause, exit 2, **not**
   `cdp-unreachable`, and the message echoes no credential-bearing URL or the browser GUID.
5. **Loopback origin is never needed.** Fixture asserting the client sends **no** `Origin` header on
   the upgrade (assert the header set of the fake handshake) — the regression test for the
   `--remote-allow-origins` temptation.
6. **Headless UA/provenance is measured, not assumed.** Fake `--version` output
   `Google Chrome 153.0.8010.52` → the reply's `browser_version` and any reduced-form UA both derive
   from that string; the CLI never emits `HeadlessChrome` itself, and never claims the UA is
   unmarked (assert the reply has a `headless_ua_token: true|false` **measured** field, not a
   constant). Fake `--version` failing → no `--user-agent` flag at all and the reply says the UA was
   left to the browser.
7. **`navigator.webdriver` reported as measured, never guessed.** Fake `Runtime.evaluate` text
   `true` → `webdriver: true, verified: true`; a failed eval → `webdriver: null, verified: false`
   (never `false`); a fake page whose value is `false` while the argv lacks
   `--disable-blink-features=AutomationControlled` → still reported as `false` **plus** a note that
   the marker is expected to be on for any port-launched browser (per the measurement in §2), so a
   future Chrome change is visible in the reply rather than silently trusted.
8. **The flag pair that controls the marker.** Every captured launch argv contains
   `--disable-blink-features=AutomationControlled` (when the caller asked for the marker to be
   suppressed) and contains **no** `--enable-automation` and no `--test-type`. With
   `--disable-blink-features=AutomationControlled` present, a launch that also carries
   `--enable-automation` is rejected as contradictory (`ERR[bad-args]`) rather than sent.
9. **No `Runtime.enable`, ever.** With a fake transport recording every frame, run `tab js`,
   `tab wait --for js`, `tab text`, `tab find`, `tab click`: assert the frame set contains
   `Runtime.evaluate` and **zero** `Runtime.enable`, zero `Runtime.disable`, zero `Console.enable`,
   and zero `Debugger.enable`. This is the hermetic regression test for the one CDP side-channel
   that is documented as actively exploited (§2) and needs no browser.
10. **`Emulation.setAutomationOverride` is only ever an assertion.** The CLI may send it only with
    `enabled: false` when it *already* passed the blink flag, and its reply field must be
    `webdriver_override: false` with `verified: true` only if a following read confirms it; a launch
    with `--enable-automation` must never claim it can clear the marker (per the measurement that the
    runtime-feature branch wins).
11. **UA-CH is not silently desynchronised.** Any `--user-agent` value the CLI generates matches
    `^Mozilla/5\.0 \(<platform>\) AppleWebKit/537\.36 \(KHTML, like Gecko\) Chrome/<major>\.0\.0\.0
    Safari/537\.36$` for the platform the binary runs on, never a hard-coded `Macintosh`/`Windows NT`
    on Linux, and never `HeadlessChrome`. A launch with `--user-agent` set is reported as
    `ua_override: true` with a note that `navigator.userAgentData` is unaffected (a fake
    `Runtime.evaluate` returning `{brands:[{Google Chrome,153}], fullVersionList:[…153.0.8010.52…]}`
    must produce exactly that in the reply, not a claim of consistency).
12. **Challenge pages are not success.** Fake page whose title is `Just a moment...` / body contains
    `cf-chl` markers, answered by `tab nav` → the reply carries `challenge: true` and the verb refuses
    `ERR[challenge-blocked]` (or returns `ok: true` **with** the challenge field); it must not report
    plain `moved: true` alone. A control fixture (same DOM, no markers) → `ok: true, challenge: false`.
13. **Profile isolation in argv, always.** Every launch argv contains
    `--user-data-dir=<absolute path under BROWSER_CONTROL_ROOT>/<name>` plus `--no-first-run`,
    `--no-default-browser-check`, `--disable-extensions`,
    `--disable-component-extensions-with-background-pages` (and `--headless=new` when asked). No
    launch argv ever contains a vendor default path; if a `--user-data-dir` was supplied by the
    caller that resolves to `~/.config/google-chrome`, `~/.config/chromium`, their snap/flatpak
    variants or `$HOME` itself → `ERR[default-profile]`, exit 2, no `Popen`.
14. **The 0700/0600 discipline is enforced, not hoped for.** After `open` on a fresh root: the profile
    root, the profile dir and the lock dir are 0700 and the lock file / log file are 0600 (assert with
    `stat`). Fixture where the root already exists 0755 → the verb either fixes it to 0700 or refuses
    `ERR[profile-perms]`; it must not proceed silently.
15. **Profile busy is decided by the lock's liveness, not the file.** (a) Test holds
    `flock(LOCK_EX)` → `open` exits 2 `ERR[profile-busy]` naming the holder pid, `Popen` never called.
    (b) A lock *file* left on disk, no lock held, no live pid matching the lock's `hostname-pid`
    target → `open` succeeds and no signal is ever sent to the recorded pid. (c) A live pid whose
    `/proc/<pid>/exe` basename is not a Chromium-family browser → the lock is treated as stale
    (`profile-busy: false`) with a note, never signalled.
16. **Chrome's own refusal is surfaced, not swallowed.** Fake `Popen` returning exit 21 with
    `process_singleton_posix.cc`/`Failed to create …SingletonLock: File exists` on stderr → the verb
    refuses with a code naming "profile in use" and the profile path, exit 2, within the deadline, and
    does not retry the launch in a loop.
17. **Cmdline is parsed as the blob it is.** Fake `/proc/<pid>/cmdline` containing a single
    space-separated string with one trailing NUL: `--user-data-dir=<profile>` is still found, and a
    sibling fake process with `--type=renderer --user-data-dir=<same profile>
    --remote-debugging-port=0` is **not** treated as the browser. `--remote-debugging-port=0` in the
    blob is accepted as consistent with the discovered port. A profile path containing a space →
    the result is `verified: false` with a reason, not a guessed match.
18. **Identity is (pid, starttime), and signals go through pidfd when available.** Fake `/proc` with
    pid N, starttime A, cmdline matching the profile → `close --pid N` proceeds (signal 15 recorded).
    Flip starttime to B with the same pid → refusal naming pid **and** starttime, zero signals. A
    fixture where `os.pidfd_open` succeeds but the task has exited → `ESRCH`-based refusal, still zero
    signals; a monkeypatched `pidfd_open` returning `ENOSYS` → fall back to starttime + `kill` with a
    note in the reply, never a silent plain-`kill` path.
19. **Zombie ≠ alive.** Fake `/proc/<pid>/stat` state `Z` → `list` does not report the browser as
    running, `close --pid N` refuses `not-running`, and `open` on that profile does not report
    `profile-busy` with the zombie as holder. Same fixture with state `S` → the opposite.
20. **Never SIGKILL, on every stop path.** Fake process that exits on SIGTERM → `close` reports
    `stopped: true` only after `/proc` shows the pid gone or non-`Z`/`X`; the fake records signal 15
    and **never** 9. Fake process that ignores SIGTERM → within the bound, `stopped: false` with a
    reason naming "ignored SIGTERM", exit 2, still no signal 9 — including `close --force --pid N`.
    `close` on a live endpoint sends `Browser.close` **once** and does not follow it with a signal
    while the socket is still open.
21. **Detach-and-nothing-else.** For a granted attach: `close`, `close --pid`, `close --force`,
    `close --port`, `profile seed`, `profile reset` against the attached profile each refuse
    (`ERR[attach-write-only]` / `ERR[not-managed]`) with zero CDP frames — assert specifically that no
    `Browser.close`, `Browser.crash`, `Browser.crashGpuProcess`,
    `Browser.setDownloadBehavior` or `Page.setDownloadBehavior` frame is recorded anywhere in the
    suite. After `detach`, a tab write refuses `not-attached` while `list` still shows the process
    running with `attached: false`.
22. **Attach requires kernel evidence, re-checked per use.** Fake `/proc` where the port's listener
    inode maps to a pid whose `exe` is `/usr/bin/python3` → `ERR[cdp-not-local]`, no websocket
    opened. Fake endpoint answering while argv names a *different* profile → refused. With a valid
    record, flipping the fake starttime for that pid → the next write verb refuses and reports
    `stale: true`, sending nothing. A fake `Browser.getVersion` product `Chrome/153.0.8010.52` with a
    UA of `HeadlessChrome/153.0.0.0` → **accepted** (family+major match), proving the guard does not
    compare full strings; product `Firefox/141.0` → refused.
23. **A vendor default profile is never attachable.** Fake cmdline
    `/opt/google/chrome/chrome --remote-debugging-port=9222 --user-data-dir=/home/u/.config/google-chrome`
    → `attach` refuses `ERR[attach-default-profile]`, exit 2, no record written. The same with a
    non-default dir → a record containing pid, starttime, port, resolved profile.
24. **Secrets never reach the log or stderr.** With a fake transport echoing payloads: `tab insert
    <pw>`, `tab nav https://u:p@h/?token=T`, `tab js 'document.cookie="a=b"'`, `tab type <pw>` →
    assert the JSONL line and stderr contain none of `pw`, `T`, `b`, `u:p`; that the record contains
    a length or a redaction marker for the text a write verb proved it touched; and that the recorded
    argv is redacted **even though** the test's own fake `/proc/<pid>/cmdline` blob still shows the
    raw value (the redactor must read the argv the CLI was given, not `/proc`).
25. **Redaction runs before formatting, and survives truncation.** Fake CDP error whose message
    embeds `set-cookie: session=SECRET` → neither the stderr `ERR[…]` line nor the log contains
    `SECRET`, including in the truncated tail; a `result-too-large` refusal on a payload containing a
    cookie value still redacts.
26. **Log file and artifact safety.** Pre-create the log path as a symlink to another file → the CLI
    refuses (`ERR[log-unsafe]`) or opens `O_NOFOLLOW`; assert the symlink target is byte-identical
    afterwards. Two processes appending 100 records → exactly 200 intact lines, every line ≤
    `PIPE_BUF` (4096). `tab screenshot` to a path pre-created as a symlink → refusal even with
    `--force`, never a write through the link; a created PNG is 0600 and its verification is the
    file's own `\x89PNG` header.
27. **Minimal child environment, no fd leaks.** Fake `Popen` recorder: the env contains
    PATH/HOME/TMPDIR/XDG_*/DISPLAY (as applicable) and does **not** contain a planted
    `AWS_SECRET_ACCESS_KEY`; `close_fds` is not disabled; the lock fd, the log fd and any CDP socket
    are absent from `pass_fds`; `start_new_session=True` is set; stdout/stderr are DEVNULL or a
    drained sink, never an undrained `PIPE`. Then, in-process: release the lock fd and assert a second
    invocation acquires the lock immediately (the immortal-ghost-lock regression).
28. **argv injection is closed.** `open -- --user-data-dir=/tmp/evil`, `tab nav "-http://x"`,
    `open --headless=false`, `--profile ../../etc` → `ERR[bad-args]`, exit 2, `Popen` never called
    with an argument beginning `-`, and no path outside `BROWSER_CONTROL_ROOT` is created. Assert
    `shell` is not enabled and argv is a list.
29. **Capability matrix is a table.** For each (verb → class) × (`--allow`/`--deny` set): allow/refuse,
    the refusal text naming the grant, and zero CDP frames on refusal. Required rows: `--deny js` +
    `tab js`; `--deny js` + an attach (js stays denied); `--allow dom-write` only + `tab screenshot`
    (file write is its own class) and + `tab upload` (file read is its own class); `--deny
    process-stop` + `close`; `--deny profile-mutate` + `profile seed`; `--deny net` + a launch
    (a denied class must not even start a browser).
30. **Version-gated claims are derived, not assumed.** Fixture: `/json/version` reporting a major
    below the build that introduced the WebSocket Origin check / the Chrome-136 data-dir rule →
    `selftest`/`info` report `origin_check: absent` / `default_dir_gate: absent` as *fields*, and any
    test asserting the protection is skipped with a reason (never green by default). A branded-build
    field distinguishes `google-chrome` from Chromium branding for the 136 gate.
31. **(live) Closure of the measured facts.** Against a real browser on a throwaway root:
    `open --headless` then `tab js 'navigator.webdriver'` → the reply's `webdriver` matches what
    `tab js` returns (both false when the blink flag was passed), and the same page loaded with
    nothing attached (a plain `open` with no CDP client) proves the CLI's own launch is the only thing
    that could have flipped it; `close` → exit within the bound, `/proc/<pid>` gone, and the
    `DevToolsActivePort` file's dead port probed, not trusted, by the next `open`.
32. **(live) No silent delegation.** Launch on a fresh profile, then run `open`/`open --headless`
    again against the *same* profile: the second invocation must refuse with the profile-in-use code
    and must not leave a second browser process (assert by counting Chromium-family pids whose exe is
    the resolved binary and whose cmdline names that profile before and after).

## Gaps and conflicts (recorded, not resolved)

- **`Runtime.enable` detector**: my positive control did not fire (§2). The mitigation (never send
  `Runtime.enable`) stands on the protocol's own description plus the vendors' write-ups, but
  "we are undetectable" is unsupported in both directions.
- **Mechanism of port ⇒ `navigator.webdriver === true`**: the behaviour is measured; I did not locate
  the code that enables Blink's `AutomationControlled` (or the override probe) when the remote
  debugging server starts. `chrome://version`'s rendered command line showed **no**
  `--enable-automation` and no `--enable-blink-features` on such a launch, so it is not an appended
  switch. Must be re-measured per build.
- **Cloudflare/bot-wall behaviour**: not measured in this run; the in-repo README's looping-challenge
  observation is the only evidence, and test implication 12 is a design proposal.
- **`--user-agent` + UA-CH high-entropy**: the full version list was measured only *without* the
  override (`153.0.8010.52`); under the override my `getHighEntropyValues` call returned nothing, so
  the desynchronisation in §2 is asserted from the brands result and the source, not fully measured.
- **Snap/Flatpak Chromium profile paths and confinement** were not verified (search backend returned
  empty results late in the run); the process's ability to start on a profile outside its allowed
  paths remains untested here.
- **Approval mode vs `--remote-debugging-port`** precedence is read from source; the user-visible
  dialog text and its behaviour on a windowed browser were not exercised.
- NUL-separation of `/proc/<pid>/cmdline` was measured on **this** build/platform; treat
  "space-joined blob" as version-dependent and test for it rather than assuming either shape.
