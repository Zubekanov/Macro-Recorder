"""X11 window management and overlay shaping via python-xlib.

This is the Linux counterpart of the Win32 calls scattered through the
Windows build: EWMH properties replace ``EnumWindows``/``GetForegroundWindow``,
client messages to the root window replace ``MoveWindow``/``SetForegroundWindow``,
and the SHAPE extension replaces ``-transparentcolor`` for the click-through
overlay.  Everything is imported lazily by the callers so the rest of the app
(and the test suite) never hard-depends on an X connection.

Conventions
-----------
- A window's rectangle is its outer frame, like Win32 ``GetWindowRect``: the
  client geometry grown by ``_NET_FRAME_EXTENTS`` when the window manager
  publishes it.
- python-xlib ``Display`` objects are not thread-safe.  Every function takes the
  display it should use; ``watch_active_window`` opens a private one because it
  runs on its own thread.
- Without a window manager (bare Xvfb, the test suite) the EWMH properties are
  absent and the functions fall back to core protocol: ``query_tree`` for the
  window list, ``get_input_focus`` for the active window, ``configure`` and
  ``set_input_focus`` for move and activate.
- Under a Wayland session only XWayland clients are visible.  Log in to an X11
  session (``Cinnamon (X11)`` on Linux Mint) for full functionality.
"""

from __future__ import annotations

import logging
import select
import threading
from typing import Callable, Iterable

from macro_recorder.window_manager import WindowInfo

log = logging.getLogger(__name__)

SHAPE_BOUNDING = 0   # region that is painted
SHAPE_INPUT = 2      # region that receives pointer events (python-xlib has no constant)

_SOURCE_PAGER = 2    # EWMH source indication: treat our requests like a pager's
_MOVERESIZE_X = 1 << 8
_MOVERESIZE_Y = 1 << 9
_MOVERESIZE_W = 1 << 10
_MOVERESIZE_H = 1 << 11


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _log_x_error(*args) -> None:
    log.debug("X protocol error: %s", args[0] if args else "?")


def open_display():
    """Return a fresh ``Xlib.display.Display``.

    Raises OSError when ``DISPLAY`` is unset or the server cannot be reached,
    ImportError when python-xlib is missing.
    """
    from Xlib import display
    from Xlib.error import DisplayError

    try:
        d = display.Display()
    except (DisplayError, OSError, ValueError, OverflowError) as e:
        # DisplayError covers a bad name and a refused connection; the others
        # come from malformed DISPLAY values before python-xlib validates them.
        raise OSError("Cannot open X display: %s" % e) from e
    # Errors from requests without a reply (configure, send_event...) would
    # otherwise be printed to stderr by python-xlib's default handler.
    d.set_error_handler(_log_x_error)
    return d


def _atom(d, name: str) -> int:
    cache = d.__dict__.setdefault("_macro_recorder_atoms", {})
    if name not in cache:
        cache[name] = d.intern_atom(name)
    return cache[name]


def _prop_ints(d, win, name: str) -> list[int] | None:
    """Return a 32-bit list property (WINDOW, CARDINAL, ATOM...) or None."""
    from Xlib import X

    prop = win.get_full_property(_atom(d, name), X.AnyPropertyType)
    if prop is None or prop.format != 32:
        return None
    return [int(v) for v in prop.value]


def _wm_supports(d, name: str) -> bool:
    """True if the running window manager lists ``name`` in _NET_SUPPORTED."""
    cache = d.__dict__.setdefault("_macro_recorder_supported", {})
    if "atoms" not in cache:
        cache["atoms"] = set(_prop_ints(d, d.screen().root, "_NET_SUPPORTED") or ())
    return _atom(d, name) in cache["atoms"]


def _window(d, xid: int):
    return d.create_resource_object("window", xid)


# ---------------------------------------------------------------------------
# Geometry (pure helper is separate so it can be unit-tested)
# ---------------------------------------------------------------------------

def outer_rect(client_left: int, client_top: int, client_width: int, client_height: int,
               extents: Iterable[int] | None) -> tuple[int, int, int, int]:
    """Grow a client rectangle by _NET_FRAME_EXTENTS ``(left, right, top, bottom)``."""
    ext = list(extents) if extents else []
    if len(ext) != 4:
        return client_left, client_top, client_width, client_height
    left, right, top, bottom = ext
    return (client_left - left, client_top - top,
            client_width + left + right, client_height + top + bottom)


