"""seedtree — the profile tree walk and the verified copy.

One walker and one copy engine, with the skip policy passed IN: `walk(source,
skips=...)` never consults a module-level set, so each verb names the policy it
means (`seed` copies nothing from the skips; `reset` counts them).
`TreeFacts` stays dict-shaped — it IS the manifest the replies report — so the
verb code keeps reading `facts["bytes"]`.

The MANIFEST carries a size per file (`sizes`), recorded by the walk and
re-recorded by the copy from the SOURCE it read — stat'ed just before `copy2`
and again just after it: the read-back's oracle is that record, never a
`getsize` of the source at check time, because a source in use (a RUNNING
browser, the documented main case) rewrites its `*-journal`/`*-wal` and leveldb
`.log` files while the copy runs — re-statting it made files that HAD landed
read as "did not land". A source file whose size moved under the copy is
reported as `changed`: a fact about the source, not a missing file, and its size
expectation is dropped with it. The size the copy recorded is the SOURCE's, so
a destination that landed SHORT is named by `missing` instead of verifying
against its own truncated self.
"""
from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass, field

from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    ERR_PROFILE_UNUSABLE,
    ERR_SEED_FAILED,
    fail,
)
from browser_control.lib.paths import (
    ensure_root,
    root,
)

__all__ = ["TreeFacts", "copy_verified", "has_content", "missing",
           "private_dir", "remove", "trees_overlap", "walk"]


@dataclass
class TreeFacts:
    """What a copy would touch — and, after it ran, what it WROTE.

    `entries` is the manifest (every file the walk saw), `sizes` maps each of
    those paths to the SOURCE size the copy read it from, and `changed` names
    the files whose source moved while the copy ran — whose size expectation is
    therefore gone. The two are the read-back's whole oracle: `missing` compares
    the landed file against `sizes`, so nothing it says depends on what the
    source looks like afterwards, and a destination that landed SHORT is a
    failure rather than its own (truncated) evidence.
    """

    bytes: int = 0
    files: int = 0
    dirs: int = 0
    links: int = 0
    special: int = 0
    links_planted: int = 0
    unreadable: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    entries: list = field(default_factory=list)
    sizes: dict = field(default_factory=dict)
    changed: list = field(default_factory=list)

    def __getitem__(self, key: str):  # noqa: ANN204
        return getattr(self, key)

    def __setitem__(self, key: str, value) -> None:  # noqa: ANN001
        setattr(self, key, value)


def walk(source: str, *, skips: frozenset,
         descend_skips: bool = False) -> TreeFacts:
    """Everything a naive copy would touch: bytes, files, dirs, skips, links.

    Walks by hand rather than with `copytree` for four reasons: the skips are by
    name at any depth, symlinks are COUNTED and not followed (a profile can
    contain links into the filesystem, and following one would write wherever it
    pointed), a directory that cannot be READ is counted rather than skipped
    silently (it used to make a partial copy verify: neither the walk nor the check
    saw it — a review flagged it), and the numbers are what the reply reports.

    `descend_skips` descends into the skip directories so a caller about to
    DELETE the tree (`profile reset`) sees and reports what the skip list would
    otherwise hide — a half-gigabyte `Cache` used to be wiped as "0 files,
    0 bytes" (a review flagged it). `seed` keeps the default: it copies nothing
    from there, so those bytes are not part of its manifest.
    """
    facts = TreeFacts()
    stack = [source]
    while stack:
        here = stack.pop()
        try:
            children = list(os.scandir(here))
        except OSError as e:
            facts.unreadable.append(f"{here}: {e}")
            continue
        for child in children:
            if child.name in skips:
                facts.skipped.append(child.name)
                if descend_skips and child.is_dir(follow_symlinks=False):
                    stack.append(child.path)
                continue
            if child.is_symlink():
                facts.links += 1
                continue
            if child.is_dir(follow_symlinks=False):
                facts.dirs += 1
                stack.append(child.path)
                continue
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                # a FIFO or a device: copying one blocks with no deadline, and
                # the manifest has to agree with what the copy will do
                facts.special += 1
                continue
            facts.bytes += info.st_size
            facts.files += 1
            facts.entries.append(child.path)
            # the size the copy will be CHECKED against, recorded as it is
            # walked: the source may be rewritten before the read-back runs
            facts.sizes[child.path] = info.st_size
    return facts


