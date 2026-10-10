"""Global input on X11: injection, recording, the stop key, hotkeys, targeting.

- :class:`XTestInput` is the ``InputBackend``: synthetic input through XTest.
- :class:`XRecordListener` is the ``InputListener``: every client's device
  events through XRecord, as :class:`RawEvent`.
- :class:`XI2StopKeyWatcher` is the ``StopKeyWatcher``: XInput2 raw key
  presses, ignoring the XTEST keyboard, with an XRecord fallback.
- :class:`XGrabHotkeys` is the ``HotkeyGrabber``: XGrabKey on the root.
- :func:`capture_point` grabs pointer and keyboard to pick a screen point.

Each class owns its X connections; none of them is shared between threads
without a lock.
"""

from __future__ import annotations

import logging
import select
import threading
import time
from collections import deque
from typing import Any, Callable

from Xlib import X

from macro_recorder.model.keys import MODIFIER_KEYSYMS, normalise_key, parse_combo
from macro_recorder.model.protocols import RawEvent
from macro_recorder.platform import x11

log = logging.getLogger(__name__)

_VT_KEYS = frozenset({"F%d" % i for i in range(1, 13)} | {"BackSpace", "Delete", "KP_Delete"})
_FAMILY_OF = x11.MODIFIER_FAMILIES


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def combo_problem(combo: str) -> str | None:
    """Why ``combo`` cannot be a global hotkey, or None when it can.

    Ctrl+Alt with F1..F12, BackSpace or Delete belongs to the X server
    (virtual terminals, zap, reboot) and is rejected.
    """
    try:
        names = parse_combo(combo)
    except ValueError as e:
        return str(e)
    key = names[-1]
    if key in MODIFIER_KEYSYMS:
        return "A hotkey needs a key that is not a modifier"
    families = {_FAMILY_OF.get(n) for n in names[:-1]}
    if {"Control", "Alt"} <= families and key in _VT_KEYS:
        return "%s is reserved by the X server" % combo
    return None


class SyntheticKeys:
    """What the injector has pressed, for the XRecord stop-key fallback.

    Every synthetic KeyPress is noted with its time; the fallback consumes
    one note per recorded press of the same keycode, so a synthetic press is
    never taken for the user's.  Notes older than ``ttl_s`` expire and at
    most ``limit`` are kept per keycode, so a long playback holds no more
    than a few seconds of notes.  A held synthetic key also suppresses its
    autorepeat.
    """

    def __init__(self, ttl_s: float = 2.0, clock: Callable[[], float] = time.monotonic,
                 limit: int = 256) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._limit = limit
        self._pending: dict[int, deque[float]] = {}
        self._held: set[int] = set()
        self._lock = threading.Lock()

    def _expire(self, pending: deque[float]) -> None:
        cutoff = self._clock() - self._ttl
        while pending and pending[0] < cutoff:
            pending.popleft()

    def press(self, keycode: int) -> None:
        with self._lock:
            pending = self._pending.setdefault(keycode, deque(maxlen=self._limit))
            self._expire(pending)
            pending.append(self._clock())
            self._held.add(keycode)

    def release(self, keycode: int) -> None:
        with self._lock:
            self._held.discard(keycode)

    def is_held(self, keycode: int) -> bool:
        with self._lock:
            return keycode in self._held

    def consume(self, keycode: int) -> bool:
        """True (and forget it) when a press of ``keycode`` was synthetic."""
        with self._lock:
            pending = self._pending.get(keycode)
            if not pending:
                return False
            self._expire(pending)
            if not pending:
                return False
            pending.popleft()
            return True

    def count(self, keycode: int) -> int:
        with self._lock:
            pending = self._pending.get(keycode)
            if not pending:
                return 0
            self._expire(pending)
            return len(pending)


