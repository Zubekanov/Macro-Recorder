"""Full-screen overlay that previews the selected action on screen.

OverlayRenderer owns a transparent, click-through Toplevel + Canvas spanning the
virtual desktop and draws a small marker for the currently selected row (a
trajectory line for moves, a crosshair for clicks, a dot for a single target,
or a start→end line for timed moves).  It is created lazily on first draw and
torn down by :meth:`hide`.

Coordinates passed to the draw methods are absolute screen coordinates; the
renderer converts them to canvas-local coordinates using the virtual-screen
origin.
"""

from __future__ import annotations

import tkinter as tk

from macro_recorder.macro import MacroEvent

# Overlay marker styling.
_OVERLAY_COLOR = "#e74c3c"        # primary red: lines, end markers, click cross
_OVERLAY_START_COLOR = "#2ecc71"  # green: start marker of a movement
_OVERLAY_LINE_WIDTH = 2
_OVERLAY_DOT_RADIUS = 5           # move/target/endpoint dots
_OVERLAY_CLICK_RADIUS = 14        # click crosshair circle


def _get_virtual_screen() -> tuple[int, int, int, int]:
    """Return (x, y, width, height) of the bounding rectangle across all monitors.

    Uses Windows GetSystemMetrics so that secondary monitors are included.
    Falls back to (0, 0, 0, 0) on other platforms (filled in at the call site).
    """
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


class OverlayRenderer:
    """Manages the transparent preview overlay and draws action markers."""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._overlay: tk.Toplevel | None = None
        self._canvas: tk.Canvas | None = None
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

    def _ensure_canvas(self) -> tk.Canvas:
        """Return the overlay canvas, creating the Toplevel window if needed."""
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
            ov.attributes("-transparentcolor", "white")
            canvas = tk.Canvas(ov, bg="white", highlightthickness=0, bd=0)
            canvas.pack(fill=tk.BOTH, expand=True)
            ov.update_idletasks()
            _make_click_through(ov.winfo_id())

            self._overlay = ov
            self._canvas = canvas
        return self._canvas

    def hide(self) -> None:
        """Destroy the overlay window if it is showing."""
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

    def draw_click(self, ev: MacroEvent) -> None:
        """Draw a crosshair at a click position."""
        canvas = self._ensure_canvas()
        canvas.delete("all")
        ox, oy = self._offset
        x, y = ev.x - ox, ev.y - oy
        r = _OVERLAY_CLICK_RADIUS
        canvas.create_oval(x - r, y - r, x + r, y + r, outline=_OVERLAY_COLOR, width=_OVERLAY_LINE_WIDTH)
        canvas.create_line(x - r, y - r, x + r, y + r, fill=_OVERLAY_COLOR, width=_OVERLAY_LINE_WIDTH)
        canvas.create_line(x + r, y - r, x - r, y + r, fill=_OVERLAY_COLOR, width=_OVERLAY_LINE_WIDTH)

    def draw_target(self, ev: MacroEvent) -> None:
        """Draw a single target dot for a manual mouse-move."""
        canvas = self._ensure_canvas()
        canvas.delete("all")
        ox, oy = self._offset
        x, y = ev.x - ox, ev.y - oy
        r = _OVERLAY_DOT_RADIUS
        canvas.create_oval(x - r, y - r, x + r, y + r, fill=_OVERLAY_COLOR, outline="")

    def draw_timed_move(self, ev: MacroEvent) -> None:
        """Draw a start→end line for a mouse_move_timed action.

        dx/dy hold the absolute To-coordinate (not a delta).
        """
        canvas = self._ensure_canvas()
        canvas.delete("all")
        ox, oy = self._offset
        x1, y1 = ev.x - ox, ev.y - oy
        x2, y2 = (ev.dx or 0) - ox, (ev.dy or 0) - oy
        canvas.create_line(x1, y1, x2, y2, fill=_OVERLAY_COLOR, width=_OVERLAY_LINE_WIDTH, smooth=True)
        r = _OVERLAY_DOT_RADIUS
        canvas.create_oval(x1 - r, y1 - r, x1 + r, y1 + r, fill=_OVERLAY_START_COLOR, outline="")
        canvas.create_oval(x2 - r, y2 - r, x2 + r, y2 + r, fill=_OVERLAY_COLOR, outline="")

    def draw_region(self, ev: MacroEvent) -> None:
        """Draw the OCR capture rectangle from (x,y) to the absolute (dx,dy)."""
        canvas = self._ensure_canvas()
        canvas.delete("all")
        ox, oy = self._offset
        x1, y1 = ev.x - ox, ev.y - oy
        x2, y2 = (ev.dx or 0) - ox, (ev.dy or 0) - oy
        canvas.create_rectangle(x1, y1, x2, y2, outline=_OVERLAY_COLOR, width=_OVERLAY_LINE_WIDTH)
