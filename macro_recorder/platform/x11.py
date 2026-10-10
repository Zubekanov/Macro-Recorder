"""python-xlib helpers: EWMH, geometry, activation, SHAPE, XTest, XRecord,
XInput2 raw key events and XGrabKey.

This is the only module that speaks the X protocol directly.  The classes in
``windows.py`` and ``input.py`` wrap these helpers behind the Protocols in
``model/protocols.py``.

Conventions
-----------
- A window's ``rect`` is its outer frame: the client geometry grown by
  ``_NET_FRAME_EXTENTS`` when the window manager publishes it.
  ``client_rect`` is the client area in root coordinates.
- python-xlib ``Display`` objects are not thread-safe.  Every function takes
  the display it should use; anything that runs on its own thread opens its
  own connection.
- Without a window manager (bare Xvfb) the EWMH properties are absent and the
  functions fall back to the core protocol: ``query_tree`` for the window
  list, ``get_input_focus`` for the active window, ``configure`` and
  ``set_input_focus`` for move and activate.
- Keys are X keysym names as ``model/keys.py`` describes them.  The pure
  helpers (name tables, :class:`KeyboardMap`, mask building, the raw event
  struct, :class:`ServerClock`) need no display and are unit-tested.
"""

from __future__ import annotations

import logging
import select
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Collection, Iterable, Sequence

import Xlib.keysymdef
from Xlib import X, XK, Xatom
from Xlib.protocol import rq

from macro_recorder.model.keys import CHAR_KEYSYMS, NAMED_KEYS
from macro_recorder.model.protocols import Rect, WindowInfo

log = logging.getLogger(__name__)

SHAPE_BOUNDING = 0   # region that is painted
SHAPE_INPUT = 2      # region that receives pointer events (python-xlib has no constant)

SOURCE_PAGER = 2     # EWMH source indication: treat our requests like a pager's
_MOVERESIZE_X = 1 << 8
_MOVERESIZE_Y = 1 << 9
_MOVERESIZE_W = 1 << 10
_MOVERESIZE_H = 1 << 11
_STATE_REMOVE = 0
_STATE_ADD = 1
STICKY_DESKTOP = 0xFFFFFFFF

GENERIC_EVENT = 35            # X GenericEvent code
XI_RAW_KEY_PRESS = 13         # XI_RawKeyPress evtype
XTEST_KEYBOARD_NAME = "Virtual core XTEST keyboard"
TIME_PROPERTY = "_MACRO_RECORDER_TIME"

# Pointer buttons as X numbers them.
BUTTON_NUMBERS: dict[str, int] = {"left": 1, "middle": 2, "right": 3, "x1": 8, "x2": 9}
BUTTON_NAMES: dict[int, str] = {v: k for k, v in BUTTON_NUMBERS.items()}
# Scroll buttons -> (dx, dy) per notch; positive dy scrolls down.
SCROLL_BUTTONS: dict[int, tuple[int, int]] = {4: (0, -1), 5: (0, 1), 6: (-1, 0), 7: (1, 0)}


def scroll_button(dx: int, dy: int) -> int:
    """The X button for one scroll notch; exactly one of dx, dy is non-zero."""
    for button, step in SCROLL_BUTTONS.items():
        if step == (_sign(dx), _sign(dy)):
            return button
    raise ValueError("A scroll notch needs exactly one of dx, dy set to +1 or -1")


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _log_x_error(*args: Any) -> None:
    log.debug("X protocol error: %s", args[0] if args else "?")


def open_display(name: str | None = None):
    """Return a fresh ``Xlib.display.Display``.

    Raises OSError when ``DISPLAY`` is unset or the server cannot be reached.
    """
    from Xlib import display
    from Xlib.error import DisplayError

    try:
        d = display.Display(name)
    except (DisplayError, OSError, ValueError, OverflowError) as e:
        raise OSError("Cannot open X display: %s" % e) from e
    # Errors from requests without a reply (configure, send_event...) would
    # otherwise be printed to stderr by python-xlib's default handler.
    d.set_error_handler(_log_x_error)
    return d


def atom(d, name: str) -> int:
    """Intern ``name`` once per connection."""
    cache = d.__dict__.setdefault("_macro_recorder_atoms", {})
    if name not in cache:
        cache[name] = d.intern_atom(name)
    return cache[name]


def prop_ints(d, win, name: str) -> list[int] | None:
    """A 32-bit list property (WINDOW, CARDINAL, ATOM...) or None."""
    prop = win.get_full_property(atom(d, name), X.AnyPropertyType)
    if prop is None or prop.format != 32:
        return None
    return [int(v) for v in prop.value]


def wm_supports(d, name: str) -> bool:
    """True if the running window manager lists ``name`` in _NET_SUPPORTED."""
    cache = d.__dict__.setdefault("_macro_recorder_supported", {})
    if "atoms" not in cache:
        cache["atoms"] = set(prop_ints(d, d.screen().root, "_NET_SUPPORTED") or ())
    return atom(d, name) in cache["atoms"]


def window(d, xid: int):
    return d.create_resource_object("window", xid)


def _xid(value: Any) -> int:
    """A window id from a reply field that is either a Window or X.NONE."""
    return int(getattr(value, "id", value) or 0)


def bell(d) -> None:
    d.bell(0)
    d.flush()


def screen_size(d) -> tuple[int, int]:
    s = d.screen()
    return int(s.width_in_pixels), int(s.height_in_pixels)


def has_shape_extension(d) -> bool:
    return "SHAPE" in d.list_extensions()


# ---------------------------------------------------------------------------
# Geometry (pure)
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


