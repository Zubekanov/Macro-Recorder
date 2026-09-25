"""Screen capture + image/text matching for the wait-for-match actions.

UI-free and mockable.  The heavy/optional dependencies (``mss`` for robust
multi-monitor capture, ``opencv-python`` for tolerant template matching) are
imported lazily so the rest of the app and the test suite don't hard-require
them; a missing dependency surfaces as a clear ``MatchError``.

Public API:
- ``monitor_region(index)``        → (left, top, width, height) of a monitor
- ``find_image(region, path, conf)`` → (cx, cy, score) | None  (absolute coords)
- ``match_text(region, expected, tol)`` → (ocr_text, ratio, matched)
"""

from __future__ import annotations

import os
from typing import Optional


class MatchError(RuntimeError):
    """Raised when a screen region cannot be captured or matched."""


def grab_region(region):
    """Capture (left, top, width, height) as a PIL RGB image via mss."""
    try:
        import mss
        from PIL import Image
    except ImportError as e:
        raise MatchError(
            "Screen matching requires 'mss' and 'Pillow' (pip install mss Pillow)."
        ) from e
    left, top, width, height = region
    with mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def monitor_region(index: int) -> tuple:
    """Return the (left, top, width, height) of 1-based monitor ``index``."""
    try:
        import mss
    except ImportError as e:
        raise MatchError("Monitor capture requires 'mss' (pip install mss).") from e
    with mss.mss() as sct:
        monitors = sct.monitors           # [0] = virtual screen, [1..] = each monitor
        if index < 1 or index >= len(monitors):
            raise MatchError("Monitor %d not found (%d available)."
                             % (index, len(monitors) - 1))
        m = monitors[index]
        return (m["left"], m["top"], m["width"], m["height"])


def find_image(region, needle_path: str, confidence: float) -> Optional[tuple]:
    """Locate ``needle_path`` within ``region`` above ``confidence`` (0..1).

    Returns ``(center_x, center_y, score)`` in absolute screen coordinates if
    found, else None.  Raises MatchError for missing deps / bad image / region.
    """
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        raise MatchError(
            "Image matching requires 'opencv-python' (pip install opencv-python)."
        ) from e
    if not needle_path or not os.path.isfile(needle_path):
        raise MatchError("Image file not found: %r" % needle_path)
    needle = cv2.imread(needle_path, cv2.IMREAD_COLOR)
    if needle is None:
        raise MatchError("Could not read image file: %r" % needle_path)

    haystack = cv2.cvtColor(np.array(grab_region(region)), cv2.COLOR_RGB2BGR)
    nh, nw = needle.shape[:2]
    if nh > haystack.shape[0] or nw > haystack.shape[1]:
        raise MatchError("Reference image is larger than the search region.")

    result = cv2.matchTemplate(haystack, needle, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val < confidence:
        return None
    left, top = region[0], region[1]
    return (left + max_loc[0] + nw // 2, top + max_loc[1] + nh // 2, float(max_val))


def _similarity(a: str, b: str) -> float:
    """Normalized Levenshtein similarity 0..1 (1 = identical) after normalizing
    whitespace and case, to absorb OCR artefacts."""
    a = " ".join(a.split()).lower()
    b = " ".join(b.split()).lower()
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    la, lb = len(a), len(b)
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return 1.0 - prev[lb] / max(la, lb)


def match_text(region, expected: str, tolerance: float) -> tuple:
    """OCR ``region`` and compare to ``expected``.

    Returns ``(ocr_text, ratio, matched)`` where ratio is the 0..1 similarity and
    matched is ``ratio >= tolerance``.  Raises MatchError on capture failure
    (OCR errors propagate as OcrError).
    """
    from macro_recorder.ocr import ocr_image
    text = ocr_image(grab_region(region))
    ratio = _similarity(text, expected)
    return (text, ratio, ratio >= tolerance)
