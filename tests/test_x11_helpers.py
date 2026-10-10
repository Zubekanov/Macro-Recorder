"""Pure helpers of the platform layer: no X server is contacted."""

import struct
import sys

import pytest
from Xlib import X

from macro_recorder.model.keys import CHAR_KEYSYMS, NAMED_KEYS
from macro_recorder.model.protocols import Monitor, Rect, WindowInfo
from macro_recorder.platform import x11
from macro_recorder.platform.input import (
    CoreEventTranslator, SyntheticKeys, XI2StopKeyWatcher, combo_problem,
)
from macro_recorder.platform.windows import (
    NullWindowManager, create_window_manager, monitor_work_areas, select_window, settle,
    title_matches,
)
from tests.conftest import FakeClock


def ks(name):
    return x11.keysym_from_name(name)


# A small US-like keymap.  Keycode 39 has a second group; 200 is spare.
ROWS = {
    9: ["Escape"],
    10: ["1", "exclam", "1", "exclam", "onesuperior", "exclamdown"],
    24: ["q", "Q", "q", "Q", "at", "Greek_OMEGA"],
    38: ["a", "A", "a", "A", "ae", "AE"],
    39: ["s", "S", "Cyrillic_yeru", "Cyrillic_YERU", "ssharp", "section"],
    40: ["d"],
    37: ["Control_L"],
    50: ["Shift_L"],
    64: ["Alt_L", "Meta_L"],
    66: ["Caps_Lock"],
    67: ["F1"],
    77: ["Num_Lock"],
    87: ["KP_End", "KP_1"],
    108: ["ISO_Level3_Shift"],
    133: ["Super_L"],
}
MODIFIERS = [[50], [66], [37], [64], [77], [], [133], [108]]


@pytest.fixture
def kmap():
    width = 6
    rows = []
    for keycode in range(8, 256):
        names = ROWS.get(keycode, [])
        rows.append([ks(n) for n in names] + [0] * (width - len(names)))
    return x11.KeyboardMap(8, rows, MODIFIERS)


# ---------------------------------------------------------------- geometry

def test_outer_rect_grows_by_frame_extents():
    # client at (100, 80) 400x300, frame: left 2, right 2, top 30, bottom 2
    assert x11.outer_rect(100, 80, 400, 300, [2, 2, 30, 2]) == (98, 50, 404, 332)


def test_outer_rect_without_extents_is_identity():
    assert x11.outer_rect(5, 6, 7, 8, None) == (5, 6, 7, 8)
    assert x11.outer_rect(5, 6, 7, 8, [1, 2]) == (5, 6, 7, 8)


def test_client_size_inverts_outer_rect():
    ext = [3, 3, 25, 3]
    _, _, ow, oh = x11.outer_rect(0, 0, 640, 480, ext)
    assert x11.client_size(ow, oh, ext) == (640, 480)
    assert x11.client_size(2, 2, ext) == (1, 1)     # never below 1x1


def test_intersect():
    assert x11.intersect(Rect(0, 0, 100, 100), Rect(50, 20, 100, 100)) == Rect(50, 20, 50, 80)
    assert x11.intersect(Rect(0, 0, 10, 10), Rect(10, 0, 5, 5)) is None


def test_monitor_work_areas_clip_the_workarea_per_monitor():
    rects = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1280, 1024)]
    mons = monitor_work_areas(rects, Rect(0, 0, 3200, 1050))
    assert mons == [Monitor(1, rects[0], Rect(0, 0, 1920, 1050)),
                    Monitor(2, rects[1], Rect(1920, 0, 1280, 1024))]
    assert monitor_work_areas(rects[:1], None) == [Monitor(1, rects[0], rects[0])]


# ---------------------------------------------------------------- keysym names

@pytest.mark.parametrize("name", NAMED_KEYS)
def test_every_named_key_round_trips(name):
    keysym = ks(name)
    assert keysym, name
    assert x11.keysym_name(keysym) == name


def test_printable_ascii_names_follow_model_keys():
    for code in range(0x20, 0x7F):
        char = chr(code)
        expected = CHAR_KEYSYMS.get(char, char)
        assert x11.keysym_name(code) == expected
        assert ks(expected) == code
        assert x11.keysym_for_char(char) == code