def intersect(a: Rect, b: Rect) -> Rect | None:
    left, top = max(a.left, b.left), max(a.top, b.top)
    right, bottom = min(a.right, b.right), min(a.bottom, b.bottom)
    if right <= left or bottom <= top:
        return None
    return Rect(left, top, right - left, bottom - top)


# ---------------------------------------------------------------------------
# Reading windows
# ---------------------------------------------------------------------------

def window_title(d, win) -> str:
    """The window's _NET_WM_NAME, else WM_NAME, else an empty string."""
    prop = win.get_full_property(atom(d, "_NET_WM_NAME"), atom(d, "UTF8_STRING"))
    value = prop.value if prop is not None else None
    if not value:
        value = win.get_wm_name()
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return value or ""


def window_rects(d, win) -> tuple[Rect, Rect]:
    """``(outer, client)`` rectangles of ``win`` in root coordinates."""
    geom = win.get_geometry()
    pos = d.screen().root.translate_coords(win, 0, 0)
    client = Rect(int(pos.x), int(pos.y), int(geom.width), int(geom.height))
    outer = Rect(*outer_rect(client.left, client.top, client.width, client.height,
                             prop_ints(d, win, "_NET_FRAME_EXTENTS")))
    return outer, client


def window_desktop(d, win) -> int:
    """_NET_WM_DESKTOP; -1 for a sticky window, 0 when unknown."""
    value = prop_ints(d, win, "_NET_WM_DESKTOP")
    if not value:
        return 0
    return -1 if value[0] == STICKY_DESKTOP else value[0]


def is_maximised(d, win) -> bool:
    """True when _NET_WM_STATE holds both maximised atoms."""
    state = set(prop_ints(d, win, "_NET_WM_STATE") or ())
    return {atom(d, "_NET_WM_STATE_MAXIMIZED_VERT"),
            atom(d, "_NET_WM_STATE_MAXIMIZED_HORZ")} <= state


def read_window(d, xid: int, require_title: bool = True,
                require_viewable: bool = False) -> WindowInfo | None:
    """Snapshot a window, or None if it is gone (or untitled when required).

    Raises XError for errors other than a vanished window so the caller can
    drop a broken connection.
    """
    from Xlib.error import BadDrawable, BadMatch, BadWindow

    win = window(d, xid)
    try:
        if require_viewable and win.get_attributes().map_state != X.IsViewable:
            return None
        title = window_title(d, win)
        if require_title and not title:
            return None
        outer, client = window_rects(d, win)
        return WindowInfo(id=xid, title=title, rect=outer, client_rect=client,
                          desktop=window_desktop(d, win), maximised=is_maximised(d, win))
    except (BadWindow, BadDrawable, BadMatch) as e:
        log.debug("Could not read window %#x: %s", xid, e)
        return None


def list_windows(d) -> list[WindowInfo]:
    """All titled top-level windows, top-most first.

    Uses _NET_CLIENT_LIST_STACKING when a window manager provides it, else the
    viewable children of the root window.
    """
    root = d.screen().root
    ids = prop_ints(d, root, "_NET_CLIENT_LIST_STACKING")
    if ids is not None:
        candidates = [(xid, False) for xid in reversed(ids)]
    else:
        candidates = [(w.id, True) for w in reversed(root.query_tree().children)]
    found: list[WindowInfo] = []
    for xid, check_viewable in candidates:
        info = read_window(d, xid, require_viewable=check_viewable)
        if info is not None:
            found.append(info)
    return found


def toplevel_of(d, xid: int) -> int:
    """Climb from ``xid`` to the child of the root window that contains it."""
    from Xlib.error import XError

    root_id = d.screen().root.id
    win = window(d, xid)
    try:
        while True:
            parent = win.query_tree().parent
            if _xid(parent) in (0, root_id):
                return win.id
            win = parent
    except XError:
        return win.id


def active_window_id(d) -> int | None:
    """The active top-level window id, or None."""
    ids = prop_ints(d, d.screen().root, "_NET_ACTIVE_WINDOW")
    if ids is not None:
        return ids[0] if ids and ids[0] else None
    focus = d.get_input_focus().focus
    xid = _xid(focus)
    if xid in (X.NONE, X.PointerRoot):
        return None
    return toplevel_of(d, xid)


def _has_wm_state(d, win) -> bool:
    return win.get_full_property(atom(d, "WM_STATE"), X.AnyPropertyType) is not None


def window_at(d, x: int, y: int) -> int | None:
    """The top-level window under a screen point, or None over the bare root.

    Walks down from the root through the viewable windows containing the
    point and returns the first that carries WM_STATE (the client window
    under a reparenting window manager).  When the walk ends on a frame
    without reaching a client (the point is on the title bar or border),
    the client inside the frame is returned, as XmuClientWindow finds it.
    Otherwise the root's child (no window manager, or an override-redirect
    window such as the overlay).
    """
    from Xlib.error import XError

    root = d.screen().root
    top = _xid(root.translate_coords(root, x, y).child)
    if not top:
        return None
    current = window(d, top)
    try:
        for _ in range(16):
            if _has_wm_state(d, current):
                return current.id
            child = _xid(current.translate_coords(root, x, y).child)
            if not child:
                break
            current = window(d, child)
        return client_in(d, window(d, top)) or top
    except XError as e:
        log.debug("window_at(%d, %d) stopped early: %s", x, y, e)
    return top


def client_in(d, win, max_depth: int = 4) -> int | None:
    """The first window carrying WM_STATE below ``win``, breadth first, or None."""
    level = [win]
    for _ in range(max_depth):
        below = []
        for w in level:
            for child in w.query_tree().children:
                if _has_wm_state(d, child):
                    return _xid(child)
                below.append(child)
        if not below:
            break
        level = below
    return None


def current_desktop(d) -> int | None:
    value = prop_ints(d, d.screen().root, "_NET_CURRENT_DESKTOP")
    return value[0] if value else None


