"""The platform layer against a real X server.

Run only under Xvfb: ``MACRO_RECORDER_LIVE_X=1 xvfb-run -a pytest -m live_x``.
Xvfb has no window manager, so the core-protocol fallbacks are what runs.
"""

import threading
import time

import pytest

from macro_recorder.model.keys import NAMED_KEYS
from macro_recorder.model.protocols import Rect
from macro_recorder.platform import x11
from macro_recorder.platform.input import (
    XGrabHotkeys, XI2StopKeyWatcher, XRecordListener, XTestInput, capture_point,
)
from macro_recorder.platform.windows import X11WindowManager, create_window_manager

pytestmark = pytest.mark.live_x

ALPHA = "Macro Recorder Probe Alpha"
BETA = "Macro Recorder Probe Beta"


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def tk():
    return pytest.importorskip("tkinter")


@pytest.fixture
def probe(tk):
    root = tk.Tk()
    root.title(ALPHA)
    root.geometry("300x200+120+80")
    root.update()
    yield root
    root.destroy()


@pytest.fixture
def wm():
    manager = X11WindowManager()
    yield manager
    manager.close()


@pytest.fixture
def injector():
    inp = XTestInput()
    yield inp
    inp.close()


@pytest.fixture
def display():
    d = x11.open_display()
    yield d
    d.close()


def tap(inp, *names):
    for name in names:
        inp.press_key(name)
    for name in reversed(names):
        inp.release_key(name)


# ---------------------------------------------------------------- windows

def test_factory_picks_x11():
    assert isinstance(create_window_manager(), X11WindowManager)


def test_find_reports_tk_geometry(probe, wm, display):
    info = wm.find("probe alpha")                   # case-insensitive substring
    assert info is not None and info.title == ALPHA
    assert info.rect == Rect(120, 80, 300, 200)
    assert info.client_rect == info.rect            # no frame without a window manager
    assert info.id == x11.tk_toplevel_xid(display, probe.winfo_id())
    assert wm.find(r"Probe\s+Alpha$", "regex").id == info.id
    assert wm.find("no such window") is None
    assert info.id in [w.id for w in wm.list()]
    assert wm.refresh(info) == info


def test_move_settles_on_the_new_rect(probe, wm):
    info = wm.find(ALPHA)
    moved = wm.move(info, Rect(40, 30, 250, 150), settle_ms=1000)
    assert moved is not None and moved.rect == Rect(40, 30, 250, 150)
    probe.update()
    assert (probe.winfo_rootx(), probe.winfo_rooty()) == (40, 30)
    assert (probe.winfo_width(), probe.winfo_height()) == (250, 150)
    started = time.monotonic()
    assert wm.move(moved, moved.rect, settle_ms=1000).rect == moved.rect
    assert time.monotonic() - started < 0.5          # already there: two reads agree at once


def test_activate_settles_and_reports_the_window(probe, wm, tk):
    beta = tk.Toplevel(probe)
    beta.title(BETA)
    beta.geometry("100x100+500+300")
    beta.update()
    alpha = wm.find(ALPHA)
    assert wm.activate(alpha, settle_ms=1000).id == alpha.id
    assert wm.active().id == alpha.id
    other = wm.find(BETA)
    assert wm.activate(other, settle_ms=1000).id == other.id
    assert wm.active().title == BETA


def test_activate_of_a_vanished_window_fails(probe, wm, tk):
    beta = tk.Toplevel(probe)
    beta.title(BETA)
    beta.update()
    info = wm.find(BETA)
    beta.destroy()
    probe.update()
    assert wm.refresh(info) is None
    assert wm.activate(info, settle_ms=200) is None


def test_watch_active_reports_only_changes(probe, wm, tk):
    seen = []
    got_beta = threading.Event()

    def on_active(info):
        seen.append(info.title)
        if info.title == BETA:
            got_beta.set()

    wm.activate(wm.find(ALPHA))
    stop = wm.watch_active(on_active)
    try:
        beta = tk.Toplevel(probe)
        beta.title(BETA)
        beta.geometry("100x100+500+300")
        beta.update()
        wm.activate(wm.find(BETA))
        assert got_beta.wait(timeout=3.0), seen
    finally:
        stop()
    assert ALPHA not in seen and seen[-1] == BETA