class CoreEventTranslator:
    """Core device events -> :class:`RawEvent`.

    A key release reports the name its press reported, so a modifier
    released first never changes the name between press and release.
    Buttons 4-7 become scroll notches (positive dy is down); their releases
    and unknown buttons are dropped.
    """

    def __init__(self, kmap: x11.KeyboardMap) -> None:
        self.kmap = kmap
        self._pressed: dict[int, str] = {}

    def translate(self, event_type: int, detail: int, state: int, x: int, y: int,
                  t: float) -> RawEvent | None:
        if event_type == X.KeyPress:
            name = self.kmap.name_at(detail, state)
            self._pressed[detail] = name
            return RawEvent("key_press", t, x, y, key=name)
        if event_type == X.KeyRelease:
            name = self._pressed.pop(detail, None) or self.kmap.name_at(detail, state)
            return RawEvent("key_release", t, x, y, key=name)
        if event_type == X.ButtonPress:
            if detail in x11.SCROLL_BUTTONS:
                dx, dy = x11.SCROLL_BUTTONS[detail]
                return RawEvent("scroll", t, x, y, dx=dx, dy=dy)
            if detail in x11.BUTTON_NAMES:
                return RawEvent("button_press", t, x, y, button=x11.BUTTON_NAMES[detail])
            return None
        if event_type == X.ButtonRelease:
            if detail in x11.BUTTON_NAMES:
                return RawEvent("button_release", t, x, y, button=x11.BUTTON_NAMES[detail])
            return None
        if event_type == X.MotionNotify:
            return RawEvent("motion", t, x, y)
        return None


def _drain_mapping_changes(d) -> bool:
    """Read pending events; True when the keyboard or modifier mapping changed."""
    changed = False
    while d.pending_events():
        ev = d.next_event()
        if ev.type == X.MappingNotify and ev.request in (X.MappingKeyboard, X.MappingModifier):
            changed = True
    return changed


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

# How long borrowed keycodes stay mapped after the last key event, so clients
# that fetch the keymap lazily still decode the last borrowed key.
BORROW_RESTORE_DELAY_S = 0.15


