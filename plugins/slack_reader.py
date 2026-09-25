"""slack-reader — read a Slack message, its thread, or a channel's recent
messages, from a permalink.

One call that replaces the walk a caller otherwise does by hand: navigate to
the permalink, get past the desktop-app launch stub, wait for the client to
render the message, read it, notice that Slack split one long payload across
several messages, and open the reply bar when the thread is asked for. Every
step is the core CLI's own verb through `plugin_api` — this file is the SITE
knowledge: the URLs, the selectors, and what Slack's own words mean.

Two reads share that machinery: `slack message PERMALINK` answers the message
the permalink names (with `--thread`, the replies the bar opens), and
`slack channel PERMALINK` answers the channel's recent messages, merged across
the timeline's recycled window because the client unmounts what scrolls out.

The permalink is the input (a message's "Copy link"), and the reply is the
page's own answer: `sender`, `text`, `posted_at` (from the message's own
timestamp), the `parts` when Slack chunked it, and — with `--thread` — the
thread pane's replies, with the reply bar's own count beside them so a
virtualized pane cannot look complete when it is not.

Three things here were measured on the real client, and each is why the code
looks the way it does:

* **The launch stub answers `readyState complete`.** A workspace permalink
  renders an interstitial ("We've redirected you to the desktop app. You can
  also open this link in your browser.") whose own link opens the web client;
  `--for load` passes on the stub, so the ADDRESS is the oracle. `_open` waits
  for the client's URL, and clicks the stub's link only when the address never
  left the stub.
* **Slack cuts a message at ~4 000 characters and posts the overflow as the
  next message.** Measured: one error payload arrived as four messages whose
  timestamps are 20–60 ms apart, each 3 730–3 835 characters. `_continuations`
  walks the rendered messages while they are the SAME sender and less than
  `CHUNK_GAP_S` apart — a second, orders of magnitude above the measured gap
  and far below "two different messages from one app".
* **The reply bar is not inside the message body.** `[data-qa="reply_bar_count"]`
  is a sibling of the body inside the container, so the selector that names
  "the reply bar OF THAT message" needs `:has()`, exactly as the shipped
  Google plugin does for its result cards.

Install: copy this file into ``~/.local/share/browser-control/plugins/`` (or
any directory in ``BROWSER_CONTROL_PLUGIN_PATH``). The browser must be logged
in to the workspace on the tab's profile — seed the managed profile with
``profile seed`` first.

    browser-control-cli slack message \
        https://raventrack.slack.com/archives/C02Q99A8VGS/p1790340351996489 \
        --thread
    browser-control-cli slack channel \
        https://raventrack.slack.com/archives/C02Q99A8VGS --cap 20
"""
from __future__ import annotations

import contextlib
import re
import time
from datetime import datetime, timezone

from browser_control import plugin_api
from browser_control.plugin_api import (
    ControlError,
    errors,
    fail,
    float_arg,
    int_arg,
    pop,
    switch,
    tab_arg,
    text_arg,
)

# --- Slack web's rendered shape (the one place these selectors live) -------
MESSAGE = '[data-qa="message_container"]'
BODY = '[data-qa="message-text"]'
SENDER = '[data-qa="message_sender_name"]'
REPLY_BAR = '[data-qa="reply_bar_count"]'
THREAD_PANE = '[data-qa="threads_flexpane"]'
#: The stub's own link — `/messages/` is the address Slack's launch page puts
#: on it, and the one place that spelling is trusted is right after a nav to a
#: workspace permalink, before the client has claimed the tab.
STUB_LINK = 'a[href*="/messages/"]'
#: The address a workspace permalink ends on once the client has the tab. The
#: measured spelling; an enterprise domain that redirects elsewhere simply
#: takes the stub link's fallback path instead.
CLIENT_MATCH = "app.slack.com/client/"

PERMALINK_FIELDS = ["ts=[data-ts]@data-ts",
                    f"sender={SENDER}",
                    f"claimed={REPLY_BAR}",
                    f"text={BODY}"]