def test_aliases_resolve_to_the_preferred_name():
    assert x11.keysym_name(ks("Prior")) == "Page_Up"
    assert x11.keysym_name(ks("KP_Prior")) == "KP_Page_Up"
    assert x11.keysym_name(ks("script_switch")) == "Mode_switch"


def test_xf86_and_unicode_and_raw_names():
    assert ks("XF86AudioMute") == 0x1008FF12 == ks("XF86_AudioMute")
    assert x11.keysym_name(0x1008FF12) == "XF86AudioMute"
    assert ks("U20AC") == 0x010020AC
    assert x11.keysym_name(0x010020AC) == "U20AC"
    assert ks("0x1234abcd") == 0x1234ABCD
    assert ks("no such key") == 0 and ks("") == 0 and ks("Uzzzz") == 0


def test_characters_map_to_latin1_or_unicode_keysyms():
    assert x11.keysym_for_char("\u00e9") == 0xE9
    assert x11.keysym_name(0xE9) == "eacute"
    assert x11.keysym_for_char("\u20ac") == 0x010020AC
    assert x11.keysym_for_char("\n") == ks("Return")
    assert ks("\u00e9") == 0xE9


def test_keysym_case():
    assert x11.keysym_case(ks("a")) == (ks("a"), ks("A"))
    assert x11.keysym_case(ks("A")) == (ks("a"), ks("A"))
    assert x11.keysym_case(0xE9) == (0xE9, 0xC9)
    assert x11.keysym_case(ks("1")) is None
    assert x11.keysym_case(ks("Return")) is None


def test_scroll_buttons():
    assert x11.scroll_button(0, 1) == 5 and x11.scroll_button(0, -1) == 4
    assert x11.scroll_button(1, 0) == 7 and x11.scroll_button(-1, 0) == 6
    with pytest.raises(ValueError):
        x11.scroll_button(0, 0)


# ---------------------------------------------------------------- keyboard map

def test_modifier_masks_come_from_the_modifier_mapping(kmap):
    assert kmap.family_masks["Shift"] == X.ShiftMask
    assert kmap.family_masks["Control"] == X.ControlMask
    assert kmap.family_masks["Alt"] == X.Mod1Mask
    assert kmap.family_masks["Meta"] == X.Mod1Mask
    assert kmap.numlock_mask == X.Mod2Mask
    assert kmap.family_masks["Super"] == X.Mod4Mask
    assert kmap.level3_mask == X.Mod5Mask


def test_combo_mask(kmap):
    assert x11.combo_mask(("Control_L", "Shift_L"), kmap) == X.ControlMask | X.ShiftMask
    assert x11.combo_mask(("Alt_R", "Super_L"), kmap) == X.Mod1Mask | X.Mod4Mask
    assert x11.combo_mask((), kmap) == 0
    with pytest.raises(ValueError, match="not a modifier"):
        x11.combo_mask(("a",), kmap)
    with pytest.raises(ValueError, match="not bound"):
        x11.combo_mask(("Hyper_L",), kmap)


def test_lock_variants_and_clean_state(kmap):
    assert x11.lock_variants(kmap) == [0, X.LockMask, X.Mod2Mask, X.Mod2Mask | X.LockMask]
    state = X.ControlMask | X.LockMask | X.Mod2Mask | X.Button1Mask | (1 << 13)
    assert x11.clean_state(state, kmap) == X.ControlMask


def test_lock_variants_without_num_lock():
    km = x11.KeyboardMap(8, [[0, 0]] * 4, [[], [], [], [], [], [], [], []])
    assert x11.lock_variants(km) == [0, X.LockMask]
    assert km.family_masks["Alt"] == X.Mod1Mask     # falls back to the usual bits


