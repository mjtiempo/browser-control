"""images — the screenshot file layer: PNG header, geometry, atomic write.

No CDP here: the bytes come from the caller, and this module judges them by
their own header, turns the page's CSS geometry into device pixels, validates
the output path, and writes the file (exclusively, or atomically over). The
verbs that talk to a page live in the DOM tier; the file rules are pure.
"""
from __future__ import annotations

from browser_control.lib import sink
from browser_control.lib.errors import (
    ERR_SCREENSHOT_NOT_VERIFIED,
    fail,
)

PNG_SIG = b"\x89PNG\r\n\x1a\n"
PNG_IHDR_LEN = 13       # the IHDR chunk body: width, height, and 5 more bytes
#: What a PNG must have after the signature: the IHDR length+type+body+CRC
#: (33 bytes) and at least ONE following chunk header (8) — a 24-byte file that
#: stops after the width and height is a signature with a promise in it.
PNG_HEAD_BYTES = 8 + 4 + 4 + PNG_IHDR_LEN + 4 + 8
MAX_PX = 100_000        # a dimension no screenshot of this page can have

__all__ = ["MAX_PX", "PNG_SIG", "expected_pixels", "output_path", "png_size",
           "write_atomic"]


def png_size(data: bytes) -> list[int]:
    """[width, height] from the PNG's OWN header, or [] when it is not one.

    The file is judged by its bytes, not by the answer that produced it: a
    screenshot whose header disagrees with the page's own geometry is refused
    before it is written anywhere.

    A signature plus a size is not a PNG: the IHDR chunk must be the 13 bytes
    the format says it is, at least one chunk header must follow it, and the
    file must END with the IEND chunk — a truncated or truncated-after-the-
    header file used to be read as a picture with its dimensions (a review
    flagged it). Real `Page.captureScreenshot` output has all three.
    """
    if (len(data) < PNG_HEAD_BYTES or not data.startswith(PNG_SIG)
            or data[8:12] != PNG_IHDR_LEN.to_bytes(4, "big")
            or data[12:16] != b"IHDR"
            # the trailing IEND: length 0, type, CRC — the last 12 bytes
            or data[-12:-8] != b"\x00\x00\x00\x00"
            or data[-8:-4] != b"IEND"):
        return []
    return [int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big")]


def expected_pixels(css: int, dpr: float) -> int:
    """CSS pixels → device pixels, or a refusal.

    Both numbers come from the PAGE, so a nonsense pair must become a refusal
    rather than an exception or a silently wrong expectation: this is the value
    the PNG's own header is compared against.
    """
    try:
        value = int(round(css * dpr))
    except (OverflowError, ValueError) as e:
        fail(ERR_SCREENSHOT_NOT_VERIFIED,
             f"the page reports {css} px at devicePixelRatio {dpr:g}, which is "
             f"not a size ({e}) — nothing was written")
    if not 0 < value <= MAX_PX:
        fail(ERR_SCREENSHOT_NOT_VERIFIED,
             f"the page reports {css} px at devicePixelRatio {dpr:g}, i.e. "
             f"{value} device pixels — no screenshot of it exists — nothing "
             "was written")
    return value


def output_path(path: str) -> str:
    """The absolute path a screenshot may be written to, or a refusal.

    The path discipline lives in `lib/sink.py` now that a second verb writes a
    file the caller names; what stays here is this verb's own rule — the name
    must say PNG, because the data IS one.
    """
    return sink.output_path(
        "tab screenshot", path, suffix=".png",
        suffix_why=("the data IS a PNG, and a name that says otherwise is a "
                    "lie about the file"))


def write_atomic(target: str, data: bytes, force: bool) -> int:
    """Write the PNG through `lib/sink.py` (0600, exclusive/atomic) and answer
    the bytes on disk."""
    return sink.write_atomic("tab screenshot", target, data, force)