class XTestInput:
    """``InputBackend`` over XTest on its own connection.

    Keys are keysym names, looked up in the active XKB group.
    ``press_key`` presses the keycode that carries the keysym and adds no
    modifiers, except Shift for a keypad keysym or an upper-case letter that
    the live Num Lock or Caps Lock state would otherwise turn into the other
    level, held until ``release_key`` of that name or :meth:`release_all`;
    ``type_char`` adds Shift or Level 3 as the keymap and the lock
    state need.  Keysyms the active group lacks are typed on a borrowed spare
    keycode, restored by :meth:`release_all`.  Every synthetic key press is
    noted in ``synthetic`` for the stop-key fallback.
    """

    def __init__(self, display_name: str | None = None,
                 synthetic: SyntheticKeys | None = None) -> None:
        self._d = x11.open_display(display_name)
        if not x11.has_xtest(self._d):
            self._d.close()
            raise OSError("The X server has no XTEST extension")
        self._lock = threading.RLock()
        self.synthetic = synthetic or SyntheticKeys()
        self._kmap = x11.KeyboardMap.from_display(self._d)
        self._borrower = x11.KeycodeBorrower(self._d, self._kmap)
        self._held_keys: list[int] = []
        self._held_buttons: list[int] = []
        # key name -> (keycode press_key used, the Shift it added or None)
        self._pressed_names: dict[str, tuple[int, int | None]] = {}

    # -- keymap -------------------------------------------------------------

    def _refresh_keymap(self) -> None:
        if _drain_mapping_changes(self._d):
            self._kmap = x11.KeyboardMap.from_display(self._d)
            self._borrower.kmap = self._kmap
            self._borrower.forget_stale()

    def _state(self) -> int:
        """The live modifier state: lock bits and the XKB group are what matter."""
        return int(self._d.screen().root.query_pointer().mask)

    def _resolve_key(self, name: str) -> tuple[int, bool]:
        """``(keycode, add_shift)`` for a key name.  Raises ValueError.

        Shift is added only for a keypad keysym or an upper-case letter that
        needs it under the live lock state (KP_1 with Num Lock off, A with
        Caps Lock off); any other keysym is pressed bare, as recorded.
        """
        name = normalise_key(name)
        keysym = x11.keysym_from_name(name)
        if not keysym:
            raise ValueError("Unknown key %r" % name)
        with self._lock:
            self._refresh_keymap()
            found = self._kmap.find_for_state(keysym, self._state(), level3=False)
            if found is not None:
                keycode, shift, _ = found
                case = x11.keysym_case(keysym)
                wants_level = x11.is_keypad(keysym) or (case is not None and case[1] == keysym)
                return keycode, shift and wants_level
            keycode = self._borrower.borrow(keysym, busy=self._held_keys)
            if keycode is None:
                raise ValueError("No spare keycode to type %r" % name)
            return keycode, False

    def keycode_for(self, name: str) -> int:
        """Keycode for a key name, borrowing one when unmapped.  Raises ValueError."""
        return self._resolve_key(name)[0]

    def _shift_keycode(self) -> int | None:
        """The Shift_L keycode, or None when the macro already holds a Shift."""
        shift = self._kmap.keycode_for(x11.keysym_from_name("Shift_L"))
        held = {self._kmap.keycode_for(x11.keysym_from_name(n)) for n in ("Shift_L", "Shift_R")}
        if shift is None or held.intersection(self._held_keys):
            return None
        return shift

    # -- low level ----------------------------------------------------------

    def _key(self, keycode: int, press: bool) -> None:
        if press:
            self.synthetic.press(keycode)
            if keycode not in self._held_keys:
                self._held_keys.append(keycode)
        else:
            self.synthetic.release(keycode)
            if keycode in self._held_keys:
                self._held_keys.remove(keycode)
        x11.fake_key(self._d, keycode, press)
        self._d.flush()

    def _button(self, button: int, press: bool) -> None:
        if press and button not in self._held_buttons:
            self._held_buttons.append(button)
        elif not press and button in self._held_buttons:
            self._held_buttons.remove(button)
        x11.fake_button(self._d, button, press)
        self._sync()

    def _sync(self) -> None:
        """Wait until the server has processed the requests sent so far.

        XTest requests have no reply, and the server hands buffered XRecord
        data to recording clients only when it writes to some client.  The
        round trip makes it do so, so a listener sees button and scroll
        events at once even when no window selects them.
        """
        self._d.sync()

    # -- InputBackend -------------------------------------------------------

    def move(self, x: float, y: float) -> None:
        with self._lock:
            x11.fake_motion(self._d, int(round(x)), int(round(y)))
            self._d.flush()

    def position(self) -> tuple[int, int]:
        with self._lock:
            p = self._d.screen().root.query_pointer()
            return int(p.root_x), int(p.root_y)

    def press_button(self, button: str) -> None:
        with self._lock:
            self._button(_button_number(button), True)

    def release_button(self, button: str) -> None:
        with self._lock:
            self._button(_button_number(button), False)

    def scroll(self, dx: int, dy: int) -> None:
        button = x11.scroll_button(dx, dy)
        with self._lock:
            x11.fake_button(self._d, button, True)
            x11.fake_button(self._d, button, False)
            self._sync()

    def press_key(self, name: str) -> None:
        keycode, add_shift = self._resolve_key(name)
        with self._lock:
            shift = self._shift_keycode() if add_shift else None
            if shift is not None:
                self._key(shift, True)
            self._key(keycode, True)
            # The added Shift stays down until the key is released, so an
            # autorepeat of the held key keeps the level it was pressed at.
            self._pressed_names[normalise_key(name)] = (keycode, shift)

    def release_key(self, name: str) -> None:
        with self._lock:
            keycode, shift = self._pressed_names.pop(normalise_key(name), (None, None))
        if keycode is None:
            keycode = self.keycode_for(name)
        with self._lock:
            self._key(keycode, False)
            if shift is not None:
                self._key(shift, False)

    def type_char(self, char: str) -> None:
        """Press and release whatever produces ``char``, with Shift or Level 3 as needed.

        The keymap may hold ``char`` as its Unicode keysym or a legacy one
        (EuroSign); either is used before a spare keycode is borrowed.
        """
        keysyms = x11.keysyms_for_char(char)
        with self._lock:
            self._refresh_keymap()
            level3_key = self._kmap.keycode_for(x11.keysym_from_name("ISO_Level3_Shift"))
            can_level3 = level3_key is not None and bool(self._kmap.level3_mask)
            state = self._state()
            found = None
            for keysym in keysyms:
                found = self._kmap.find_for_state(keysym, state, level3=can_level3)
                if found is not None:
                    break
            if found is None:
                keycode = self._borrower.borrow(keysyms[0], busy=self._held_keys)
                if keycode is None:
                    raise ValueError("No spare keycode to type %r" % char)
                found = (keycode, False, False)
            keycode, shift, level3 = found
            modifiers = []
            if shift:
                modifiers.append(self._kmap.keycode_for(x11.keysym_from_name("Shift_L")))
            if level3:
                modifiers.append(level3_key)
            modifiers = [m for m in modifiers if m is not None and m not in self._held_keys]
            for m in modifiers:
                self._key(m, True)
            self._key(keycode, True)
            self._key(keycode, False)
            for m in reversed(modifiers):
                self._key(m, False)

    def release_all(self) -> None:
        """Release every held key and button, then restore borrowed keycodes.

        A core-protocol client (xterm, Java, Wine) fetches the keymap only
        when it decodes a key event after the MappingNotify, so restoring at
        once could leave it decoding the last borrowed key with the restored
        mapping.  The restore therefore waits ``BORROW_RESTORE_DELAY_S``.
        """
        with self._lock:
            for keycode in reversed(list(self._held_keys)):
                self._key(keycode, False)
            for button in reversed(list(self._held_buttons)):
                self._button(button, False)
            self._pressed_names.clear()
            if self._borrower.borrowed:
                self._d.sync()
                time.sleep(BORROW_RESTORE_DELAY_S)
                self._borrower.restore_all()

    def close(self) -> None:
        with self._lock:
            try:
                self.release_all()
            finally:
                self._d.close()