@pytest.mark.parametrize("keycode, state, expected", [
    (38, 0, "a"),
    (38, X.ShiftMask, "A"),
    (38, X.LockMask, "A"),
    (38, X.LockMask | X.ShiftMask, "a"),
    (38, X.Mod5Mask, "ae"),
    (38, X.Mod5Mask | X.ShiftMask, "AE"),
    (10, 0, "1"),
    (10, X.ShiftMask, "exclam"),
    (10, X.LockMask, "1"),                    # Caps Lock leaves digits alone
    (39, 1 << 13, "Cyrillic_yeru"),           # second group
    (39, (1 << 13) | X.ShiftMask, "Cyrillic_YERU"),
    (40, X.ShiftMask, "D"),                   # missing level 2 of a letter is its upper case
    (9, X.ShiftMask, "Escape"),
    (40, 1 << 13, "d"),                       # no second group: falls back to group 1
    (87, 0, "KP_End"),
    (87, X.Mod2Mask, "KP_1"),
    (87, X.Mod2Mask | X.ShiftMask, "KP_End"),
    (67, X.ControlMask | X.Mod1Mask, "F1"),
])
def test_keysym_for_event_state(kmap, keycode, state, expected):
    assert kmap.name_at(keycode, state) == expected


def test_unmapped_keycode_has_a_placeholder_name(kmap):
    assert kmap.name_at(200, 0) == "keycode_200"


def test_find_prefers_plain_then_shift_then_level3(kmap):
    assert kmap.find(ks("a")) == (38, 0)
    assert kmap.find(ks("A")) == (38, 1)
    assert kmap.find(ks("ae")) == (38, 4)
    assert kmap.find(ks("AE")) == (38, 5)
    assert kmap.find(ks("D")) == (40, 1)
    assert kmap.find(ks("Cyrillic_yeru")) is None     # second group is not typeable
    assert kmap.find(ks("eacute")) is None
    assert kmap.keycode_for(ks("Escape")) == 9


def test_spare_keycodes(kmap):
    spare = kmap.spare_keycodes()
    assert 200 in spare and 38 not in spare
    assert spare[0] == 8


class FakeKeymapDisplay:
    def __init__(self):
        self.changes = []

    def change_keyboard_mapping(self, keycode, rows):
        self.changes.append((keycode, [list(r) for r in rows]))

    def sync(self):
        pass


def test_borrower_uses_spare_keycodes_and_restores_them(kmap):
    d = FakeKeymapDisplay()
    borrower = x11.KeycodeBorrower(d, kmap)
    euro = ks("U20AC")
    first = borrower.borrow(euro)
    assert first == 8
    assert kmap.find(euro) == (8, 0)
    assert borrower.borrow(euro) == 8                   # reused, no new change
    assert len(d.changes) == 1
    second = borrower.borrow(0xE9)
    assert second == 11 and borrower.borrowed == {euro: 8, 0xE9: 11}
    borrower.restore_all()
    assert kmap.find(euro) is None and not any(kmap.syms(8))
    assert d.changes[-1] == (11, [[0] * 6]) and borrower.borrowed == {}


def test_borrower_overwrites_the_least_recent_free_borrow_when_full():
    km = x11.KeyboardMap(8, [[0], [0], [ks("a")]], [[]] * 8)
    borrower = x11.KeycodeBorrower(FakeKeymapDisplay(), km)
    assert borrower.borrow(0xE9) == 8
    assert borrower.borrow(0xE8) == 9
    assert borrower.borrow(0xE7, busy=[8]) == 9          # 8 is held, so 9 is overwritten
    assert borrower.borrowed == {0xE9: 8, 0xE7: 9}
    assert borrower.borrow(0xE6, busy=[8, 9]) is None


# ---------------------------------------------------------------- event translation

def test_translator_keeps_the_press_name_for_the_release(kmap):
    tr = CoreEventTranslator(kmap)
    press = tr.translate(X.KeyPress, 38, X.ShiftMask, 10, 20, 1.0)
    release = tr.translate(X.KeyRelease, 38, 0, 10, 20, 1.1)
    assert (press.kind, press.key, press.x, press.y) == ("key_press", "A", 10, 20)
    assert (release.kind, release.key) == ("key_release", "A")
    assert tr.translate(X.KeyRelease, 38, 0, 0, 0, 1.2).key == "a"


