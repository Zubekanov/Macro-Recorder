"""End-to-end tests of the merged engine.

Recorded streams go through bundling, the file format and validation, and
documents play on the real :class:`Player` with the real registered action
specs.  Only the platform is faked (``tests/conftest.py``): nothing here
touches the X server.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from macro_recorder.engine import paths
from macro_recorder.engine.player import Player
from macro_recorder.gui.settings import RecordingSettings, Settings
from macro_recorder.model import fileformat, validate
from macro_recorder.model.bundling import rows_from_stream
from macro_recorder.model.catalogue import make
from macro_recorder.model.document import MacroDocument, Row, make_row
from macro_recorder.model.protocols import RawEvent, Rect, WindowInfo
from macro_recorder.platform.windows import NullWindowManager
from tests.conftest import FakeClock, FakeHost, FakeInput, FakeScreen, FakeWindows

XED = 0x100
RECORDED_RECT = Rect(100, 80, 900, 600)
RECORDED_CLIENT = Rect(100, 110, 900, 570)
TITLE = "Untitled - xed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def xed_at(dx: int = 0, dy: int = 0, title: str = TITLE, id: int = XED) -> WindowInfo:
    """The xed window, ``dx``/``dy`` px away from where it was recorded."""
    return WindowInfo(id, title,
                      Rect(RECORDED_RECT.left + dx, RECORDED_RECT.top + dy, 900, 600),
                      Rect(RECORDED_CLIENT.left + dx, RECORDED_CLIENT.top + dy, 900, 570))


def window_row(title: str = TITLE, **fields: Any) -> Row:
    params = dict(window=title, rect=RECORDED_RECT.as_tuple(), client_rect=RECORDED_CLIENT.as_tuple())
    row_fields = {k: fields.pop(k) for k in ("label", "enabled", "breakpoint", "delay_ms", "comment")
                  if k in fields}
    params.update(fields)
    return make_row(make("window_focus", **params), **row_fields)


def r(type_: str, label: str = "", enabled: bool = True, breakpoint: bool = False,
      delay_ms: int = 0, **params: Any) -> Row:
    return make_row(make(type_, **params), label=label, enabled=enabled,
                    breakpoint=breakpoint, delay_ms=delay_ms)


class Rig:
    """A Player wired to the conftest fakes."""

    def __init__(self, rows: list[Row] | MacroDocument, *, windows: Any = None,
                 host: FakeHost | None = None, screen: FakeScreen | None = None,
                 clock: FakeClock | None | bool = True, settings: Settings | None = None,
                 filter: tuple[str, ...] = (), path_mode: str = "recorded",
                 path: str | None = None) -> None:
        self.doc = rows if isinstance(rows, MacroDocument) else MacroDocument(
            rows, filter=filter, path_mode=path_mode, path=path)
        self.settings = settings or Settings()
        self.host = host or FakeHost()
        self.input = FakeInput()
        self.windows = windows if windows is not None else FakeWindows([xed_at()])
        self.screen = screen or FakeScreen()
        self.clock = FakeClock() if clock is True else (clock or None)
        self.player = Player(self.doc, self.settings, self.host, self.input, self.windows,
                             self.screen, clock=self.clock)

    def play(self) -> str | None:
        self.player.play()
        assert len(self.host.finished) == 1
        return self.host.finished[0]

    def actions(self, *kinds: str) -> list[tuple]:
        """Injected actions, optionally only those of the given kinds."""
        return [a for a in self.input.actions if not kinds or a[0] in kinds]

    def typed(self) -> str:
        return "".join(a[1] for a in self.input.actions if a[0] == "type_char")

    def activations(self) -> list[int]:
        return [c[1] for c in self.windows.calls if c[0] == "activate"]


def no_errors(doc: MacroDocument) -> list[str]:
    return [str(p) for p in validate.check(doc) if p.severity == "error"]


# ---------------------------------------------------------------------------
# 1. Record -> bundle -> save -> load -> validate -> play
# ---------------------------------------------------------------------------

def recorded_stream() -> list[RawEvent]:
    """Focus marker, a click, a drag, Shift+H then i, three scroll notches, with pauses."""
    w = WindowInfo(XED, TITLE, RECORDED_RECT, RECORDED_CLIENT)
    return [
        RawEvent("window_focus", 0.0, window=w),
        RawEvent("motion", 0.5, x=300, y=300),
        RawEvent("button_press", 0.6, x=300, y=300, button="left"),
        RawEvent("button_release", 0.65, x=300, y=300, button="left"),
        RawEvent("button_press", 1.5, x=400, y=400, button="left"),
        RawEvent("motion", 1.52, x=410, y=405),
        RawEvent("motion", 1.54, x=420, y=410),
        RawEvent("motion", 1.56, x=440, y=420),
        RawEvent("button_release", 1.6, x=440, y=420, button="left"),
        RawEvent("key_press", 2.5, key="Shift_L"),
        RawEvent("key_press", 2.55, key="h"),
        RawEvent("key_release", 2.6, key="h"),
        RawEvent("key_release", 2.62, key="Shift_L"),
        RawEvent("key_press", 2.7, key="i"),
        RawEvent("key_release", 2.75, key="i"),
        RawEvent("scroll", 3.5, x=500, y=500, dy=1),
        RawEvent("scroll", 3.53, x=500, y=500, dy=1),
        RawEvent("scroll", 3.56, x=500, y=500, dy=1),
    ]


def recorded_document(tmp_path) -> MacroDocument:
    rows = rows_from_stream(recorded_stream(), RecordingSettings())
    path = tmp_path / "recorded.json"
    fileformat.save(MacroDocument(rows), path)
    return fileformat.load(path)


def test_recording_bundles_into_the_expected_rows(tmp_path):
    doc = recorded_document(tmp_path)
    assert [(row.type, row.event.get("action"), len(row.events)) for row in doc.rows] == [
        ("window_focus", None, 1),
        ("wait", None, 1),
        ("mouse_move", None, 1),
        ("mouse_click", "click", 1),
        ("wait", None, 1),
        ("mouse_click", "down", 1),
        ("mouse_move", None, 3),
        ("mouse_click", "up", 1),
        ("wait", None, 1),
        ("key_combo", None, 1),
        ("key_combo", None, 1),
        ("wait", None, 1),
        ("mouse_scroll", None, 1),
    ]
    combos = [row.event["keys"] for row in doc.rows if row.type == "key_combo"]
    assert combos == ["shift+h", "i"]
    assert no_errors(doc) == []
    assert validate.check(doc) == []


def test_saved_and_reloaded_document_is_identical(tmp_path):
    rows = rows_from_stream(recorded_stream(), RecordingSettings())
    path = tmp_path / "again.json"
    fileformat.save(MacroDocument(rows), path)
    loaded = fileformat.load(path)
    assert fileformat.rows_to_dicts(loaded.rows) == fileformat.rows_to_dicts(rows)
    assert not loaded.dirty


def test_recorded_macro_plays_in_a_window_moved_40px(tmp_path):
    doc = recorded_document(tmp_path)
    settings = Settings()
    settings.playback.restore_rect = False
    rig = Rig(doc, windows=FakeWindows([xed_at(40, 40)]), settings=settings)
    assert rig.play() is None
    assert rig.activations() == [XED]
    assert not any(c[0] == "move" for c in rig.windows.calls)
    assert rig.input.actions == [
        ("move", 340, 340),
        ("press_button", "left"), ("release_button", "left"),
        ("move", 440, 440), ("press_button", "left"),
        ("move", 450, 445), ("move", 460, 450), ("move", 480, 460),
        ("release_button", "left"),
        ("press_key", "Shift_L"), ("press_key", "h"),
        ("release_key", "h"), ("release_key", "Shift_L"),
        ("press_key", "i"), ("release_key", "i"),
        ("move", 540, 540), ("scroll", 0, 1), ("scroll", 0, 1), ("scroll", 0, 1),
    ]
    # The waits, delays and holds replay the recorded span, plus the minimum
    # row spacing; nothing is skipped or doubled.
    assert 3.5 <= rig.clock.t <= 3.56 + 0.2


def test_recorded_macro_restores_the_window_frame_by_default(tmp_path):
    doc = recorded_document(tmp_path)
    rig = Rig(doc, windows=FakeWindows([xed_at(40, 40)]))
    assert rig.play() is None
    assert ("move", XED, RECORDED_RECT) in rig.windows.calls
    # Restored to the recorded frame, so the delta is 0.
    assert rig.actions("move")[0] == ("move", 300, 300)


def test_recorded_macro_at_double_speed_halves_the_timing(tmp_path):
    doc = recorded_document(tmp_path)
    settings = Settings()
    settings.playback.speed = 2.0
    rig = Rig(doc, settings=settings)
    assert rig.play() is None
    assert 1.75 <= rig.clock.t <= 1.78 + 0.1


# ---------------------------------------------------------------------------
# 2. Control flow
# ---------------------------------------------------------------------------

def test_var_set_and_if_else_loop():
    rig = Rig([
        r("var_set", var_name="n", expr="0"),
        r("var_set", label="again", var_name="n", expr="n + 1"),
        r("type_text", text="{n}"),
        r("if_else", expr="n < 3", target="again"),
        r("if_else", expr="n == 3", target="yes", else_target="End"),
        r("type_text", text="skipped"),
        r("type_text", label="yes", text="!"),
    ])
    assert rig.play() is None
    assert rig.typed() == "123!"
    assert rig.player.variables["n"] == 3


def test_if_else_false_goes_to_else_target():
    rig = Rig([
        r("var_set", var_name="x", expr="'abc'"),
        r("if_else", expr="len(x) > 5", target="big", else_target="small"),
        r("type_text", label="big", text="B"),
        r("goto", target="End"),
        r("type_text", label="small", text="S"),
    ])
    assert rig.play() is None
    assert rig.typed() == "S"


def test_nested_repeat_across_two_program_passes():
    settings = Settings()
    settings.playback.repeat = 2
    rig = Rig([
        r("type_text", text="P{iteration} "),
        r("type_text", label="outer", text="O{loop} "),
        r("type_text", label="inner", text="I{loop} "),
        r("repeat", target="inner", count=2),
        r("repeat", target="outer", count=3),
    ], settings=settings)
    assert rig.play() is None
    one_pass = "O1 I1 I2 O2 I1 I2 O3 I1 I2 "
    assert rig.typed() == "P1 " + one_pass + "P2 " + one_pass


def test_repeat_count_from_expression_and_zero_falls_through():
    rig = Rig([
        r("var_set", var_name="times", expr="2"),
        r("type_text", label="body", text="x"),
        r("repeat", target="body", count="times + 1"),
        r("type_text", label="once", text="y"),
        r("repeat", target="once", count=0),
    ])
    assert rig.play() is None
    assert rig.typed() == "xxxy"


def test_goto_to_a_window_row_reactivates_it():
    windows = FakeWindows([xed_at(), WindowInfo(0x200, "Firefox", Rect(0, 0, 1920, 1080),
                                                  Rect(0, 30, 1920, 1050))])
    rig = Rig([
        r("var_set", var_name="c", expr="0"),
        window_row(label="win"),
        r("var_set", var_name="c", expr="c + 1"),
        r("mouse_click", x=200, y=200),
        r("if_else", expr="c < 2", target="win"),
    ], windows=windows)
    windows.active_id = 0x200
    assert rig.play() is None
    assert rig.activations() == [XED, XED]
    assert rig.actions("press_button") == [("press_button", "left")] * 2


def test_jump_into_a_group_activates_its_window_first():
    rig = Rig([
        r("goto", target="inside"),
        window_row(),
        r("mouse_click", label="inside", x=200, y=200),
    ], windows=FakeWindows([xed_at(40, 40)]))
    rig.settings.playback.restore_rect = False
    assert rig.play() is None
    assert rig.activations() == [XED]
    assert rig.actions("move") == [("move", 240, 240)]


def test_missing_label_aborts_with_the_row_number():
    rig = Rig([
        r("var_set", var_name="a", expr="1"),
        r("goto", target="nowhere"),
        r("type_text", text="never"),
    ])
    error = rig.play()
    assert error is not None and error.startswith("Row 2: ")
    assert "nowhere" in error
    assert rig.typed() == ""


def test_expression_error_aborts_with_the_row_number():
    rig = Rig([r("type_text", text="ok"), r("var_set", var_name="a", expr="1 / 0")])
    error = rig.play()
    assert error is not None and error.startswith("Row 2: ")


# ---------------------------------------------------------------------------
# 3. match_image in a windowed frame, then a click on the capture vars
# ---------------------------------------------------------------------------

def image_document(tmp_path, **image_params: Any) -> MacroDocument:
    (tmp_path / "button.png").write_bytes(b"png")
    params = dict(image_path="button.png", x=100, y=110, x2=1000, y2=680, timeout_ms=2000,
                  var_name="found", capture_var="found_x", capture_var_y="found_y")
    params.update(image_params)
    rows = [
        window_row(restore_rect="no"),
        r("match_image", **params),
        r("if_else", expr="found", target="hit", else_target="End"),
        r("mouse_click", label="hit", x="found_x", y="found_y"),
        r("mouse_click", x="found_x + 10", y="found_y"),
    ]
    path = tmp_path / "image.json"
    fileformat.save(MacroDocument(rows), path)
    return fileformat.load(path)


def test_match_image_capture_vars_drive_a_click_in_a_window_frame(tmp_path):
    doc = image_document(tmp_path)
    assert no_errors(doc) == []
    screen = FakeScreen()
    screen.images[str(tmp_path / "button.png")] = (600, 400, 0.95)
    rig = Rig(doc, windows=FakeWindows([xed_at(40, 40)]), screen=screen)
    assert rig.play() is None
    variables = rig.player.variables
    # Capture vars are relative to the window's client origin (140, 150).
    assert (variables["found"], variables["found_x"], variables["found_y"]) == (1, 460, 250)
    assert rig.input.actions == [
        ("move", 600, 400), ("press_button", "left"), ("release_button", "left"),
        ("move", 610, 400), ("press_button", "left"), ("release_button", "left"),
    ]


def test_match_image_region_is_frame_adjusted(tmp_path):
    doc = image_document(tmp_path)
    screen = FakeScreen()
    # Inside the recorded region but outside the region shifted by the delta.
    screen.images[str(tmp_path / "button.png")] = (1020, 700, 0.95)
    rig = Rig(doc, windows=FakeWindows([xed_at(40, 40)]), screen=screen)
    assert rig.play() is None
    assert rig.player.variables["found_x"] == 880
    screen.images[str(tmp_path / "button.png")] = (120, 130, 0.95)
    rig2 = Rig(image_document(tmp_path), windows=FakeWindows([xed_at(40, 40)]), screen=screen)
    assert rig2.play() is None
    assert rig2.player.variables["found"] == 0


def test_match_image_timeout_is_not_scaled_by_speed(tmp_path):
    doc = image_document(tmp_path, timeout_ms=3000)
    settings = Settings()
    settings.playback.speed = 4.0
    rig = Rig(doc, windows=FakeWindows([xed_at(40, 40)]), settings=settings)
    assert rig.play() is None
    assert rig.player.variables["found"] == 0
    assert rig.input.actions == []
    assert 3.0 <= rig.clock.t <= 3.2


def test_match_image_click_found(tmp_path):
    doc = image_document(tmp_path, click_found=True)
    screen = FakeScreen()
    screen.images[str(tmp_path / "button.png")] = (600, 400, 0.95)
    rig = Rig(doc, windows=FakeWindows([xed_at(40, 40)]), screen=screen)
    assert rig.play() is None
    assert rig.input.actions[:3] == [("move", 600, 400), ("press_button", "left"),
                                     ("release_button", "left")]


# ---------------------------------------------------------------------------
# 4. wait_pixel on the fake clock
# ---------------------------------------------------------------------------

class TimedScreen(FakeScreen):
    """A pixel that turns ``colour`` once the clock reaches ``at`` seconds."""

    def __init__(self, clock: FakeClock, point: tuple[int, int], colour: tuple[int, int, int],
                 at: float) -> None:
        super().__init__()
        self.clock, self.point, self.colour, self.at = clock, point, colour, at
        self.reads: list[float] = []

    def pixel(self, x, y):
        self.reads.append(self.clock.now())
        if (x, y) == self.point and self.clock.now() >= self.at:
            return self.colour
        return self.default_pixel


def test_wait_pixel_equals_waits_until_the_colour_appears():
    clock = FakeClock()
    screen = TimedScreen(clock, (240, 250), (255, 0, 0), at=1.234)
    settings = Settings()
    settings.playback.restore_rect = False
    rig = Rig([
        window_row(),
        r("wait_pixel", x=200, y=210, colour="#ff0000", timeout_ms=5000),
        r("type_text", text="done"),
    ], windows=FakeWindows([xed_at(40, 40)]), screen=screen, clock=clock, settings=settings)
    assert rig.play() is None
    assert rig.typed() == "done"
    assert 1.234 <= clock.t <= 1.234 + 0.05 + 0.05
    gaps = [b - a for a, b in zip(screen.reads, screen.reads[1:])]
    assert gaps and max(gaps) <= 0.05 + 1e-9


def test_wait_pixel_differs():
    clock = FakeClock()
    screen = TimedScreen(clock, (10, 10), (1, 2, 3), at=0.3)
    rig = Rig([r("wait_pixel", x=10, y=10, colour="#000000", mode="differs", timeout_ms=2000),
               r("type_text", text="moved")], screen=screen, clock=clock)
    assert rig.play() is None
    assert rig.typed() == "moved"
    assert 0.3 <= clock.t <= 0.45


def test_wait_pixel_timeout_aborts_and_is_not_scaled():
    settings = Settings()
    settings.playback.speed = 5.0
    rig = Rig([r("type_text", text="a"),
               r("wait_pixel", x=10, y=20, colour="#ff0000", timeout_ms=1500),
               r("type_text", text="b")], settings=settings)
    error = rig.play()
    assert error == "Row 2: pixel (10, 20) did not become #ff0000 within 1500 ms"
    assert rig.typed() == "a"
    assert 1.5 <= rig.clock.t <= 1.6


def test_wait_pixel_differs_timeout_continue_and_goto():
    rig = Rig([
        r("wait_pixel", x=1, y=1, colour="#000000", mode="differs", timeout_ms=200,
          on_timeout="continue"),
        r("wait_pixel", x=1, y=1, colour="#ffffff", timeout_ms=300, on_timeout="goto", target="out"),
        r("type_text", text="skipped"),
        r("type_text", label="out", text="out"),
    ])
    assert rig.play() is None
    assert rig.typed() == "out"
    assert any("did not change from #000000 within 200 ms; continuing" in line for line in rig.host.logs)
    assert any("going to out" in line for line in rig.host.logs)
    assert 0.5 <= rig.clock.t <= 0.6


# ---------------------------------------------------------------------------
# 5. Breakpoints
# ---------------------------------------------------------------------------

class ResumingHost(FakeHost):
    """Resumes the player as soon as it pauses, recording what it saw."""

    def __init__(self, fake_input: FakeInput | None = None) -> None:
        super().__init__()
        self.player: Player | None = None
        self.input = fake_input
        self.held_at_pause: list[set[str]] = []

    def on_paused(self, row_id, variables):
        super().on_paused(row_id, variables)
        if self.input is not None:
            self.held_at_pause.append(set(self.input.held_keys) | set(self.input.held_buttons))
        assert self.player is not None
        self.player.resume()


def test_breakpoint_releases_held_input_and_restores_it_on_resume():
    host = ResumingHost()
    rows = [
        window_row(),
        r("var_set", var_name="score", expr="41 + 1"),
        r("key_press", key="Shift_L"),
        r("mouse_click", x=300, y=300, action="down"),
        r("type_text", text="a", breakpoint=True),
        r("mouse_click", x=300, y=300, action="up"),
        r("key_release", key="Shift_L"),
    ]
    rig = Rig(rows, host=host)
    host.player, host.input = rig.player, rig.input
    assert rig.play() is None
    assert host.held_at_pause == [set()]
    (row_id, variables), = host.paused
    assert row_id == rows[4].id
    assert variables["score"] == 42
    assert variables["iteration"] == 1 and variables["op_num"] == 5
    assert (variables["win_x"], variables["win_y"]) == (100, 110)
    assert rig.activations() == [XED, XED]
    assert rig.input.actions == [
        ("press_key", "Shift_L"),
        ("move", 300, 300), ("press_button", "left"),
        ("release_button", "left"), ("release_key", "Shift_L"),     # pause
        ("press_key", "Shift_L"), ("press_button", "left"),         # resume
        ("type_char", "a"),
        ("release_button", "left"), ("release_key", "Shift_L"),
    ]


def test_breakpoint_on_a_groups_first_row_pauses_in_that_window():
    host = ResumingHost()
    rows = [window_row(), r("type_text", text="x", breakpoint=True)]
    rig = Rig(rows, host=host, windows=FakeWindows([xed_at()]))
    host.player = rig.player
    assert rig.play() is None
    assert rig.activations() == [XED, XED]
    assert host.paused[0][1]["win_x"] == 100


def test_paused_time_is_excluded_from_rt_ms():
    clock = FakeClock()

    class SlowResume(ResumingHost):
        def on_paused(self, row_id, variables):
            clock.advance(30.0)
            super().on_paused(row_id, variables)

    host = SlowResume()
    rig = Rig([r("wait", duration_ms=200), r("var_set", var_name="t", expr="rt_ms", breakpoint=True)],
              host=host, clock=clock)
    host.player = rig.player
    assert rig.play() is None
    assert rig.player.variables["t"] < 1000


def test_stop_while_paused_from_another_thread():
    host = FakeHost()
    rig = Rig([r("key_press", key="Control_L"), r("type_text", text="x", breakpoint=True)],
              host=host, clock=None)
    worker = threading.Thread(target=rig.player.play)
    worker.start()
    deadline = time.monotonic() + 5
    while not host.paused and time.monotonic() < deadline:
        time.sleep(0.01)
    assert host.paused
    rig.player.stop()
    worker.join(5)
    assert not worker.is_alive()
    assert host.finished == [None]
    assert rig.typed() == ""
    assert rig.input.held_keys == set()


# ---------------------------------------------------------------------------
# 6. message_box
# ---------------------------------------------------------------------------

def box_rows(**box: Any) -> list[Row]:
    return [
        window_row(),
        r("key_press", key="Control_L"),
        r("message_box", title="Q", text="Answer {1 + 1}?", var_name="answer", **box),
        r("type_text", text="N"),
        r("goto", target="End"),
        r("type_text", label="first", text="1"),
        r("goto", target="End"),
        r("type_text", label="second", text="2"),
    ]


@pytest.mark.parametrize("buttons, answer, targets, typed", [
    ("ok", "ok", {}, "N"),
    ("ok", "ok", {"target_ok": "first"}, "1"),
    ("ok_cancel", "cancel", {}, ""),                                # Cancel defaults to End
    ("ok_cancel", "cancel", {"target_cancel": "second"}, "2"),
    ("ok_cancel", "ok", {"target_ok": "first", "target_cancel": "second"}, "1"),
    ("yes_no", "yes", {"target_yes": "first", "target_no": "second"}, "1"),
    ("yes_no", "no", {"target_yes": "first", "target_no": "second"}, "2"),
    ("yes_no", "no", {"target_yes": "first"}, "N"),
    ("yes_no", "closed", {"target_no": "second"}, "2"),             # closing counts as No
])
def test_message_box_answers_map_to_targets(buttons, answer, targets, typed):
    rig = Rig(box_rows(buttons=buttons, **targets), host=FakeHost([answer]))
    assert rig.play() is None
    assert rig.host.asks == [("Q", "Answer 2?", buttons)]
    assert rig.typed() == typed
    expected = answer if answer != "closed" else "no"
    assert rig.player.variables["answer"] == expected


def test_message_box_releases_keys_and_reactivates_once():
    rig = Rig(box_rows(buttons="ok"), host=FakeHost(["ok"]))
    assert rig.play() is None
    assert rig.activations() == [XED, XED]
    assert rig.input.actions[:3] == [("press_key", "Control_L"), ("release_key", "Control_L"),
                                     ("press_key", "Control_L")]


def test_message_box_none_stops_playback():
    rig = Rig(box_rows(buttons="yes_no", target_yes="first"), host=FakeHost([None]))
    assert rig.play() is None
    assert rig.typed() == ""
    assert "answer" not in rig.player.variables
    assert rig.input.held_keys == set()
    assert rig.activations() == [XED]


# ---------------------------------------------------------------------------
# 7. stop() from another thread
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("long_row", [
    r("wait", duration_ms=60_000),
    r("wait_pixel", x=1, y=1, colour="#ff0000", timeout_ms=60_000),
])
def test_stop_from_another_thread_ends_a_long_wait(long_row):
    host = FakeHost()
    rig = Rig([r("key_press", key="Shift_L"), long_row, r("type_text", text="after")],
              host=host, clock=None)
    worker = threading.Thread(target=rig.player.play)
    started = time.monotonic()
    worker.start()
    deadline = started + 5
    while long_row.id not in host.rows and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    rig.player.stop()
    worker.join(5)
    assert not worker.is_alive()
    assert time.monotonic() - started < 3
    assert host.finished == [None]
    assert rig.typed() == ""
    assert rig.input.actions[-1] == ("release_key", "Shift_L")
    assert rig.input.held_keys == set()


# ---------------------------------------------------------------------------
# 8. Disabled rows and the filter
# ---------------------------------------------------------------------------

def test_disabled_and_filtered_rows_are_skipped():
    rig = Rig([
        r("mouse_move", x=5, y=5),
        r("mouse_click", x=10, y=10),
        r("wait", duration_ms=5000),
        r("key_combo", keys="a", enabled=False),
        r("goto", target="gone"),
        r("type_text", text="skipped"),
        r("type_text", label="gone", text="disabled", enabled=False),
        r("mouse_scroll", dy=2, notch_ms=0),
        r("goto", target="filtered"),
        r("type_text", text="skipped"),
        r("wait", label="filtered", duration_ms=9000),
        r("type_text", text="end"),
    ], filter=("mouse_move", "wait"))
    assert rig.play() is None
    assert rig.input.actions == [
        ("move", 10, 10), ("press_button", "left"), ("release_button", "left"),
        ("scroll", 0, 1), ("scroll", 0, 1),
        ("type_char", "e"), ("type_char", "n"), ("type_char", "d"),
    ]
    assert rig.clock.t < 1.0


def test_filter_survives_save_and_load(tmp_path):
    doc = MacroDocument([r("mouse_move", x=5, y=5), r("type_text", text="k")],
                        filter=("mouse_move",))
    path = tmp_path / "filtered.json"
    fileformat.save(doc, path)
    rig = Rig(fileformat.load(path))
    assert rig.play() is None
    assert rig.input.actions == [("type_char", "k")]


def test_disabled_window_row_ends_the_previous_group():
    settings = Settings()
    settings.playback.restore_rect = False
    rig = Rig([
        window_row(),
        r("mouse_click", x=200, y=200),
        window_row(title="Firefox", enabled=False),
        r("mouse_click", x=200, y=200),
    ], windows=FakeWindows([xed_at(40, 40)]), settings=settings)
    assert rig.play() is None
    assert rig.activations() == [XED]
    assert rig.actions("move") == [("move", 240, 240), ("move", 200, 200)]


# ---------------------------------------------------------------------------
# 9. Path rows under the path modes
# ---------------------------------------------------------------------------

def path_row() -> Row:
    return make_row((make("mouse_move", x=100, y=100),
                     make("mouse_move", x=150, y=120, dt=0.1),
                     make("mouse_move", x=200, y=200, dt=0.2),
                     make("mouse_move", x=300, y=250, dt=0.1)))


def test_path_replays_recorded_points_with_their_timing():
    row = path_row()
    rig = Rig([row], path_mode="recorded")
    times: list[float] = []
    original = rig.input.move

    def move(x, y):
        times.append(rig.clock.now())
        original(x, y)

    rig.input.move = move
    assert rig.play() is None
    assert rig.actions("move") == [("move", 100, 100), ("move", 150, 120),
                                   ("move", 200, 200), ("move", 300, 250)]
    start = times[0]
    assert [round(t - start, 6) for t in times] == [0.0, 0.1, 0.3, 0.4]


def test_path_under_linear_mode_draws_one_line_over_the_span():
    rig = Rig([r("mouse_move", x=0, y=0), path_row()], path_mode="linear")
    assert rig.play() is None
    moves = rig.actions("move")
    assert moves[0] == ("move", 0, 0)
    drawn = moves[1:]
    expected = paths.generate("linear", (0, 0), (300, 250), 400.0, rig.player.rng)
    assert len(drawn) == len(expected)
    assert drawn[-1] == ("move", 300, 250)
    # Straight line: every point lies on y = 250/300 x.
    assert all(abs(y - x * 250 / 300) <= 1 for _, x, y in drawn)
    assert (150, 120) not in [(x, y) for _, x, y in drawn]


@pytest.mark.parametrize("mode", ["spline", "natural"])
def test_path_under_generated_modes_ends_on_the_last_point_over_the_span(mode):
    rig = Rig([r("mouse_move", x=0, y=0), path_row()], path_mode=mode)
    rig.player.rng.seed(7)
    times: list[float] = []
    original = rig.input.move

    def move(x, y):
        times.append(rig.clock.now())
        original(x, y)

    rig.input.move = move
    assert rig.play() is None
    moves = rig.actions("move")
    assert moves[-1] == ("move", 300, 250)
    assert len(moves) > 10
    assert times[-1] - times[1] == pytest.approx(0.4, abs=0.011)


def test_path_is_frame_adjusted_in_a_window_group():
    settings = Settings()
    settings.playback.restore_rect = False
    rig = Rig([window_row(), path_row()], windows=FakeWindows([xed_at(-20, 10)]),
              settings=settings)
    assert rig.play() is None
    assert rig.actions("move") == [("move", 80, 110), ("move", 130, 130),
                                   ("move", 180, 210), ("move", 280, 260)]


# ---------------------------------------------------------------------------
# 10. CLI-style play with no window manager
# ---------------------------------------------------------------------------

def test_no_window_manager_skips_window_groups():
    rig = Rig([
        r("mouse_click", x=10, y=10),
        r("var_set", var_name="w", expr="win_w"),
        window_row(),
        r("mouse_click", x=200, y=200),
        window_row(title="", enabled=False),
        r("type_text", text="after"),
    ], windows=NullWindowManager())
    assert rig.play() is None
    assert rig.actions("move") == [("move", 10, 10)]
    assert rig.typed() == "after"
    assert rig.player.variables["w"] == 0
    assert any("not found, skipping its rows" in line for line in rig.host.logs)
    # The lookup polled for the window timeout on the engine clock.
    assert 5.0 <= rig.clock.t <= 5.5


def test_no_window_manager_with_halt_aborts():
    settings = Settings()
    settings.playback.on_missing = "halt"
    settings.playback.window_timeout_ms = 300
    rig = Rig([r("type_text", text="a"), window_row(), r("type_text", text="b")],
              windows=NullWindowManager(), settings=settings)
    assert rig.play() == "Row 2: window %r was not found" % TITLE
    assert rig.typed() == "a"


def test_no_window_manager_active_window_row():
    rig = Rig([window_row(title=""), r("type_text", text="x"), r("beep")],
              windows=NullWindowManager())
    assert rig.play() is None
    assert rig.typed() == ""