def work_area(d) -> Rect | None:
    """The current desktop's _NET_WORKAREA, or None without a window manager."""
    value = prop_ints(d, d.screen().root, "_NET_WORKAREA")
    if not value or len(value) < 4:
        return None
    desk = current_desktop(d) or 0
    if len(value) < 4 * (desk + 1):
        desk = 0
    left, top, width, height = value[4 * desk:4 * desk + 4]
    return Rect(left, top, width, height)


# ---------------------------------------------------------------------------
# Activation and placement
# ---------------------------------------------------------------------------

def create_time_window(d):
    """An unmapped InputOnly window whose PropertyNotify events give server time."""
    root = d.screen().root
    win = root.create_window(-10, -10, 1, 1, 0, 0, window_class=X.InputOnly,
                             visual=X.CopyFromParent, event_mask=X.PropertyChangeMask)
    d.flush()
    return win


def server_time(d, time_window, timeout_s: float = 0.5) -> int:
    """The X server's current time, read from a PropertyNotify on ``time_window``.

    Falls back to CurrentTime when no event arrives within ``timeout_s``.
    """
    prop = atom(d, TIME_PROPERTY)
    time_window.change_property(prop, Xatom.STRING, 8, b"", mode=X.PropModeAppend)
    d.flush()
    deadline = time.monotonic() + timeout_s
    while True:
        while d.pending_events():
            ev = d.next_event()
            if ev.type == X.PropertyNotify and _xid(ev.window) == time_window.id \
                    and ev.atom == prop:
                return int(ev.time)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.debug("No PropertyNotify for the server time; using CurrentTime")
            return X.CurrentTime
        select.select([d.fileno()], [], [], remaining)


def _client_message(d, target_xid: int, message: str, data: Sequence[int]) -> None:
    from Xlib import protocol

    values = [int(v) & 0xFFFFFFFF for v in data] + [0] * (5 - len(data))
    ev = protocol.event.ClientMessage(window=window(d, target_xid),
                                      client_type=atom(d, message), data=(32, values))
    d.screen().root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)


def activate(d, xid: int, desktop: int, timestamp: int) -> None:
    """Switch to the window's desktop unless sticky, then raise and focus it."""
    root = d.screen().root
    if wm_supports(d, "_NET_ACTIVE_WINDOW"):
        if desktop >= 0 and wm_supports(d, "_NET_CURRENT_DESKTOP") \
                and current_desktop(d) not in (None, desktop):
            _client_message(d, root.id, "_NET_CURRENT_DESKTOP", [desktop, timestamp])
        _client_message(d, xid, "_NET_ACTIVE_WINDOW",
                        [SOURCE_PAGER, timestamp, active_window_id(d) or 0])
    else:
        win = window(d, xid)
        win.map()
        win.configure(stack_mode=X.Above)
        win.set_input_focus(X.RevertToParent, timestamp)
    d.flush()


def move_resize(d, xid: int, rect: Rect) -> None:
    """Place the window's outer frame at ``rect``."""
    win = window(d, xid)
    cw, ch = client_size(rect.width, rect.height, prop_ints(d, win, "_NET_FRAME_EXTENTS"))
    if wm_supports(d, "_NET_MOVERESIZE_WINDOW"):
        flags = (X.NorthWestGravity | _MOVERESIZE_X | _MOVERESIZE_Y
                 | _MOVERESIZE_W | _MOVERESIZE_H | (SOURCE_PAGER << 12))
        _client_message(d, xid, "_NET_MOVERESIZE_WINDOW", [flags, rect.left, rect.top, cw, ch])
    else:
        win.configure(x=rect.left, y=rect.top, width=cw, height=ch)
    d.flush()


def set_maximised(d, xid: int, maximised: bool) -> bool:
    """Ask the window manager to (un)maximise; False when it has no _NET_WM_STATE."""
    if not wm_supports(d, "_NET_WM_STATE"):
        return False
    _client_message(d, xid, "_NET_WM_STATE", [
        _STATE_ADD if maximised else _STATE_REMOVE,
        atom(d, "_NET_WM_STATE_MAXIMIZED_VERT"), atom(d, "_NET_WM_STATE_MAXIMIZED_HORZ"),
        SOURCE_PAGER])
    d.flush()
    return True


# ---------------------------------------------------------------------------
# Active-window watching (own connection, own thread)
# ---------------------------------------------------------------------------

def watch_active_window(callback: Callable[[WindowInfo], None], stop_event: threading.Event,
                        poll_interval: float = 0.25, display_name: str | None = None,
                        ready: threading.Event | None = None) -> None:
    """Call ``callback`` for every change of active window, until ``stop_event``.

    The window active at the start is not reported; ``active_window_id``
    reads it.  ``ready`` is set once that starting window is known, so every
    change after it is reported.  A callback that raises is logged and watching goes on.  Wakes on _NET_ACTIVE_WINDOW property changes for a prompt reaction and
    re-checks every ``poll_interval`` seconds, which is what makes it work
    without a window manager as well.
    """
    d = open_display(display_name)
    try:
        root = d.screen().root
        root.change_attributes(event_mask=X.PropertyChangeMask)
        d.flush()
        last_id = active_window_id(d)
        if ready is not None:
            ready.set()
        fd = d.fileno()
        while not stop_event.is_set():
            readable, _, _ = select.select([fd], [], [], poll_interval)
            if readable or d.pending_events():
                while d.pending_events():
                    d.next_event()      # drain; the check below reads the property
            xid = active_window_id(d)
            if xid != last_id:
                last_id = xid
                info = read_window(d, xid) if xid else None
                if info is not None:
                    try:
                        callback(info)
                    except Exception:
                        log.exception("Active-window callback failed")
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
    from Xlib.protocol import structs

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
    inner = window(d, inner_xid)
    parent = inner.query_tree().parent
    if _xid(parent) in (0, d.screen().root.id):
        return inner_xid
    return _xid(parent)


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
    """The rectangles currently making up ``region`` of ``xid``."""
    _, GetRectangles = _shape_requests()
    reply = GetRectangles(display=d.display, opcode=d.display.get_extension_major("SHAPE"),
                          window=xid, region=region)
    return [(r.x, r.y, r.width, r.height) for r in reply.rectangles]


