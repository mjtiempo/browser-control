"""`python -m browser_control` — the same CLI as the `browser-control-cli` script.

`plan.md` promises both spellings, and only the console script existed: a plan
that names an invocation nobody can run is drift, so this is three lines of it.
"""
from __future__ import annotations

import sys

from browser_control.cli.main import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