def _button_number(button: str) -> int:
    try:
        return x11.BUTTON_NUMBERS[button]
    except KeyError:
        raise ValueError("Unknown mouse button %r" % button) from None


# ---------------------------------------------------------------------------
# Recording stream
# ---------------------------------------------------------------------------

class XRecordListener:
    """``InputListener`` on XRecord: keys, buttons, scroll and motion with root coordinates.

    Times are server times converted to ``time.monotonic`` seconds.
    """

    def __init__(self, display_name: str | None = None) -> None:
        self._display_name = display_name
        self._context: x11.RecordContext | None = None
        self._translator: CoreEventTranslator | None = None
        self._clock = x11.ServerClock()
        self._callback: Callable[[RawEvent], None] | None = None

    def start(self, callback: Callable[[RawEvent], None]) -> None:
        """Start delivering events on a background thread; returns once live."""
        self.stop()
        self._callback = callback
        self._clock = x11.ServerClock()
        context = x11.RecordContext(X.KeyPress, X.MotionNotify, self._on_event,
                                    self._display_name)
        self._context = context
        context.start()

    def _on_event(self, event: Any) -> None:
        context = self._context
        if context is None or self._callback is None:
            return
        with context.control_lock:
            if self._translator is None:
                self._translator = CoreEventTranslator(
                    x11.KeyboardMap.from_display(context.control))
            elif _drain_mapping_changes(context.control):
                self._translator.kmap = x11.KeyboardMap.from_display(context.control)
        t = self._clock.to_monotonic(int(event.time), time.monotonic())
        raw = self._translator.translate(event.type, int(event.detail), int(event.state),
                                         int(event.root_x), int(event.root_y), t)
        if raw is not None:
            self._callback(raw)

    def stop(self) -> None:
        context, self._context = self._context, None
        if context is not None:
            context.stop()
        self._translator = None

    def is_alive(self) -> bool:
        return self._context is not None and self._context.is_alive()


# ---------------------------------------------------------------------------
# Stop key
# ---------------------------------------------------------------------------

