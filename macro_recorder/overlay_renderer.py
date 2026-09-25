"""Full-screen overlay that previews the selected action on screen.

OverlayRenderer owns a transparent, click-through Toplevel + Canvas spanning the
virtual desktop and draws a small marker for the currently selected row (a
trajectory line for moves, a crosshair for clicks, a dot for a single target,
or a start→end line for timed moves).  It is created lazily on first draw and
torn down by :meth:`hide`.

Transparency is platform-specific:

- Windows: Tk's ``-transparentcolor`` keys out the white background, and the
  colour-keyed area is click-through by itself.
- X11 (Linux Mint and other Linux desktops): Tk has no colour keying, so after
  every draw the window's SHAPE bounding region is set to just the drawn
  marks and its input region to nothing.  Everything outside the marks is
  neither painted nor clickable.  If the X server lacks the SHAPE extension the
  overlay stays disabled rather than covering the screen with an opaque window.

Coordinates passed to the draw methods are absolute screen coordinates; the
renderer converts them to canvas-local coordinates using the virtual-screen
origin.
"""

from __future__ import annotations

import logging
import sys
import tkinter as tk
from typing import Iterable

from macro_recorder.macro import MacroEvent

log = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# Overlay marker styling.
_OVERLAY_COLOR = "#e74c3c"        # primary red: lines, end markers, click cross
_OVERLAY_START_COLOR = "#2ecc71"  # green: start marker of a movement
_OVERLAY_LINE_WIDTH = 2
_OVERLAY_DOT_RADIUS = 5           # move/target/endpoint dots
_OVERLAY_CLICK_RADIUS = 14        # click crosshair circle

# X11 shaping: how finely a line is sampled and how much slack each sample gets.
_SHAPE_STEP = 4
_SHAPE_PAD = 3


def _get_virtual_screen() -> tuple[int, int, int, int]:
    """Return (x, y, width, height) of the bounding rectangle across all monitors.

    Uses Windows GetSystemMetrics so that secondary monitors are included.
    Returns (0, 0, 0, 0) elsewhere; the caller then uses Tk's screen size,
    which on X11 already spans every monitor of the root window.
    """
    if not _IS_WINDOWS:
        return 0, 0, 0, 0
    try:
        import ctypes
        gm = ctypes.windll.user32.GetSystemMetrics
        return gm(76), gm(77), gm(78), gm(79)  # SM_X/Y/CX/CY VIRTUALSCREEN
    except Exception:
        return 0, 0, 0, 0


def _make_click_through(hwnd: int) -> None:
    """Add WS_EX_NOACTIVATE so the overlay never steals keyboard focus.

    WS_EX_TRANSPARENT is intentionally NOT set here: on DWM-composited Windows,
    combining it with WS_EX_LAYERED stops the window from painting, producing a
    solid black overlay.  The colour-key transparency set by tkinter's
    -transparentcolor already makes white-background areas click-through via
    LWA_COLORKEY, so WS_EX_TRANSPARENT is redundant and harmful.
    """
    try:
        import ctypes
        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE = 0x08000000
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# X11 shaping
# ---------------------------------------------------------------------------

Rect = tuple[int, int, int, int]


def shape_rectangles_for_items(items: Iterable[tuple[str, list[float], float]],
                               step: int = _SHAPE_STEP, pad: int = _SHAPE_PAD) -> list[Rect]:
    """Rectangles covering the given canvas items, for an X11 bounding shape.

    ``items`` are ``(type, coords, width)`` triples as Tk reports them:
    ``oval``/``rectangle`` carry a bounding box, ``line`` a flat point list.
    Ovals are covered by their box, rectangle outlines by four strips, and
    lines by small squares sampled every ``step`` pixels along each segment.
    Every rectangle is padded by ``pad`` so anti-aliasing and Tk's smoothing
    never paint outside the shape.
    """
    rects: list[Rect] = []

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
            box(x0 - half, y0 - half, x1 + half, y0 + half)   # top edge
            box(x0 - half, y1 - half, x1 + half, y1 + half)   # bottom edge
            box(x0 - half, y0 - half, x0 + half, y1 + half)   # left edge
            box(x1 - half, y0 - half, x1 + half, y1 + half)   # right edge
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


