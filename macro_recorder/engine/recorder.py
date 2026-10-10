"""Drives the platform input listener into a filtered raw event stream.

The recorder keeps the listener's events plus ``window_focus`` markers from
the active-window watcher, with times in seconds since :meth:`Recorder.start`.
It filters out everything aimed at the application's own windows and the keys
that stop a recording; :mod:`macro_recorder.model.bundling` turns the result
into rows.

Decisions where the design is silent:

- A release is kept exactly when its press was kept, so a key pressed in
  another window and released after focus moved to the application still
  closes, and releases of keys or buttons pressed before the recording
  started are dropped silently.
- Markers for windows without a title are not emitted: an empty title would
  match any window on playback.
- Recording halts the moment the stop key or the record combo is seen in the
  stream, before the GUI gets round to calling :meth:`Recorder.stop`, so the
  combo's modifier releases never reach the stream.  The walk back that
  removes trailing modifier presses without releases runs on every halt.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import Callable

from macro_recorder.model.keys import display_key, is_modifier, parse_combo
from macro_recorder.model.protocols import InputListener, RawEvent, WindowInfo, WindowManager
from macro_recorder.platform.session import OwnWindows

log = logging.getLogger(__name__)


class RecorderError(RuntimeError):
    """The recording could not start or ended abnormally."""


def _family(name: str) -> str:
    """Modifier family of a keysym name: ``Control_R`` -> ``ctrl``."""
    return display_key(name).lower()


class Recorder:
    """Records global input into a raw stream.

    Listener and watcher callbacks arrive on other threads; every public
    method may be called from any thread.
    """

    def __init__(self, listener: InputListener, windows: WindowManager, own_windows: OwnWindows,
                 stop_key: str = "F6", record_combo: str | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._listener = listener
        self._windows = windows
        self._own = own_windows
        self._stop_key = stop_key
        self._combo_key: str | None = None
        self._combo_mods: frozenset[str] = frozenset()
        if record_combo:
            names = parse_combo(record_combo)
            self._combo_key = names[-1].lower()
            self._combo_mods = frozenset(_family(n) for n in names[:-1])
        self._clock = clock
        self._lock = threading.Lock()
        self._events: list[RawEvent] = []
        self._t0 = 0.0
        self._started = False
        self._accepting = False
        self._running = False
        self._stop_requested = False
        self._error: str | None = None
        self._unwatch: Callable[[], None] | None = None
        self._down: set[str] = set()            # keys physically down
        self._kept_keys: set[str] = set()       # keys whose press was recorded
        self._dropped_keys: set[str] = set()    # keys whose press was dropped
        self._kept_buttons: set[str] = set()
        self._dropped_buttons: set[str] = set()

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the watcher and the listener; returns once events flow."""
        with self._lock:
            if self._started:
                raise RuntimeError("A recorder records once")
            self._started = True
            self._t0 = self._clock()
            self._accepting = True
            self._running = True
        active = self._windows.active()
        if active is not None and self._wanted_marker(active):
            with self._lock:
                self._events.append(RawEvent("window_focus", 0.0, window=active))
        self._unwatch = self._windows.watch_active(self._on_active)
        try:
            self._listener.start(self._on_raw)
        except Exception as e:
            self.stop()
            raise RecorderError("Could not start recording: %s" % e) from e

    def stop(self) -> None:
        """Stop recording.  Idempotent; events arriving afterwards are discarded."""
        with self._lock:
            if self._accepting:
                self._halt_locked()
            running, self._running = self._running, False
        if not running:
            return
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None
        self._listener.stop()

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._accepting

    @property
    def stop_requested(self) -> bool:
        """True once the stop key or the record combo was pressed."""
        with self._lock:
            return self._stop_requested

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    def now(self) -> float:
        """Seconds since :meth:`start`, on the same scale as event times."""
        return self._clock() - self._t0

    # --------------------------------------------------------------- output

    def events_since(self, cursor: int) -> tuple[list[RawEvent], int]:
        """Events recorded from index ``cursor`` on, and the next cursor.

        Raises RecorderError (once) when the listener died while recording;
        the recording is halted and :meth:`result` still holds what was
        recorded.
        """
        with self._lock:
            died = self._accepting and not self._listener.is_alive()
            if died:
                self._error = "The input listener stopped unexpectedly"
                self._halt_locked()
            events = self._events[cursor:]
            new_cursor = len(self._events)
        if died:
            log.error("Recording: %s", self._error)
            raise RecorderError(self._error)
        return events, new_cursor

    def result(self) -> list[RawEvent]:
        """The filtered raw stream; complete once the recorder has stopped."""
        with self._lock:
            return list(self._events)

    # -------------------------------------------------------------- filtering

    def _wanted_marker(self, info: WindowInfo) -> bool:
        return bool(info.title) and not self._own.contains(info.id)

    def _on_active(self, info: WindowInfo) -> None:
        """Watcher callback: a marker for every non-application window."""
        if not self._wanted_marker(info):
            return
        t = max(0.0, self._clock() - self._t0)
        with self._lock:
            if self._accepting:
                self._events.append(RawEvent("window_focus", t, window=info))

    def _own_at(self, x: int, y: int) -> bool:
        wid = self._windows.window_at(x, y)
        return wid is not None and self._own.contains(wid)

    def _own_active(self) -> bool:
        active = self._windows.active()
        return active is not None and self._own.contains(active.id)

    def _on_raw(self, event: RawEvent) -> None:
        """Listener callback."""
        kind = event.kind
        own = False
        # X queries happen outside the lock.  The key sets read here are only
        # written by this (the listener) thread.
        if kind in ("button_press", "scroll"):
            own = self._own_at(event.x, event.y)
        elif kind == "key_press" and event.key not in self._kept_keys | self._dropped_keys:
            own = self._own_active()
        event = replace(event, time=max(0.0, event.time - self._t0))
        with self._lock:
            if not self._accepting:
                return
            keep = _FILTERS[kind](self, event, own) if kind in _FILTERS else True
            if keep:
                self._events.append(event)

    def _filter_button_press(self, e: RawEvent, own: bool) -> bool:
        if own:
            self._dropped_buttons.add(e.button)
            return False
        self._kept_buttons.add(e.button)
        return True

    def _filter_button_release(self, e: RawEvent, own: bool) -> bool:
        self._dropped_buttons.discard(e.button)
        if e.button in self._kept_buttons:
            self._kept_buttons.discard(e.button)
            return True
        return False

    def _filter_motion(self, e: RawEvent, own: bool) -> bool:
        return not self._dropped_buttons

    def _filter_scroll(self, e: RawEvent, own: bool) -> bool:
        return not own

    def _filter_key_press(self, e: RawEvent, own: bool) -> bool:
        key = e.key
        self._down.add(key)
        if key in self._kept_keys:
            return True                         # autorepeat
        if key in self._dropped_keys:
            return False
        if key == self._stop_key or self._is_combo(key):
            self._stop_requested = True
            self._dropped_keys.add(key)
            self._halt_locked()
            return False
        if own:
            self._dropped_keys.add(key)
            return False
        self._kept_keys.add(key)
        return True

    def _filter_key_release(self, e: RawEvent, own: bool) -> bool:
        key = e.key
        self._down.discard(key)
        self._dropped_keys.discard(key)
        if key in self._kept_keys:
            self._kept_keys.discard(key)
            return True
        return False

    def _is_combo(self, key: str) -> bool:
        if self._combo_key is None or key.lower() != self._combo_key:
            return False
        held = {_family(k) for k in self._down if is_modifier(k)}
        return self._combo_mods <= held

    def _halt_locked(self) -> None:
        """Stop accepting events and drop trailing modifier presses without releases."""
        self._accepting = False
        released: set[str] = set()
        i = len(self._events) - 1
        while i >= 0:
            e = self._events[i]
            if e.kind == "motion":
                i -= 1
                continue
            if e.kind not in ("key_press", "key_release") or not is_modifier(e.key):
                break
            if e.kind == "key_release":
                released.add(e.key)
            elif e.key not in released:
                del self._events[i]
            i -= 1


_FILTERS: dict[str, Callable[[Recorder, RawEvent, bool], bool]] = {
    "button_press": Recorder._filter_button_press,
    "button_release": Recorder._filter_button_release,
    "motion": Recorder._filter_motion,
    "scroll": Recorder._filter_scroll,
    "key_press": Recorder._filter_key_press,
    "key_release": Recorder._filter_key_release,
}