class XI2StopKeyWatcher:
    """``StopKeyWatcher``: the user's press of one key, never the injector's.

    With XInput 2.1 or later it selects XI_RawKeyPress on the root and
    ignores events whose source is the XTEST keyboard, so synthetic presses
    (and their autorepeat) never count.  Otherwise it falls back to XRecord
    and ``synthetic``: a recorded press counts unless it matches a noted
    synthetic press or the injector holds that key.
    """

    def __init__(self, synthetic: SyntheticKeys | None = None,
                 display_name: str | None = None, use_xi2: bool = True) -> None:
        self._synthetic = synthetic
        self._display_name = display_name
        self._use_xi2 = use_xi2
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._record: x11.RecordContext | None = None

    @property
    def mode(self) -> str | None:
        """``xi2``, ``xrecord`` or None when stopped."""
        if self._thread is not None:
            return "xi2"
        if self._record is not None:
            return "xrecord"
        return None

    def start(self, key: str, callback: Callable[[], None]) -> None:
        self.stop()
        keysym = x11.keysym_from_name(normalise_key(key))
        d = x11.open_display(self._display_name)
        try:
            kmap = x11.KeyboardMap.from_display(d)
            keycodes = {kc for kc in kmap.keycodes() if keysym in kmap.syms(kc)}
            if not keycodes:
                raise ValueError("Key %r is not on the keyboard" % key)
            sources = self._xi2_sources(d)
            opcode = x11.select_raw_key_presses(d) if sources is not None else 0
        except BaseException:
            d.close()
            raise
        if sources is not None:
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run_xi2, args=(d, opcode, keycodes, sources, callback, self._stop),
                name="stop-key", daemon=True)
            self._thread.start()
            return
        d.close()
        if self._synthetic is None:
            log.warning("XInput 2.1 is unavailable and no injector counter was given; "
                        "synthetic %s presses will count as the user's", key)
        self._record = x11.RecordContext(
            X.KeyPress, X.KeyPress, lambda ev: self._on_record(ev, keycodes, callback),
            self._display_name)
        self._record.start()

    def _xi2_sources(self, d) -> set[int] | None:
        """XTEST keyboard ids when XInput 2.1 can be used, else None."""
        if not self._use_xi2:
            return None
        version = x11.xi2_version(d)
        if version is None or version < (2, 1):
            return None
        sources = x11.xtest_keyboard_ids(d)
        return sources or None

    @staticmethod
    def _run_xi2(d, opcode: int, keycodes: set[int], sources: set[int],
                 callback: Callable[[], None], stop: threading.Event) -> None:
        from Xlib.error import ConnectionClosedError, XError

        try:
            fd = d.fileno()
            while not stop.is_set():
                if not d.pending_events():
                    select.select([fd], [], [], 0.1)
                    continue
                ev = d.next_event()
                if x11.is_user_raw_key(ev, opcode, keycodes, sources):
                    callback()
        except (XError, ConnectionClosedError, OSError) as e:
            log.warning("Stop key watcher stopped: %s", e)
        finally:
            d.close()

    def _on_record(self, event: Any, keycodes: set[int], callback: Callable[[], None]) -> None:
        if event.type != X.KeyPress or int(event.detail) not in keycodes:
            return
        keycode = int(event.detail)
        if self._synthetic is not None and (self._synthetic.consume(keycode)
                                            or self._synthetic.is_held(keycode)):
            return
        callback()

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            self._stop.set()
            thread.join(timeout=2.0)
        record, self._record = self._record, None
        if record is not None:
            record.stop()


# ---------------------------------------------------------------------------
# Global hotkeys
# ---------------------------------------------------------------------------

