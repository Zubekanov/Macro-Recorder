"""The X-facing logic of the platform layer, driven through small fake displays.

No X server is contacted: ``x11.open_display`` and the request helpers are
replaced, so these tests cover the code paths around the requests (reconnect,
settling, hotkey routing, modifier choice, targeting, XRecord parsing).
"""

import os
import struct
import threading
from collections import deque
from types import SimpleNamespace

import pytest
from Xlib import X
from Xlib.error import ConnectionClosedError, XError
from Xlib.protocol import event as xevent

from macro_recorder.model.protocols import Rect, WindowInfo
from macro_recorder.platform import input as xinput_mod
from macro_recorder.platform import x11
from macro_recorder.platform.input import XGrabHotkeys, XTestInput, _capture_loop
from macro_recorder.platform.session import OwnWindows
from macro_recorder.platform.windows import X11WindowManager
from tests.conftest import FakeClock
from tests.test_x11_helpers import MODIFIERS, ROWS


def ks(name):
    return x11.keysym_from_name(name)


def make_kmap(rows):
    width = 6
    table = []
    for keycode in range(8, 256):
        names = rows.get(keycode, [])
        table.append([ks(n) for n in names] + [0] * (width - len(names)))
    return x11.KeyboardMap(8, table, MODIFIERS)


# Keycode 26 holds EuroSign on Level 3, as the legacy keysym; 65 is Space, 68 F2.
FAKE_ROWS = dict(ROWS) | {26: ["e", "E", "e", "E", "EuroSign", "cent"], 65: ["space"], 68: ["F2"]}
NUM_LOCK = X.Mod2Mask


class FakeDisplay:
    """Just enough of an Xlib Display: an event queue, a pointer, a keymap sink."""

    def __init__(self):
        self._r, self._w = os.pipe()
        self.events = deque()
        self.pointer = SimpleNamespace(mask=0, root_x=0, root_y=0)
        self.keymap_changes = []
        self.closed = False
        self.root = SimpleNamespace(query_pointer=lambda: self.pointer)

    def fileno(self):
        return self._r

    def pending_events(self):
        return len(self.events)

    def next_event(self):
        return self.events.popleft()

    def screen(self):
        return SimpleNamespace(root=self.root)

    def change_keyboard_mapping(self, keycode, rows):
        self.keymap_changes.append((keycode, [list(r) for r in rows]))

    def flush(self):
        pass

    def sync(self):
        pass

    def close(self):
        if not self.closed:
            self.closed = True
            os.close(self._r)
            os.close(self._w)


class KeymapSource:
    """Stands in for ``KeyboardMap.from_display``; tests swap ``current``."""

    def __init__(self, kmap):
        self.current = kmap

    def install(self, monkeypatch):
        monkeypatch.setattr(x11.KeyboardMap, "from_display",
                            classmethod(lambda cls, d: self.current))


@pytest.fixture
def displays(monkeypatch):
    opened = []

    def open_display(name=None):
        d = FakeDisplay()
        opened.append(d)
        return d
    monkeypatch.setattr(x11, "open_display", open_display)
    yield opened
    for d in opened:
        d.close()


@pytest.fixture
def keymap(monkeypatch):
    source = KeymapSource(make_kmap(FAKE_ROWS))
    source.install(monkeypatch)
    return source


def ev(type_, **fields):
    return SimpleNamespace(type=type_, **fields)


class _FakeXError(XError):
    def __init__(self):
        self._data = {}

    def __str__(self):
        return "BadMatch"


# ---------------------------------------------------------------- XTestInput

@pytest.fixture
def injector(displays, keymap, monkeypatch):
    monkeypatch.setattr(x11, "has_xtest", lambda d: True)
    keys = []
    monkeypatch.setattr(x11, "fake_key", lambda d, keycode, press: keys.append((keycode, press)))
    inj = XTestInput()
    inj.keys = keys
    yield inj
    inj.close()


SHIFT, LEVEL3 = 50, 108


def taps(*keycodes):
    """Press the keycodes in order, release them in reverse."""
    return [(k, True) for k in keycodes] + [(k, False) for k in reversed(keycodes)]


