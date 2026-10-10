"""engine/screen.py: pure helpers and XScreen over a fake mss (no real screen)."""

import os
import subprocess
import sys
import types
from unittest import mock

import pytest
from PIL import Image, ImageDraw

from macro_recorder.engine.screen import (
    ScreenError, XScreen, best_match, monitor_rect, ocr_image, rect_from_corners,
    text_similarity,
)
from macro_recorder.model.protocols import Monitor, Rect


# ---------------------------------------------------------------------------
# A fake mss over a PIL "desktop"
# ---------------------------------------------------------------------------

class _Shot:
    def __init__(self, image):
        self.size = image.size
        r, g, b = image.convert("RGB").split()
        self.bgra = Image.merge("RGBA", (b, g, r, Image.new("L", image.size, 255))).tobytes()


class FakeMss:
    def __init__(self, desktop, monitors=None):
        self.desktop = desktop
        self.monitors = monitors or [
            {"left": 0, "top": 0, "width": desktop.width, "height": desktop.height},
            {"left": 0, "top": 0, "width": desktop.width, "height": desktop.height},
        ]
        self.grabs = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def grab(self, box):
        self.grabs.append(box)
        l, t = box["left"], box["top"]
        return _Shot(self.desktop.crop((l, t, l + box["width"], t + box["height"])))


def textured(size, seed=0):
    """An image with enough structure for template matching."""
    img = Image.new("RGB", size, (30, 30, 30))
    draw = ImageDraw.Draw(img)
    for i in range(0, size[0], 7):
        draw.line((i, 0, (i * 3 + seed) % size[0], size[1]), fill=(200, (i * 5) % 255, 90))
    for j in range(0, size[1], 11):
        draw.line((0, j, size[0], (j * 2 + seed) % size[1]), fill=(40, 180, (j * 7) % 255))
    return img


@pytest.fixture
def desktop():
    img = textured((300, 200))
    img.putpixel((12, 34), (1, 2, 3))
    return img


@pytest.fixture
def fake_mss(desktop):
    return FakeMss(desktop)


@pytest.fixture
def xscreen(fake_mss):
    return XScreen(lambda: fake_mss)


# ---------------------------------------------------------------------------
# text_similarity (ported from the old matching tests)
# ---------------------------------------------------------------------------

def test_similarity_identical():
    assert text_similarity("hello", "hello") == 1.0


def test_similarity_ignores_case_and_whitespace():
    assert text_similarity("  Hello   World ", "hello world") == 1.0


def test_similarity_partial():
    assert text_similarity("hello", "hallo") == pytest.approx(0.8)


def test_similarity_disjoint_is_low():
    assert text_similarity("abc", "xyz") < 0.5


def test_similarity_empty_strings():
    assert text_similarity("", "") == 1.0
    assert text_similarity("abc", "") == 0.0
    assert text_similarity("", "   ") == 1.0


def test_similarity_is_symmetric_with_length_difference():
    assert text_similarity("kitten", "sitting") == pytest.approx(1 - 3 / 7)
    assert text_similarity("sitting", "kitten") == pytest.approx(1 - 3 / 7)


# ---------------------------------------------------------------------------
# Region maths
# ---------------------------------------------------------------------------

def test_rect_from_corners_any_order():
    assert rect_from_corners(110, 70, 10, 20) == Rect(10, 20, 100, 50)
    assert rect_from_corners(10, 20, 110, 70) == Rect(10, 20, 100, 50)
    assert rect_from_corners(10.4, 20.6, 10.4, 20.6) == Rect(10, 21, 0, 0)


def test_monitor_rect():
    mons = [Monitor(1, Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1050)),
            Monitor(2, Rect(1920, 0, 1280, 1024), Rect(1920, 0, 1280, 1024))]
    assert monitor_rect(mons, 2) == Rect(1920, 0, 1280, 1024)
    with pytest.raises(ScreenError, match="Monitor 3 not found"):
        monitor_rect(mons, 3)


# ---------------------------------------------------------------------------
# Template matching on synthetic images
# ---------------------------------------------------------------------------

