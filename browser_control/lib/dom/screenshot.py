"""screenshot — `tab screenshot`: a PNG the file's own header vouches for.

The geometry check is arithmetic, not faith: the PNG's IHDR must agree with
the page's own innerWidth/innerHeight times devicePixelRatio.
"""
from __future__ import annotations

import base64
import binascii
import math

from browser_control.lib import (
    browser as browser_lib,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib import dom as _pkg  # pyright: ignore[reportMissingImports]
from browser_control.lib import (
    images,  # pyright: ignore[reportMissingImports]
)
from browser_control.lib.coerce import (  # pyright: ignore[reportMissingImports]
    as_float,
    as_int,
)
from browser_control.lib.dom.scripts import (  # pyright: ignore[reportMissingImports]
    SHOT_METRICS,
)
from browser_control.lib.errors import (  # pyright: ignore[reportMissingImports]
    ERR_CDP_ERROR,
    ERR_SCREENSHOT_NOT_VERIFIED,
    fail,
)


def screenshot(path: str, full: bool = False, force: bool = False,
               tab: str = "", browser: str = "") -> dict:
    """`tab screenshot`: the page as a PNG the file's OWN header vouches for.

    A READ: nothing about the page changes, so it needs no `attach` — but it
    writes a file, which is why the path must be absolute, end in `.png`, and
    not already exist without `--force`.

    `Page.captureScreenshot` is the CDP method, and the verification is
    arithmetic rather than faith: the PNG's IHDR must agree with the page's
    own `innerWidth/innerHeight` (or `scrollWidth/scrollHeight` with `--full`)
    times `devicePixelRatio` — measured, those are EXACT on this browser. A
    file that would not match is not written at all.
    """
    target = images.output_path(path)
    row, tab_row = _pkg._resolve(tab, browser, for_write=False)
    with _pkg._session(row, tab_row) as session:
        metrics = session.evaluate(SHOT_METRICS)
        if not isinstance(metrics, dict):
            fail(ERR_CDP_ERROR, "the page did not report its geometry")
        shot = session.call("Page.captureScreenshot",
                            {"format": "png",
                             "captureBeyondViewport": bool(full)})
        encoded = str(shot.get("data") or "")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as e:
            fail(ERR_CDP_ERROR,
                 f"Page.captureScreenshot: the data is not base64 ({e})")
    dpr = as_float(metrics.get("dpr"), 1.0)
    if not math.isfinite(dpr) or dpr <= 0:
        fail(ERR_SCREENSHOT_NOT_VERIFIED,
             f"the page reports devicePixelRatio {metrics.get('dpr')!r}, which "
             "no size can be checked against — nothing was written")
    want = ([as_int(metrics.get("sw")), as_int(metrics.get("sh"))] if full
            else [as_int(metrics.get("iw")), as_int(metrics.get("ih"))])
    expected = [images.expected_pixels(css, dpr) for css in want]
    size = images.png_size(data)
    if not size:
        fail(ERR_SCREENSHOT_NOT_VERIFIED,
             "the bytes are not a PNG (no signature, no IHDR) — nothing was "
             "written")
    if size != expected:
        fail(ERR_SCREENSHOT_NOT_VERIFIED,
             f"the PNG is {size[0]}x{size[1]} but this page says the "
             f"{'document' if full else 'viewport'} is {want[0]}x{want[1]} CSS "
             f"px at devicePixelRatio {dpr:g} ({expected[0]}x{expected[1]} "
             "pixels) — nothing was written")
    images.write_atomic(target, data, force)
    return {"ok": True, "path": target, "bytes": len(data),
            "width": size[0], "height": size[1], "full": bool(full),
            "device_pixel_ratio": dpr, "css_size": want, "verified": True,
            "url": metrics.get("url"), "title": metrics.get("title"),
            "visibility": metrics.get("visibility"),
            "note": ("the file's IHDR matches the page's own geometry; a "
                     "read, so no attach is needed"),
            "tab": f"id:{tab_row['id']}",
            "browser": browser_lib.brief(row)}