# ---------------------------------------------------------------------------
# Keysym names (pure)
# ---------------------------------------------------------------------------

for _group in Xlib.keysymdef.__all__:
    XK.load_keysym_group(_group)

# Names preferred when several share one keysym value.
_PREFERRED_NAMES: tuple[str, ...] = NAMED_KEYS + ("Mode_switch", "Multi_key", "ISO_Left_Tab")


# Standard keysyms python-xlib's tables lack.
_EXTRA_KEYSYMS: dict[str, int] = {
    "EcuSign": 0x20A0, "ColonSign": 0x20A1, "CruzeiroSign": 0x20A2, "FFrancSign": 0x20A3,
    "LiraSign": 0x20A4, "MillSign": 0x20A5, "NairaSign": 0x20A6, "PesetaSign": 0x20A7,
    "RupeeSign": 0x20A8, "WonSign": 0x20A9, "NewSheqelSign": 0x20AA, "DongSign": 0x20AB,
    "EuroSign": 0x20AC, "OE": 0x13BC, "oe": 0x13BD, "Ydiaeresis": 0x13BE,
}


def _build_name_tables() -> tuple[dict[str, int], dict[int, str]]:
    by_name: dict[str, int] = dict(_EXTRA_KEYSYMS)
    for attr, value in vars(XK).items():
        if not attr.startswith("XK_") or not isinstance(value, int):
            continue
        name = attr[3:]
        if name.startswith("XF86_"):
            name = "XF86" + name[5:]        # the canonical X spelling: XF86AudioMute
        by_name.setdefault(name, value)
    by_value: dict[int, str] = {}
    for name in sorted(by_name):
        by_value.setdefault(by_name[name], name)
    for name in _PREFERRED_NAMES:
        if name in by_name:
            by_value[by_name[name]] = name
    # Printable ASCII is stored as the character itself except for the names
    # model/keys.py lists in CHAR_KEYSYMS.
    for code in range(0x21, 0x7F):
        char = chr(code)
        by_value[code] = CHAR_KEYSYMS.get(char, char)
        by_name.setdefault(char, code)
    by_value[0x20] = "space"
    return by_name, by_value


_KEYSYM_BY_NAME, _NAME_BY_KEYSYM = _build_name_tables()


def keysym_from_name(name: str) -> int:
    """Keysym value for a name; 0 (NoSymbol) when unknown.

    Accepts the stored names (``a``, ``Return``, ``XF86AudioMute``), python-
    xlib spellings (``XF86_AudioMute``), single characters,
    ``U20AC`` Unicode names and ``0x1008ff12`` raw values.
    """
    if not name:
        return X.NoSymbol
    if name in _KEYSYM_BY_NAME:
        return _KEYSYM_BY_NAME[name]
    if len(name) == 1:
        return keysym_for_char(name)
    if name.startswith("XF86_") and "XF86" + name[5:] in _KEYSYM_BY_NAME:
        return _KEYSYM_BY_NAME["XF86" + name[5:]]
    if name[0] == "U" and len(name) >= 5:
        try:
            return 0x01000000 | int(name[1:], 16)
        except ValueError:
            return X.NoSymbol
    if name.lower().startswith("0x"):
        try:
            return int(name, 16)
        except ValueError:
            return X.NoSymbol
    return X.NoSymbol


def keysym_name(keysym: int) -> str:
    """The stored name of a keysym; ``U20AC`` or ``0x...`` when it has no name."""
    if keysym in _NAME_BY_KEYSYM:
        return _NAME_BY_KEYSYM[keysym]
    if keysym & 0xFF000000 == 0x01000000:
        return "U%04X" % (keysym & 0x00FFFFFF)
    return "0x%x" % keysym


def keysym_for_char(char: str) -> int:
    """The keysym that types ``char``: Latin-1 directly, else the Unicode keysym."""
    if char in CHAR_KEYSYMS:
        return _KEYSYM_BY_NAME[CHAR_KEYSYMS[char]]
    code = ord(char)
    if 0x20 <= code < 0x7F or 0xA0 <= code < 0x100:
        return code
    return 0x01000000 | code


# Legacy keysym pages that follow an 8-bit character set: (page, codec, first
# byte, last byte).  Keysym ``page | byte`` is the codec's character for
# ``byte``; only values that are named keysyms are used.
_LEGACY_PAGES: tuple[tuple[int, str, int, int], ...] = (
    (0x100, "iso8859_2", 0xA1, 0xFF),       # Latin-2
    (0x200, "iso8859_3", 0xA1, 0xFF),       # Latin-3
    (0x300, "iso8859_4", 0xA1, 0xFF),       # Latin-4
    (0x500, "iso8859_6", 0xAC, 0xF2),       # Arabic
    (0x600, "koi8_r", 0xC0, 0xFF),          # Cyrillic letters, in KOI8 order
    (0x700, "iso8859_7", 0xC1, 0xF9),       # Greek letters
    (0xC00, "iso8859_8", 0xDF, 0xFA),       # Hebrew
    (0xD00, "tis_620", 0xA1, 0xF9),         # Thai
)


