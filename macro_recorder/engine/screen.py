"""Screen access for playback: capture, pixel read, image find and OCR.

``XScreen`` implements the ``Screen`` Protocol with mss (capture and
monitors), OpenCV (template matching) and Tesseract through pytesseract
(OCR).  Every heavy dependency is imported lazily, so importing this module
costs nothing and a missing package or binary surfaces as a ``ScreenError``
with the fix in its message.  The pure helpers (text similarity, region
maths, template scoring on images in memory) need no display.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable

from macro_recorder.model.protocols import Monitor, Rect


class ScreenError(RuntimeError):
    """A capture, match or OCR step failed; the message says why."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def text_similarity(a: str, b: str) -> float:
    """Normalised Levenshtein similarity from 0 to 1 (1 means identical).

    Case and runs of whitespace are ignored first, which absorbs the usual
    OCR artefacts.  Two empty strings are identical; one empty string is
    entirely different from any other.
    """
    a = " ".join(a.split()).lower()
    b = " ".join(b.split()).lower()
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return 1.0 - prev[-1] / max(len(a), len(b))


def rect_from_corners(x: float, y: float, x2: float, y2: float) -> Rect:
    """The rectangle between two opposite corners given in any order."""
    left, right = sorted((int(round(x)), int(round(x2))))
    top, bottom = sorted((int(round(y)), int(round(y2))))
    return Rect(left, top, right - left, bottom - top)


def monitor_rect(monitors: list[Monitor], index: int) -> Rect:
    """The full rectangle of 1-based monitor ``index``.  Raises ScreenError when absent."""
    for m in monitors:
        if m.index == index:
            return m.rect
    raise ScreenError("Monitor %d not found (%d available)" % (index, len(monitors)))