def test_translator_buttons_scroll_and_motion(kmap):
    tr = CoreEventTranslator(kmap)
    for detail, name in ((1, "left"), (2, "middle"), (3, "right"), (8, "x1"), (9, "x2")):
        assert tr.translate(X.ButtonPress, detail, 0, 5, 6, 0.0).button == name
        assert tr.translate(X.ButtonRelease, detail, 0, 5, 6, 0.0).kind == "button_release"
    down = tr.translate(X.ButtonPress, 5, 0, 5, 6, 0.0)
    assert (down.kind, down.dx, down.dy) == ("scroll", 0, 1)
    assert tr.translate(X.ButtonPress, 6, 0, 0, 0, 0.0).dx == -1
    assert tr.translate(X.ButtonRelease, 5, 0, 5, 6, 0.0) is None
    assert tr.translate(X.ButtonPress, 12, 0, 5, 6, 0.0) is None
    motion = tr.translate(X.MotionNotify, 0, 0, 300, 400, 2.5)
    assert (motion.kind, motion.x, motion.y, motion.time) == ("motion", 300, 400, 2.5)


def test_server_clock_never_reports_events_late():
    clock = x11.ServerClock()
    assert clock.to_monotonic(1000, now=50.0) == pytest.approx(50.0)
    # Delivered 30 ms after it happened: the first offset already explains it.
    assert clock.to_monotonic(1100, now=50.13) == pytest.approx(50.1)
    # A faster delivery tightens the offset.
    assert clock.to_monotonic(1200, now=50.19) == pytest.approx(50.19)
    assert clock.to_monotonic(1100, now=50.2) == pytest.approx(50.09)


def test_server_clock_resets_on_wrap():
    clock = x11.ServerClock()
    clock.to_monotonic(0xFFFFFF00, now=10.0)
    assert clock.to_monotonic(100, now=10.5) == pytest.approx(10.5)


# ---------------------------------------------------------------- XInput2 raw events

class _Event:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _raw_event(opcode, detail, sourceid, evtype=x11.XI_RAW_KEY_PRESS):
    order = "<" if sys.byteorder == "little" else ">"
    body = struct.pack(order + "HIIHHI4x", 3, 123456, detail, sourceid, 0, 0)
    data, rest = x11.RAW_KEY_EVENT.parse_binary(body + b"\0" * 8, None)
    assert rest == b"\0" * 8
    return _Event(type=x11.GENERIC_EVENT, extension=opcode, evtype=evtype, data=data)


def test_raw_key_struct_decodes_the_documented_layout():
    ev = _raw_event(131, 9, 7)
    assert (ev.data.deviceid, ev.data.time, ev.data.detail, ev.data.sourceid) == (3, 123456, 9, 7)


def test_raw_key_filter_ignores_xtest_and_other_keys():
    xtest = {6}
    assert x11.is_user_raw_key(_raw_event(131, 9, 10), 131, {9}, xtest)
    assert not x11.is_user_raw_key(_raw_event(131, 9, 6), 131, {9}, xtest)       # synthetic
    assert not x11.is_user_raw_key(_raw_event(131, 38, 10), 131, {9}, xtest)     # other key
    assert not x11.is_user_raw_key(_raw_event(130, 9, 10), 131, {9}, xtest)      # other extension
    assert not x11.is_user_raw_key(_raw_event(131, 9, 10, evtype=14), 131, {9}, xtest)
    assert not x11.is_user_raw_key(_Event(type=X.KeyPress), 131, {9}, xtest)


def test_record_fallback_ignores_synthetic_and_held_presses():
    synthetic = SyntheticKeys()
    watcher = XI2StopKeyWatcher(synthetic=synthetic)
    hits = []

    def press(keycode=9):
        watcher._on_record(_Event(type=X.KeyPress, detail=keycode), {9}, lambda: hits.append(1))

    synthetic.press(9)
    press()                      # the injector's own press
    assert hits == []
    press()                      # autorepeat while the injector holds it
    assert hits == []
    synthetic.release(9)
    press()                      # the user
    assert hits == [1]
    press(38)
    assert hits == [1]


def test_synthetic_keys_expire():
    clock = FakeClock()
    keys = SyntheticKeys(ttl_s=2.0, clock=clock.now)
    keys.press(9)
    keys.press(9)
    assert keys.count(9) == 2
    assert keys.consume(9)
    clock.advance(3.0)
    assert not keys.consume(9)
    assert not keys.consume(10)
    assert keys.is_held(9)
    keys.release(9)
    assert not keys.is_held(9)