def _build_legacy_tables() -> tuple[dict[str, int], dict[int, str]]:
    by_char: dict[str, int] = {}
    for page, codec, first, last in _LEGACY_PAGES:
        for byte in range(first, last + 1):
            keysym = page | byte
            if keysym not in _NAME_BY_KEYSYM:
                continue
            try:
                char = bytes([byte]).decode(codec)
            except UnicodeDecodeError:
                continue
            by_char.setdefault(char, keysym)
    # X puts the sigmas where ISO 8859-7 does not: Greek_SIGMA, Greek_sigma
    # and Greek_finalsmallsigma.
    by_char.update({"\u03a3": 0x7D2, "\u03c3": 0x7F2, "\u03c2": 0x7F3})
    for keysym in range(0x20A0, 0x20AD):                    # currency: EcuSign..EuroSign
        by_char.setdefault(chr(keysym), keysym)
    for keysym, char in ((0x13BC, "\u0152"), (0x13BD, "\u0153"), (0x13BE, "\u0178")):
        by_char.setdefault(char, keysym)                    # Latin-9: OE, oe, Ydiaeresis
    return by_char, {keysym: char for char, keysym in by_char.items()}


_LEGACY_BY_CHAR, _CHAR_BY_LEGACY = _build_legacy_tables()


def legacy_keysym_for_char(char: str) -> int | None:
    """The pre-Unicode keysym for ``char`` (EuroSign, Cyrillic_a), or None."""
    return _LEGACY_BY_CHAR.get(char)


def keysyms_for_char(char: str) -> list[int]:
    """Every keysym a keymap may use for ``char``: the Unicode form, then the legacy one."""
    keysyms = [keysym_for_char(char)]
    legacy = legacy_keysym_for_char(char)
    if legacy is not None and legacy not in keysyms:
        keysyms.append(legacy)
    return keysyms


def _keysym_char(keysym: int) -> str | None:
    if 0x20 <= keysym < 0x100:
        return chr(keysym)
    if keysym & 0xFF000000 == 0x01000000:
        return chr(keysym & 0x00FFFFFF)
    return _CHAR_BY_LEGACY.get(keysym)


def _char_keysym(char: str, legacy: bool) -> int | None:
    if legacy:
        return _LEGACY_BY_CHAR.get(char)
    code = ord(char)
    return code if code < 0x100 else 0x01000000 | code


def keysym_case(keysym: int) -> tuple[int, int] | None:
    """``(lower, upper)`` when the keysym is a letter with two cases, else None.

    Both keysyms are of the same kind as ``keysym``: legacy for a legacy one.
    """
    char = _keysym_char(keysym)
    if char is None:
        return None
    lower, upper = char.lower(), char.upper()
    if lower == upper or len(lower) != 1 or len(upper) != 1:
        return None
    legacy = keysym in _CHAR_BY_LEGACY
    pair = _char_keysym(lower, legacy), _char_keysym(upper, legacy)
    if pair[0] is None or pair[1] is None:
        return None
    return pair[0], pair[1]


def is_keypad(keysym: int) -> bool:
    return 0xFF80 <= keysym <= 0xFFBD


# ---------------------------------------------------------------------------
# Keyboard map (pure once loaded)
# ---------------------------------------------------------------------------

# Modifier family -> keysyms whose modifier bit stands for it.
_FAMILY_KEYSYMS: dict[str, tuple[str, ...]] = {
    "Alt": ("Alt_L", "Alt_R"),
    "Meta": ("Meta_L", "Meta_R"),
    "Super": ("Super_L", "Super_R"),
    "Hyper": ("Hyper_L", "Hyper_R"),
    "AltGr": ("ISO_Level3_Shift",),
    "NumLock": ("Num_Lock",),
    "ModeSwitch": ("Mode_switch",),
}

# Model modifier keysym -> family.
MODIFIER_FAMILIES: dict[str, str] = {
    "Control_L": "Control", "Control_R": "Control",
    "Shift_L": "Shift", "Shift_R": "Shift",
    "Alt_L": "Alt", "Alt_R": "Alt",
    "Meta_L": "Meta", "Meta_R": "Meta",
    "Super_L": "Super", "Super_R": "Super",
    "Hyper_L": "Hyper", "Hyper_R": "Hyper",
    "ISO_Level3_Shift": "AltGr",
}