def _canvas_items(canvas: tk.Canvas) -> list[tuple[str, list[float], float]]:
    out = []
    for item in canvas.find_all():
        kind = canvas.type(item)
        coords = [float(c) for c in canvas.coords(item)]
        try:
            width = float(canvas.itemcget(item, "width"))
        except tk.TclError:
            width = 1.0
        out.append((kind, coords, width))
    return out


class _X11Shaper:
    """Applies SHAPE regions to the overlay's X window."""

    def __init__(self, display, xid: int) -> None:
        self._display = display
        self._xid = xid

    @classmethod
    def create(cls, toplevel: tk.Toplevel) -> "_X11Shaper | None":
        """Shape ``toplevel`` invisible and click-through; None if unsupported."""
        try:
            from macro_recorder import x11
            display = x11.open_display()
            if not x11.has_shape_extension(display):
                display.close()
                log.warning("Overlay disabled: the X server has no SHAPE extension")
                return None
            xid = x11.tk_toplevel_xid(display, toplevel.winfo_id())
            x11.set_window_shape(display, xid, [], [])
            return cls(display, xid)
        except Exception as e:
            log.warning("Overlay disabled: %s", e)
            return None

    def apply(self, rects: list[Rect]) -> None:
        from macro_recorder import x11
        try:
            x11.set_window_shape(self._display, self._xid, rects, [])
        except Exception as e:
            log.debug("Overlay shape update failed: %s", e)

    def close(self) -> None:
        try:
            self._display.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