def best_match(haystack: Any, needle: Any) -> tuple[int, int, float]:
    """Find ``needle`` in ``haystack`` (PIL images) with TM_CCOEFF_NORMED.

    Returns the centre of the best match in haystack pixels and its score
    from -1 to 1.  TM_CCOEFF_NORMED cannot tell flat colours apart (any flat
    window scores 1 against a flat needle), so a single-colour needle is
    scored as 1 minus the RMS colour difference over 255 instead.  Raises
    ScreenError when OpenCV is missing or the needle is larger than the
    haystack.
    """
    cv2, np = _import_cv2()
    hay = cv2.cvtColor(np.asarray(haystack.convert("RGB")), cv2.COLOR_RGB2BGR)
    ndl = cv2.cvtColor(np.asarray(needle.convert("RGB")), cv2.COLOR_RGB2BGR)
    nh, nw = ndl.shape[:2]
    if nh > hay.shape[0] or nw > hay.shape[1]:
        raise ScreenError("The image (%dx%d) is larger than the search region (%dx%d)"
                          % (nw, nh, hay.shape[1], hay.shape[0]))
    if ndl.reshape(-1, 3).min(axis=0).tolist() == ndl.reshape(-1, 3).max(axis=0).tolist():
        sqdiff = cv2.matchTemplate(hay, ndl, cv2.TM_SQDIFF)
        result = 1.0 - np.sqrt(np.maximum(sqdiff, 0) / (nh * nw * 3)) / 255.0
    else:
        result = cv2.matchTemplate(hay, ndl, cv2.TM_CCOEFF_NORMED)
        result = np.nan_to_num(result, nan=0.0, posinf=0.0, neginf=0.0)
    _, score, _, (mx, my) = cv2.minMaxLoc(result)
    return (mx + nw // 2, my + nh // 2, float(score))


def ocr_image(image: Any) -> str:
    """The text Tesseract reads from a PIL image, stripped.  Raises ScreenError on failure."""
    try:
        import pytesseract
    except ImportError as e:
        raise ScreenError("OCR needs the pytesseract package (uv sync installs it)") from e
    try:
        return pytesseract.image_to_string(image).strip()
    except pytesseract.TesseractNotFoundError as e:
        raise ScreenError("The tesseract binary is not installed. "
                          "On Linux Mint run: sudo apt install tesseract-ocr") from e
    except Exception as e:
        raise ScreenError("Tesseract OCR failed: %s" % e) from e


def _import_cv2() -> tuple[Any, Any]:
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        raise ScreenError("Image matching needs OpenCV (pip install opencv-python-headless)") from e
    return cv2, np


def _import_pil_image() -> Any:
    try:
        from PIL import Image
    except ImportError as e:
        raise ScreenError("Screen capture needs Pillow (pip install Pillow)") from e
    return Image


def _default_mss() -> Any:
    try:
        import mss
    except ImportError as e:
        raise ScreenError("Screen capture needs the mss package (pip install mss)") from e
    return mss.mss()


# ---------------------------------------------------------------------------
# XScreen
# ---------------------------------------------------------------------------

class XScreen:
    """The ``Screen`` Protocol on the X server named by ``$DISPLAY``.

    Each call opens and closes its own mss session and the template cache is
    locked, so one instance is safe to share between the player thread and
    the GUI thread.
    ``mss_factory`` returns an mss-like context manager; tests pass a fake.
    """

    def __init__(self, mss_factory: Callable[[], Any] = _default_mss) -> None:
        self._mss_factory = mss_factory
        self._templates: dict[tuple[str, int], Any] = {}
        self._templates_lock = threading.Lock()

    def capture(self, rect: Rect) -> Any:
        """A PIL RGB image of ``rect`` in screen pixels."""
        if rect.width <= 0 or rect.height <= 0:
            raise ScreenError("Cannot capture an empty region %r" % (rect.as_tuple(),))
        image_mod = _import_pil_image()
        try:
            with self._mss_factory() as sct:
                shot = sct.grab({"left": rect.left, "top": rect.top,
                                 "width": rect.width, "height": rect.height})
        except ScreenError:
            raise
        except Exception as e:
            raise ScreenError("Could not capture %r: %s" % (rect.as_tuple(), e)) from e
        return image_mod.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        r, g, b = self.capture(Rect(int(x), int(y), 1, 1)).getpixel((0, 0))
        return (r, g, b)

    def find_image(self, rect: Rect, path: str, confidence: float) -> tuple[int, int, float] | None:
        """Centre and score of the best match of the image at ``path`` inside ``rect``."""
        needle = self._template(path)
        cx, cy, score = best_match(self.capture(rect), needle)
        if score < confidence:
            return None
        return (rect.left + cx, rect.top + cy, score)

    def ocr(self, rect: Rect) -> str:
        return ocr_image(self.capture(rect))

    def monitors(self) -> list[Monitor]:
        """Every monitor, 1-based as mss numbers them.

        mss knows no work area, so ``work_area`` is the full monitor rect.
        """
        try:
            with self._mss_factory() as sct:
                raw = list(sct.monitors)
        except ScreenError:
            raise
        except Exception as e:
            raise ScreenError("Could not list monitors: %s" % e) from e
        out = []
        for index, m in enumerate(raw[1:], 1):
            rect = Rect(int(m["left"]), int(m["top"]), int(m["width"]), int(m["height"]))
            out.append(Monitor(index, rect, rect))
        return out

    def _template(self, path: str) -> Any:
        """The image at ``path``, cached until the file changes."""
        if not path or not os.path.isfile(path):
            raise ScreenError("Image file not found: %s" % (path or "(none)"))
        key = (os.path.abspath(path), os.stat(path).st_mtime_ns)
        with self._templates_lock:
            cached = self._templates.get(key)
        if cached is not None:
            return cached
        image_mod = _import_pil_image()
        try:
            with image_mod.open(path) as img:
                needle = img.convert("RGB")
        except OSError as e:
            raise ScreenError("Could not read image file %s: %s" % (path, e)) from e
        with self._templates_lock:
            for stale in [k for k in self._templates if k[0] == key[0]]:
                del self._templates[stale]
            self._templates[key] = needle
        return needle