class KeyboardMap:
    """The core keyboard mapping plus the modifier bits, as plain data.

    ``rows[i]`` holds the keysyms of keycode ``min_keycode + i`` in core
    order: group 1 levels 1-2, group 2 levels 1-2, group 1 levels 3-4,
    group 2 levels 3-4.  ``modifier_rows`` is GetModifierMapping's eight
    keycode lists (Shift, Lock, Control, Mod1..Mod5).
    """

    def __init__(self, min_keycode: int, rows: Sequence[Sequence[int]],
                 modifier_rows: Sequence[Sequence[int]]) -> None:
        self.min_keycode = min_keycode
        self.rows: list[list[int]] = [list(r) for r in rows]
        self.width = max((len(r) for r in self.rows), default=0)
        self.modifier_rows = [list(r) for r in modifier_rows]
        mask_by_keysym: dict[int, int] = {}
        for bit, keycodes in enumerate(self.modifier_rows):
            for kc in keycodes:
                for ks in self.syms(kc):
                    if ks:
                        mask_by_keysym.setdefault(ks, 1 << bit)
        self.family_masks: dict[str, int] = {"Shift": X.ShiftMask, "Control": X.ControlMask}
        for family, names in _FAMILY_KEYSYMS.items():
            mask = 0
            for name in names:
                mask = mask or mask_by_keysym.get(keysym_from_name(name), 0)
            self.family_masks[family] = mask
        if not self.family_masks["Alt"]:
            self.family_masks["Alt"] = X.Mod1Mask
        if not self.family_masks["Super"]:
            self.family_masks["Super"] = X.Mod4Mask

    @classmethod
    def from_display(cls, d) -> "KeyboardMap":
        info = d.display.info
        count = info.max_keycode - info.min_keycode + 1
        rows = d.get_keyboard_mapping(info.min_keycode, count)
        return cls(info.min_keycode, rows, d.get_modifier_mapping())

    @property
    def numlock_mask(self) -> int:
        return self.family_masks["NumLock"]

    @property
    def level3_mask(self) -> int:
        return self.family_masks["AltGr"]

    def keycodes(self) -> range:
        return range(self.min_keycode, self.min_keycode + len(self.rows))

    def syms(self, keycode: int) -> list[int]:
        i = keycode - self.min_keycode
        if 0 <= i < len(self.rows):
            return self.rows[i]
        return []

    def set_row(self, keycode: int, row: Sequence[int]) -> None:
        self.rows[keycode - self.min_keycode] = list(row)

    @staticmethod
    def _pair(syms: Sequence[int], index: int) -> tuple[int, int] | None:
        """The (level 1, level 2) keysyms at ``index`` with the core NoSymbol rules."""
        first = syms[index] if index < len(syms) else X.NoSymbol
        second = syms[index + 1] if index + 1 < len(syms) else X.NoSymbol
        if not first and not second:
            return None
        if not second:
            case = keysym_case(first)
            return case if case is not None and case[0] == first else (first, first)
        if not first:
            return second, second
        return first, second

    def keysym_at(self, keycode: int, state: int) -> int:
        """The keysym a KeyPress with ``keycode`` and ``state`` produces.

        Uses the XKB group from bits 13-14 and level 3 from the AltGr
        modifier, falling back to lower levels and group 1 like Xlib does.
        Letters follow XKB's ALPHABETIC type (Shift xor Caps Lock); with Num
        Lock on a keypad key's levels swap.
        """
        syms = self.syms(keycode)
        group = 1 if (state >> 13) & 3 else 0
        level3 = bool(self.level3_mask and state & self.level3_mask)
        candidates = []
        if level3:
            candidates.append(6 if group else 4)
        candidates.append(2 if group else 0)
        if group:
            if level3:
                candidates.append(4)
            candidates.append(0)
        pair = None
        for index in candidates:
            pair = self._pair(syms, index)
            if pair is not None:
                break
        if pair is None:
            return X.NoSymbol
        first, second = pair
        shift = bool(state & X.ShiftMask)
        if self.numlock_mask and state & self.numlock_mask and is_keypad(second):
            return first if shift else second
        case = keysym_case(first)
        if case is not None and case == (first, second):
            shift = shift != bool(state & X.LockMask)
        return second if shift else first

    def name_at(self, keycode: int, state: int) -> str:
        """Keysym name for a key event; ``0x...`` style for an unmapped keycode."""
        keysym = self.keysym_at(keycode, state)
        if not keysym:
            keysym = next((s for s in self.syms(keycode) if s), X.NoSymbol)
        if not keysym:
            return "keycode_%d" % keycode
        return keysym_name(keysym)

    def find(self, keysym: int) -> tuple[int, int] | None:
        """``(keycode, index)`` typing ``keysym`` in group 1.

        ``index`` is 0 (plain), 1 (Shift), 4 (Level 3) or 5 (Level 3 and
        Shift).  Lower indexes win, then lower keycodes.
        """
        best: tuple[int, int] | None = None
        for keycode in self.keycodes():
            syms = self.syms(keycode)
            for base in (0, 4):
                pair = self._pair(syms, base)
                if pair is None:
                    continue
                for level, value in enumerate(pair):
                    if value == keysym:
                        index = base + level
                        if best is None or index < best[1]:
                            best = (keycode, index)
                        break
            if best is not None and best[1] == 0:
                return best
        return best

    def keycode_for(self, keysym: int) -> int | None:
        found = self.find(keysym)
        return found[0] if found else None

    def spare_keycodes(self) -> list[int]:
        """Keycodes with no keysyms at all, usable for borrowing."""
        return [kc for kc in self.keycodes() if not any(self.syms(kc))]


def combo_mask(modifiers: Iterable[str], kmap: KeyboardMap) -> int:
    """Modifier state mask for the model's modifier keysym names.

    Raises ValueError for a name that is not a modifier or whose modifier is
    not bound in the keymap.
    """
    mask = 0
    for name in modifiers:
        family = MODIFIER_FAMILIES.get(name)
        if family is None:
            raise ValueError("%s is not a modifier" % name)
        bit = kmap.family_masks.get(family, 0)
        if not bit:
            raise ValueError("%s is not bound to a modifier in this keymap" % name)
        mask |= bit
    return mask


def lock_variants(kmap: KeyboardMap) -> list[int]:
    """Extra masks a grab needs so Caps Lock and Num Lock do not defeat it."""
    variants = [0, X.LockMask]
    if kmap.numlock_mask:
        variants += [kmap.numlock_mask, kmap.numlock_mask | X.LockMask]
    return variants


def clean_state(state: int, kmap: KeyboardMap) -> int:
    """Strip lock bits, buttons and the XKB group from an event state."""
    return state & 0xFF & ~(X.LockMask | kmap.numlock_mask)


# ---------------------------------------------------------------------------
# XTest injection
# ---------------------------------------------------------------------------

def has_xtest(d) -> bool:
    return d.has_extension("XTEST")


def fake_motion(d, x: int, y: int) -> None:
    d.xtest_fake_input(X.MotionNotify, 0, x=int(x), y=int(y))


def fake_button(d, button: int, press: bool) -> None:
    d.xtest_fake_input(X.ButtonPress if press else X.ButtonRelease, button)