def client_size(outer_width: int, outer_height: int,
                extents: Iterable[int] | None) -> tuple[int, int]:
    """Inverse of :func:`outer_rect` for the size: the client size of an outer size."""
    ext = list(extents) if extents else []
    if len(ext) != 4:
        return max(1, outer_width), max(1, outer_height)
    left, right, top, bottom = ext
    return max(1, outer_width - left - right), max(1, outer_height - top - bottom)


def window_title(d, win) -> str:
    """The window's _NET_WM_NAME, else WM_NAME, else an empty string."""
    prop = win.get_full_property(_atom(d, "_NET_WM_NAME"), _atom(d, "UTF8_STRING"))
    value = prop.value if prop is not None else None
    if not value:
        value = win.get_wm_name()
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return value or ""


def window_rect(d, win) -> tuple[int, int, int, int]:
    """Outer ``(left, top, width, height)`` of ``win`` in root coordinates."""
    geom = win.get_geometry()
    pos = d.screen().root.translate_coords(win, 0, 0)
    return outer_rect(pos.x, pos.y, geom.width, geom.height,
                      _prop_ints(d, win, "_NET_FRAME_EXTENTS"))


def window_info(d, win, require_viewable: bool = False) -> WindowInfo | None:
    """Snapshot ``win`` as a WindowInfo, or None if it is untitled or gone."""
    from Xlib import X
    from Xlib.error import XError

    try:
        if require_viewable and win.get_attributes().map_state != X.IsViewable:
            return None
        title = window_title(d, win)
        if not title:
            return None
        left, top, width, height = window_rect(d, win)
        return WindowInfo(title=title, left=left, top=top, width=width, height=height, hwnd=win.id)
    except XError as e:
        log.debug("Could not read window %#x: %s", win.id, e)
        return None


# ---------------------------------------------------------------------------
# Enumeration and activation
# ---------------------------------------------------------------------------

def list_windows(d) -> list[WindowInfo]:
    """All titled top-level windows, top-most first.

    Uses _NET_CLIENT_LIST_STACKING when a window manager provides it, else the
    viewable children of the root window.
    """
    root = d.screen().root
    ids = _prop_ints(d, root, "_NET_CLIENT_LIST_STACKING")
    if ids is not None:
        candidates = [(_window(d, xid), False) for xid in reversed(ids)]
    else:
        candidates = [(w, True) for w in reversed(root.query_tree().children)]
    found: list[WindowInfo] = []
    for win, check_viewable in candidates:
        info = window_info(d, win, require_viewable=check_viewable)
        if info:
            found.append(info)
    return found


def _toplevel_of(d, win):
    """Climb from ``win`` to the child of the root window that contains it."""
    from Xlib.error import XError

    root = d.screen().root
    try:
        while True:
            parent = win.query_tree().parent
            if parent is None or parent.id == root.id or parent.id == 0:
                return win
            win = parent
    except XError:
        return win


def active_window(d):
    """The active window object, or None."""
    from Xlib import X

    ids = _prop_ints(d, d.screen().root, "_NET_ACTIVE_WINDOW")
    if ids and ids[0]:
        return _window(d, ids[0])
    focus = d.get_input_focus().focus
    if isinstance(focus, int) or focus.id in (X.NONE, X.PointerRoot):
        return None
    return _toplevel_of(d, focus)


def foreground_info(d) -> WindowInfo | None:
    win = active_window(d)
    return window_info(d, win) if win is not None else None


def activate(d, xid: int) -> None:
    """Raise and focus the window, via _NET_ACTIVE_WINDOW when the WM supports it."""
    from Xlib import X, protocol

    win = _window(d, xid)
    root = d.screen().root
    if _wm_supports(d, "_NET_ACTIVE_WINDOW"):
        ev = protocol.event.ClientMessage(
            window=win, client_type=_atom(d, "_NET_ACTIVE_WINDOW"),
            data=(32, [_SOURCE_PAGER, X.CurrentTime, 0, 0, 0]))
        root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
    else:
        win.configure(stack_mode=X.Above)
        win.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()


