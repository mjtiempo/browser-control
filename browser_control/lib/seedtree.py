"""seedtree — the profile tree walk and the verified copy.

One walker and one copy engine, with the skip policy passed IN: `walk(source,
skips=...)` never consults a module-level set, so each verb names the policy it
means (`seed` copies nothing from the skips; `reset` counts them).
`TreeFacts` stays dict-shaped — it IS the manifest the replies report — so the
verb code keeps reading `facts["bytes"]`.
"""
from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass, field

from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_SEED_FAILED,
    fail,
)

__all__ = ["TreeFacts", "copy_verified", "has_content", "missing", "remove",
           "walk"]


@dataclass
class TreeFacts:
    """What a naive copy would touch: bytes, files, dirs, skips, links."""

    bytes: int = 0
    files: int = 0
    dirs: int = 0
    links: int = 0
    special: int = 0
    links_planted: int = 0
    unreadable: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    entries: list = field(default_factory=list)

    def __getitem__(self, key: str):
        return getattr(self, key)

    def __setitem__(self, key: str, value) -> None:
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
    return facts


def missing(entries: list[str], source: str, target: str) -> list[str]:
    """Files the COPY walked that the target does not have, by size.

    The oracle is the manifest the copy actually walked, not a second walk of the
    source: re-walking read the source's CURRENT state, so Chrome's lazy cookie
    flush refused a seed whose files HAD landed, and a file that vanished from
    the source between the copy and the check was never verified at all (a review
    flagged it).
    """
    absent: list[str] = []
    for path in entries:
        relative = os.path.relpath(path, source)
        landed = os.path.join(target, relative)
        try:
            if os.path.getsize(landed) != os.path.getsize(path):
                absent.append(relative)
        except OSError:
            absent.append(relative)
        if len(absent) >= 8:
            break
    return absent


def copy_verified(source: str, target: str, dry: bool, *,
                  skips: frozenset) -> TreeFacts:
    """Copy source into target, skipping `skips` names and symlinks."""
    facts = walk(source, skips=skips)
    if dry:
        return facts
    stack = [(source, target)]
    while stack:
        here, there = stack.pop()
        try:
            os.makedirs(there, mode=0o700, exist_ok=True)
        except OSError as e:
            fail(ERR_SEED_FAILED, f"cannot create {there}: {e}")
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
                mode = child.stat(follow_symlinks=False).st_mode
            except OSError:
                continue
            if not stat.S_ISREG(mode):
                continue                 # the manifest already counted it
            try:
                shutil.copy2(child.path, destination)
            except OSError as e:
                fail(ERR_SEED_FAILED, f"cannot copy {child.path}: {e}")
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