def test_window_at_finds_the_toplevel(probe, wm, display):
    expected = x11.tk_toplevel_xid(display, probe.winfo_id())
    assert wm.window_at(150, 100) == expected
    assert wm.window_at(5, 5) is None


def test_monitors_screen_size_and_bell(wm):
    width, height = wm.screen_size()
    assert width > 0 and height > 0
    monitors = wm.monitors()
    assert monitors and monitors[0].index == 1
    assert monitors[0].rect.width <= width and monitors[0].work_area.width <= width
    wm.bell()


# ---------------------------------------------------------------- injection and recording

def test_xtest_injection_is_recorded(injector):
    events = []
    listener = XRecordListener()
    listener.start(events.append)
    try:
        assert listener.is_alive()
        injector.move(50, 60)
        injector.move(70, 80)
        tap(injector, "a")
        injector.type_char("A")
        injector.press_button("left")
        injector.release_button("left")
        injector.press_button("right")
        injector.release_button("right")
        injector.scroll(0, 1)
        injector.scroll(-1, 0)
        assert wait_for(lambda: sum(e.kind == "scroll" for e in events) == 2), events
    finally:
        listener.stop()
    assert not listener.is_alive()
    assert injector.position() == (70, 80)
    keys = [(e.kind, e.key) for e in events if e.key]
    assert keys[:2] == [("key_press", "a"), ("key_release", "a")]
    assert ("key_press", "Shift_L") in keys and ("key_press", "A") in keys
    assert ("key_release", "A") in keys
    buttons = [(e.kind, e.button) for e in events if e.button]
    assert buttons == [("button_press", "left"), ("button_release", "left"),
                       ("button_press", "right"), ("button_release", "right")]
    assert all((e.x, e.y) == (70, 80) for e in events if e.button)
    assert [(e.dx, e.dy) for e in events if e.kind == "scroll"] == [(0, 1), (-1, 0)]
    assert any(e.kind == "motion" and (e.x, e.y) == (70, 80) for e in events)
    times = [e.time for e in events]
    assert times == sorted(times) and times[-1] <= time.monotonic()


def test_every_named_key_resolves(injector):
    for name in NAMED_KEYS:
        assert 8 <= injector.keycode_for(name) <= 255, name
    euro = injector.keycode_for("U20AC")             # not on a US keymap: borrowed
    assert euro >= 8
    injector.release_all()                           # restores borrowed keycodes
    d = x11.open_display()
    try:
        kmap = x11.KeyboardMap.from_display(d)
        assert kmap.find(x11.keysym_from_name("U20AC")) is None
    finally:
        d.close()