def move_resize(d, xid: int, left: int, top: int, width: int, height: int) -> None:
    """Place the window's outer frame at the given rectangle."""
    from Xlib import X, protocol

    win = _window(d, xid)
    cw, ch = client_size(width, height, _prop_ints(d, win, "_NET_FRAME_EXTENTS"))
    if _wm_supports(d, "_NET_MOVERESIZE_WINDOW"):
        flags = (X.NorthWestGravity | _MOVERESIZE_X | _MOVERESIZE_Y
                 | _MOVERESIZE_W | _MOVERESIZE_H | (_SOURCE_PAGER << 12))
        ev = protocol.event.ClientMessage(
            window=win, client_type=_atom(d, "_NET_MOVERESIZE_WINDOW"),
            data=(32, [flags, left & 0xFFFFFFFF, top & 0xFFFFFFFF, cw, ch]))
        d.screen().root.send_event(
            ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
    else:
        win.configure(x=left, y=top, width=cw, height=ch)
    d.sync()


# ---------------------------------------------------------------------------
# Active-window watching (own connection, own thread)
# ---------------------------------------------------------------------------

def watch_active_window(callback: Callable[[WindowInfo], None], stop_event: threading.Event,
                        poll_interval: float = 0.25) -> None:
    """Call ``callback`` whenever the active window changes, until ``stop_event``.

    Wakes on _NET_ACTIVE_WINDOW property changes for a prompt reaction and
    additionally re-checks every ``poll_interval`` seconds, which is what makes
    it work without a window manager as well.
    """
    from Xlib import X

    d = open_display()
    try:
        root = d.screen().root
        root.change_attributes(event_mask=X.PropertyChangeMask)
        d.flush()
        last_id = None
        fd = d.fileno()
        while not stop_event.is_set():
            readable, _, _ = select.select([fd], [], [], poll_interval)
            if readable:
                while d.pending_events():
                    d.next_event()      # drain; the check below reads the property
            win = active_window(d)
            xid = win.id if win is not None else None
            if xid != last_id:
                last_id = xid
                info = window_info(d, win) if win is not None else None
                if info:
                    callback(info)
    finally:
        d.close()


# ---------------------------------------------------------------------------
# SHAPE extension (overlay)
# ---------------------------------------------------------------------------

def _shape_requests():
    """Build the raw SHAPE requests once.

    python-xlib's ``shape_rectangles`` rejects the input region, so these two
    request classes mirror ShapeRectangles / ShapeGetRectangles with a plain
    ``region`` byte.
    """
    from Xlib.protocol import rq, structs

    class Rectangles(rq.Request):
        _request = rq.Struct(
            rq.Card8("opcode"), rq.Opcode(1), rq.RequestLength(),
            rq.Card8("operation"), rq.Card8("region"), rq.Card8("ordering"), rq.Pad(1),
            rq.Window("window"), rq.Int16("x"), rq.Int16("y"),
            rq.List("rectangles", structs.Rectangle),
        )

    class GetRectangles(rq.ReplyRequest):
        _request = rq.Struct(
            rq.Card8("opcode"), rq.Opcode(8), rq.RequestLength(),
            rq.Window("window"), rq.Card8("region"), rq.Pad(3),
        )
        _reply = rq.Struct(
            rq.ReplyCode(), rq.Card8("ordering"), rq.Card16("sequence_number"),
            rq.ReplyLength(), rq.LengthOf("rectangles", 4), rq.Pad(20),
            rq.List("rectangles", structs.Rectangle),
        )

    return Rectangles, GetRectangles


def tk_toplevel_xid(d, inner_xid: int) -> int:
    """The X window that carries a Tk toplevel's geometry.

    On X11 Tk wraps every toplevel in a parent "wrapper" window that owns the
    position and size (``winfo_id`` returns the child).  Shape and configure
    requests must go to the wrapper.
    """
    inner = _window(d, inner_xid)
    parent = inner.query_tree().parent
    if parent is None or parent.id == d.screen().root.id:
        return inner_xid
    return parent.id


def set_window_shape(d, xid: int, bounding: Iterable[tuple[int, int, int, int]],
                     input_rects: Iterable[tuple[int, int, int, int]] = ()) -> None:
    """Restrict what ``xid`` paints and what it catches clicks on.

    An empty ``input_rects`` makes the window click-through everywhere; an
    empty ``bounding`` makes it invisible.
    """
    Rectangles, _ = _shape_requests()
    opcode = d.display.get_extension_major("SHAPE")
    for region, rects in ((SHAPE_BOUNDING, bounding), (SHAPE_INPUT, input_rects)):
        Rectangles(display=d.display, opcode=opcode, operation=0, region=region,
                   ordering=0, window=xid, x=0, y=0,
                   rectangles=[{"x": x, "y": y, "width": w, "height": h} for x, y, w, h in rects])
    d.sync()


def get_window_shape(d, xid: int, region: int) -> list[tuple[int, int, int, int]]:
    """The rectangles currently making up ``region`` of ``xid`` (for tests/debugging)."""
    _, GetRectangles = _shape_requests()
    reply = GetRectangles(display=d.display, opcode=d.display.get_extension_major("SHAPE"),
                          window=xid, region=region)
    return [(r.x, r.y, r.width, r.height) for r in reply.rectangles]


def has_shape_extension(d) -> bool:
    return "SHAPE" in d.list_extensions()
