"""Screen-region OCR, isolated behind a single function.

``read_region`` screenshots a rectangle of the screen and returns the text the
Windows OCR engine reads from it.  Windows 10/11 ship an OCR engine, so this
needs no external binary — only the pip-installable ``winsdk`` bindings (plus
``pyautogui``/Pillow for the screenshot).  That keeps the app self-contained and
shippable as a package.

Imports are deferred so the rest of the app (and the test suite) does not
hard-depend on these packages; a missing dependency or unavailable OCR engine
surfaces as a clear ``OcrError``.  Keeping the whole dependency in one function
also makes the OCR step trivially mockable in tests and swappable later (e.g. to
``mss`` for multi-monitor capture, or a cross-platform engine) without touching
any callers.
"""

from __future__ import annotations


class OcrError(RuntimeError):
    """Raised when a screen region cannot be captured or OCR'd."""


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


def ocr_image(image) -> str:
    """Return the text Windows OCR reads from a PIL image.

    Raises OcrError if ``winsdk`` is unavailable, the OCR engine cannot be
    created, or recognition fails.
    """
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
        text = asyncio.run(_recognize_png(buffer.getvalue()))
    except OcrError:
        raise
    except Exception as e:
        raise OcrError("Windows OCR failed: %s" % e) from e
    return text.strip()


def read_region(left: int, top: int, width: int, height: int) -> str:
    """Screenshot ``(left, top, width, height)`` and return the OCR'd text.

    Raises OcrError if the required packages are unavailable, if the Windows OCR
    engine cannot be created, or if the capture/OCR otherwise fails.
    """
    try:
        import pyautogui
    except ImportError as e:
        raise OcrError("OCR requires 'pyautogui' (pip install pyautogui Pillow).") from e
    try:
        image = pyautogui.screenshot(region=(left, top, width, height))
    except Exception as e:
        raise OcrError("Could not capture screen region "
                       "(%d, %d, %d, %d): %s" % (left, top, width, height, e)) from e
    return ocr_image(image)