def test_best_match_finds_centre_and_scores_exact_hit():
    hay = textured((200, 150), seed=3)
    needle = hay.crop((60, 40, 90, 64))           # 30 x 24
    cx, cy, score = best_match(hay, needle)
    assert (cx, cy) == (75, 52)
    assert score == pytest.approx(1.0, abs=1e-4)


def test_best_match_absent_scores_low():
    hay = Image.new("RGB", (100, 100), (0, 0, 0))
    ImageDraw.Draw(hay).rectangle((10, 10, 30, 30), fill=(255, 255, 255))
    needle = textured((20, 20), seed=9)
    assert best_match(hay, needle)[2] < 0.5


def test_best_match_flat_needle_compares_colour():
    hay = Image.new("RGB", (60, 40), (30, 30, 30))
    ImageDraw.Draw(hay).rectangle((40, 10, 49, 19), fill=(200, 0, 0))
    cx, cy, score = best_match(hay, Image.new("RGB", (6, 6), (200, 0, 0)))
    assert 42 <= cx <= 47 and 12 <= cy <= 17
    assert score == pytest.approx(1.0)
    assert best_match(hay, Image.new("RGB", (6, 6), (10, 10, 10)))[2] == pytest.approx(1 - 20 / 255)
    assert best_match(Image.new("RGB", (9, 9), (255, 255, 255)),
                      Image.new("RGB", (3, 3), (0, 0, 0)))[2] == pytest.approx(0.0)


def test_best_match_needle_larger_than_region():
    with pytest.raises(ScreenError, match="larger than the search region"):
        best_match(Image.new("RGB", (10, 10)), Image.new("RGB", (20, 5)))


def test_best_match_without_opencv():
    with mock.patch.dict(sys.modules, {"cv2": None}), pytest.raises(ScreenError, match="OpenCV"):
        best_match(Image.new("RGB", (10, 10)), Image.new("RGB", (5, 5)))


# ---------------------------------------------------------------------------
# XScreen
# ---------------------------------------------------------------------------

def test_capture_returns_rgb_image_of_rect(xscreen, fake_mss, desktop):
    img = xscreen.capture(Rect(10, 30, 40, 20))
    assert img.mode == "RGB" and img.size == (40, 20)
    assert img.getpixel((2, 4)) == (1, 2, 3)
    assert img.tobytes() == desktop.crop((10, 30, 50, 50)).tobytes()
    assert fake_mss.grabs == [{"left": 10, "top": 30, "width": 40, "height": 20}]


def test_capture_empty_rect_is_an_error(xscreen):
    with pytest.raises(ScreenError, match="empty region"):
        xscreen.capture(Rect(0, 0, 0, 10))


def test_capture_failure_is_wrapped():
    def broken():
        raise OSError("XGetImage failed")
    with pytest.raises(ScreenError, match="XGetImage failed"):
        XScreen(broken).capture(Rect(0, 0, 1, 1))


def test_missing_mss_is_a_screen_error():
    with mock.patch.dict(sys.modules, {"mss": None}), pytest.raises(ScreenError, match="mss"):
        XScreen().capture(Rect(0, 0, 1, 1))


def test_pixel_is_a_one_by_one_capture(xscreen, fake_mss):
    assert xscreen.pixel(12, 34) == (1, 2, 3)
    assert fake_mss.grabs == [{"left": 12, "top": 34, "width": 1, "height": 1}]


def test_find_image_returns_screen_centre(xscreen, desktop, tmp_path):
    path = tmp_path / "button.png"
    desktop.crop((150, 100, 180, 120)).save(path)
    hit = xscreen.find_image(Rect(100, 50, 150, 120), str(path), 0.9)
    assert hit is not None
    assert hit[:2] == (165, 110)
    assert hit[2] == pytest.approx(1.0, abs=1e-4)


def test_find_image_below_confidence_is_none(xscreen, tmp_path):
    path = tmp_path / "other.png"
    textured((20, 20), seed=99).save(path)
    assert xscreen.find_image(Rect(0, 0, 300, 200), str(path), 0.99) is None


def test_find_image_missing_file(xscreen, tmp_path):
    with pytest.raises(ScreenError, match="not found"):
        xscreen.find_image(Rect(0, 0, 10, 10), str(tmp_path / "nope.png"), 0.9)
    with pytest.raises(ScreenError, match="not found"):
        xscreen.find_image(Rect(0, 0, 10, 10), "", 0.9)