@pytest.mark.parametrize("char, caps, expected", [
    ("a", False, taps(38)),
    ("A", False, taps(SHIFT, 38)),
    ("A", True, taps(38)),                          # Caps Lock already upper-cases
    ("a", True, taps(SHIFT, 38)),                   # Shift undoes Caps Lock on letters
    ("!", False, taps(SHIFT, 10)),
    ("!", True, taps(SHIFT, 10)),                   # Caps Lock does not touch symbols
    ("\u00e6", False, taps(LEVEL3, 38)),            # ae on Level 3
    ("\u00c6", False, taps(SHIFT, LEVEL3, 38)),     # AE on Level 3 with Shift
    ("\u00c6", True, taps(LEVEL3, 38)),
    ("@", False, taps(LEVEL3, 24)),
    ("\u20ac", False, taps(LEVEL3, 26)),            # held as the legacy EuroSign keysym
])
def test_type_char_picks_shift_and_level3_from_the_keymap(injector, displays, char, caps,
                                                         expected):
    displays[0].pointer.mask = X.LockMask if caps else 0
    injector.type_char(char)
    assert injector.keys == expected
    assert displays[0].keymap_changes == []


def test_type_char_borrows_a_spare_keycode_for_an_unmapped_character(injector, displays):
    injector.type_char("\u0436")                    # Cyrillic zhe: not in the keymap
    assert injector.keys == taps(8)
    keycode, rows = displays[0].keymap_changes[0]
    assert keycode == 8 and rows[0][0] == 0x01000436
    injector.release_all()
    assert displays[0].keymap_changes[-1] == (8, [[0] * 6])


def test_type_char_does_not_press_a_held_shift_again(injector):
    injector.press_key("Shift_L")
    injector.type_char("A")
    assert injector.keys == [(SHIFT, True), (38, True), (38, False)]
    injector.release_all()
    assert injector.keys[-1] == (SHIFT, False)


def test_type_char_without_level3_borrows_instead(displays, monkeypatch):
    rows = {k: v for k, v in FAKE_ROWS.items() if k != LEVEL3}
    KeymapSource(make_kmap(rows)).install(monkeypatch)
    monkeypatch.setattr(x11, "has_xtest", lambda d: True)
    keys = []
    monkeypatch.setattr(x11, "fake_key", lambda d, keycode, press: keys.append((keycode, press)))
    inj = XTestInput()
    try:
        inj.type_char("\u00e6")
        assert keys == taps(8)
    finally:
        inj.close()


GROUP_2 = 1 << 13


@pytest.mark.parametrize("char, state, expected", [
    ("a", GROUP_2, taps(38)),                       # group 2 of key 38 still holds a
    ("\u044b", GROUP_2, taps(39)),                  # Cyrillic yeru is typed in the active group
    ("\u042b", GROUP_2, taps(SHIFT, 39)),
    ("s", GROUP_2, taps(8)),                        # s is not in group 2: borrowed
    ("\u044b", 0, taps(8)),                         # yeru is not in group 1: borrowed
])
def test_type_char_follows_the_active_group(injector, displays, char, state, expected):
    displays[0].pointer.mask = state
    injector.type_char(char)
    assert injector.keys == expected


def test_caps_lock_adds_shift_only_to_a_case_pair(displays, monkeypatch):
    # French AZERTY: AE02 is [eacute, 2], so Shift would type "2".
    KeymapSource(make_kmap(FAKE_ROWS | {11: ["eacute", "2"]})).install(monkeypatch)
    monkeypatch.setattr(x11, "has_xtest", lambda d: True)
    keys = []
    monkeypatch.setattr(x11, "fake_key", lambda d, keycode, press: keys.append((keycode, press)))
    inj = XTestInput()
    try:
        displays[0].pointer.mask = X.LockMask
        inj.type_char("\u00e9")
        inj.type_char("a")
        assert keys == taps(11) + taps(SHIFT, 38)
    finally:
        inj.close()


