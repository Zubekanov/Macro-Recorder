"""``WindowManager`` implementations.

:class:`X11WindowManager` keeps one display connection under a lock, because
the player, the recorder and the GUI call in from different threads.  An X
error other than a vanished window, or a closed connection, drops the
connection; the next call reconnects.  Activation, moves and maximising poll
every 50 ms until two reads agree or ``settle_ms`` passes, releasing the lock
between reads.  Windows registered in the application's
:class:`~macro_recorder.platform.session.OwnWindows` are left out of
:meth:`X11WindowManager.list` and so never match a window row.

:class:`NullWindowManager` stands in when there is no X display.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Callable, TypeVar

from macro_recorder.model.protocols import Clock, Monitor, Rect, WindowInfo
from macro_recorder.platform import x11
from macro_recorder.platform.session import OwnWindows

log = logging.getLogger(__name__)

T = TypeVar("T")

POLL_S = 0.05
_UNSET = object()


class _SystemClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def title_matches(title: str, pattern: str, mode: str = "substring") -> bool:
    """Case-insensitive substring, or ``re.search`` in ``regex`` mode.

    Raises ValueError for an invalid regex.
    """
    if mode == "regex":
        try:
            return re.search(pattern, title) is not None
        except re.error as e:
            raise ValueError("Invalid window title pattern %r: %s" % (pattern, e)) from e
    return pattern.lower() in title.lower()


def select_window(windows: list[WindowInfo], pattern: str,
                  mode: str = "substring") -> WindowInfo | None:
    """An exact (case-insensitive) title first, else the first match in stacking order."""
    for w in windows:
        if w.title.lower() == pattern.lower():
            return w
    for w in windows:
        if title_matches(w.title, pattern, mode):
            return w
    return None


def settle(read: Callable[[], T], accept: Callable[[T], bool], settle_ms: float,
           clock: Clock, poll_s: float = POLL_S) -> tuple[T, bool]:
    """Poll ``read`` until two consecutive accepted reads agree or ``settle_ms`` passes.

    Returns the last value and whether it settled.
    """
    deadline = clock.now() + max(0.0, settle_ms) / 1000.0
    previous: Any = _UNSET
    while True:
        value = read()
        if accept(value) and value == previous:
            return value, True
        if clock.now() >= deadline:
            return value, False
        previous = value
        clock.sleep(poll_s)


def monitor_work_areas(rects: list[Rect], work: Rect | None,
                       gtk_areas: list[Rect] | None = None,
                       struts: list[Rect] | None = None) -> list[Monitor]:
    """Monitors numbered from 1, each with its own work area.

    The window manager's per-monitor areas (``_GTK_WORKAREAS_D<n>``, which
    Muffin and Mutter publish) win: each monitor takes the one overlapping it
    most.  Without them, panel struts (``_NET_WM_STRUT_PARTIAL`` rectangles
    in root coordinates) are cut from each monitor they touch.  Only when
    neither is known is the single ``_NET_WORKAREA`` intersected with each
    monitor, which is wrong when a panel sits on one monitor only.
    """
    monitors = []
    for i, rect in enumerate(rects, start=1):
        area = None
        if gtk_areas:
            best = max(gtk_areas, key=lambda a: _overlap(rect, a))
            area = x11.intersect(rect, best)
        elif struts:
            area = _cut_struts(rect, struts)
        elif work is not None:
            area = x11.intersect(rect, work)
        monitors.append(Monitor(i, rect, area or rect))
    return monitors


def _overlap(a: Rect, b: Rect) -> int:
    common = x11.intersect(a, b)
    return common.width * common.height if common is not None else 0


def _cut_struts(monitor: Rect, struts: list[Rect]) -> Rect | None:
    """``monitor`` less every strut that touches it, each cut from its nearest edge."""
    left, top, right, bottom = monitor.left, monitor.top, monitor.right, monitor.bottom
    for strut in struts:
        if x11.intersect(monitor, strut) is None:
            continue
        gaps = {"left": strut.right - monitor.left, "right": monitor.right - strut.left,
                "top": strut.bottom - monitor.top, "bottom": monitor.bottom - strut.top}
        side = min(gaps, key=lambda k: gaps[k])
        if side == "left":
            left = max(left, strut.right)
        elif side == "right":
            right = min(right, strut.left)
        elif side == "top":
            top = max(top, strut.bottom)
        else:
            bottom = min(bottom, strut.top)
    if right <= left or bottom <= top:
        return None
    return Rect(left, top, right - left, bottom - top)


# ---------------------------------------------------------------------------
# X11
# ---------------------------------------------------------------------------

class X11WindowManager:
    """``WindowManager`` over EWMH on one locked display connection."""

    def __init__(self, display_name: str | None = None, clock: Clock | None = None,
                 own_windows: OwnWindows | None = None) -> None:
        self._display_name = display_name
        self._clock = clock or _SystemClock()
        self._own_windows = own_windows
        self._display = None
        self._time_window = None
        self._lock = threading.RLock()

    # -- connection ---------------------------------------------------------

    def _call(self, fn: Callable[[Any], T], default: T) -> T:
        """Run ``fn(display)`` under the lock; ``default`` on X failure."""
        from Xlib.error import ConnectionClosedError, XError

        with self._lock:
            try:
                if self._display is None:
                    self._display = x11.open_display(self._display_name)
                return fn(self._display)
            except (XError, ConnectionClosedError, OSError) as e:
                log.debug("X11 window operation failed: %s", e)
                self._drop_locked()
                return default

    def _drop_locked(self) -> None:
        display, self._display, self._time_window = self._display, None, None
        if display is not None:
            try:
                display.close()
            except Exception as e:
                log.debug("Closing the window manager connection failed: %s", e)

    def close(self) -> None:
        with self._lock:
            self._drop_locked()

    def _timestamp(self, d) -> int:
        if self._time_window is None:
            self._time_window = x11.create_time_window(d)
        return x11.server_time(d, self._time_window)

    # -- queries ------------------------------------------------------------

    def list(self) -> list[WindowInfo]:
        """Titled top-level windows, top-most first, without the application's own."""
        windows = self._call(x11.list_windows, [])
        own = self._own_windows
        if own is None:
            return windows
        return [w for w in windows if not own.contains(w.id, include_overlay=True)]

    def active(self) -> WindowInfo | None:
        def read(d):
            xid = x11.active_window_id(d)
            return x11.read_window(d, xid, require_title=False) if xid else None
        return self._call(read, None)

    def find(self, pattern: str, mode: str = "substring") -> WindowInfo | None:
        return select_window(self.list(), pattern, mode)

    def refresh(self, window: WindowInfo) -> WindowInfo | None:
        return self._call(lambda d: x11.read_window(d, window.id, require_title=False), None)

    def _snapshot(self, xid: int) -> tuple[int | None, WindowInfo | None]:
        """``(focus, info)``: ``focus`` is ``xid`` when the window or a modal
        dialog transient for it is active, else the active id."""
        def read(d):
            active = x11.active_window_id(d)
            if (active is not None and active != xid and x11.is_modal(d, active)
                    and x11.transient_reaches(d, active, xid)):
                log.debug("Activating %#x focused its modal dialog %#x", xid, active)
                active = xid
            return active, x11.read_window(d, xid, require_title=False)
        return self._call(read, (None, None))

    # -- actions ------------------------------------------------------------

    def activate(self, window: WindowInfo, settle_ms: int = 1000) -> WindowInfo | None:
        sent = self._call(lambda d: x11.activate(d, window.id, window.desktop,
                                                 self._timestamp(d)) or True, False)
        if not sent:
            return None
        (active, info), _ = settle(lambda: self._snapshot(window.id),
                                   lambda v: v[0] == window.id and v[1] is not None,
                                   settle_ms, self._clock)
        if active != window.id or info is None:
            log.debug("Window %#x did not become active", window.id)
            return None
        return info

    def move(self, window: WindowInfo, rect: Rect, settle_ms: int = 1000) -> WindowInfo | None:
        current = self.refresh(window)
        if current is None:
            return None
        if current.maximised:
            current = self.set_maximised(current, False, settle_ms) or current
        before = current.rect
        if not self._call(lambda d: x11.move_resize(d, window.id, rect) or True, False):
            return None
        info, _ = settle(lambda: self.refresh(window),
                         lambda v: v is not None and (v.rect == rect or v.rect != before),
                         settle_ms, self._clock)
        return info

    def set_maximised(self, window: WindowInfo, maximised: bool,
                      settle_ms: int = 1000) -> WindowInfo | None:
        if not self._call(lambda d: x11.set_maximised(d, window.id, maximised), False):
            return self.refresh(window)
        info, _ = settle(lambda: self.refresh(window),
                         lambda v: v is not None and v.maximised == maximised,
                         settle_ms, self._clock)
        return info

    def watch_active(self, callback: Callable[[WindowInfo], None]) -> Callable[[], None]:
        """Report every change of active window until the returned stop is called.

        The window active at the start is not reported; :meth:`active` reads
        it.  Returns once the watcher knows the starting window, so a change
        made after the call is always reported.
        """
        stop_event = threading.Event()
        ready = threading.Event()

        def run() -> None:
            from Xlib.error import ConnectionClosedError, XError
            try:
                x11.watch_active_window(callback, stop_event, display_name=self._display_name,
                                        ready=ready)
            except (XError, ConnectionClosedError, OSError) as e:
                log.warning("Active-window watcher stopped: %s", e)
            finally:
                ready.set()

        thread = threading.Thread(target=run, name="active-window", daemon=True)
        thread.start()
        ready.wait(timeout=2.0)

        def stop() -> None:
            stop_event.set()
            if thread is not threading.current_thread():
                thread.join(timeout=2.0)
        return stop

    def monitors(self) -> list[Monitor]:
        def read(d):
            rects = _randr_monitors(d) or _mss_monitors()
            if not rects:
                w, h = x11.screen_size(d)
                rects = [Rect(0, 0, w, h)]
            return monitor_work_areas(rects, x11.work_area(d), x11.gtk_work_areas(d),
                                      x11.panel_struts(d))
        return self._call(read, [])

    def bell(self) -> None:
        self._call(x11.bell, None)

    def screen_size(self) -> tuple[int, int]:
        return self._call(x11.screen_size, (0, 0))

    def window_at(self, x: int, y: int) -> int | None:
        return self._call(lambda d: x11.window_at(d, int(x), int(y)), None)


