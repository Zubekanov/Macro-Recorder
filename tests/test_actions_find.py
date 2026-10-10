"""match_image and match_text play, and text_similarity."""

import pytest

from macro_recorder.model.actions.find import POLL_MS, text_similarity
from macro_recorder.model.catalogue import END_TARGET, Abort, Flow, Jump
from macro_recorder.model.protocols import Rect
from tests._fake_context import ctx, play, row  # noqa: F401


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "button.png"
    path.write_bytes(b"png")
    return str(path)


def image_row(path, **params):
    params.setdefault("x", 0)
    params.setdefault("y", 0)
    params.setdefault("x2", 400)
    params.setdefault("y2", 300)
    return row("match_image", image_path=path, **params)


# ---------------------------------------------------------------------------
# text_similarity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a, b, expected", [
    ("Hello World", "hello world", 1.0),
    ("  Hello \n World ", "hello world", 1.0),
    ("", "", 1.0),
    ("abc", "", 0.0),
    ("kitten", "sitting", 1 - 3 / 7),
    ("Hello Wrld", "Hello World", 1 - 1 / 11),
])
def test_text_similarity(a, b, expected):
    assert text_similarity(a, b) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# match_image
# ---------------------------------------------------------------------------

def test_image_found_sets_vars_in_shifted_frame(ctx, image):
    ctx.origin = (100, 110)
    ctx.screen.images[image] = (250, 210, 0.95)
    r = image_row(image, capture_var="cx", capture_var_y="cy")
    assert play(ctx, r) is Flow.NEXT
    assert ctx.variables["found"] == 1
    assert (ctx.variables["cx"], ctx.variables["cy"]) == (150, 100)
    assert ctx.screen.captures == [] and ctx.sleeps == []
    assert ctx.input.actions == []


def test_image_click_found_clicks_screen_centre(ctx, image):
    ctx.origin = (100, 110)
    ctx.screen.images[image] = (250, 210, 0.95)
    play(ctx, image_row(image, click_found=True))
    assert ctx.input.actions == [("move", 250, 210), ("press_button", "left"),
                                 ("release_button", "left")]


def test_image_found_after_polling(ctx, image, monkeypatch):
    calls = []

    def find_image(rect, path, confidence):
        calls.append((rect, path, confidence))
        return (50, 60, 0.99) if len(calls) == 3 else None

    monkeypatch.setattr(ctx.screen, "find_image", find_image)
    r = image_row(image, confidence=0.8, capture_var="cx")
    assert play(ctx, r) is Flow.NEXT
    assert calls == [(Rect(0, 0, 400, 300), image, 0.8)] * 3
    assert ctx.sleeps == [(POLL_MS, False)] * 2
    assert ctx.variables["cx"] == 50


def test_image_below_confidence_is_not_found(ctx, image):
    ctx.screen.images[image] = (50, 60, 0.85)
    play(ctx, image_row(image, confidence=0.9, timeout_ms=0))
    assert ctx.variables["found"] == 0


def test_image_outside_region_is_not_found(ctx, image):
    ctx.screen.images[image] = (500, 60, 0.99)
    play(ctx, image_row(image, timeout_ms=0))
    assert ctx.variables["found"] == 0


def test_image_timeout_polls_unscaled_until_deadline(ctx, image):
    ctx.variables["cx"] = "unchanged"
    r = image_row(image, timeout_ms=1000, capture_var="cx")
    assert play(ctx, r) is Flow.NEXT
    assert ctx.sleeps == [(300, False), (300, False), (300, False), (pytest.approx(100), False)]
    assert ctx.variables["found"] == 0
    assert ctx.variables["cx"] == "unchanged"
    assert ctx.input.actions == []


def test_image_timeout_abort_and_goto(ctx, image):
    result = play(ctx, image_row(image, timeout_ms=0, on_timeout="abort"))
    assert isinstance(result, Abort)
    assert result.message == 'image "button.png" not found within 0 ms'
    assert play(ctx, image_row(image, timeout_ms=0, on_timeout="goto", target="retry")) == Jump("retry")
    assert play(ctx, image_row(image, timeout_ms=0, on_timeout="goto")) == Jump(END_TARGET)


def test_image_stop_while_polling(ctx, image):
    ctx.stop_after = 1
    assert play(ctx, image_row(image, timeout_ms=5000)) is Flow.STOP
    assert len(ctx.sleeps) == 1


def test_image_relative_path_uses_document_dir(ctx, image, tmp_path):
    ctx.document_dir = str(tmp_path)
    ctx.screen.images[image] = (50, 60, 0.99)
    play(ctx, image_row("button.png"))
    assert ctx.variables["found"] == 1


def test_image_missing_file_aborts(ctx, tmp_path):
    result = play(ctx, image_row(str(tmp_path / "nope.png")))
    assert isinstance(result, Abort) and "nope.png" in result.message
    assert isinstance(play(ctx, image_row("")), Abort)


def test_image_reserved_capture_var_aborts_before_search(ctx, image, monkeypatch):
    monkeypatch.setattr(ctx.screen, "find_image", lambda *a: pytest.fail("searched"))
    result = play(ctx, image_row(image, capture_var="mouse_x"))
    assert isinstance(result, Abort) and "mouse_x" in result.message


def test_image_without_found_variable(ctx, image):
    ctx.screen.images[image] = (50, 60, 0.99)
    assert play(ctx, image_row(image, var_name="")) is Flow.NEXT
    assert ctx.variables.user == {}


# ---------------------------------------------------------------------------
# match_text
# ---------------------------------------------------------------------------

def text_row(**params):
    params.setdefault("x", 0)
    params.setdefault("y", 0)
    params.setdefault("x2", 400)
    params.setdefault("y2", 300)
    return row("match_text", **params)


@pytest.mark.parametrize("threshold, found", [(0.9, 1), (0.95, 0), (0.0, 1)])
def test_text_similarity_threshold(ctx, threshold, found):
    ctx.screen.text = "Hello Wrld\n"
    play(ctx, text_row(expr="Hello World", similarity=threshold, timeout_ms=0, capture_var="t"))
    assert ctx.variables["found"] == found
    assert ("t" in ctx.variables) == bool(found)


def test_text_found_captures_text_in_framed_region(ctx):
    ctx.origin = (10, 20)
    ctx.variables["name"] = "World"
    ctx.screen.text = " hello   world \n"
    r = text_row(expr="Hello {name}", capture_var="seen", x2=100, y2=50)
    assert play(ctx, r) is Flow.NEXT
    assert ctx.variables["found"] == 1
    assert ctx.variables["seen"] == "hello   world"
    assert ctx.screen.captures == [Rect(10, 20, 100, 50)]


def test_text_timeout_outcomes(ctx):
    ctx.screen.text = "nothing here"
    assert play(ctx, text_row(expr="Hello", timeout_ms=600)) is Flow.NEXT
    assert ctx.sleeps == [(300, False), (300, False)]
    assert len(ctx.screen.captures) == 3
    result = play(ctx, text_row(expr="Hello", timeout_ms=0, on_timeout="abort"))
    assert isinstance(result, Abort) and result.message == 'text "Hello" not found within 0 ms'
    assert play(ctx, text_row(expr="Hello", timeout_ms=0, on_timeout="goto", target="b")) == Jump("b")


def test_text_stop_while_polling(ctx):
    ctx.stop_after = 2
    assert play(ctx, text_row(expr="Hello", timeout_ms=5000)) is Flow.STOP