@pytest.mark.parametrize("name, state, expected", [
    ("KP_1", 0, [(SHIFT, True), (87, True), (SHIFT, False)]),      # Num Lock off: Shift picks KP_1
    ("KP_1", NUM_LOCK, [(87, True)]),
    ("KP_End", NUM_LOCK, [(SHIFT, True), (87, True), (SHIFT, False)]),
    ("KP_End", 0, [(87, True)]),
    ("A", 0, [(SHIFT, True), (38, True), (SHIFT, False)]),        # recorded with Caps Lock on
    ("A", X.LockMask, [(38, True)]),
    ("a", X.LockMask, [(38, True)]),                # combos name physical keys: no Shift
    ("exclam", 0, [(10, True)]),                    # other keysyms stay bare, as recorded
])
def test_press_key_matches_the_live_lock_state(injector, displays, name, state, expected):
    displays[0].pointer.mask = state
    injector.press_key(name)
    assert injector.keys == expected
    displays[0].pointer.mask = 0
    injector.release_key(name)
    (keycode,) = [k for k, _ in expected if k != SHIFT]
    assert injector.keys[-1] == (keycode, False)    # the keycode that was pressed


def test_press_key_keeps_a_held_shift_and_releases_the_pressed_keycode(injector, displays):
    injector.press_key("Shift_L")
    injector.press_key("KP_1")
    assert injector.keys == [(SHIFT, True), (87, True)]
    injector.release_key("KP_1")
    assert injector.keys[-1] == (87, False)


def test_borrow_is_mapped_again_after_another_client_reloads_the_keymap(injector, displays, keymap):
    injector.type_char("\u0436")                    # borrowed on spare keycode 8
    assert injector.keys == taps(8)
    keymap.current = make_kmap(FAKE_ROWS | {8: ["b"]})      # another client took keycode 8
    displays[0].events.append(ev(X.MappingNotify, request=X.MappingKeyboard))
    injector.keys.clear()
    injector.type_char("\u0436")
    assert injector.keys == taps(11)                # mapped afresh on the next spare keycode
    assert displays[0].keymap_changes[-1][0] == 11
    injector.release_all()
    assert [kc for kc, rows in displays[0].keymap_changes if rows[0][0] == 0] == [11]


def test_borrowed_keycodes_are_restored_after_a_pause(injector, displays, monkeypatch):
    order = []
    monkeypatch.setattr(xinput_mod.time, "sleep", lambda s: order.append(("sleep", s)))
    displays[0].change_keyboard_mapping = lambda keycode, rows: order.append(("map", keycode, rows[0][0]))
    injector.type_char("\u0436")
    injector.release_all()
    assert order == [("map", 8, 0x01000436), ("sleep", xinput_mod.BORROW_RESTORE_DELAY_S), ("map", 8, 0)]
    assert xinput_mod.BORROW_RESTORE_DELAY_S >= 0.1
    order.clear()
    injector.release_all()                          # nothing borrowed: no pause
    assert order == []


def test_every_synthetic_press_is_noted(injector):
    injector.type_char("A")
    assert injector.synthetic.count(SHIFT) == 1 and injector.synthetic.count(38) == 1
    assert not injector.synthetic.is_held(38)


# ---------------------------------------------------------------- XGrabHotkeys

COMBO = "ctrl+shift+F1"
F1, F2, MASK = 67, 68, X.ControlMask | X.ShiftMask


class GrabLog:
    def __init__(self):
        self.refuse: set[tuple[int, int]] = set()
        self.grabs = []
        self.ungrabs = []

    def grab(self, d, keycode, mask, kmap):
        self.grabs.append((keycode, mask))
        return (keycode, mask) not in self.refuse

    def ungrab(self, d, keycode, mask, kmap):
        self.ungrabs.append((keycode, mask, kmap.numlock_mask))


@pytest.fixture
def grabs(monkeypatch):
    log = GrabLog()
    monkeypatch.setattr(x11, "grab_key", log.grab)
    monkeypatch.setattr(x11, "ungrab_key", log.ungrab)
    return log


@pytest.fixture
def grabber(displays, keymap, grabs):
    g = XGrabHotkeys()
    yield g
    g.close()


def handle(g, event):
    fire = []
    with g._lock:
        g._handle(event, fire)
    for callback in fire:
        callback()


def press(keycode, state, t):
    return ev(X.KeyPress, detail=keycode, state=state, time=t)