def trees_overlap(source: str, target: str) -> str:
    """How two trees OVERLAP, by REAL path, or "" when they are separate.

    By REAL path, because the lexical spelling is not the identity: a `--from`
    that reached the destination through a SYMLINK passed the `abspath` compare
    and the copy walked into its own output —
    `Default/inst/Default/inst/…` until ENAMETOOLONG, 938 directories / 1560
    files / 312 levels deep, all of it left behind (a review measured it). The
    source being the destination, being INSIDE it, or CONTAINING it is refused
    `bad-args` before anything is written; the caller and the copy both ask
    this, so the guard does not depend on either one remembering.
    """
    real_source = os.path.realpath(source)
    real_target = os.path.realpath(target)
    if real_source == real_target:
        return "they are the same directory"
    if real_target.startswith(real_source + os.sep):
        return f"the destination {target} is INSIDE the source {source}"
    if real_source.startswith(real_target + os.sep):
        return f"the source {source} CONTAINS the destination {target}"
    return ""


def private_dir(path: str) -> None:
    """Create `path` 0700 — and NARROW a directory that is already there.

    `os.makedirs(mode=…, exist_ok=True)` applies `mode` only when it CREATES
    the leaf: `mkdir "$ROOT/work"` (0755) followed by
    `profile seed --profile "$ROOT/work"` left a world-traversable instance
    whose copied files kept the source's modes (a review measured it), against
    the documented "a seeded instance directory is 0700". A directory that
    already existed is chmod'ed; an `OSError` there is a REFUSAL, because an
    instance other users can walk into is not one this CLI will seed.

    The managed ROOT itself is exempt: `BROWSER_CONTROL_ROOT` names a directory
    the CALLER chose, `ensure_root` already creates it 0700 when it makes it,
    and a copy does not get to change the mode of a directory it did not make.
    """
    if os.path.realpath(path) == os.path.realpath(root()):
        return
    existed = os.path.isdir(path)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as e:
        fail(ERR_SEED_FAILED, f"cannot create {path}: {e}")
    if existed:
        try:
            os.chmod(path, 0o700)
        except OSError as e:
            fail(ERR_PROFILE_UNUSABLE,
                 f"cannot make {path} private (0700): {e} — a seeded instance "
                 "directory must not be traversable by other users")


def missing(entries: list[str], source: str, target: str, *,
            sizes: dict[str, int] | None = None) -> list[str]:
    """Files the COPY walked that the target does not hold at the RECORDED size.

    The oracle is the manifest the copy walked — `sizes`, the size each file
    was copied at — and never a second stat of the SOURCE: re-statting read the
    source's CURRENT state, so a running Chrome's lazy rewrite of
    `*-journal`/`*-wal`/leveldb `.log` files refused a seed whose files HAD all
    landed, with a cause that was simply false (a review measured it; a source
    that moved under the copy is `TreeFacts.changed` — a reported fact).

    An entry with no recorded size (a caller that passes paths only) is checked
    for EXISTENCE: there is nothing else to compare it against, and inventing
    an expectation from the source is the bug above. The copy DROPS the size of
    a file whose source moved under it (see `_record_copy`), so such a file
    falls back to the same existence check — the source it was read from is not
    a fixed thing to compare against, and it is reported as `changed` instead.
    The refusal is kept for what genuinely did not land — absent, or present at
    a size other than the SOURCE size it was copied from — and at most 8 are
    named.
    """
    absent: list[str] = []
    for path in entries:
        relative = os.path.relpath(path, source)
        landed = os.path.join(target, relative)
        try:
            landed_size = os.path.getsize(landed)
        except OSError:
            absent.append(relative)
        else:
            want = None if sizes is None else sizes.get(path)
            if want is not None and landed_size != want:
                absent.append(relative)
        if len(absent) >= 8:
            break
    return absent


def _record_copy(facts: TreeFacts, path: str, landed: str, *,
                 before: int, after: int | None) -> None:
    """Record the size the read-back OWES for one walked file, and if it moved.

    `facts.sizes[path]` is the size the SOURCE had as the copy read it
    (`before`, stat'ed just before `copy2`), and that is what `missing` holds
    the destination to. Re-recording the size the file LANDED at instead made
    the destination its own oracle: a 1000-byte source truncated to a 10-byte
    destination verified as `verified: true` and was reported only as `changed`,
    with a note blaming the source (a review measured it).

    A source whose size moved under the copy — `after` differs, or it cannot be
    stat'ed any more — has no size the destination can be held to, because the
    bytes landed from somewhere between the two: its expectation is DROPPED (the
    destination is still checked for EXISTENCE) and the file is reported as
    `changed`. That is the same rule as before, from the other side: a file that
    really landed is never called missing because the source moved under it.
    """
    try:
        # `copy2` returned, so the file is there: this call only proves it can
        # be stat'ed at all — a destination that cannot is a filesystem failure,
        # not a miss — while the SIZE it must match is the SOURCE's, below.
        os.path.getsize(landed)
    except OSError as e:
        fail(ERR_SEED_FAILED,
             f"cannot read back {landed} just after copying it: {e}")
    if after is None or after != before:
        facts.sizes.pop(path, None)
        facts.changed.append(path)
        return
    facts.sizes[path] = before


