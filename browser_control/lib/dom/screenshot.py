"""screenshot — `tab screenshot`: a PNG the file's own header vouches for.

The geometry check is arithmetic, not faith: the PNG's IHDR must agree with
the region the page reported for the chosen mode times devicePixelRatio.
"""
from __future__ import annotations

import base64
import binascii
import math

from browser_control.lib import (
    browser as browser_lib,
)
from browser_control.lib import dom as _pkg
from browser_control.lib import (
    images,
)
from browser_control.lib.coerce import (
    as_float,
    as_int,
)
from browser_control.lib.dom.queries import (
    FIND_MAX_MATCHES,
)
from browser_control.lib.dom.scripts import (
    SHOT_METRICS,
    fill,
)
from browser_control.lib.errors import (
    ERR_BAD_ARGS,
    ERR_CDP_ERROR,
    ERR_SCREENSHOT_NOT_VERIFIED,
    fail,
)

#: The noun each mode's mismatch refusal uses for the region it measured: the
#: message already named the viewport and the document, and the two new modes
#: name the element box and the caller's clip rather than claiming a page fact.
_REGION_WHAT = {"viewport": "viewport", "full": "document",
                "element": "element box", "region": "clip"}

#: What each mode's reply note says the IHDR was checked against. Viewport and
#: full keep their existing sentence byte for byte; the two new modes name the
#: region they actually measured.
_NOTE = {
    "viewport": ("the file's IHDR matches the page's own geometry; a read, so "
                 "no attach is needed"),
    "full": ("the file's IHDR matches the page's own geometry; a read, so no "
             "attach is needed"),
    "element": ("the file's IHDR matches the picked element's border box, "
                "rounded outward to whole CSS px; a read, so no attach is "
                "needed"),
    "region": ("the file's IHDR matches the --clip rectangle; a read, so no "
               "attach is needed"),
}


def _capture_params(mode: str, clip: list[int] | None) -> dict:
    """The `Page.captureScreenshot` parameters for a mode.

    Viewport and full are UNCHANGED from what ships: no `clip`, and
    `captureBeyondViewport` equal to whether the whole document was asked for.
    The two new modes always send an integer CSS `clip` with
    `captureBeyondViewport: true` — measured, a clip below the viewport with
    that flag FALSE returns unreliable pixels, while the clip is in PAGE
    coordinates so an element below the fold still captures whole.
    """
    params: dict = {"format": "png", "captureBeyondViewport": mode != "viewport"}
    if clip is not None:
        params["clip"] = {"x": clip[0], "y": clip[1], "width": clip[2],
                          "height": clip[3], "scale": 1}
    return params


def _border_clip(element: dict, metrics: dict) -> list[int]:
    """The element's RAW page border box rounded OUTWARD to whole CSS px.

    floor left/top and ceil right/bottom (measured): the element captures
    whole, and a fractional edge never clips a pixel of it. `element` is a
    `find` row, whose `rect` is the raw, unrounded
    `[left+scrollX, top+scrollY, width, height]`.

    A box that would land past `images.MAX_PX` DEVICE pixels at the page's own
    `devicePixelRatio` refuses BEFORE the capture is sent: `expected_pixels`
    applies the same ceiling, but only after Chrome was asked for the bitmap.
    """
    rect = element.get("rect")
    if not isinstance(rect, (list, tuple)) or len(rect) < 4:
        fail(ERR_SCREENSHOT_NOT_VERIFIED,
             f"the element {_pkg._describe(element)} reports no border box "
             f"({rect!r}) — nothing was written")
    left, top, width, height = (as_float(v) for v in rect[:4])
    if (not all(math.isfinite(v) for v in (left, top, width, height))
            or width <= 0 or height <= 0):
        fail(ERR_SCREENSHOT_NOT_VERIFIED,
             f"the element {_pkg._describe(element)} has no area "
             f"({width:g}x{height:g} CSS px) — nothing was written")
    x = math.floor(left)
    y = math.floor(top)
    w = math.ceil(left + width) - x
    h = math.ceil(top + height) - y
    if w <= 0 or h <= 0:
        fail(ERR_SCREENSHOT_NOT_VERIFIED,
             f"the element {_pkg._describe(element)} rounds to no pixels "
             f"({w}x{h}) — nothing was written")
    dpr = as_float(metrics.get("dpr"), 1.0)
    if (math.isfinite(dpr) and dpr > 0
            and (w > images.MAX_PX / dpr or h > images.MAX_PX / dpr)):
        fail(ERR_SCREENSHOT_NOT_VERIFIED,
             f"the element {_pkg._describe(element)} is {w}x{h} CSS px at "
             f"devicePixelRatio {dpr:g}, past the {images.MAX_PX} DEVICE px "
             "dimension limit for a screenshot — nothing was written")
    return [x, y, w, h]


