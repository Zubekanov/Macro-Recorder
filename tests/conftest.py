"""Test fixtures and fakes.

Every Protocol in ``macro_recorder.model.protocols`` has a fake here.  Tests
that must open a real X display or a Tk root are marked ``live_x`` and run
only when ``MACRO_RECORDER_LIVE_X=1`` (see ``scripts/test-live.sh``).  An
unmarked test that tries to create a Tk root or open an X connection fails.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from typing import Any, Callable, Mapping

import pytest

from macro_recorder.model.protocols import Monitor, RawEvent, Rect, WindowInfo

LIVE_X = os.environ.get("MACRO_RECORDER_LIVE_X") == "1"


def pytest_configure(config):
    config.addinivalue_line("markers", "live_x: opens windows on an X display; run only under Xvfb")


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="set MACRO_RECORDER_LIVE_X=1 under xvfb-run to run live X tests")
    for item in items:
        if "live_x" in item.keywords and not LIVE_X:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_display_for_unmarked_tests(request, monkeypatch):
    """Fail fast if a test that is not marked live_x touches the X server."""
    if "live_x" in request.keywords:
        return
    try:
        import tkinter
    except ImportError:
        tkinter = None
    if tkinter is not None:
        def _forbidden(*args, **kwargs):
            raise AssertionError("This test creates a Tk root; mark it live_x")
        monkeypatch.setattr(tkinter, "Tk", _forbidden)
    try:
        import Xlib.display
    except ImportError:
        return

    def _forbidden_display(*args, **kwargs):
        raise ForbiddenXAccess("This test opens an X display; mark it live_x")
    monkeypatch.setattr(Xlib.display, "Display", _forbidden_display)


class ForbiddenXAccess(OSError):
    """Raised when an unmarked test opens an X display."""


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeClock:
    """Manual clock: ``sleep`` advances time instantly and records the request."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeInput:
    """Records every injected action as a tuple."""

    def __init__(self) -> None:
        self.actions: list[tuple] = []
        self._pos = (0, 0)
        self.held_keys: set[str] = set()
        self.held_buttons: set[str] = set()

    def move(self, x, y):
        self._pos = (int(round(x)), int(round(y)))
        self.actions.append(("move", self._pos[0], self._pos[1]))

    def position(self):
        return self._pos

    def press_button(self, button):
        self.held_buttons.add(button)
        self.actions.append(("press_button", button))

    def release_button(self, button):
        self.held_buttons.discard(button)
        self.actions.append(("release_button", button))

    def scroll(self, dx, dy):
        self.actions.append(("scroll", dx, dy))

    def press_key(self, name):
        self.held_keys.add(name)
        self.actions.append(("press_key", name))

    def release_key(self, name):
        self.held_keys.discard(name)
        self.actions.append(("release_key", name))

    def type_char(self, char):
        self.actions.append(("type_char", char))

    def release_all(self):
        for b in sorted(self.held_buttons):
            self.actions.append(("release_button", b))
        for k in sorted(self.held_keys):
            self.actions.append(("release_key", k))
        self.held_buttons.clear()
        self.held_keys.clear()

    def close(self):
        self.release_all()


class FakeListener:
    """Delivers the RawEvents a test feeds it."""

    def __init__(self) -> None:
        self._callback: Callable[[RawEvent], None] | None = None
        self.alive = False

    def start(self, callback):
        self._callback = callback
        self.alive = True

    def stop(self):
        self.alive = False

    def is_alive(self):
        return self.alive

    def close(self):
        self.alive = False

    def feed(self, *events: RawEvent):
        assert self._callback is not None, "listener not started"
        for e in events:
            self._callback(e)


class FakeStopKey:
    def __init__(self) -> None:
        self.key: str | None = None
        self._callback: Callable[[], None] | None = None

    def start(self, key, callback):
        self.key, self._callback = key, callback

    def stop(self):
        self.key, self._callback = None, None

    def close(self):
        self.stop()

    def press(self):
        if self._callback:
            self._callback()


class FakeHotkeys:
    def __init__(self, refuse: set[str] | None = None) -> None:
        self.grabbed: dict[str, Callable[[], None]] = {}
        self.refuse = refuse or set()
        self.suspended = False
        self.waits: list[float] = []

    def grab(self, combo, callback):
        if combo in self.refuse:
            return False
        self.grabbed[combo] = callback
        return True

    def ungrab_all(self):
        self.grabbed.clear()

    def suspend(self):
        self.suspended = True

    def resume(self):
        self.suspended = False
        return []

    def wait_keys_released(self, timeout_s):
        self.waits.append(timeout_s)
        return True

    def close(self):
        self.grabbed.clear()

    def press(self, combo):
        if not self.suspended and combo in self.grabbed:
            self.grabbed[combo]()


