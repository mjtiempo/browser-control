# Sessions and profiles

## Its own profile, not yours

Managed browsers run on `BROWSER_CONTROL_ROOT` (default
`~/.local/share/browser-control/cdp-profiles`), one directory per browser
binary name. Your everyday browser is listed by `list` and readable by
`tab list` / `tab text`, but it is never written to unless you say so.

`--profile DIR` picks the **instance** — a profile directory under the root —
which is how two instances of one browser, two logins, two sessions, are
addressed on every verb:

```bash
ROOT=~/.local/share/browser-control/cdp-profiles
browser-control-cli open --profile "$ROOT/work"     --headless https://example.com
browser-control-cli open --profile "$ROOT/personal" --headless https://example.com
browser-control-cli tab text --profile "$ROOT/work" --tab active
```

Everything is serialized: one profile lock for `open`/`close`, one for the
attach records, so two calls cannot race a profile (`profile-busy` names the
holder).

## `profile info`, `profile logins`, `profile seed`, `profile reset`

`profile seed` copies logins in; `profile logins` says what is actually there —
without opening a browser, and without reading a single value:

```console
$ browser-control-cli profile logins --site x.com
{"ok": true, "profile": "…/google-chrome-stable", "exists": true,
 "profile_dirs": ["…/Default"],
 "stores": {"cookies": {"present": true, "readable": true, "rows": 12,
                        "hosts": 2, "error": ""},
            "passwords": {"present": true, "readable": true, "rows": 0,
                          "origins": 0, "error": ""}},
 "sites": [{"host": ".x.com", "count": 10,
            "cookies": ["auth_token", "ct0", "twid", …],
            "expires": "2027-10-25T22:42:05", "expired": false, "more": 0}],
 "site": "x.com", "snapshot": false, …}
```

Every store is read from a **copy** of the profile's own `Cookies` / `Login
Data` file (Chrome holds the real one open), and only hosts, cookie NAMES,
expiry and COUNTS are reported: never a cookie value, never a username, never a
password. `--site HOST` narrows by host suffix — so `x.com` answers `.x.com`
and `www.x.com`, and `notx.com` is neither — and `--cap N` bounds the hosts
listed. `profile seed` reports the same facts for what it just copied, so
"what landed" is answered by the login stores rather than by file sizes alone.

```bash
# what would a seed copy — and what would --force destroy?
browser-control-cli profile seed --from ~/.config/google-chrome --dry

# copy; an existing target refuses profile-exists unless --force,
# and --force wipes it first: the source's content, never a mix
browser-control-cli profile seed --from ~/.config/google-chrome

# wipe a managed profile, logins included (refuses a live profile)
browser-control-cli profile reset --force
```

`--from` takes a whole user-data directory (`~/.config/google-chrome`) or one
Chrome profile (`.../Default`, `.../Profile 1`); either lands where Chrome
reads it.

It is evidence, not a verdict: a session-only cookie has no expiry on disk, a
cookie can be revoked server-side, and `snapshot: true` says a browser is
running on the profile, so what is on disk may lag it. A store that cannot be
read (a `Cookies` that is not a database, a schema this CLI does not know) is
reported as `readable: false` with the reason in `stores.<name>.error` — its
rows are unknown, not zero.

## Attaching your own browser

`attach` is the consent step for a browser this CLI did not start. It grants
**tab writes only**; `close` never stops an attached browser, and `detach`
revokes the grant.

```bash
# the browser must name its profile on the command line to be attachable
google-chrome --user-data-dir="$HOME/.config/google-chrome" --remote-debugging-port=9222

browser-control-cli attach --port 9222        # or --pid N, or --profile DIR
browser-control-cli attach --list             # what is attached, still up?
browser-control-cli tab text --tab active     # writes now allowed on tabs
browser-control-cli detach --port 9222        # revoke; or --all
```

The verification is the browser's own command line naming the profile
(`--user-data-dir=<profile>`), so a browser started on its vendor default
profile — with no such flag — cannot be verified and `attach` refuses it.
Without a grant, a write at that browser refuses `not-managed`.

Prefer a seeded managed profile over attaching the live browser: it cannot
disturb what you are doing.