def test_grabbed_combo_fires_whatever_the_lock_state(grabber, grabs):
    hits = []
    assert grabber.grab(COMBO, lambda: hits.append(1))
    assert grabs.grabs == [(F1, MASK)]
    handle(grabber, press(F1, MASK, 10))
    handle(grabber, press(F1, MASK | X.LockMask | NUM_LOCK, 20))
    handle(grabber, press(F1, MASK | (1 << 13), 30))        # XKB group 2
    handle(grabber, press(F1, X.ControlMask, 40))            # Shift missing
    handle(grabber, press(F2, MASK, 50))
    assert hits == [1, 1, 1]


def test_autorepeat_is_ignored(grabber):
    hits = []
    grabber.grab(COMBO, lambda: hits.append(1))
    handle(grabber, press(F1, MASK, 100))
    handle(grabber, ev(X.KeyRelease, detail=F1, state=MASK, time=130))
    handle(grabber, press(F1, MASK, 130))                    # autorepeat: same time
    handle(grabber, ev(X.KeyRelease, detail=F1, state=MASK, time=160))
    handle(grabber, press(F1, MASK, 400))                    # a new press
    assert hits == [1, 1]


def test_duplicate_and_refused_combos(grabber, grabs):
    assert grabber.grab(COMBO, lambda: None)
    assert not grabber.grab("shift+ctrl+F1", lambda: None)   # the same keys
    grabs.refuse.add((F2, MASK))
    assert not grabber.grab("ctrl+shift+F2", lambda: None)
    assert not grabber.grab("ctrl+alt+F2", lambda: None)     # reserved, never asked
    assert grabs.grabs == [(F1, MASK), (F2, MASK)]


def test_resume_reports_combos_refused_after_suspension(grabber, grabs):
    grabber.grab(COMBO, lambda: None)
    grabber.suspend()
    assert grabs.ungrabs == [(F1, MASK, NUM_LOCK)]
    grabber.ungrab_all()
    assert grabber.grab("ctrl+shift+F2", lambda: None)       # recorded, not yet asked
    assert grabs.grabs == [(F1, MASK)]
    grabs.refuse.add((F2, MASK))
    assert grabber.resume() == ["ctrl+shift+F2"]
    assert grabs.grabs[-1] == (F2, MASK)
    hits = []
    grabber.grab(COMBO, lambda: hits.append(1))
    handle(grabber, press(F2, MASK, 10))
    handle(grabber, press(F1, MASK, 20))
    assert hits == [1]


def test_suspended_grabber_fires_nothing(grabber):
    hits = []
    grabber.grab(COMBO, lambda: hits.append(1))
    grabber.suspend()
    handle(grabber, press(F1, MASK, 10))
    assert hits == []
    assert grabber.resume() == []
    handle(grabber, press(F1, MASK, 20))
    assert hits == [1]


MAPPING = ev(X.MappingNotify, request=X.MappingKeyboard)


def test_keymap_change_that_moves_nothing_keeps_the_grabs(grabber, grabs, keymap):
    grabber.grab(COMBO, lambda: None)
    keymap.current = make_kmap(FAKE_ROWS | {8: ["Cyrillic_zhe"]})    # a borrowed keycode
    handle(grabber, MAPPING)
    assert grabs.grabs == [(F1, MASK)] and grabs.ungrabs == []


def test_keymap_change_that_moves_the_key_regrabs(grabber, grabs, keymap):
    hits = []
    grabber.grab(COMBO, lambda: hits.append(1))
    rows = dict(FAKE_ROWS)
    rows[F1], rows[F2] = [], ["F1"]
    keymap.current = make_kmap(rows)
    handle(grabber, MAPPING)
    assert grabs.ungrabs == [(F1, MASK, NUM_LOCK)]
    assert grabs.grabs[-1] == (F2, MASK)
    handle(grabber, press(F2, MASK, 10))
    handle(grabber, press(F1, MASK, 20))
    assert hits == [1]