class FakeScreen:
    """Pixels, images and OCR text come from dicts the test fills in."""

    def __init__(self) -> None:
        self.pixels: dict[tuple[int, int], tuple[int, int, int]] = {}
        self.default_pixel = (0, 0, 0)
        self.images: dict[str, tuple[int, int, float] | None] = {}
        self.text = ""
        self.captures: list[Rect] = []
        self._monitors = [Monitor(1, Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1050))]

    def capture(self, rect):
        self.captures.append(rect)
        return None

    def pixel(self, x, y):
        return self.pixels.get((x, y), self.default_pixel)

    def find_image(self, rect, path, confidence):
        hit = self.images.get(path)
        if hit is None:
            return None
        cx, cy, score = hit
        if score < confidence or not rect.contains(cx, cy):
            return None
        return hit

    def ocr(self, rect):
        self.captures.append(rect)
        return self.text

    def monitors(self):
        return list(self._monitors)


class FakeWindows:
    """A list of windows the test controls; activation and moves succeed by default."""

    def __init__(self, windows: list[WindowInfo] | None = None) -> None:
        self.windows: list[WindowInfo] = list(windows or [])
        self.active_id: int | None = self.windows[0].id if self.windows else None
        self.refuse_move = False
        self.refuse_activate = False
        self.calls: list[tuple] = []
        self._watchers: list[Callable[[WindowInfo], None]] = []
        self.bells = 0

    def list(self):
        return list(self.windows)

    def active(self):
        for w in self.windows:
            if w.id == self.active_id:
                return w
        return None

    def find(self, pattern, mode="substring"):
        import re
        for w in self.windows:
            if w.title.lower() == pattern.lower():
                return w
        for w in self.windows:
            if mode == "regex":
                if re.search(pattern, w.title):
                    return w
            elif pattern.lower() in w.title.lower():
                return w
        return None

    def refresh(self, window):
        for w in self.windows:
            if w.id == window.id:
                return w
        return None

    def activate(self, window, settle_ms=1000):
        self.calls.append(("activate", window.id))
        if self.refuse_activate:
            return None
        self.active_id = window.id
        for cb in self._watchers:
            cb(window)
        return self.refresh(window)

    def move(self, window, rect, settle_ms=1000):
        self.calls.append(("move", window.id, rect))
        if self.refuse_move:
            return self.refresh(window)
        delta_x = rect.left - window.rect.left
        delta_y = rect.top - window.rect.top
        client = Rect(window.client_rect.left + delta_x, window.client_rect.top + delta_y,
                      window.client_rect.width + (rect.width - window.rect.width),
                      window.client_rect.height + (rect.height - window.rect.height))
        moved = WindowInfo(window.id, window.title, rect, client, window.desktop, window.maximised)
        self.windows = [moved if w.id == window.id else w for w in self.windows]
        return moved

    def set_maximised(self, window, maximised, settle_ms=1000):
        self.calls.append(("maximise", window.id, maximised))
        updated = WindowInfo(window.id, window.title, window.rect, window.client_rect,
                             window.desktop, maximised)
        self.windows = [updated if w.id == window.id else w for w in self.windows]
        return updated

    def watch_active(self, callback):
        self._watchers.append(callback)
        return lambda: self._watchers.remove(callback)

    def monitors(self):
        return [Monitor(1, Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1050))]

    def bell(self):
        self.bells += 1

    def screen_size(self):
        return (1920, 1080)

    def window_at(self, x, y):
        for w in self.windows:
            if w.rect.contains(x, y):
                return w.id
        return None


class FakeHost:
    """Records player callbacks; ``answers`` feeds ``ask``."""

    def __init__(self, answers: list[str | None] | None = None) -> None:
        self.rows: list[int] = []
        self.logs: list[str] = []
        self.paused: list[tuple[int, dict]] = []
        self.finished: list[str | None] = []
        self.answers = deque(answers or [])
        self.asks: list[tuple[str, str, str]] = []
        self.resume_event = threading.Event()

    def on_row(self, row_id):
        self.rows.append(row_id)

    def on_log(self, line):
        self.logs.append(line)

    def on_paused(self, row_id, variables):
        self.paused.append((row_id, dict(variables)))

    def on_finished(self, error):
        self.finished.append(error)

    def ask(self, title, text, buttons):
        self.asks.append((title, text, buttons))
        return self.answers.popleft() if self.answers else "ok"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake_input():
    return FakeInput()


@pytest.fixture
def fake_listener():
    return FakeListener()


@pytest.fixture
def fake_screen():
    return FakeScreen()


@pytest.fixture
def fake_windows():
    return FakeWindows([
        WindowInfo(id=0x100, title="xed", rect=Rect(100, 80, 900, 600),
                   client_rect=Rect(100, 110, 900, 570)),
        WindowInfo(id=0x200, title="Firefox", rect=Rect(0, 0, 1920, 1080),
                   client_rect=Rect(0, 30, 1920, 1050), maximised=True),
    ])


@pytest.fixture
def fake_host():
    return FakeHost()


@pytest.fixture
def settings():
    from macro_recorder.gui.settings import Settings
    return Settings()


def window(id: int, title: str, left: int = 0, top: int = 0, width: int = 800, height: int = 600,
           frame: int = 30) -> WindowInfo:
    """Build a WindowInfo with a title bar of ``frame`` pixels."""
    return WindowInfo(id=id, title=title, rect=Rect(left, top, width, height),
                      client_rect=Rect(left, top + frame, width, height - frame))