class XGrabHotkeys:
    """``HotkeyGrabber``: XGrabKey on the root, events read on a thread.

    Callbacks run on the grabber's thread.  An autorepeated press (a press
    with the same server time as the release before it) is ignored.  While
    suspended, :meth:`grab` only records the combo; :meth:`resume` grabs
    and reports the combos the server refused.
    """

    def __init__(self, display_name: str | None = None) -> None:
        self._d = x11.open_display(display_name)
        self._lock = threading.Lock()
        self._kmap = x11.KeyboardMap.from_display(self._d)
        self._combos: dict[str, tuple[int, int, Callable[[], None]]] = {}
        self._active: dict[tuple[int, int], Callable[[], None]] = {}
        self._suspended = False
        self._last_release: dict[int, int] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()

    def _resolve(self, combo: str) -> tuple[int, int]:
        names = parse_combo(combo)
        keycode = self._kmap.keycode_for(x11.keysym_from_name(names[-1]))
        if keycode is None:
            raise ValueError("%s is not on the keyboard" % names[-1])
        return keycode, x11.combo_mask(names[:-1], self._kmap)

    def grab(self, combo: str, callback: Callable[[], None]) -> bool:
        problem = combo_problem(combo)
        if problem is not None:
            log.warning("Hotkey %s rejected: %s", combo, problem)
            return False
        with self._lock:
            try:
                keycode, mask = self._resolve(combo)
            except ValueError as e:
                log.warning("Hotkey %s cannot be grabbed: %s", combo, e)
                return False
            if any((kc, m) == (keycode, mask) for kc, m, _ in self._combos.values()):
                log.warning("Hotkey %s is already grabbed", combo)
                return False
            if not self._suspended and not x11.grab_key(self._d, keycode, mask, self._kmap):
                log.warning("Hotkey %s is grabbed by another application", combo)
                return False
            self._combos[combo] = (keycode, mask, callback)
            if not self._suspended:
                self._active[(keycode, mask)] = callback
            return True

    def ungrab_all(self) -> None:
        with self._lock:
            self._ungrab_locked()
            self._combos.clear()

    def _ungrab_locked(self, kmap: x11.KeyboardMap | None = None) -> None:
        """Ungrab every active combo in the lock variants of ``kmap`` (default the current)."""
        for keycode, mask in self._active:
            x11.ungrab_key(self._d, keycode, mask, kmap or self._kmap)
        self._active.clear()

    def suspend(self) -> None:
        """Release the grabs so a dialog can capture the combos; keep them for resume."""
        with self._lock:
            self._suspended = True
            self._ungrab_locked()

    def resume(self) -> list[str]:
        """Grab the kept combos again; returns those that could not be grabbed."""
        with self._lock:
            self._suspended = False
            return self._regrab_locked()

    def _regrab_locked(self) -> list[str]:
        self._ungrab_locked()
        failed = []
        for combo, (_, _, callback) in list(self._combos.items()):
            try:
                keycode, mask = self._resolve(combo)
            except ValueError as e:
                log.warning("Hotkey %s cannot be grabbed: %s", combo, e)
                failed.append(combo)
                continue
            self._combos[combo] = (keycode, mask, callback)
            if x11.grab_key(self._d, keycode, mask, self._kmap):
                self._active[(keycode, mask)] = callback
            else:
                log.warning("Hotkey %s is grabbed by another application", combo)
                failed.append(combo)
        return failed

    def wait_keys_released(self, timeout_s: float) -> bool:
        """Block until the keyboard has no key down, up to ``timeout_s``.

        A passive grab activated by a hotkey press lasts until that key is
        released, and until then the server sends every key event (synthetic
        ones too) to this connection, where they are dropped.  Playback
        started from a hotkey waits here so its first keystrokes reach the
        target window, and so held modifiers do not change them.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            with self._lock:
                down = x11.keys_down(self._d)
            if not down:
                return True
            if time.monotonic() >= deadline:
                log.warning("Keys still held after %.1f s: keycodes %s", timeout_s,
                            ", ".join(map(str, down)))
                return False
            time.sleep(0.01)

    def _grabs_changed_locked(self, old: x11.KeyboardMap) -> bool:
        """True when a keymap change moved a combo or the lock masks a grab covers."""
        if x11.lock_variants(old) != x11.lock_variants(self._kmap):
            return True
        for combo, (keycode, mask, _) in self._combos.items():
            try:
                if self._resolve(combo) != (keycode, mask):
                    return True
            except ValueError:
                return True
        return False

    def _run(self) -> None:
        from Xlib.error import ConnectionClosedError, XError

        fd = self._d.fileno()
        while not self._stop.is_set():
            try:
                select.select([fd], [], [], 0.05)
                fire: list[Callable[[], None]] = []
                with self._lock:
                    while self._d.pending_events():
                        self._handle(self._d.next_event(), fire)
            except (XError, ConnectionClosedError, OSError) as e:
                log.warning("Hotkey listener stopped: %s", e)
                return
            for callback in fire:
                try:
                    callback()
                except Exception:
                    log.exception("Hotkey callback failed")

    def _handle(self, ev: Any, fire: list[Callable[[], None]]) -> None:
        if ev.type == X.MappingNotify and ev.request in (X.MappingKeyboard, X.MappingModifier):
            old, self._kmap = self._kmap, x11.KeyboardMap.from_display(self._d)
            if not self._suspended and self._grabs_changed_locked(old):
                self._ungrab_locked(old)
                self._regrab_locked()
        elif ev.type == X.KeyRelease:
            self._last_release[int(ev.detail)] = int(ev.time)
        elif ev.type == X.KeyPress:
            keycode = int(ev.detail)
            if self._last_release.get(keycode) == int(ev.time):
                return
            callback = self._active.get((keycode, x11.clean_state(int(ev.state), self._kmap)))
            if callback is not None:
                fire.append(callback)

    def close(self) -> None:
        self.ungrab_all()
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._d.close()


# ---------------------------------------------------------------------------
# Targeting
# ---------------------------------------------------------------------------

class TargetingError(RuntimeError):
    """Pointer or keyboard could not be grabbed."""


_GRAB_RETRY_S = 0.5
_XC_CROSSHAIR = 34


def capture_point(timeout_s: float, on_cancel_check: Callable[[], bool],
                  display_name: str | None = None) -> tuple[int, int, str] | None:
    """Let the user pick a screen point; ``(x, y, "#rrggbb")`` or None when cancelled.

    Grabs pointer and keyboard on the root (``owner_events`` false) on a
    connection of its own, retrying for 500 ms.  The first button press
    captures the point and the colour under it and the matching release ends
    targeting; Space captures the pointer position without a click; Escape,
    ``on_cancel_check()`` or ``timeout_s`` cancel.  Raises TargetingError
    when the grabs fail.
    """
    d = x11.open_display(display_name)
    try:
        root = d.screen().root
        cursor = _crosshair(d)
        try:
            _grab_both(d, root, cursor)
            return _capture_loop(d, root, timeout_s, on_cancel_check)
        finally:
            d.ungrab_pointer(X.CurrentTime)
            d.ungrab_keyboard(X.CurrentTime)
            d.flush()
    finally:
        d.close()


def _crosshair(d) -> int:
    from Xlib.error import XError

    try:
        font = d.open_font("cursor")
        return font.create_glyph_cursor(font, _XC_CROSSHAIR, _XC_CROSSHAIR + 1,
                                        (0, 0, 0), (0xFFFF, 0xFFFF, 0xFFFF))
    except XError as e:
        log.debug("No crosshair cursor: %s", e)
        return X.NONE


def _grab_both(d, root, cursor: Any) -> None:
    deadline = time.monotonic() + _GRAB_RETRY_S
    pointer = keyboard = None
    while True:
        if pointer != X.GrabSuccess:
            pointer = root.grab_pointer(False, X.ButtonPressMask | X.ButtonReleaseMask,
                                        X.GrabModeAsync, X.GrabModeAsync, X.NONE, cursor,
                                        X.CurrentTime)
        if keyboard != X.GrabSuccess:
            keyboard = root.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
        if pointer == X.GrabSuccess and keyboard == X.GrabSuccess:
            return
        if time.monotonic() >= deadline:
            what = "pointer" if pointer != X.GrabSuccess else "keyboard"
            raise TargetingError("Another application holds the %s; targeting cancelled" % what)
        time.sleep(0.02)


def _capture_loop(d, root, timeout_s: float,
                  on_cancel_check: Callable[[], bool]) -> tuple[int, int, str] | None:
    kmap = x11.KeyboardMap.from_display(d)
    space = x11.keysym_from_name("space")
    escape = x11.keysym_from_name("Escape")
    deadline = time.monotonic() + timeout_s
    captured: tuple[int, int, str] | None = None
    held_button: int | None = None
    fd = d.fileno()
    while True:
        if on_cancel_check():
            return None
        if time.monotonic() >= deadline:
            return captured
        if not d.pending_events():
            select.select([fd], [], [], 0.05)
            continue
        ev = d.next_event()
        if ev.type in (X.ButtonPress, X.ButtonRelease) and int(ev.detail) in x11.SCROLL_BUTTONS:
            continue            # a wheel notch is not a pick
        if ev.type == X.ButtonPress and held_button is None:
            x, y = int(ev.root_x), int(ev.root_y)
            captured = (x, y, x11.pixel_colour(d, x, y))
            held_button = int(ev.detail)
        elif ev.type == X.ButtonRelease and held_button == int(ev.detail):
            return captured
        elif ev.type == X.KeyPress:
            keysym = kmap.keysym_at(int(ev.detail), 0)
            if keysym == escape:
                return None
            if keysym == space and held_button is None:
                p = root.query_pointer()
                x, y = int(p.root_x), int(p.root_y)
                return (x, y, x11.pixel_colour(d, x, y))