def test_num_lock_moving_regrabs_with_the_old_variants_released(grabber, grabs, keymap):
    grabber.grab(COMBO, lambda: None)
    modifiers = [list(r) for r in MODIFIERS]
    modifiers[4], modifiers[5] = [], [77]                    # Num Lock: Mod2 -> Mod3
    rows = []
    for keycode in range(8, 256):
        names = FAKE_ROWS.get(keycode, [])
        rows.append([ks(n) for n in names] + [0] * (6 - len(names)))
    keymap.current = x11.KeyboardMap(8, rows, modifiers)
    handle(grabber, MAPPING)
    assert grabs.ungrabs == [(F1, MASK, NUM_LOCK)]           # released in the old variants
    assert grabs.grabs[-1] == (F1, MASK)


# ---------------------------------------------------------------- capture loop

@pytest.fixture
def capture(displays, keymap, monkeypatch):
    monkeypatch.setattr(x11, "pixel_colour", lambda d, x, y: "#%02x%02x00" % (x, y))
    d = FakeDisplay()
    displays.append(d)
    return d


def run_capture(d, timeout_s=1.0, cancel=lambda: False):
    return _capture_loop(d, d.root, timeout_s, cancel)


def test_click_captures_on_press_and_ends_on_its_release(capture):
    capture.events.extend([
        ev(X.ButtonPress, detail=1, root_x=10, root_y=20),
        ev(X.ButtonPress, detail=3, root_x=99, root_y=99),   # ignored while 1 is held
        ev(X.ButtonRelease, detail=3),
        ev(X.ButtonRelease, detail=1),
    ])
    assert run_capture(capture) == (10, 20, "#0a1400")
    assert not capture.events


def test_space_captures_the_pointer_without_a_click(capture):
    capture.pointer.root_x, capture.pointer.root_y = 5, 6
    capture.events.append(ev(X.KeyPress, detail=65, state=0))
    assert run_capture(capture) == (5, 6, "#050600")


def test_escape_cancels_even_after_a_press(capture):
    capture.events.extend([ev(X.ButtonPress, detail=1, root_x=1, root_y=2),
                           ev(X.KeyPress, detail=9, state=0)])
    assert run_capture(capture) is None


def test_space_while_a_button_is_held_does_nothing(capture):
    capture.events.extend([ev(X.ButtonPress, detail=1, root_x=1, root_y=2),
                           ev(X.KeyPress, detail=65, state=0),
                           ev(X.ButtonRelease, detail=1)])
    assert run_capture(capture) == (1, 2, "#010200")


def test_wheel_notches_do_not_capture(capture):
    capture.events.extend([
        ev(X.ButtonPress, detail=4, root_x=50, root_y=60),
        ev(X.ButtonRelease, detail=4),
        ev(X.ButtonPress, detail=7, root_x=51, root_y=61),
        ev(X.ButtonRelease, detail=7),
        ev(X.ButtonPress, detail=1, root_x=10, root_y=20),
        ev(X.ButtonRelease, detail=1),
    ])
    assert run_capture(capture) == (10, 20, "#0a1400")


def test_cancel_check_and_timeout(capture):
    capture.events.append(ev(X.ButtonPress, detail=1, root_x=1, root_y=2))
    assert run_capture(capture, cancel=lambda: True) is None
    assert run_capture(capture, timeout_s=0.0) is None
    assert run_capture(capture, timeout_s=0.1) == (1, 2, "#010200")   # pressed, never released


# ---------------------------------------------------------------- window manager connection

def test_call_reuses_the_connection(displays):
    wm = X11WindowManager()
    assert wm._call(lambda d: d, None) is wm._call(lambda d: d, None)
    assert len(displays) == 1


@pytest.mark.parametrize("error", [_FakeXError(), ConnectionClosedError("display"),
                                   OSError("broken pipe")], ids=["xerror", "closed", "oserror"])
def test_call_drops_the_connection_on_failure_and_reconnects(displays, error):
    wm = X11WindowManager()

    def fail(d):
        raise error
    assert wm._call(lambda d: 1, None) == 1
    assert wm._call(fail, "default") == "default"
    assert displays[0].closed
    assert wm._call(lambda d: d, None) is displays[1]


def test_call_returns_default_when_the_display_cannot_open(monkeypatch):
    def refuse(name=None):
        raise OSError("no display")
    monkeypatch.setattr(x11, "open_display", refuse)
    assert X11WindowManager().list() == []