def _source_size(path: str) -> int | None:
    """The SOURCE file's size now, or None when it cannot be read any more.

    `None` is a fact this copy needs, not an error: a source being rewritten (a
    running browser moving its `*-journal`/`-wal` files) is the documented main
    case, and a file that vanished mid-copy is reported as `changed` rather than
    failing the whole seed at that point.
    """
    try:
        return os.stat(path, follow_symlinks=False).st_size
    except OSError:
        return None


def copy_verified(source: str, target: str, dry: bool, *,
                  skips: frozenset) -> TreeFacts:
    """Copy source into target, skipping `skips` names and symlinks.

    The destination is never a LINK and never inside the SOURCE, both checked
    by real path: `makedirs(exist_ok=True)` accepts a symlinked directory and
    `copy2` follows it, and a destination nested in the source makes the walk
    descend into its own output for ever (see `trees_overlap`). Every directory
    this copy makes is 0700, and one that already existed is NARROWED to it
    (`private_dir`).
    """
    facts = walk(source, skips=skips)
    if dry:
        return facts
    # the root FIRST, so it is never an intermediate of the profile's own
    # makedirs and left at the umask default 0755
    ensure_root()
    stack = [(source, target)]
    while stack:
        here, there = stack.pop()
        if os.path.islink(there):
            # never write INTO a link that is already there — the per-child
            # check below cannot see the destination ROOT itself, so a link
            # planted at `<target>/Default` would have sent the whole copy
            # into whatever it pointed at (the user's real profile, say)
            fail(ERR_SEED_FAILED,
                 f"refusing to copy into {there}: it is a symlink — this CLI "
                 "writes a profile directory, never through a link")
        overlap = trees_overlap(source, there)
        if overlap:
            # defence in depth beside the caller's own same-tree refusal: the
            # walk creates `there` FIRST, so a destination that resolves into
            # the source would be scanned straight back into the copy
            fail(ERR_BAD_ARGS,
                 f"refusing to copy {source} into itself: {overlap} — the "
                 "copy would walk into its own output")
        # the destination's PARENT first, as a leaf: `makedirs(there)` applies
        # `mode` to the leaf only and creates intermediates at the umask
        # default, so a single-profile source (`<target>/Default`) left
        # `<target>` at 0755 while `launch` creates the same directory as a leaf
        # 0700 (a review flagged it). The root is already 0700 above, so every
        # intermediate made here is ours — and `private_dir` NARROWS one that
        # already existed, which is what made a pre-created (0755) instance
        # stay traversable. A directory that cannot be made private refuses
        # there.
        parent = os.path.dirname(there)
        if parent:
            private_dir(parent)
        private_dir(there)
        try:
            children = list(os.scandir(here))
        except OSError:
            continue
        for child in children:
            if child.name in skips or child.is_symlink():
                continue
            destination = os.path.join(there, child.name)
            if os.path.islink(destination):
                # never write THROUGH a link that is already there — neither a
                # file one nor a DIRECTORY one: `copy2` follows the first and
                # `makedirs(exist_ok=True)` accepts the second, so the bytes
                # would land outside this profile (a review flagged the dir
                # case, which this check used to be BELOW). Counted and named.
                facts.links_planted += 1
                continue
            if child.is_dir(follow_symlinks=False):
                stack.append((child.path, destination))
                continue
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue                 # the manifest already counted it
            try:
                # `copy2` keeps the source's FILE mode on purpose: inside a
                # 0700 directory (`private_dir`) that mode is not a disclosure,
                # and rewriting it would hand Chrome files with modes it did
                # not choose.
                shutil.copy2(child.path, destination)
            except OSError as e:
                fail(ERR_SEED_FAILED, f"cannot copy {child.path}: {e}")
            # the source's size on BOTH sides of the copy: the one before it is
            # the expectation the read-back checks the destination against, and
            # the two together are how a source rewritten UNDER the copy is told
            # apart from a file that did not land (`_record_copy`)
            _record_copy(facts, child.path, destination,
                         before=info.st_size, after=_source_size(child.path))
    return facts


def has_content(path: str, *, ours: tuple[str, ...]) -> bool:
    """Does that directory exist and hold anything? Never raises.

    Our OWN bookkeeping does not count. Taking the profile lock (whose file lives
    inside the profile) before this check made a fresh, empty profile look
    occupied, so `profile seed` into a new directory refused `profile-exists` —
    measured while fixing the lock order, and the reason `ours` filters them out.
    """
    try:
        return bool([name for name in os.listdir(path) if name not in ours])
    except OSError:
        return False


def remove(path: str) -> None:
    """Remove a file if it is there. A removal that cannot happen is not a
    failure — the thing it was removing is what matters, and the caller checks
    that."""
    try:
        os.remove(path)
    except OSError:
        return
