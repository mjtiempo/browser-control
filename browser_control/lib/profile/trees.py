"""trees — the profile tree walk, copy and wipe, under ONE skip policy.

Four verbs need to agree on what a profile "holds" and what is never copied:
`info` and `reset` walk a tree, `seed` copies one and checks a target for
existing content. Split across their own modules, each would have to name its
own skip set, and the verb that REPORTS would count a different set than the
verb that WRITES. `SEED_SKIP` is that one policy; every helper below is the
`seedtree` call that uses it.
"""
from __future__ import annotations

from browser_control.lib import seedtree
from browser_control.lib.paths import (
    LOCK_FILE,
    PID_FILE,
)

# Never copied, at any depth, by NAME: the lock files that make a profile look
# in use, our own records, and the caches that are hundreds of megabytes of
# nothing a caller wants. Everything else is copied, so a Chrome version that
# adds a new login store still gets seeded.
SEED_SKIP = frozenset({
    "SingletonLock", "SingletonSocket", "SingletonCookie", "Lock File",
    "DevToolsActivePort", ".pid", ".browser-control.lock",
    "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
    "DawnCache", "DawnGraphiteCache", "GraphiteDawnCache", "Media Cache",
    "CacheStorage", "ScriptCache", "Crashpad", "Crash Reports",
    "BrowserMetrics", "BrowserMetrics-spare.pma", "component_crx_cache",
    "extensions_crx_cache", "Safe Browsing", "SSLErrorAssistant",
    "First Run", "Last Version",
})


def _tree(source: str, count_skips: bool = False) -> seedtree.TreeFacts:
    """The profile tree walk, with the seed/copy skip policy named here."""
    return seedtree.walk(source, skips=SEED_SKIP, descend_skips=count_skips)


def _copy(source: str, target: str, dry: bool) -> seedtree.TreeFacts:
    """The verified copy, with the seed/copy skip policy named here."""
    return seedtree.copy_verified(source, target, dry, skips=SEED_SKIP)


def _has_content(path: str) -> bool:
    """`seedtree.has_content`, with this CLI's own bookkeeping named."""
    return seedtree.has_content(path, ours=(LOCK_FILE, PID_FILE))


_remove = seedtree.remove