class OverlayRenderer:
    """Manages the transparent preview overlay and draws action markers."""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._overlay: tk.Toplevel | None = None
        self._canvas: tk.Canvas | None = None
        self._shaper: _X11Shaper | None = None
        self._offset: tuple[int, int] = (0, 0)
        # Auto-hide whenever the application loses focus, so nothing drawn lingers
        # on screen after the user switches away.  Bound once here, so EVERY
        # draw_* automatically inherits the behaviour.
        root.bind("<FocusOut>", self._on_app_focus_out, add="+")

    # ------------------------------------------------------------------
    # Focus handling
    # ------------------------------------------------------------------

    def _on_app_focus_out(self, _event=None) -> None:
        if self._overlay is None:
            return
        # Defer so focus_get() reflects the post-change state, then hide only if
        # no widget in this application holds focus (i.e. another app was
        # activated).  Internal focus moves (table → details panel, the targeting
        # window, etc.) keep focus inside the app and leave the overlay alone.
        self._root.after(1, self._hide_if_app_unfocused)

    def _hide_if_app_unfocused(self) -> None:
        try:
            focused = self._root.focus_get()
        except (KeyError, tk.TclError):
            return   # uncertain — leave the overlay as-is
        if focused is None:
            self.hide()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ensure_canvas(self) -> tk.Canvas | None:
        """Return the overlay canvas, creating the Toplevel window if needed.

        Returns None when the platform cannot make the overlay transparent and
        click-through, in which case nothing is drawn.
        """
        if self._overlay is None:
            vx, vy, vw, vh = _get_virtual_screen()
            if vw == 0:
                vx, vy = 0, 0
                vw = self._root.winfo_screenwidth()
                vh = self._root.winfo_screenheight()
            self._offset = (vx, vy)

            ov = tk.Toplevel(self._root)
            ov.geometry(f"{vw}x{vh}+{vx}+{vy}")
            ov.overrideredirect(True)
            ov.config(bg="white")           # window background must also be white
            ov.attributes("-topmost", True)
            if _IS_WINDOWS:
                ov.attributes("-transparentcolor", "white")
            canvas = tk.Canvas(ov, bg="white", highlightthickness=0, bd=0)
            canvas.pack(fill=tk.BOTH, expand=True)
            ov.update_idletasks()
            if _IS_WINDOWS:
                _make_click_through(ov.winfo_id())
            else:
                self._shaper = _X11Shaper.create(ov)
                if self._shaper is None:
                    ov.destroy()
                    return None

            self._overlay = ov
            self._canvas = canvas
        return self._canvas

    def _commit(self, canvas: tk.Canvas) -> None:
        """Push the drawn marks to the X11 shape after a draw (no-op elsewhere)."""
        if self._shaper is not None:
            canvas.update_idletasks()
            self._shaper.apply(shape_rectangles_for_items(_canvas_items(canvas)))

    def hide(self) -> None:
        """Destroy the overlay window if it is showing."""
        if self._shaper is not None:
            self._shaper.close()
            self._shaper = None
        if self._overlay is not None:
            self._overlay.destroy()
            self._overlay = None
            self._canvas = None

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def draw_move(self, group: list[MacroEvent]) -> None:
        """Draw a trajectory line through a bundled mouse-move row."""
        canvas = self._ensure_canvas()
        if canvas is None:
            return
        canvas.delete("all")
        ox, oy = self._offset
        coords = [(ev.x - ox, ev.y - oy) for ev in group if ev.x is not None]
        if len(coords) >= 2:
            flat = [v for xy in coords for v in xy]
            canvas.create_line(*flat, fill=_OVERLAY_COLOR, width=_OVERLAY_LINE_WIDTH, smooth=True)
        r = _OVERLAY_DOT_RADIUS
        if coords:
            x0, y0 = coords[0]
            canvas.create_oval(x0 - r, y0 - r, x0 + r, y0 + r, fill=_OVERLAY_START_COLOR, outline="")
        if len(coords) > 1:
            xn, yn = coords[-1]
            canvas.create_oval(xn - r, yn - r, xn + r, yn + r, fill=_OVERLAY_COLOR, outline="")
        self._commit(canvas)

    def draw_click(self, ev: MacroEvent) -> None:
        """Draw a crosshair at a click position."""
        canvas = self._ensure_canvas()
        if canvas is None:
            return
        canvas.delete("all")
        ox, oy = self._offset
        x, y = ev.x - ox, ev.y - oy
        r = _OVERLAY_CLICK_RADIUS
        canvas.create_oval(x - r, y - r, x + r, y + r, outline=_OVERLAY_COLOR, width=_OVERLAY_LINE_WIDTH)
        canvas.create_line(x - r, y - r, x + r, y + r, fill=_OVERLAY_COLOR, width=_OVERLAY_LINE_WIDTH)
        canvas.create_line(x + r, y - r, x - r, y + r, fill=_OVERLAY_COLOR, width=_OVERLAY_LINE_WIDTH)
        self._commit(canvas)

    def draw_target(self, ev: MacroEvent) -> None:
        """Draw a single target dot for a manual mouse-move."""
        canvas = self._ensure_canvas()
        if canvas is None:
            return
        canvas.delete("all")
        ox, oy = self._offset
        x, y = ev.x - ox, ev.y - oy
        r = _OVERLAY_DOT_RADIUS
        canvas.create_oval(x - r, y - r, x + r, y + r, fill=_OVERLAY_COLOR, outline="")
        self._commit(canvas)

    def draw_timed_move(self, ev: MacroEvent) -> None:
        """Draw a start→end line for a mouse_move_timed action.

        dx/dy hold the absolute To-coordinate (not a delta).
        """
        canvas = self._ensure_canvas()
        if canvas is None:
            return
        canvas.delete("all")
        ox, oy = self._offset
        x1, y1 = ev.x - ox, ev.y - oy
        x2, y2 = (ev.dx or 0) - ox, (ev.dy or 0) - oy
        canvas.create_line(x1, y1, x2, y2, fill=_OVERLAY_COLOR, width=_OVERLAY_LINE_WIDTH, smooth=True)
        r = _OVERLAY_DOT_RADIUS
        canvas.create_oval(x1 - r, y1 - r, x1 + r, y1 + r, fill=_OVERLAY_START_COLOR, outline="")
        canvas.create_oval(x2 - r, y2 - r, x2 + r, y2 + r, fill=_OVERLAY_COLOR, outline="")
        self._commit(canvas)

    def draw_region(self, ev: MacroEvent) -> None:
        """Draw the OCR capture rectangle from (x,y) to the absolute (dx,dy)."""
        canvas = self._ensure_canvas()
        if canvas is None:
            return
        canvas.delete("all")
        ox, oy = self._offset
        x1, y1 = ev.x - ox, ev.y - oy
        x2, y2 = (ev.dx or 0) - ox, (ev.dy or 0) - oy
        canvas.create_rectangle(x1, y1, x2, y2, outline=_OVERLAY_COLOR, width=_OVERLAY_LINE_WIDTH)
        self._commit(canvas)
