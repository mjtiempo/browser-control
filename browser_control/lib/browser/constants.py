"""constants — the shared timing constants and browser names.

A leaf: every other module imports from here, so a constant never creates an
import cycle between the verbs.
"""
from __future__ import annotations

BROWSER_BINS = ("google-chrome-stable", "google-chrome", "chromium",
                "chromium-browser", "brave-browser", "microsoft-edge-stable",
                "vivaldi-stable")

DEFAULT_PROFILES = {
    "chrome": "~/.config/google-chrome",
    "google-chrome": "~/.config/google-chrome",
    "google-chrome-stable": "~/.config/google-chrome",
    "chromium": "~/.config/chromium",
    "chromium-browser": "~/.config/chromium",
    "brave": "~/.config/BraveSoftware/Brave-Browser",
    "brave-browser": "~/.config/BraveSoftware/Brave-Browser",
    "msedge": "~/.config/microsoft-edge",
    "microsoft-edge": "~/.config/microsoft-edge",
    "vivaldi": "~/.config/vivaldi",
    "vivaldi-bin": "~/.config/vivaldi",
}

LAUNCH_WAIT_S = 20.0

TAB_WAIT_S = 10.0

STOP_WAIT_S = 10.0

PORT_WAIT_S = 5.0

ACTIVE_SPEC = "active"

NAV_TIMEOUT_S = 25.0        # a cold page; long enough, short enough to report

NAV_MOVE_S = 10.0           # how long the tab gets to LEAVE the old document

HISTORY_TIMEOUT_S = 10.0

RELOAD_TIMEOUT_S = 20.0

ACTIVATE_TIMEOUT_S = 3.0    # a tab becomes visible immediately, or it will not

READY_EXPR = "document.readyState + (document.body ? '+body' : '')"
