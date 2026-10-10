"""Click-through overlay that marks the selected row's position on screen.

An override-redirect Tk toplevel covers the screen; its X SHAPE bounding
region is cut down to the drawn marks and its input region is empty, so
nothing outside the marks is painted and nothing at all catches the
pointer.  The window is shaped while still unmapped and only then shown.
Without the SHAPE extension the overlay stays off rather than covering the
screen with an opaque window.

The geometry (:func:`overlay_items`, :func:`shape_rectangles_for_items`,
:func:`clip_rectangles`) is Tk-free and unit-tested.
"""

from __future__ import annotations

import logging
import tkinter as tk
from dataclasses import dataclass
from typing import Iterable, Sequence

from macro_recorder.model.protocols import OverlayShape
from macro_recorder.platform import x11
from macro_recorder.platform.session import OwnWindows

log = logging.getLogger(__name__)

MARK_COLOUR = "#e74c3c"         # lines, end dots, the click cross
START_COLOUR = "#2ecc71"        # the start dot of a movement
LINE_WIDTH = 2
DOT_RADIUS = 5
CLICK_RADIUS = 14
SHAPE_STEP = 4                  # a line is covered by a square every this many pixels
SHAPE_PAD = 3                   # slack around every square for Tk's line ends

Rectangle = tuple[int, int, int, int]       # x, y, width, height


@dataclass(frozen=True)
class Item:
    """One canvas item: ``kind`` is a Tk canvas type (oval, line, rectangle)."""
    kind: str
    coords: tuple[float, ...]
    width: float
    fill: str = ""
    outline: str = ""


def _dot(x: float, y: float, colour: str) -> Item:
    r = DOT_RADIUS
    return Item("oval", (x - r, y - r, x + r, y + r), 1, fill=colour)


def overlay_items(shape: OverlayShape | None) -> list[Item]:
    """The canvas items that draw ``shape`` (screen coordinates)."""
    if shape is None or not shape.points:
        return []
    points = [(float(x), float(y)) for x, y in shape.points]
    (x0, y0), (xn, yn) = points[0], points[-1]
    if shape.kind == "point":
        return [_dot(x0, y0, MARK_COLOUR)]
    if shape.kind == "click":
        r = CLICK_RADIUS
        return [
            Item("oval", (x0 - r, y0 - r, x0 + r, y0 + r), LINE_WIDTH, outline=MARK_COLOUR),
            Item("line", (x0 - r, y0 - r, x0 + r, y0 + r), LINE_WIDTH, fill=MARK_COLOUR),
            Item("line", (x0 + r, y0 - r, x0 - r, y0 + r), LINE_WIDTH, fill=MARK_COLOUR),
        ]
    if shape.kind in ("line", "path"):
        items = []
        if len(points) > 1:
            flat = tuple(v for point in points for v in point)
            items.append(Item("line", flat, LINE_WIDTH, fill=MARK_COLOUR))
        items.append(_dot(x0, y0, START_COLOUR))
        if len(points) > 1:
            items.append(_dot(xn, yn, MARK_COLOUR))
        return items
    if shape.kind == "rect" and len(points) >= 2:
        return [Item("rectangle", (x0, y0, xn, yn), LINE_WIDTH, outline=MARK_COLOUR)]
    return []


