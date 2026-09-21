"""verbs.profile — the `profile` subcommand handlers."""
from __future__ import annotations

from browser_control.cli.argv import (
    _int,
    _none,
    _pop,
    _switch,
)
from browser_control.lib import profile as profile_lib
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    fail,
)


def cmd_profile_info(rest: list[str], browser: str) -> dict:
    """`profile info [--profile DIR]`."""
    _none(rest, "profile info")
    if browser:
        fail(ERR_BAD_ARGS,
             "profile info: --browser does not narrow it — the instance is "
             "named with --profile DIR, and a scope that cannot apply is "
             "refused rather than dropped")
    return profile_lib.info()

def cmd_profile_logins(rest: list[str], browser: str) -> dict:
    """`profile logins [--profile DIR] [--site HOST] [--cap N]`."""
    rest, site = _pop(rest, "--site", "profile logins")
    rest, cap = _pop(rest, "--cap", "profile logins")
    _none(rest, "profile logins")
    if site is not None and not str(site).strip():
        fail(ERR_BAD_ARGS,
             "profile logins: --site needs a HOST — an empty value is not a "
             "site; leave the flag out to report every host")
    return profile_lib.logins(
        browser=browser, site=site or "",
        cap=(_int(cap, "profile logins --cap") if cap is not None
             else profile_lib.DEFAULT_SITES))

def cmd_profile_seed(rest: list[str], browser: str) -> dict:
    """`profile seed --from DIR [--force] [--dry]`."""
    rest, source = _pop(rest, "--from", "profile seed")
    rest, force = _switch(rest, "--force")
    rest, dry = _switch(rest, "--dry")
    _none(rest, "profile seed")
    return profile_lib.seed(source or "", browser=browser, force=force,
                            dry=dry)

def cmd_profile_reset(rest: list[str], browser: str) -> dict:
    """`profile reset [--force]`."""
    rest, force = _switch(rest, "--force")
    _none(rest, "profile reset")
    return profile_lib.reset(browser=browser, force=force)