def test_list_and_find_leave_out_own_windows(displays, monkeypatch):
    rect = Rect(0, 0, 10, 10)
    own_window = WindowInfo(1, "xed-demo - Macro Recorder", rect, rect)
    overlay = WindowInfo(2, "overlay", rect, rect)
    xed = WindowInfo(3, "notes.txt - xed", rect, rect)
    monkeypatch.setattr(x11, "list_windows", lambda d: [own_window, overlay, xed])
    own = OwnWindows()
    own.add(1)
    own.add(2, overlay=True)
    wm = X11WindowManager(own_windows=own)
    assert wm.list() == [xed]
    assert wm.find("xed") == xed
    own.add(3)
    assert wm.find("xed") is None
    assert X11WindowManager().find("xed") == own_window


def test_unexpected_exceptions_propagate(displays):
    wm = X11WindowManager()

    def bug(d):
        raise KeyError("bug")
    with pytest.raises(KeyError):
        wm._call(bug, None)


# ---------------------------------------------------------------- activate, move, maximise

class FakeDesktop:
    """Window state behind x11's request helpers, changing as a window manager would."""

    def __init__(self, monkeypatch, lag=1):
        self.lag = lag
        self.active = 1
        self.windows = {1: self._info(1, Rect(0, 0, 100, 100)),
                        2: self._info(2, Rect(50, 50, 300, 200), desktop=1)}
        self.pending = []
        self.sent = []
        monkeypatch.setattr(x11, "create_time_window", lambda d: "time-window")
        monkeypatch.setattr(x11, "server_time", lambda d, w: 1234)
        monkeypatch.setattr(x11, "activate", self._activate)
        monkeypatch.setattr(x11, "move_resize", self._move)
        monkeypatch.setattr(x11, "set_maximised", self._maximise)
        monkeypatch.setattr(x11, "active_window_id", lambda d: self._tick() or self.active)
        monkeypatch.setattr(x11, "read_window", self._read)
        self.transient_for: dict[int, int] = {}         # dialog -> parent
        monkeypatch.setattr(x11, "transient_reaches", self._reaches)

    def _reaches(self, d, xid, target):
        while xid in self.transient_for:
            xid = self.transient_for[xid]
            if xid == target:
                return True
        return False

    @staticmethod
    def _info(xid, rect, desktop=0, maximised=False):
        return WindowInfo(xid, "w%d" % xid, rect, rect, desktop, maximised)

    def _later(self, change):
        self.pending.append([self.lag, change])

    def _tick(self):
        for item in list(self.pending):
            item[0] -= 1
            if item[0] < 0:
                self.pending.remove(item)
                item[1]()

    def _read(self, d, xid, require_title=True, require_viewable=False):
        self._tick()
        return self.windows.get(xid)

    def _activate(self, d, xid, desktop, timestamp):
        self.sent.append(("activate", xid, desktop, timestamp))
        if xid in self.windows:
            self._later(lambda: setattr(self, "active", xid))

    def _move(self, d, xid, rect):
        self.sent.append(("move", xid, rect))
        w = self.windows[xid]
        self._later(lambda: self.windows.__setitem__(xid, self._info(xid, rect, w.desktop)))

    def _maximise(self, d, xid, maximised):
        self.sent.append(("maximise", xid, maximised))
        w = self.windows[xid]
        self._later(lambda: self.windows.__setitem__(
            xid, self._info(xid, w.rect, w.desktop, maximised)))
        return True



@pytest.fixture
def desk(displays, monkeypatch):
    return FakeDesktop(monkeypatch)


@pytest.fixture
def wm(displays):
    return X11WindowManager(clock=FakeClock())


def test_activate_waits_for_the_window_to_become_active(desk, wm):
    info = wm.activate(desk.windows[2], settle_ms=1000)
    assert info == desk.windows[2] and desk.active == 2
    assert desk.sent == [("activate", 2, 1, 1234)]