def fake_key(d, keycode: int, press: bool) -> None:
    d.xtest_fake_input(X.KeyPress if press else X.KeyRelease, keycode)


class KeycodeBorrower:
    """Maps unmapped keysyms onto spare keycodes and restores them afterwards.

    A borrowed keycode keeps its keysym until :meth:`restore_all`, because a
    client translates a key event with the mapping it fetches after the
    MappingNotify, which could otherwise already be restored.  When no spare
    keycode is free the least recently used borrow that is not held is
    overwritten.
    """

    def __init__(self, d, kmap: KeyboardMap) -> None:
        self._d = d
        self.kmap = kmap
        self._borrowed: OrderedDict[int, int] = OrderedDict()   # keysym -> keycode

    @property
    def borrowed(self) -> dict[int, int]:
        return dict(self._borrowed)

    def borrow(self, keysym: int, busy: Collection[int] = ()) -> int | None:
        if keysym in self._borrowed:
            self._borrowed.move_to_end(keysym)
            return self._borrowed[keysym]
        spare = self.kmap.spare_keycodes()
        if spare:
            keycode = spare[0]
        else:
            victim = next((ks for ks, kc in self._borrowed.items() if kc not in busy), None)
            if victim is None:
                return None
            keycode = self._borrowed.pop(victim)
        row = [keysym] * max(1, self.kmap.width)
        self._d.change_keyboard_mapping(keycode, [row])
        self._d.sync()
        self.kmap.set_row(keycode, row)
        self._borrowed[keysym] = keycode
        return keycode

    def restore_all(self) -> None:
        if not self._borrowed:
            return
        empty = [X.NoSymbol] * max(1, self.kmap.width)
        for keycode in self._borrowed.values():
            self._d.change_keyboard_mapping(keycode, [empty])
            self.kmap.set_row(keycode, empty)
        self._borrowed.clear()
        self._d.sync()


# ---------------------------------------------------------------------------
# XRecord
# ---------------------------------------------------------------------------

class ServerClock:
    """Converts X server milliseconds to ``time.monotonic`` seconds.

    The offset is the smallest ``now - server`` seen, so delivery latency
    never makes events look later than they were.  A wrap of the 32-bit
    server time resets the offset.
    """

    def __init__(self) -> None:
        self._offset: float | None = None
        self._last: int | None = None

    def to_monotonic(self, server_ms: int, now: float) -> float:
        if self._last is not None and server_ms + 0x80000000 < self._last:
            self._offset = None
        self._last = server_ms
        offset = now - server_ms / 1000.0
        if self._offset is None or offset < self._offset:
            self._offset = offset
        return server_ms / 1000.0 + self._offset


class RecordContext:
    """Delivers core device events from every client through XRecord.

    Two connections of its own, like pynput's design: ``data`` blocks in
    RecordEnableContext on a background thread, ``control`` disables the
    context from :meth:`stop`.  ``handler`` receives python-xlib event
    objects on the record thread.  Other code may use ``control`` (for the
    keyboard map) while holding ``control_lock``.
    """

    def __init__(self, first_event: int, last_event: int, handler: Callable[[Any], None],
                 display_name: str | None = None) -> None:
        self._range = (first_event, last_event)
        self._handler = handler
        self._display_name = display_name
        self.control = None
        self.control_lock = threading.Lock()
        self._data = None
        self._context: int | None = None
        self._thread: threading.Thread | None = None
        self._live = threading.Event()

    def start(self, timeout_s: float = 2.0) -> None:
        """Start recording; returns once the server delivers data.  Raises OSError."""
        from Xlib.ext import record

        from Xlib.error import XError

        try:
            self.control = open_display(self._display_name)
            self._data = open_display(self._display_name)
            if not self._data.has_extension("RECORD"):
                raise OSError("The X server has no RECORD extension")
            self._context = self._data.record_create_context(0, [record.AllClients], [{
                "core_requests": (0, 0), "core_replies": (0, 0),
                "ext_requests": (0, 0, 0, 0), "ext_replies": (0, 0, 0, 0),
                "delivered_events": (0, 0), "device_events": self._range,
                "errors": (0, 0), "client_started": False, "client_died": False,
            }])
        except XError as e:
            self._close()
            raise OSError("Cannot create an XRecord context: %s" % e) from e
        except OSError:
            self._close()
            raise
        self._thread = threading.Thread(target=self._run, name="xrecord", daemon=True)
        self._thread.start()
        if not self._live.wait(timeout_s):
            self.stop()
            raise OSError("XRecord did not start")

    def _run(self) -> None:
        from Xlib.error import ConnectionClosedError, XError

        try:
            self._data.record_enable_context(self._context, self._on_reply)
        except (XError, ConnectionClosedError, OSError) as e:
            log.warning("XRecord stopped: %s", e)

    def _on_reply(self, reply) -> None:
        from Xlib.ext import record

        if reply.category == record.StartOfData:
            self._live.set()
            return
        if reply.category != record.FromServer or reply.client_swapped:
            return
        data = reply.data
        parser = rq.EventField(None)
        while len(data) >= 32:
            event, data = parser.parse_binary_value(data, self._data.display, None, None)
            try:
                self._handler(event)
            except Exception:
                log.exception("XRecord event handler failed")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        from Xlib.error import ConnectionClosedError, XError

        if self.control is not None and self._context is not None:
            try:
                with self.control_lock:
                    self.control.record_disable_context(self._context)
                    self.control.flush()
            except (XError, ConnectionClosedError, OSError) as e:
                log.debug("Could not disable the XRecord context: %s", e)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                log.warning("XRecord thread did not finish")
                return
        if self._data is not None and self._context is not None:
            try:
                self._data.record_free_context(self._context)
                self._data.flush()
            except (XError, ConnectionClosedError, OSError) as e:
                log.debug("Could not free the XRecord context: %s", e)
        self._close()

    def _close(self) -> None:
        for d in (self._data, self.control):
            if d is not None:
                try:
                    d.close()
                except Exception as e:
                    log.debug("Closing an XRecord connection failed: %s", e)
        self._data = self.control = None
        self._context = None