PROBE_FIELDS = ["ts=[data-ts]@data-ts", f"sender={SENDER}"]
PART_FIELDS = ["ts=[data-ts]@data-ts", f"text={BODY}"]
THREAD_FIELDS = ["ts=[data-ts]@data-ts", f"sender={SENDER}", f"text={BODY}"]
WINDOW_FIELDS = ["ts=[data-ts]@data-ts", f"sender={SENDER}", f"text={BODY}"]

DEFAULT_CHARS = 4_000        # one Slack message's worth of one message
MAX_CHARS = 20_000           # `tab extract`'s own per-field ceiling
DEFAULT_TIMEOUT_S = 30.0     # a cold client is slow to render a deep link
STUB_TIMEOUT_S = 6.0         # how long the address is given to leave the stub
THREAD_TIMEOUT_S = 15.0      # the pane mounts after the reply-bar click
PARTS_CAP = 12               # overflow messages followed before giving up
PROBE_CAP = 120              # rendered containers read to find them
THREAD_CAP = 50              # replies one pane read returns

# --- `slack channel`: the timeline's window, and what loading it costs ------
#: `tab extract`'s page-side budget is 20 000 chars of field text for the WHOLE
#: reply, so a window read is `--cap × --chars` that has to fit inside it: the
#: defaults below do, and a caller who asks for more of either still gets
#: `truncated` from the extraction rather than a silent shortfall.
DEFAULT_CAP = 20             # messages a channel read aims for
MAX_CAP = 50                 # and the most one reply may carry
DEFAULT_WINDOW_CHARS = 500   # chars kept of one message's text
MAX_WINDOW_CHARS = 4_000
DEFAULT_SCROLLS = 5          # wheel steps spent trying to fill `--cap`
MAX_SCROLLS = 30
SCROLL_PIXELS = 2_400        # one wheel event, a few viewports
SCROLL_PAUSE_S = 1.0         # Slack mounts the next window after the wheel
STALL_PAUSE_S = 0.4          # a second read before calling a round empty
STALL_ROUNDS = 2             # empty rounds in a row = nothing older is coming
#: Same sender, this close in the timestamp, and it is the SAME payload's
#: overflow — measured at 20–60 ms; a second is the conservative bound.
CHUNK_GAP_S = 1.0

#: `https://<workspace>.slack.com/archives/<CHANNEL>/p<TS>` — the address a
#: message's own "Copy link" gives. The timestamp is the last 6 digits
#: (microseconds) split off the packed seconds.
_PERMALINK = re.compile(
    r"^https?://(?P<host>[^/]+)/archives/(?P<channel>[A-Za-z0-9]+)"
    r"/p(?P<packed>\d{7,20})$")
#: `https://app.slack.com/client/<TEAM>/<CHANNEL>/<TS>` — what the address bar
#: holds once the client has the tab; accepted so a caller who copied THAT can
#: use it, with the ts already dotted.
_CLIENT = re.compile(
    r"^https?://[^/]+/client/(?P<team>[A-Za-z0-9]+)/(?P<channel>[A-Za-z0-9]+)"
    r"/(?P<ts>\d{1,12}\.\d{1,9})$")
_CLAIMED = re.compile(r"(\d+)\s+repl")
#: `…/archives/<CHANNEL>` — the channel itself, with or without a message on
#: the end (the message form opens the window AT that message).
_CHANNEL = re.compile(
    r"^https?://[^/]+/archives/(?P<channel>[A-Za-z0-9]+)"
    r"(?:/p\d{7,20})?$")
#: `…/client/<TEAM>/<CHANNEL>[/<ts>]` — the address bar's own spelling.
_CLIENT_CHANNEL = re.compile(
    r"^https?://[^/]+/client/[A-Za-z0-9]+/(?P<channel>[A-Za-z0-9]+)"
    r"(?:/\d{1,12}\.\d{1,9})?$")