def test_activate_gives_up_when_the_window_manager_refuses(desk, wm, monkeypatch):
    monkeypatch.setattr(x11, "activate", lambda d, xid, desktop, t: None)
    assert wm.activate(desk.windows[2], settle_ms=300) is None
    assert 0.3 <= wm._clock.t < 0.4


def test_activate_succeeds_when_the_window_manager_focuses_its_modal_dialog(desk, wm, monkeypatch):
    desk.windows[3] = desk._info(3, Rect(80, 80, 200, 100), desktop=1)
    desk.transient_for[3] = 2
    monkeypatch.setattr(x11, "activate", lambda d, xid, desktop, t: desk._later(
        lambda: setattr(desk, "active", 3)))
    info = wm.activate(desk.windows[2], settle_ms=1000)
    assert info == desk.windows[2] and desk.active == 3


def test_activate_of_a_vanished_window_returns_none(desk, wm):
    gone = desk.windows.pop(2)
    assert wm.activate(gone, settle_ms=300) is None


def test_move_settles_on_the_new_rect(desk, wm):
    target = Rect(10, 20, 400, 300)
    info = wm.move(desk.windows[2], target, settle_ms=1000)
    assert info.rect == target
    assert wm._clock.t < 1.0


def test_move_unmaximises_first(desk, wm):
    desk.windows[2] = desk._info(2, Rect(0, 0, 1920, 1050), 1, maximised=True)
    target = Rect(10, 20, 400, 300)
    info = wm.move(desk.windows[2], target, settle_ms=1000)
    assert [s[0] for s in desk.sent] == ["maximise", "move"]
    assert info.rect == target and not info.maximised


def test_set_maximised_settles(desk, wm):
    info = wm.set_maximised(desk.windows[1], True, settle_ms=1000)
    assert info.maximised


def test_set_maximised_without_ewmh_returns_the_current_state(desk, wm, monkeypatch):
    monkeypatch.setattr(x11, "set_maximised", lambda d, xid, maximised: False)
    assert wm.set_maximised(desk.windows[1], True) == desk.windows[1]


# ---------------------------------------------------------------- window_at

class FakeWindow:
    def __init__(self, xid, children=(), hit=None):
        self.id = xid
        self.children = list(children)
        self.hit = hit                  # the child under the test point, or None

    def translate_coords(self, src, x, y):
        return SimpleNamespace(child=self.hit or X.NONE)

    def query_tree(self):
        return SimpleNamespace(children=self.children)


def tree_display(monkeypatch, root, clients):
    windows = {}

    def collect(w):
        windows[w.id] = w
        for c in w.children:
            collect(c)
    collect(root)
    monkeypatch.setattr(x11, "window", lambda d, xid: windows[xid])
    monkeypatch.setattr(x11, "_has_wm_state", lambda d, w: w.id in clients)
    return SimpleNamespace(screen=lambda: SimpleNamespace(root=root))


def test_window_at_returns_the_client_inside_a_frame(monkeypatch):
    client = FakeWindow(0x300)
    frame = FakeWindow(0x200, [FakeWindow(0x210), client], hit=client)
    root = FakeWindow(1, [frame], hit=frame)
    assert x11.window_at(tree_display(monkeypatch, root, {0x300}), 5, 5) == 0x300


def test_window_at_over_the_title_bar_still_finds_the_client(monkeypatch):
    client = FakeWindow(0x300)
    wrapper = FakeWindow(0x220, [client])
    frame = FakeWindow(0x200, [FakeWindow(0x210), wrapper], hit=None)
    root = FakeWindow(1, [frame], hit=frame)
    assert x11.window_at(tree_display(monkeypatch, root, {0x300}), 5, 5) == 0x300


def test_window_at_an_override_redirect_window_returns_it(monkeypatch):
    overlay = FakeWindow(0x400, [FakeWindow(0x410)])
    root = FakeWindow(1, [overlay], hit=overlay)
    assert x11.window_at(tree_display(monkeypatch, root, set()), 5, 5) == 0x400


def test_window_at_the_bare_root_is_none(monkeypatch):
    root = FakeWindow(1)
    assert x11.window_at(tree_display(monkeypatch, root, set()), 5, 5) is None


# ---------------------------------------------------------------- active-window watcher