def test_find_image_unreadable_file(xscreen, tmp_path):
    path = tmp_path / "bad.png"
    path.write_bytes(b"not an image")
    with pytest.raises(ScreenError, match="Could not read image"):
        xscreen.find_image(Rect(0, 0, 10, 10), str(path), 0.9)


def test_find_image_template_reloads_when_file_changes(xscreen, desktop, tmp_path):
    path = tmp_path / "t.png"
    desktop.crop((0, 0, 20, 20)).save(path)
    assert xscreen.find_image(Rect(0, 0, 300, 200), str(path), 0.9)[:2] == (10, 10)
    desktop.crop((200, 150, 220, 170)).save(path)
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert xscreen.find_image(Rect(0, 0, 300, 200), str(path), 0.9)[:2] == (210, 160)


def test_monitors_from_mss_skip_the_virtual_screen():
    fake = FakeMss(Image.new("RGB", (10, 10)), monitors=[
        {"left": 0, "top": 0, "width": 3200, "height": 1080},
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 1920, "top": 0, "width": 1280, "height": 1024},
    ])
    mons = XScreen(lambda: fake).monitors()
    assert mons == [Monitor(1, Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1080)),
                    Monitor(2, Rect(1920, 0, 1280, 1024), Rect(1920, 0, 1280, 1024))]


# ---------------------------------------------------------------------------
# OCR (ported from the old OCR backend tests; pytesseract is faked)
# ---------------------------------------------------------------------------

def _fake_pytesseract(text=None, error=None):
    mod = types.SimpleNamespace(TesseractNotFoundError=type("TNF", (Exception,), {}))
    mod.image_to_string = mock.Mock(side_effect=error, return_value=text)
    return mod


def test_ocr_uses_tesseract_and_strips():
    fake = _fake_pytesseract("  hello\n")
    image = Image.new("RGB", (4, 4))
    with mock.patch.dict(sys.modules, {"pytesseract": fake}):
        assert ocr_image(image) == "hello"
    fake.image_to_string.assert_called_once_with(image)


def test_missing_tesseract_binary_names_the_apt_package():
    fake = _fake_pytesseract()
    fake.image_to_string.side_effect = fake.TesseractNotFoundError()
    with mock.patch.dict(sys.modules, {"pytesseract": fake}), \
            pytest.raises(ScreenError, match="apt install tesseract-ocr"):
        ocr_image(Image.new("RGB", (4, 4)))


def test_missing_pytesseract_package():
    with mock.patch.dict(sys.modules, {"pytesseract": None}), \
            pytest.raises(ScreenError, match="pytesseract"):
        ocr_image(Image.new("RGB", (4, 4)))


def test_tesseract_failure_is_wrapped():
    fake = _fake_pytesseract(error=RuntimeError("bad language"))
    with mock.patch.dict(sys.modules, {"pytesseract": fake}), \
            pytest.raises(ScreenError, match="bad language"):
        ocr_image(Image.new("RGB", (4, 4)))


def test_xscreen_ocr_captures_the_rect_then_reads(xscreen, fake_mss):
    fake = _fake_pytesseract("Total 42")
    with mock.patch.dict(sys.modules, {"pytesseract": fake}):
        assert xscreen.ocr(Rect(5, 6, 50, 20)) == "Total 42"
    assert fake_mss.grabs == [{"left": 5, "top": 6, "width": 50, "height": 20}]
    assert fake.image_to_string.call_args.args[0].size == (50, 20)


def test_module_import_is_lazy():
    code = ("import sys, macro_recorder.engine.screen; "
            "print(sorted({'cv2', 'mss', 'pytesseract', 'numpy', 'PIL'} & set(sys.modules)))")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=root)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         check=True)
    assert out.stdout.strip() == "[]"


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------

@pytest.mark.live_x
def test_live_capture_and_monitors():
    xs = XScreen()
    mons = xs.monitors()
    assert mons and mons[0].index == 1
    img = xs.capture(Rect(0, 0, 8, 6))
    assert img.size == (8, 6) and img.mode == "RGB"
    assert len(xs.pixel(0, 0)) == 3