def _ts(value: object) -> float:
    """A Slack ts as seconds — the sort key, and 0 for what is not one."""
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _stamp(ts: str) -> str:
    """The message's own time, from the timestamp Slack encodes in its id.

    No verb can read a wall clock the page never shows, but a Slack ts IS one:
    `1790340351.996489` is Unix seconds. An unparsable one answers "" rather
    than a made-up date — the ts itself is still in the reply.
    """
    try:
        moment = datetime.fromtimestamp(_ts(ts), timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _permalink(url: str) -> tuple[str, str]:
    """`(channel, ts)` for either Slack message address, or a refusal."""
    text = str(url or "").strip().split("?")[0].split("#")[0].rstrip("/")
    match = _PERMALINK.match(text)
    if match:
        packed = match.group("packed")
        return match.group("channel"), f"{packed[:-6]}.{packed[-6:]}"
    match = _CLIENT.match(text)
    if match:
        return match.group("channel"), match.group("ts")
    fail(errors.ERR_BAD_ARGS,
         f"slack message: {url!r} is not a Slack message address — give the "
         "permalink from the message's own \"Copy link\", "
         "https://<workspace>.slack.com/archives/<CHANNEL>/p<TS>")


def _claimed(text: object) -> int | None:
    """The reply count the bar STATES, or None when it states none.

    The page's own claim is kept beside what the pane rendered: a long thread
    is virtualized, so "we read 12" must never be read as "there are 12".
    """
    match = _CLAIMED.search(str(text or ""))
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:      # `\d+` makes this arm unreachable; the contract
        return None         # is "None for what cannot be read", so it stays


def _channel_ref(url: str) -> str:
    """The channel id a channel address names, or a refusal."""
    text = str(url or "").strip().split("?")[0].split("#")[0].rstrip("/")
    for pattern in (_CHANNEL, _CLIENT_CHANNEL):
        match = pattern.match(text)
        if match:
            return match.group("channel")
    fail(errors.ERR_BAD_ARGS,
         f"slack channel: {url!r} is not a Slack channel address — give the "
         "address from the channel's own \"Copy link\", "
         "https://<workspace>.slack.com/archives/<CHANNEL>")


def _leftovers(args: list[str], verb: str, takes: str) -> None:
    """Refuse a flag this verb does not read, BY NAME.

    `pop` takes out the flags this file knows, so anything left that still
    looks like a flag is one nobody reads — and the core's own verbs refuse
    those rather than guessing. Without this a mistyped flag surfaced as "one
    TEXT at most, got 2", which names neither the flag nor the fix; the CLI
    then appends this verb's usage line to the unknown-option refusal. A bare
    `--` ends the flags, as it does everywhere else in the tool.
    """
    for word in args:
        text = str(word)
        if text == "--":
            return
        if text.startswith("-"):
            fail(errors.ERR_BAD_ARGS,
                 f"{verb}: unknown option {text!r} — this verb takes {takes}")


def _open(url: str, tab: str, browser: str) -> bool:
    """Navigate, and get PAST the desktop-app launch stub. True if it was clicked.

    The stub's document is COMPLETE (`--for load` passes on it, measured), so
    the address is what says whether the client took the tab. Only when the
    address never left the stub is its own link clicked — and a client that was
    already there, or a workspace without a stub, simply skips it.
    """
    plugin_api.nav(url, tab=tab, browser=browser)
    with contextlib.suppress(ControlError):
        plugin_api.wait("url", match=CLIENT_MATCH, timeout=STUB_TIMEOUT_S,
                        tab=tab, browser=browser)
        return False
    with contextlib.suppress(ControlError):
        plugin_api.click(selector=STUB_LINK, tab=tab, browser=browser)
        return True
    return False


def _row(matches: object, ts: str) -> dict | None:
    """The record whose own ts is `ts`, from an extraction's rows."""
    for row in matches if isinstance(matches, list) else []:
        if isinstance(row, dict) and str(row.get("ts") or "") == ts:
            return row
    return None


def _target(ts: str, tab: str, browser: str, chars: int) -> dict:
    """The permalink's own message, extracted BY its timestamp.

    Targeted rather than "the Nth rendered message": the client mounts a
    window of messages, the budget is the whole extraction's, and the one
    record that must not be cut is the message the caller asked for.
    """
    data = plugin_api.extract(each=f'{MESSAGE}:has([data-ts="{ts}"])',
                              fields=PERMALINK_FIELDS, cap=1, chars=chars,
                              tab=tab, browser=browser)
    row = _row(data.get("matches"), ts)
    if row is None:
        fail(errors.ERR_NO_MATCH,
             f"slack message: the client rendered no message at ts {ts} — "
             "the page's own answer, not a claim about whether it exists")
    return row


def _sequence(tab: str, browser: str) -> list[dict]:
    """Every rendered container's ts and sender — the overflow walk's map.

    Cheap on purpose: two short fields per row, so the whole rendered window
    fits inside `tab extract`'s page-side budget and the message the caller
    asked for is never the row that got cut. Only the neighbours are needed to
    find Slack's overflow messages; their TEXT is read separately, and only
    for the ones actually walked.
    """
    data = plugin_api.extract(each=MESSAGE, fields=PROBE_FIELDS, cap=PROBE_CAP,
                              chars=64, tab=tab, browser=browser)
    rows: list[dict] = []
    for row in data.get("matches") or []:
        ts = str((row or {}).get("ts") or "")
        if ts:
            rows.append({"ts": ts, "sender": str(row.get("sender") or "")})
    rows.sort(key=lambda row: _ts(row["ts"]))
    return rows


def _payload(target: dict, sequence: list[dict]
             ) -> tuple[str, list[str], list[str], bool, bool]:
    """The payload's group sender, its ts values ABOVE and BELOW the target,
    whether the walk budget ran out, and whether its head is above the
    rendered window.

    Slack renders the author once per message group: the message that posted a
    chunked payload carries the sender's name and every overflow message after
    it has an EMPTY sender cell. So "the same sender" is `sender == group or
    ""` — Slack's own grouping, read from the page rather than guessed. The
    gap bound is what keeps a second, unrelated message from the same app out:
    measured, one payload's parts are 20–60 ms apart.

    A permalink that points INTO a payload asks `_payload` for its head — and
    the client mounts NOTHING above a deep-linked message (measured: a jump to
    the third message of four rendered the third onward), so when the group's
    own header is not in the window the answer names that instead of passing
    the tail off as the whole payload. Nothing before the target is walked in
    that case: without the header there is no sender to prove a row belongs to
    this payload, and a wrong guess is worse than a named gap.
    """
    index = next((i for i, row in enumerate(sequence)
                  if row["ts"] == target["ts"]), None)
    if index is None:
        return "", [], [], False, False
    sender = str(target.get("sender") or "")
    head_missing = False
    if not sender:
        for row in reversed(sequence[:index]):
            if row["sender"]:
                sender = row["sender"]
                break
        if not sender:
            head_missing = True
    seen = {target["ts"]}
    before: list[str] = []
    if not head_missing:
        first = target["ts"]
        for row in reversed(sequence[:index]):
            gap = _ts(first) - _ts(row["ts"])
            if row["ts"] in seen or not 0 < gap <= CHUNK_GAP_S \
                    or row["sender"] not in ("", sender):
                break
            seen.add(row["ts"])
            before.append(row["ts"])
            first = row["ts"]
            if len(before) >= PARTS_CAP:
                break
    after: list[str] = []
    last = target["ts"]
    capped = len(before) >= PARTS_CAP
    for row in sequence[index + 1:]:
        gap = _ts(row["ts"]) - _ts(last)
        if row["ts"] in seen or not 0 < gap <= CHUNK_GAP_S \
                or row["sender"] not in ("", sender):
            break
        seen.add(row["ts"])
        after.append(row["ts"])
        last = row["ts"]
        if len(before) + len(after) >= PARTS_CAP:
            capped = True
            break
    before.reverse()
    return sender, before, after, capped, head_missing


def _parts(extra: list[str], tab: str, browser: str,
           chars: int) -> tuple[dict[str, str], bool]:
    """The overflow messages' own text, in ONE extraction, keyed by ts."""
    if not extra:
        return {}, False
    each = ", ".join(f'{MESSAGE}:has([data-ts="{ts}"])' for ts in extra)
    data = plugin_api.extract(each=each, fields=PART_FIELDS, cap=len(extra),
                              chars=chars, tab=tab, browser=browser)
    found: dict[str, str] = {}
    for row in data.get("matches") or []:
        found[str((row or {}).get("ts") or "")] = str(row.get("text") or "")
    return found, bool(data.get("truncated"))


def _thread(ts: str, tab: str, browser: str, chars: int) -> dict:
    """The replies the reply bar opens, with the page's own count beside them.

    The bar is clicked, not the pane navigated to: the client's own thread
    address was measured to land back on the stub, while the click is one
    in-page action on a pane that is already mounted. A pane that never mounts
    is the wait's refusal, named, rather than an empty thread.
    """
    bar = f'{MESSAGE}:has([data-ts="{ts}"]) {REPLY_BAR}'
    # the bar can sit below the fold; revealing it is a courtesy, and the
    # click's own hit-test is the thing that decides
    with contextlib.suppress(ControlError):
        plugin_api.scroll(selector=bar, tab=tab, browser=browser)
    plugin_api.click(selector=bar, tab=tab, browser=browser)
    plugin_api.wait("element", selector=THREAD_PANE, timeout=THREAD_TIMEOUT_S,
                    tab=tab, browser=browser)
    data = plugin_api.extract(each=f"{THREAD_PANE} {MESSAGE}",
                              fields=THREAD_FIELDS, cap=THREAD_CAP,
                              chars=chars, tab=tab, browser=browser)
    replies: list[dict] = []
    for row in data.get("matches") or []:
        row = row or {}
        reply_ts = str(row.get("ts") or "")
        if not reply_ts or reply_ts == ts:
            continue                    # the pane mounts the parent too
        replies.append({"ts": reply_ts, "posted_at": _stamp(reply_ts),
                        "sender": str(row.get("sender") or ""),
                        "text": str(row.get("text") or "")})
    return {"replies": replies}


def _read_window(tab: str, browser: str, cap: int, chars: int) -> dict:
    """One extraction of what the client currently has mounted."""
    return plugin_api.extract(each=MESSAGE, fields=WINDOW_FIELDS, cap=cap,
                              chars=chars, tab=tab, browser=browser)


def _merge(seen: dict[str, dict], data: dict) -> list[str]:
    """Add this window's fresh messages to `seen`, first occurrence winning.

    Merged BY ts: the client recycles its rows, so the same message arrives in
    several windows, and it must come home once. The author is stored as the
    page rendered it — an empty cell for a grouped continuation — and resolved
    once the walk is done, because the group's name may be in a window the
    caller has not read yet.
    """
    fresh: list[str] = []
    for row in data.get("matches") or []:
        row = row or {}
        ts = str(row.get("ts") or "")
        if not ts or ts in seen:
            continue
        seen[ts] = {"ts": ts, "posted_at": _stamp(ts),
                    "sender": str(row.get("sender") or ""),
                    "text": str(row.get("text") or "")}
        fresh.append(ts)
    return fresh


def _collect(tab: str, browser: str, cap: int, chars: int,
             max_scrolls: int) -> tuple[list[dict], dict, bool]:
    """The channel's recent messages, and what loading them took.

    The loop a caller used to run by hand: extract what is mounted, wheel the
    timeline UP (older messages are above), extract again, merge by ts — one
    extraction can only ever answer the mounted window, and the client unmounts
    what scrolls out. Bounded by `max_scrolls`, and stopped early when nothing
    older arrives (`STALL_ROUNDS` empty rounds, each given a second read after
    a beat, so a slow mount is not mistaken for the beginning of the channel).

    `stop` names the cause the way the core names every other one, and
    `truncated` is the OR of every extraction's own cut flag plus "stopped
    short": a reply never claims to hold everything when a read was cut or the
    loading ran out.
    """
    seen: dict[str, dict] = {}
    data = _read_window(tab, browser, cap, chars)
    _merge(seen, data)
    truncated = bool(data.get("truncated"))
    reads, scrolls, stall = 1, 0, 0
    stop = ""
    if not seen:
        # an empty channel, or a wall: the wheel has nothing to load
        stop = "no-messages"
    while not stop and len(seen) < cap and scrolls < max_scrolls:
        try:
            plugin_api.scroll(by=-SCROLL_PIXELS, tab=tab, browser=browser)
        except ControlError:
            stop = "scroll-failed"
            break
        scrolls += 1
        time.sleep(SCROLL_PAUSE_S)
        data = _read_window(tab, browser, cap, chars)
        reads += 1
        truncated = truncated or bool(data.get("truncated"))
        fresh = _merge(seen, data)
        if not fresh:
            time.sleep(STALL_PAUSE_S)
            data = _read_window(tab, browser, cap, chars)
            reads += 1
            truncated = truncated or bool(data.get("truncated"))
            fresh = _merge(seen, data)
        if fresh:
            stall = 0
        else:
            stall += 1
            if stall >= STALL_ROUNDS:
                stop = "exhausted"
    if len(seen) > cap:
        # a window mounts many at once, so the round that reaches the target
        # can overshoot it: this verb reads the RECENT messages, so the tail
        # (newest) is what a capped reply keeps — and cutting is truncation
        truncated = True
    rows = sorted(seen.values(), key=lambda row: _ts(row["ts"]))
    if len(rows) > cap:
        rows = rows[-cap:]
    # Slack renders the author once per group: a message whose sender cell is
    # empty belongs to the last name above it, carried forward in ts order
    previous = ""
    for row in rows:
        if row["sender"]:
            previous = row["sender"]
        else:
            row["sender"] = previous
    if not stop:
        stop = "cap" if len(rows) >= cap else "max-scrolls"
    # an exhausted read is the whole channel window; anything else stopped
    # short, so older messages may exist (and an extraction's own cut counts)
    return rows, {"reads": reads, "scrolls": scrolls,
                  "max_scrolls": max_scrolls, "stop": stop}, (
        truncated or stop not in ("exhausted", "no-messages"))


def _channel(args: list[str], browser: str) -> dict:
    """`slack channel PERMALINK [--cap N] [--chars N] [--max-scrolls N]
    [--timeout S] [--tab SPEC]`."""
    args, cap_flag = pop(args, "--cap", "slack channel")
    args, chars_flag = pop(args, "--chars", "slack channel")
    args, scrolls_flag = pop(args, "--max-scrolls", "slack channel")
    args, timeout_flag = pop(args, "--timeout", "slack channel")
    args, tab = tab_arg(args, "slack channel")
    _leftovers(args, "slack channel",
               "a PERMALINK, --cap N, --chars N, --max-scrolls N, "
               "--timeout S and --tab SPEC")
    url = text_arg(args, "slack channel").strip()
    channel = _channel_ref(url)
    # every argument is validated BEFORE the first navigation: a typo must not
    # move the caller's tab and stall a client boot before refusing
    cap = int_arg(cap_flag, "slack channel: --cap") \
        if cap_flag is not None else DEFAULT_CAP
    if cap < 1:
        fail(errors.ERR_BAD_ARGS,
             f"slack channel: --cap must be at least 1, got {cap}")
    cap = min(cap, MAX_CAP)
    chars = int_arg(chars_flag, "slack channel: --chars") \
        if chars_flag is not None else DEFAULT_WINDOW_CHARS
    if chars < 1:
        fail(errors.ERR_BAD_ARGS,
             f"slack channel: --chars must be at least 1, got {chars}")
    chars = min(chars, MAX_WINDOW_CHARS)
    max_scrolls = int_arg(scrolls_flag, "slack channel: --max-scrolls") \
        if scrolls_flag is not None else DEFAULT_SCROLLS
    if max_scrolls < 0:
        # 0 is meaningful ("read the first window, wheel nothing")
        fail(errors.ERR_BAD_ARGS,
             "slack channel: --max-scrolls must be 0 or more, got "
             f"{max_scrolls}")
    max_scrolls = min(max_scrolls, MAX_SCROLLS)
    timeout = float_arg(timeout_flag, "slack channel: --timeout") \
        if timeout_flag is not None else DEFAULT_TIMEOUT_S
    if not 0 < timeout < 3600:
        fail(errors.ERR_BAD_ARGS,
             "slack channel: --timeout must be between 0 and 3600 seconds, "
             f"got {timeout_flag!r}")

    stub = _open(url, tab, browser)
    plugin_api.wait("element", selector=MESSAGE, timeout=timeout,
                    tab=tab, browser=browser)
    messages, loading, truncated = _collect(tab, browser, cap, chars,
                                            max_scrolls)
    if not messages:
        fail(errors.ERR_NO_MATCH,
             "slack channel: the client rendered no messages — the page's "
             "own answer, not a claim about the channel")
    return {
        "ok": True,
        "channel": channel,
        "permalink": url,
        "count": len(messages),
        "truncated": truncated,
        "loading": {**loading, "stub_clicked": stub},
        "messages": messages,
        "note": ("read from the client's own rendered messages and merged "
                 "across the timeline's recycled window: the NEWEST are the "
                 "end of the list, `--cap` bounds the reply, and "
                 "`loading.stop` says why the walk stopped (`cap`, "
                 "`exhausted`, `max-scrolls`, `no-messages`, "
                 "`scroll-failed`); Slack renders the author once per group, "
                 "so a name-less message here carries the one above it — a "
                 "LEADING empty sender means the group's header sits above "
                 "what the walk loaded"),
    }


def run(rest: list[str], browser: str) -> dict:
    """`slack message PERMALINK ...` or `slack channel PERMALINK ...`."""
    args = [str(arg) for arg in rest]
    if not args or args[0] not in ("message", "channel"):
        # the plugin's noun is `slack`; the subcommand is the action
        fail(errors.ERR_BAD_ARGS,
             "slack: the subcommand is required — `slack message PERMALINK "
             "[--thread] ...` or `slack channel PERMALINK [--cap N] ...`")
    head, args = args[0], args[1:]
    return _message(args, browser) if head == "message" \
        else _channel(args, browser)


def _message(args: list[str], browser: str) -> dict:
    """`slack message PERMALINK [--thread] [--chars N] [--timeout S] [--tab SPEC]`."""
    args, want_thread = switch(args, "--thread")
    args, chars_flag = pop(args, "--chars", "slack message")
    args, timeout_flag = pop(args, "--timeout", "slack message")
    args, tab = tab_arg(args, "slack message")
    _leftovers(args, "slack message",
               "a PERMALINK, --thread, --chars N, --timeout S and --tab SPEC")
    url = text_arg(args, "slack message").strip()
    channel, ts = _permalink(url)
    # every argument is validated BEFORE the first navigation: a typo must not
    # move the caller's tab and stall a client boot before refusing
    chars = int_arg(chars_flag, "slack message: --chars") \
        if chars_flag is not None else DEFAULT_CHARS
    if chars < 1:
        fail(errors.ERR_BAD_ARGS,
             f"slack message: --chars must be at least 1, got {chars}")
    chars = min(chars, MAX_CHARS)
    timeout = float_arg(timeout_flag, "slack message: --timeout") \
        if timeout_flag is not None else DEFAULT_TIMEOUT_S
    if not 0 < timeout < 3600:
        fail(errors.ERR_BAD_ARGS,
             "slack message: --timeout must be between 0 and 3600 seconds, "
             f"got {timeout_flag!r}")

    stub = _open(url, tab, browser)
    # the wait IS the render gate: the container that carries this ts is the
    # one element the rest of the call depends on
    plugin_api.wait("element",
                    selector=f'{MESSAGE}:has([data-ts="{ts}"])',
                    timeout=timeout, tab=tab, browser=browser)
    target = _target(ts, tab, browser, chars)
    sequence = _sequence(tab, browser)
    sender, before, after, parts_capped, head_missing = _payload(target,
                                                                sequence)
    texts, parts_cut = _parts(before + after, tab, browser, chars)
    parts = [{"ts": part_ts, "text": texts.get(part_ts, "")}
             for part_ts in before]
    parts.append({"ts": ts, "text": str(target.get("text") or "")})
    parts += [{"ts": part_ts, "text": texts.get(part_ts, "")}
              for part_ts in after]
    # the parts of ONE payload are contiguous text; Slack cut it mid-token, so
    # the join inserts nothing — an inserted newline would corrupt the SQL this
    # was written to read
    reply = {
        "ok": True,
        "channel": channel,
        "ts": ts,
        "posted_at": _stamp(ts),
        "permalink": url,
        "sender": sender,
        "text": "".join(part["text"] for part in parts),
        "parts": parts,
        "chunked": len(parts) > 1,
        "chars": chars,
        "truncated": bool(parts_cut or parts_capped or head_missing),
        "head_missing": head_missing,
        "loading": {"stub_clicked": stub},
        "note": ("read from the client's own rendered messages: `parts` is "
                 "every message of ONE payload (one sender group, its parts "
                 "under a second apart), joined with no separator because "
                 "Slack cuts mid-token; `posted_at` comes from the ts Slack "
                 "encodes"
                 + ("; the payload STARTED ABOVE the rendered window — the "
                    "client mounts nothing before a deep-linked message, so "
                    "`parts` is its tail: open the permalink of the "
                    "payload's first message to read the whole thing"
                    if head_missing else "")),
    }
    if not want_thread:
        return reply
    claimed = _claimed(target.get("claimed"))
    if not str(target.get("claimed") or "").strip():
        reply["thread"] = {"claimed": 0, "count": 0, "replies": [],
                           "truncated": False,
                           "note": ("the message renders no reply bar — Slack "
                                    "shows one only when a thread exists")}
        return reply
    thread = _thread(ts, tab, browser, chars)
    count = len(thread["replies"])
    # the bar's own number is the claim; fewer replies than that means the pane
    # was virtualized and what was read is not the whole thread
    thread.update({"claimed": claimed, "count": count,
                   "truncated": (claimed is not None and count < claimed
                                 or reply["truncated"])})
    if thread["truncated"] and claimed is not None:
        thread["note"] = (f"the reply bar says {claimed} replies; the pane "
                          f"rendered {count} — the client virtualizes a long "
                          "thread, so what came back is what was mounted")
    reply["thread"] = thread
    return reply


PLUGIN = {
    "api": 1,
    "name": "slack-reader",
    "description": "read a Slack message and its thread from a permalink",
    "actions": {
        "slack": {
            "run": run,
            # it navigates (a write), clicks the reply bar, and the browser
            # reaches the network for the workspace: declaring only `read`
            # would let `--allow read` authorise a verb that browses
            "classes": ("read", "write", "egress"),
            "usage": ("slack message PERMALINK [--thread] [--chars N] "
                      "[--timeout S] [--tab SPEC] | slack channel PERMALINK "
                      "[--cap N] [--chars N] [--max-scrolls N] [--timeout S] "
                      "[--tab SPEC] — `message` reads the message a permalink "
                      "names, with the overflow messages Slack split one "
                      "payload into (`parts`) and, with --thread, the replies "
                      "beside the reply bar's own count; `channel` reads the "
                      "channel's recent messages, merged across the "
                      "timeline's recycled window"),
        },
    },
}