def test_watcher_reports_changes_only_and_survives_a_failing_callback(displays, monkeypatch):
    ids = deque([1, 1, 2, 2, 3])
    monkeypatch.setattr(x11, "active_window_id", lambda d: ids[0] if len(ids) == 1
                        else ids.popleft())
    monkeypatch.setattr(x11, "read_window",
                        lambda d, xid, **kw: WindowInfo(xid, "w", Rect(0, 0, 1, 1),
                                                        Rect(0, 0, 1, 1)))
    stop = threading.Event()
    ready = threading.Event()
    seen = []

    def callback(info):
        seen.append(info.id)
        if info.id == 2:
            raise RuntimeError("recorder bug")
        if info.id == 3:
            stop.set()

    root = SimpleNamespace(change_attributes=lambda **kw: None)
    fake = FakeDisplay()
    fake.root = root
    monkeypatch.setattr(x11, "open_display", lambda name=None: fake)
    x11.watch_active_window(callback, stop, poll_interval=0.0, ready=ready)
    assert ready.is_set()
    assert seen == [2, 3]
    assert fake.closed


# ---------------------------------------------------------------- XRecord replies

def key_press_bytes(detail, state, root_x, root_y, t):
    return struct.pack("=BBHIIIIhhhhHBx", X.KeyPress, detail, 1, t, 0x100, 0x100, 0,
                       root_x, root_y, root_x, root_y, state, 1)


def record_context(events):
    from Xlib.ext import record

    ctx = x11.RecordContext(X.KeyPress, X.MotionNotify, events.append)
    ctx._data = SimpleNamespace(display=SimpleNamespace(
        event_classes=dict(xevent.event_class), get_resource_class=lambda name: None))
    return ctx, record


def test_record_reply_is_parsed_into_core_events():
    events = []
    ctx, record = record_context(events)
    ctx._on_reply(SimpleNamespace(category=record.StartOfData, client_swapped=False, data=b""))
    assert ctx._live.is_set()
    data = key_press_bytes(38, X.ShiftMask, 120, 340, 5000) + key_press_bytes(9, 0, 1, 2, 5001)
    ctx._on_reply(SimpleNamespace(category=record.FromServer, client_swapped=False, data=data))
    assert [(e.type, e.detail, e.state, e.root_x, e.root_y, e.time) for e in events] == [
        (X.KeyPress, 38, X.ShiftMask, 120, 340, 5000), (X.KeyPress, 9, 0, 1, 2, 5001)]


def test_record_reply_from_a_swapped_client_or_the_client_side_is_skipped():
    events = []
    ctx, record = record_context(events)
    data = key_press_bytes(38, 0, 1, 1, 1)
    ctx._on_reply(SimpleNamespace(category=record.FromServer, client_swapped=True, data=data))
    ctx._on_reply(SimpleNamespace(category=record.FromClient, client_swapped=False, data=data))
    assert events == []


def test_record_handler_errors_do_not_stop_the_reply():
    seen = []

    def handler(event):
        seen.append(event.detail)
        raise RuntimeError("listener bug")
    ctx, record = record_context([])
    ctx._handler = handler
    data = key_press_bytes(38, 0, 1, 1, 1) + key_press_bytes(39, 0, 1, 1, 2)
    ctx._on_reply(SimpleNamespace(category=record.FromServer, client_swapped=False, data=data))
    assert seen == [38, 39]


def test_listener_translates_record_events(displays, keymap):
    """XRecordListener._on_event names keys from the keymap and converts times."""
    listener = xinput_mod.XRecordListener()
    got = []
    listener._callback = got.append
    listener._context = SimpleNamespace(control=FakeDisplay(), control_lock=threading.Lock())
    displays.append(listener._context.control)
    listener._on_event(ev(X.KeyPress, detail=38, state=X.ShiftMask, root_x=3, root_y=4,
                          time=1000))
    listener._on_event(ev(X.ButtonPress, detail=5, state=0, root_x=3, root_y=4, time=1010))
    assert [(r.kind, r.key, r.dy) for r in got] == [("key_press", "A", 0), ("scroll", "", 1)]
    assert 0 <= got[1].time - got[0].time <= 0.010 + 1e-9     # server times, never later