def _document_clip(clip: tuple[int, int, int, int], metrics: dict) -> list[int]:
    """The caller's `--clip`, refused when it lies WHOLLY outside the document.

    The clip is in page coordinates from the document's origin. A rectangle
    that starts somewhere the page is not (x at or past the document's width,
    or y at or past its height) cannot capture any of the page, so it refuses
    `bad-args` naming the document's size rather than writing a blank PNG. A
    rectangle that merely OVERHANGS the edge is allowed: the remainder is
    opaque white (measured).

    A size past `images.MAX_PX` DEVICE pixels (`w × devicePixelRatio` — the
    same ceiling `expected_pixels` applies, but that check only runs after the
    capture) is refused here too, BEFORE `Page.captureScreenshot` sees it: a
    size Chrome cannot render (a negative one HANGS, measured) must never be
    sent to it at all.
    """
    x, y, w, h = clip
    dpr = as_float(metrics.get("dpr"), 1.0)
    if (math.isfinite(dpr) and dpr > 0
            and (w > images.MAX_PX / dpr or h > images.MAX_PX / dpr)):
        fail(ERR_BAD_ARGS,
             f"tab screenshot: --clip {x},{y},{w},{h} at devicePixelRatio "
             f"{dpr:g} is past the {images.MAX_PX} DEVICE px dimension limit "
             "for a screenshot — nothing was written")
    sw, sh = as_int(metrics.get("sw")), as_int(metrics.get("sh"))
    if x >= sw or y >= sh:
        fail(ERR_BAD_ARGS,
             f"tab screenshot: --clip {x},{y},{w},{h} is wholly outside the "
             f"document ({sw}x{sh} CSS px) — there is nothing there to capture")
    return [x, y, w, h]


def screenshot(path: str, full: bool = False, force: bool = False,
               selector: str | None = None, index: int | None = None,
               clip: tuple[int, int, int, int] | None = None,
               tab: str = "", browser: str = "") -> dict:
    """`tab screenshot`: the page as a PNG the file's OWN header vouches for.

    A READ: nothing about the page changes, so it needs no `attach` — but it
    writes a file, which is why the path must be absolute, end in `.png`, and
    not already exist without `--force`.

    FOUR modes, one oracle. The viewport is the default; `full` captures the
    whole document; `selector`/`index` capture ONE element's border box, and
    `clip` a caller-named rectangle in page coordinates. All four send
    `Page.captureScreenshot` and check the PNG's IHDR against the region the
    page reported (or the caller named) times `devicePixelRatio`, rounded
    HALF-UP (`images.expected_pixels`) — measured, Chrome rounds the device
    size that way, and a file that would not match is not written at all.

    Element mode resolves through the SAME matcher as `tab find --selector`
    (`_matches_in` + `_pick`) on a READ-opened tab, so several matches refuse
    `ambiguous-element` naming the indices, nothing rendered refuses
    `no-match`, and hidden elements are not candidates. The picked border box
    rounds OUTWARD (floor left/top, ceil right/bottom) and is sent as a CDP
    `clip` with `captureBeyondViewport: true`, so an element below the fold
    captures whole.
    """
    target = images.output_path(path)
    mode = ("full" if full
            else "element" if selector is not None
            else "region" if clip is not None
            else "viewport")
    page = _pkg.Tab.open(tab, browser, for_write=False)
    row, tab_row = page.row, page.tab_row
    element: dict | None = None
    target_clip: list[int] | None = None
    want: list[int] = []
    with page.session() as session:
        metrics = session.evaluate(fill(SHOT_METRICS))
        if not isinstance(metrics, dict):
            fail(ERR_CDP_ERROR, "the page did not report its geometry")
        if mode == "element":
            data = _pkg._matches_in(session, "", str(selector or ""),
                                    FIND_MAX_MATCHES)
            element = _pkg._pick(data, "", str(selector or ""), index,
                                 row=row, tab_row=tab_row)
        if mode == "region":
            target_clip = _document_clip(clip or (0, 0, 0, 0), metrics)
            want = [target_clip[2], target_clip[3]]
        elif mode == "element":
            target_clip = _border_clip(element or {}, metrics)
            want = [target_clip[2], target_clip[3]]
        elif mode == "full":
            want = [as_int(metrics.get("sw")), as_int(metrics.get("sh"))]
        else:
            want = [as_int(metrics.get("iw")), as_int(metrics.get("ih"))]
        shot = session.call("Page.captureScreenshot",
                            _capture_params(mode, target_clip))
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
    expected = [images.expected_pixels(css, dpr) for css in want]
    size = images.png_size(data)
    if not size:
        fail(ERR_SCREENSHOT_NOT_VERIFIED,
             "the bytes are not a PNG (no signature, no IHDR) — nothing was "
             "written")
    if size != expected:
        fail(ERR_SCREENSHOT_NOT_VERIFIED,
             f"the PNG is {size[0]}x{size[1]} but this {_REGION_WHAT[mode]} is "
             f"{want[0]}x{want[1]} CSS px at devicePixelRatio {dpr:g} "
             f"({expected[0]}x{expected[1]} pixels) — nothing was written")
    images.write_atomic(target, data, force)
    reply = {"ok": True, "path": target, "bytes": len(data),
             "width": size[0], "height": size[1], "full": bool(full),
             "mode": mode,
             "clip": list(target_clip) if target_clip is not None else None,
             "device_pixel_ratio": dpr, "css_size": want, "verified": True,
             "url": metrics.get("url"), "title": metrics.get("title"),
             "visibility": metrics.get("visibility"),
             "note": _NOTE[mode],
             "tab": f"id:{tab_row['id']}",
             "browser": browser_lib.brief(row)}
    if element is not None:
        reply["element"] = _pkg._element(element)
    return reply