# ---------------------------------------------------------------------------
# XInput2 raw key events
# ---------------------------------------------------------------------------

# The body of an XI_RawKeyPress GenericEvent after its 10-byte header.  The
# valuator masks and values that follow are not needed and stay unparsed.
RAW_KEY_EVENT = rq.Struct(
    rq.Card16("deviceid"),
    rq.Card32("time"),
    rq.Card32("detail"),
    rq.Card16("sourceid"),
    rq.Card16("valuators_len"),
    rq.Card32("flags"),
    rq.Pad(4),
)


def xi2_version(d) -> tuple[int, int] | None:
    """The XInput version the server agrees to (asking for 2.2), or None."""
    from Xlib.ext import xinput

    if not d.has_extension(xinput.extname):
        return None
    reply = xinput.XIQueryVersion(display=d.display,
                                  opcode=d.display.get_extension_major(xinput.extname),
                                  major_version=2, minor_version=2)
    return int(reply.major_version), int(reply.minor_version)


def xtest_keyboard_ids(d) -> set[int]:
    """Device ids of the XTEST slave keyboards."""
    from Xlib.ext import xinput

    ids = set()
    for device in d.xinput_query_device(xinput.AllDevices).devices:
        name = device.name.decode("utf-8", "replace") if isinstance(device.name, bytes) \
            else str(device.name)
        if name == XTEST_KEYBOARD_NAME:
            ids.add(int(device.deviceid))
    return ids


def select_raw_key_presses(d) -> int:
    """Register the raw event decoder and select XI_RawKeyPress on the root.

    Returns the XInput major opcode, which raw events carry as ``extension``.
    """
    from Xlib.ext import xinput

    opcode = d.display.get_extension_major(xinput.extname)
    d.ge_add_event_data(opcode, XI_RAW_KEY_PRESS, RAW_KEY_EVENT)
    d.screen().root.xinput_select_events([(xinput.AllMasterDevices, xinput.RawKeyPressMask)])
    d.flush()
    return opcode


def is_user_raw_key(event: Any, opcode: int, keycodes: Collection[int],
                    synthetic_sources: Collection[int]) -> bool:
    """True for an XI_RawKeyPress of one of ``keycodes`` from a real keyboard."""
    if getattr(event, "type", None) != GENERIC_EVENT:
        return False
    if getattr(event, "extension", None) != opcode or getattr(event, "evtype", None) != XI_RAW_KEY_PRESS:
        return False
    data = event.data
    return int(data.detail) in keycodes and int(data.sourceid) not in synthetic_sources


# ---------------------------------------------------------------------------
# XGrabKey
# ---------------------------------------------------------------------------

def grab_key(d, keycode: int, mask: int, kmap: KeyboardMap) -> bool:
    """Grab ``keycode`` with ``mask`` on the root in every lock variant.

    Returns False, with nothing left grabbed, when another client already
    holds any variant (BadAccess).
    """
    from Xlib.error import BadAccess, CatchError

    root = d.screen().root
    catcher = CatchError(BadAccess)
    for extra in lock_variants(kmap):
        root.grab_key(keycode, mask | extra, False, X.GrabModeAsync, X.GrabModeAsync,
                      onerror=catcher)
    d.sync()
    if catcher.get_error() is not None:
        ungrab_key(d, keycode, mask, kmap)
        return False
    return True


def ungrab_key(d, keycode: int, mask: int, kmap: KeyboardMap) -> None:
    root = d.screen().root
    for extra in lock_variants(kmap):
        root.ungrab_key(keycode, mask | extra)
    d.sync()


# ---------------------------------------------------------------------------
# Pixel colour
# ---------------------------------------------------------------------------

def _channel(value: int, mask: int) -> int:
    if not mask:
        return 0
    shift = (mask & -mask).bit_length() - 1
    bits = (mask >> shift).bit_length()
    raw = (value & mask) >> shift
    return raw * 255 // ((1 << bits) - 1)


def pixel_to_rgb(data: bytes, bits_per_pixel: int, lsb_first: bool,
                 red_mask: int, green_mask: int, blue_mask: int) -> tuple[int, int, int]:
    """Decode one ZPixmap pixel of a TrueColor visual."""
    size = max(1, bits_per_pixel // 8)
    value = int.from_bytes(bytes(data[:size]), "little" if lsb_first else "big")
    return _channel(value, red_mask), _channel(value, green_mask), _channel(value, blue_mask)


def pixel_colour(d, x: int, y: int) -> str:
    """``#rrggbb`` of the root window pixel at (x, y), read with a 1x1 GetImage."""
    screen = d.screen()
    image = screen.root.get_image(x, y, 1, 1, X.ZPixmap, 0xFFFFFFFF)
    depth = image.depth
    bpp = next((f.bits_per_pixel for f in d.display.info.pixmap_formats if f.depth == depth), 32)
    visual = None
    for allowed in screen.allowed_depths:
        for v in allowed.visuals:
            if v.visual_id == image.visual or (visual is None and v.visual_id == screen.root_visual):
                visual = v
    if visual is None:
        return "#000000"
    r, g, b = pixel_to_rgb(image.data, bpp, d.display.info.image_byte_order == X.LSBFirst,
                           visual.red_mask, visual.green_mask, visual.blue_mask)
    return "#%02x%02x%02x" % (r, g, b)
