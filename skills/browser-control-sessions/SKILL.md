---
name: browser-control-sessions
description: Use logged-in browser sessions with browser-control-cli — seed a managed profile with logins copied from the everyday Chrome/Chromium profile, inspect what a profile's login stores contain, run multiple instances/accounts, and attach/detach a browser the user started. Use when a site needs authentication, a separate account or session, or the user's own browser must be driven.
---

# Sessions and profiles with browser-control-cli

This skill covers authentication and instances. It builds on the
`browser-control` skill: lifecycle, verbs, handles, capability classes, and
refusals are documented there.

## The model

- The CLI drives a **managed browser**: its own profile under
  `BROWSER_CONTROL_ROOT` (default `~/.local/share/browser-control/cdp-profiles`),
  one directory per browser binary name. It is never the user's profile.
- `--profile DIR` names the **instance** (a profile directory under the root):
  that is how two instances of one browser — two logins, two sessions — are
  told apart on every verb.
- The user's everyday browser is **listed and readable** by `list` / `tab list` /
  `tab text`, but is never written to unless the user explicitly grants it with
  `attach` (tab writes only; `close` never stops it).
- Managed browsers load **no extensions** (a wallet extension was measured
  starving a headless instance). The files may still be in a seeded profile;
  the code is never loaded.

## Use the everyday logins in an isolated profile

The safest pattern: copy the user's logins into a managed profile and drive
that, leaving the user's browser untouched.

```bash
# 1. what managed profiles exist (size, age, running, attached)
browser-control-cli profile info

# 2. what logins are IN a profile — hosts, cookie NAMES, counts, expiry only;
#    never a cookie value, username, or password
browser-control-cli profile logins --site x.com | jq '{stores, sites}'

# 3. see what a seed WOULD copy (and what --force would destroy) first
browser-control-cli profile seed --from ~/.config/google-chrome --dry

# 4. do it; --force wipes an existing target first (source content, never a mix)
browser-control-cli profile seed --from ~/.config/google-chrome

# 5. drive the site headless on the seeded profile
browser-control-cli open --headless https://x.com
browser-control-cli tab text --chars 500
browser-control-cli close --force
```

Source shapes for `--from`:

- `~/.config/google-chrome` — a whole user-data directory (all profiles), or
- `~/.config/google-chrome/Default`, `.../Profile 1` — one Chrome profile.

Both land where Chrome reads them on the target.

`profile logins` reply (shape):

```json
{"ok": true, "profile": "…/google-chrome-stable", "exists": true,
 "profile_dirs": ["…/Default"],
 "stores": {"cookies": {"present": true, "readable": true, "rows": 12,
                        "hosts": 2, "error": ""},
            "passwords": {"present": true, "readable": true, "rows": 0,
                          "origins": 0, "error": ""}},
 "sites": [{"host": ".x.com", "count": 10,
            "cookies": ["auth_token", "ct0", "twid"], "expired": false}],
 "site": "x.com", "snapshot": false}
```

Honest limits of that evidence: a session-only cookie has no expiry on disk,
a cookie can be revoked server-side, and `snapshot: true` means a browser is
running on the profile so the disk may lag it. A store that cannot be read is
`readable: false` with the reason in `stores.<name>.error` — **unknown, not
zero**. Use it to judge "is this profile still logged in", not as a session
test; the real test is reading the site.

## Two accounts / two sessions

```bash
ROOT=~/.local/share/browser-control/cdp-profiles
browser-control-cli open --profile "$ROOT/work"    --headless https://example.com
browser-control-cli open --profile "$ROOT/personal" --headless https://example.com
# every verb now addresses one instance explicitly
browser-control-cli tab text --profile "$ROOT/work" --tab active
```

Each instance seeds independently. Concurrent calls are serialized per profile
lock; a second call refuses `profile-busy` naming the holder.

To wipe an instance (logins included): `profile reset --force`
(without `--force` it refuses when the profile exists).

## Attach the user's own browser (tab writes only)

This is the consent step for writing to a browser the CLI did not start. The
browser must have been started with an explicit `--user-data-dir` (its command
line is the verification); a vendor-default profile cannot be verified and
refuses `attach-failed`.

```bash
# the user starts their browser with the profile named explicitly, e.g.
google-chrome --user-data-dir="$HOME/.config/google-chrome" --remote-debugging-port=9222

browser-control-cli attach --port 9222        # or --pid N, or --profile DIR
browser-control-cli attach --list             # what is attached, still up?
browser-control-cli tab text --tab active     # writes now allowed on tabs
browser-control-cli detach --port 9222        # revoke; or --all
```

Rules:

- `attach` grants **tab writes only**. `close` never stops an attached browser,
  and `detach` revokes the grant.
- Without a grant, a write at that browser refuses `not-managed`.
- `close --pid N` / `--port N` can stop a browser the CLI did not start, but
  that is a named, deliberate act — only do it when the user asked.

## Safety rules

- Never run `profile seed`, `profile reset`, or `attach` unless the user asked
  or confirms. They copy credentials, wipe data, or grant write access.
- Always `--dry` a seed first; remember `--force` **wipes the target** before
  copying.
- Prefer a seeded managed profile over attaching the user's live browser; it
  cannot disturb what they are doing.
- Secrets: `profile logins` never prints values, and `insert`/`type` never echo
  text (the audit log records a password it touched as a length). Do not put
  credentials into `tab js` expressions or into replies.
- For the cleanest copy, close the source browser before seeding; store reads
  are taken from copies precisely because Chrome keeps the real files open.
