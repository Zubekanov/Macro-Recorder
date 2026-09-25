"""Screen-region OCR, isolated behind a single function.

``read_region`` screenshots a rectangle of the screen and returns the text an
OCR engine reads from it.  The engine depends on the platform:

- Windows: the built-in Windows 10/11 OCR engine through the pip-installable
  ``winsdk`` bindings.  No external binary.
- Linux (Mint and others): Tesseract through ``pytesseract``.  The ``tesseract``
  binary comes from the distribution (``sudo apt install tesseract-ocr``).

Imports are deferred so the rest of the app (and the test suite) does not
hard-depend on these packages; a missing dependency or unavailable OCR engine
surfaces as a clear ``OcrError``.  Keeping the whole dependency in one function
also makes the OCR step trivially mockable in tests.
"""

from __future__ import annotations

import sys


class OcrError(RuntimeError):
    """Raised when a screen region cannot be captured or OCR'd."""


# ---------------------------------------------------------------------------
# Windows backend
# ---------------------------------------------------------------------------

async def _recognize_png(png_bytes: bytes) -> str:
    """Run Windows OCR over PNG-encoded image bytes (async WinRT calls)."""
    from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    from winsdk.windows.graphics.imaging import (
        BitmapAlphaMode,
        BitmapDecoder,
        BitmapPixelFormat,
        SoftwareBitmap,
    )
    from winsdk.windows.media.ocr import OcrEngine

    # Feed the PNG bytes into an in-memory random-access stream.
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(bytes(png_bytes))
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    # Normalise to a format the OCR engine reliably accepts.
    bitmap = SoftwareBitmap.convert(bitmap, BitmapPixelFormat.BGRA8, BitmapAlphaMode.PREMULTIPLIED)

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise OcrError("No Windows OCR language is installed/available.")
    result = await engine.recognize_async(bitmap)
    return result.text


def _ocr_windows(image) -> str:
    import asyncio
    import io

    try:
        import winsdk  # noqa: F401  (presence check; submodules imported in _recognize_png)
    except ImportError as e:
        raise OcrError("OCR requires the 'winsdk' package (pip install winsdk).") from e

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    try:
        # Callers run on the player's worker thread, which has no running event
        # loop, so a fresh asyncio loop here is safe.
        return asyncio.run(_recognize_png(buffer.getvalue()))
    except OcrError:
        raise
    except Exception as e:
        raise OcrError("Windows OCR failed: %s" % e) from e


# ---------------------------------------------------------------------------
# Tesseract backend (Linux)
# ---------------------------------------------------------------------------

def _ocr_tesseract(image) -> str:
    try:
        import pytesseract
    except ImportError as e:
        raise OcrError("OCR requires the 'pytesseract' package (uv sync installs it on Linux).") from e
    try:
        return pytesseract.image_to_string(image)
    except pytesseract.TesseractNotFoundError as e:
        raise OcrError("The tesseract binary is not installed. "
                       "On Linux Mint run: sudo apt install tesseract-ocr") from e
    except Exception as e:
        raise OcrError("Tesseract OCR failed: %s" % e) from e


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ocr_image(image) -> str:
    """Return the text the platform OCR engine reads from a PIL image.

    Raises OcrError if the engine's package or binary is unavailable or
    recognition fails.
    """
    if sys.platform == "win32":
        text = _ocr_windows(image)
    else:
        text = _ocr_tesseract(image)
    return text.strip()


def read_region(left: int, top: int, width: int, height: int) -> str:
    """Screenshot ``(left, top, width, height)`` and return the OCR'd text.

    The capture goes through ``mss`` like the image-matching actions, which
    works on Windows and X11 alike.  Raises OcrError if the capture or the OCR
    fails.
    """
    from macro_recorder.matching import MatchError, grab_region

    try:
        image = grab_region((left, top, width, height))
    except (MatchError, Exception) as e:
        raise OcrError("Could not capture screen region "
                       "(%d, %d, %d, %d): %s" % (left, top, width, height, e)) from e
    return ocr_image(image)