def shape_rectangles_for_items(items: Iterable[tuple[str, Sequence[float], float]],
                               step: int = SHAPE_STEP, pad: int = SHAPE_PAD) -> list[Rectangle]:
    """Rectangles covering canvas items, for an X SHAPE bounding region.

    ``items`` are ``(kind, coords, width)`` triples: ``oval`` and
    ``rectangle`` carry a bounding box, ``line`` a flat point list.  Ovals
    are covered by their box, rectangle outlines by four strips, and lines
    by squares sampled every ``step`` pixels along each segment.  Every
    rectangle is padded by ``pad``.
    """
    rects: list[Rectangle] = []

    def box(x0: float, y0: float, x1: float, y1: float) -> None:
        left, top = int(min(x0, x1)) - pad, int(min(y0, y1)) - pad
        right, bottom = int(max(x0, x1)) + pad + 1, int(max(y0, y1)) + pad + 1
        rects.append((left, top, right - left, bottom - top))

    for kind, coords, width in items:
        half = width / 2.0
        if kind == "oval" and len(coords) == 4:
            x0, y0, x1, y1 = coords
            box(x0 - half, y0 - half, x1 + half, y1 + half)
        elif kind == "rectangle" and len(coords) == 4:
            x0, y0, x1, y1 = coords
            box(x0 - half, y0 - half, x1 + half, y0 + half)     # top edge
            box(x0 - half, y1 - half, x1 + half, y1 + half)     # bottom edge
            box(x0 - half, y0 - half, x0 + half, y1 + half)     # left edge
            box(x1 - half, y0 - half, x1 + half, y1 + half)     # right edge
        elif kind == "line" and len(coords) >= 2:
            points = list(zip(coords[0::2], coords[1::2]))
            if len(points) == 1:
                points.append(points[0])
            for (ax, ay), (bx, by) in zip(points, points[1:]):
                length = max(abs(bx - ax), abs(by - ay))
                samples = max(1, int(length // step)) if length > 0 else 0
                for i in range(samples + 1):
                    t = i / samples if samples else 0.0
                    px, py = ax + (bx - ax) * t, ay + (by - ay) * t
                    box(px - half, py - half, px + half, py + half)
    return rects


def clip_rectangles(rects: Iterable[Rectangle], width: int, height: int) -> list[Rectangle]:
    """Rectangles cut to the screen, empty ones and duplicates dropped, order kept."""
    out: list[Rectangle] = []
    seen: set[Rectangle] = set()
    for x, y, w, h in rects:
        left, top = max(0, x), max(0, y)
        right, bottom = min(width, x + w), min(height, y + h)
        if right <= left or bottom <= top:
            continue
        rect = (left, top, right - left, bottom - top)
        if rect not in seen:
            seen.add(rect)
            out.append(rect)
    return out


class Overlay:
    """The overlay window, created on the first shape it has to show.

    It hides when disabled, on :meth:`hide`, and while no widget of the
    application has focus; it comes back on focus with the last shape.
    """

    def __init__(self, root: tk.Tk, own_windows: OwnWindows, enabled: bool) -> None:
        self._root = root
        self._own_windows = own_windows
        self._enabled = enabled
        self._shape: OverlayShape | None = None
        self._window: tk.Toplevel | None = None
        self._canvas: tk.Canvas | None = None
        self._display = None
        self._xid: int | None = None
        self._unavailable = False
        self._visible = False
        self._closed = False
        root.bind("<FocusOut>", self._on_focus_out, add="+")
        root.bind("<FocusIn>", self._on_focus_in, add="+")

    # ------------------------------------------------------------ public API

    def show(self, shape: OverlayShape | None) -> None:
        """Draw ``shape``; None hides the overlay.

        While the application has no focus the shape is only kept, and drawn
        when the focus comes back.
        """
        self._shape = shape
        if shape is None or not self._enabled or self._closed or not self._has_focus():
            self._withdraw()
            return
        self._draw(shape)

    def hide(self) -> None:
        """Hide and forget the shape until the next :meth:`show`."""
        self._shape = None
        self._withdraw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self._withdraw()
        elif self._shape is not None and not self._closed and self._has_focus():
            self._draw(self._shape)

    def close(self) -> None:
        """Destroy the window and close the overlay's display connection."""
        self._closed = True
        self._visible = False
        if self._xid is not None:
            self._own_windows.remove(self._xid)
            self._xid = None
        if self._window is not None:
            self._window.destroy()
            self._window = None
            self._canvas = None
        if self._display is not None:
            try:
                self._display.close()
            except Exception as e:      # the connection may already be gone
                log.debug("Closing the overlay display failed: %s", e)
            self._display = None

    # ------------------------------------------------------------- internals

    def _ensure_window(self) -> bool:
        if self._window is not None:
            return True
        if self._unavailable:
            return False
        try:
            display = x11.open_display()
        except OSError as e:
            log.warning("Overlay disabled: %s", e)
            self._unavailable = True
            return False
        if not x11.has_shape_extension(display):
            display.close()
            log.warning("Overlay disabled: the X server has no SHAPE extension")
            self._unavailable = True
            return False
        width, height = self._root.winfo_screenwidth(), self._root.winfo_screenheight()
        window = tk.Toplevel(self._root)
        window.withdraw()
        window.overrideredirect(True)
        window.geometry("%dx%d+0+0" % (width, height))
        canvas = tk.Canvas(window, width=width, height=height, background="white",
                           highlightthickness=0, borderwidth=0)
        canvas.pack(fill="both", expand=True)
        window.update_idletasks()
        window.wm_frame()           # creates Tk's wrapper window, which is what gets mapped
        window.winfo_pointerxy()    # a round trip, so the wrapper exists for our connection
        try:
            xid = x11.tk_toplevel_xid(display, window.winfo_id())
            x11.set_window_shape(display, xid, [], [])
        except Exception as e:      # any X error leaves the overlay off
            log.warning("Overlay disabled: %s", e)
            window.destroy()
            display.close()
            self._unavailable = True
            return False
        self._own_windows.add(xid, overlay=True)
        self._display, self._xid, self._window, self._canvas = display, xid, window, canvas
        return True

    def _draw(self, shape: OverlayShape) -> None:
        if not self._ensure_window():
            return
        assert self._canvas is not None and self._window is not None
        items = overlay_items(shape)
        canvas = self._canvas
        canvas.delete("all")
        for item in items:
            if item.kind == "oval":
                canvas.create_oval(*item.coords, width=item.width, fill=item.fill,
                                   outline=item.outline)
            elif item.kind == "rectangle":
                canvas.create_rectangle(*item.coords, width=item.width, outline=item.outline)
            elif item.kind == "line":
                canvas.create_line(*item.coords, width=item.width, fill=item.fill)
        rects = clip_rectangles(
            shape_rectangles_for_items((i.kind, i.coords, i.width) for i in items),
            self._root.winfo_screenwidth(), self._root.winfo_screenheight())
        try:
            x11.set_window_shape(self._display, self._xid, rects, [])
        except Exception as e:      # a lost connection or an X error hides the marks
            log.warning("Overlay shape update failed: %s", e)
            self._withdraw()
            return
        if not rects:
            self._withdraw()
            return
        if not self._visible:
            self._window.deiconify()
            self._window.lift()
            self._visible = True

    def _withdraw(self) -> None:
        if self._window is not None and self._visible:
            self._window.withdraw()
        self._visible = False

    def _on_focus_out(self, _event: tk.Event) -> None:
        if self._visible:
            self._root.after(1, self._hide_if_unfocused)

    def _has_focus(self) -> bool:
        """Whether a widget of the application has the keyboard focus.

        ``focus_get`` raises KeyError for a focused widget Tk cannot name,
        such as a menu's internal clone; that still counts as focused.
        """
        try:
            return self._root.focus_get() is not None
        except (KeyError, tk.TclError):
            return True

    def _hide_if_unfocused(self) -> None:
        if not self._has_focus():
            self._withdraw()

    def _on_focus_in(self, _event: tk.Event) -> None:
        if not self._visible and self._enabled and self._shape is not None \
                and not self._closed:
            self._draw(self._shape)