# ---------------------------------------------------------------- hotkey rules

@pytest.mark.parametrize("combo", ["ctrl+alt+F1", "ctrl+alt+F12", "ctrl+alt+BackSpace",
                                   "ctrl+alt+Delete", "ctrl+shift+alt+F3", "Control_R+alt+F2"])
def test_reserved_combos_are_rejected(combo):
    assert "reserved" in combo_problem(combo)


@pytest.mark.parametrize("combo", ["ctrl+shift+F9", "ctrl+shift+F5", "alt+F1", "ctrl+F1",
                                   "ctrl+alt+F13", "ctrl+alt+s", "F9"])
def test_ordinary_combos_are_allowed(combo):
    assert combo_problem(combo) is None


def test_bad_combos_explain_themselves():
    assert combo_problem("") == "Empty key combination"
    assert "modifier" in combo_problem("ctrl+shift")
    assert "precede" in combo_problem("a+ctrl")


# ---------------------------------------------------------------- pixels

def test_pixel_to_rgb_truecolour():
    bgrx = bytes([0x30, 0x20, 0x10, 0x00])
    assert x11.pixel_to_rgb(bgrx, 32, True, 0xFF0000, 0x00FF00, 0x0000FF) == (0x10, 0x20, 0x30)
    assert x11.pixel_to_rgb(bytes([0x00, 0x10, 0x20, 0x30]), 32, False,
                            0xFF0000, 0x00FF00, 0x0000FF) == (0x10, 0x20, 0x30)
    rgb565 = (31 << 11 | 0 << 5 | 31).to_bytes(2, "little")
    assert x11.pixel_to_rgb(rgb565, 16, True, 0xF800, 0x07E0, 0x001F) == (255, 0, 255)


# ---------------------------------------------------------------- window selection and settling

def _w(id, title):
    return WindowInfo(id, title, Rect(0, 0, 10, 10), Rect(0, 0, 10, 10))


def test_title_matching_and_exact_title_wins():
    windows = [_w(1, "notes.txt - xed"), _w(2, "xed")]
    assert select_window(windows, "XED").id == 2
    assert select_window(windows, "notes").id == 1
    assert select_window(windows, r"^notes\.", "regex").id == 1
    assert select_window(windows, "firefox") is None
    assert title_matches("Mozilla Firefox", "firefox")
    assert not title_matches("Mozilla Firefox", "firefox", "regex")
    with pytest.raises(ValueError, match="Invalid window title pattern"):
        title_matches("x", "(", "regex")


def test_settle_needs_two_agreeing_accepted_reads():
    clock = FakeClock()
    reads = iter([1, 2, 2, 3, 3])
    value, ok = settle(lambda: next(reads), lambda v: v >= 2, 1000, clock)
    assert (value, ok) == (2, True)
    assert clock.sleeps == [0.05, 0.05]


def test_settle_gives_up_after_settle_ms():
    clock = FakeClock()
    counter = iter(range(1000))
    value, ok = settle(lambda: next(counter), lambda v: True, 200, clock)
    assert not ok and value >= 4 and clock.t >= 0.2 - 1e-9


def test_settle_rejects_agreeing_reads_that_are_not_accepted():
    clock = FakeClock()
    value, ok = settle(lambda: "old", lambda v: v == "new", 100, clock)
    assert (value, ok) == ("old", False)


def test_no_display_gives_the_null_window_manager(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    wm = create_window_manager()
    assert isinstance(wm, NullWindowManager)
    w = _w(1, "x")
    assert wm.list() == [] and wm.active() is None and wm.find("x") is None
    assert wm.activate(w) is None and wm.move(w, Rect(0, 0, 1, 1)) is None
    assert wm.set_maximised(w, True) is None and wm.refresh(w) is None
    assert wm.monitors() == [] and wm.screen_size() == (0, 0) and wm.window_at(1, 1) is None
    wm.watch_active(lambda info: None)()
    wm.bell()


def test_unreachable_display_gives_the_null_window_manager(monkeypatch):
    # The conftest makes any Display() raise, as an unreachable server would.
    monkeypatch.setenv("DISPLAY", ":250")
    assert isinstance(create_window_manager(), NullWindowManager)