def _randr_monitors(d) -> list[Rect]:
    """Monitor rectangles from RandR 1.5, primary first; [] when unavailable."""
    from Xlib.error import XError

    if not d.has_extension("RANDR"):
        return []
    try:
        version = d.xrandr_query_version()
        if (version.major_version, version.minor_version) < (1, 5):
            return []
        reply = d.screen().root.xrandr_get_monitors()
    except XError as e:
        log.debug("RandR monitors unavailable: %s", e)
        return []
    ordered = sorted(reply.monitors, key=lambda m: not m.primary)
    return [Rect(m.x, m.y, m.width_in_pixels, m.height_in_pixels) for m in ordered]


def _mss_monitors() -> list[Rect]:
    try:
        import mss
        with mss.mss() as sct:
            return [Rect(m["left"], m["top"], m["width"], m["height"]) for m in sct.monitors[1:]]
    except Exception as e:
        log.debug("mss monitors unavailable: %s", e)
        return []


# ---------------------------------------------------------------------------
# Null
# ---------------------------------------------------------------------------

class NullWindowManager:
    """No display: nothing is found, every action fails, watching reports nothing."""

    def list(self) -> list[WindowInfo]:
        return []

    def active(self) -> WindowInfo | None:
        return None

    def find(self, pattern: str, mode: str = "substring") -> WindowInfo | None:
        return None

    def refresh(self, window: WindowInfo) -> WindowInfo | None:
        return None

    def activate(self, window: WindowInfo, settle_ms: int = 1000) -> WindowInfo | None:
        return None

    def move(self, window: WindowInfo, rect: Rect, settle_ms: int = 1000) -> WindowInfo | None:
        return None

    def set_maximised(self, window: WindowInfo, maximised: bool,
                      settle_ms: int = 1000) -> WindowInfo | None:
        return None

    def watch_active(self, callback: Callable[[WindowInfo], None]) -> Callable[[], None]:
        return lambda: None

    def monitors(self) -> list[Monitor]:
        return []

    def bell(self) -> None:
        return None

    def screen_size(self) -> tuple[int, int]:
        return (0, 0)

    def window_at(self, x: int, y: int) -> int | None:
        return None


def create_window_manager(display_name: str | None = None, own_windows: OwnWindows | None = None
                          ) -> X11WindowManager | NullWindowManager:
    """An X11WindowManager when the display opens, else a NullWindowManager.

    ``own_windows`` names the application's windows, which ``list`` and
    ``find`` leave out.
    """
    if not (display_name or os.environ.get("DISPLAY")):
        return NullWindowManager()
    try:
        x11.open_display(display_name).close()
    except OSError as e:
        log.warning("X11 unavailable, window management disabled: %s", e)
        return NullWindowManager()
    return X11WindowManager(display_name, own_windows=own_windows)