def test_held_keys_and_buttons_are_released(injector, display):
    from Xlib import X

    shift = injector.keycode_for("Shift_L")

    def shift_down():
        return bool(display.query_keymap()[shift // 8] & (1 << (shift % 8)))

    def button_down():
        return bool(display.screen().root.query_pointer().mask & X.Button1Mask)

    injector.press_key("Shift_L")
    injector.press_button("left")
    assert wait_for(lambda: shift_down() and button_down())
    injector.release_all()
    assert wait_for(lambda: not shift_down() and not button_down())


# ---------------------------------------------------------------- stop key

def _escape_hits(watcher, injector, settle_s=0.5):
    hits = []
    watcher.start("Escape", lambda: hits.append(1))
    try:
        tap(injector, "Escape")
        time.sleep(settle_s)
    finally:
        mode = watcher.mode
        watcher.stop()
    return hits, mode


def test_xi2_watcher_ignores_synthetic_escape(injector):
    hits, mode = _escape_hits(XI2StopKeyWatcher(), injector)
    if mode != "xi2":
        pytest.skip("XInput 2.1 unavailable")
    assert hits == []


def test_xi2_watcher_sees_escape_from_another_source(injector, monkeypatch):
    # Pretend the XTEST keyboard has another id: the synthetic press then
    # stands in for a real keyboard's and must count.
    monkeypatch.setattr(x11, "xtest_keyboard_ids", lambda d: {9999})
    hits, mode = _escape_hits(XI2StopKeyWatcher(), injector)
    if mode != "xi2":
        pytest.skip("XInput 2.1 unavailable")
    assert hits == [1]


def test_record_fallback_ignores_synthetic_escape(injector):
    hits, mode = _escape_hits(XI2StopKeyWatcher(synthetic=injector.synthetic, use_xi2=False),
                              injector)
    assert mode == "xrecord"
    assert hits == []


def test_record_fallback_without_counter_counts_every_press(injector):
    hits, mode = _escape_hits(XI2StopKeyWatcher(use_xi2=False), injector)
    assert mode == "xrecord" and hits == [1]


# ---------------------------------------------------------------- hotkeys

def test_hotkey_grab_fires_suspends_and_blocks_other_clients(injector):
    first, second = XGrabHotkeys(), XGrabHotkeys()
    fired = threading.Event()
    try:
        assert first.grab("ctrl+shift+F9", fired.set)
        assert not second.grab("ctrl+shift+F9", lambda: None)        # BadAccess
        assert not first.grab("ctrl+alt+F1", lambda: None)           # reserved
        tap(injector, "Control_L", "Shift_L", "F9")
        assert fired.wait(2.0)
        fired.clear()
        tap(injector, "Shift_L", "F9")                               # wrong modifiers
        assert not fired.wait(0.3)
        first.suspend()
        assert second.grab("ctrl+shift+F9", lambda: None)            # free while suspended
        second.ungrab_all()
        tap(injector, "Control_L", "Shift_L", "F9")
        assert not fired.wait(0.3)
        assert first.resume() == []
        tap(injector, "Control_L", "Shift_L", "F9")
        assert fired.wait(2.0)
    finally:
        first.close()
        second.close()


def test_resume_reports_a_combo_taken_while_suspended():
    first, second = XGrabHotkeys(), XGrabHotkeys()
    try:
        first.suspend()
        assert first.grab("ctrl+shift+F8", lambda: None)             # only recorded
        assert second.grab("ctrl+shift+F8", lambda: None)
        assert first.resume() == ["ctrl+shift+F8"]
    finally:
        first.close()
        second.close()


def test_hotkey_survives_caps_lock(injector):
    grabber = XGrabHotkeys()
    fired = threading.Event()
    try:
        assert grabber.grab("ctrl+shift+F5", fired.set)
        tap(injector, "Caps_Lock")
        try:
            tap(injector, "Control_L", "Shift_L", "F5")
            assert fired.wait(2.0)
        finally:
            tap(injector, "Caps_Lock")
    finally:
        grabber.close()


# ---------------------------------------------------------------- targeting

def _capture_async(cancel=lambda: False, timeout_s=5.0):
    result = {}

    def run():
        result["value"] = capture_point(timeout_s, cancel)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.3)                     # let it grab
    return thread, result


def test_capture_point_by_click(injector):
    injector.move(30, 40)
    thread, result = _capture_async()
    injector.press_button("left")
    injector.release_button("left")
    thread.join(5.0)
    x, y, colour = result["value"]
    assert (x, y) == (30, 40)
    assert colour.startswith("#") and len(colour) == 7


def test_capture_point_by_space_and_escape(injector):
    injector.move(11, 12)
    thread, result = _capture_async()
    tap(injector, "space")
    thread.join(5.0)
    assert result["value"][:2] == (11, 12)
    thread, result = _capture_async()
    tap(injector, "Escape")
    thread.join(5.0)
    assert result["value"] is None


def test_capture_point_cancel_releases_the_grabs(display):
    cancelled = threading.Event()
    thread, result = _capture_async(cancel=cancelled.is_set)
    cancelled.set()
    thread.join(5.0)
    assert result["value"] is None
    root = display.screen().root
    from Xlib import X
    assert root.grab_pointer(False, 0, X.GrabModeAsync, X.GrabModeAsync, X.NONE, X.NONE,
                             X.CurrentTime) == X.GrabSuccess
    display.ungrab_pointer(X.CurrentTime)
    display.flush()
