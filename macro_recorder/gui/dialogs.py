"""Dialogs: unsaved-changes prompt, error and information boxes, message_box.

Modal dialogs count themselves in :func:`modal_active`, which the hotkey
router reads from its own thread to ignore global hotkeys meanwhile.  File
choosers go through :func:`ask_open_path` and :func:`ask_save_path` for the
same reason.
"""

from __future__ import annotations

import threading
import tkinter as tk
from contextlib import contextmanager
from tkinter import filedialog, ttk
from typing import Callable, Iterator, Sequence

from macro_recorder.gui.playback import ask_buttons

_modal_lock = threading.Lock()
_modal_depth = 0

FILE_TYPES = (("Macro files", "*.json"), ("All files", "*"))


def modal_active() -> bool:
    """True while a modal dialog or file chooser is open.  Safe from any thread."""
    with _modal_lock:
        return _modal_depth > 0


@contextmanager
def modal() -> Iterator[None]:
    """Count a modal interaction for :func:`modal_active`."""
    global _modal_depth
    with _modal_lock:
        _modal_depth += 1
    try:
        yield
    finally:
        with _modal_lock:
            _modal_depth -= 1


class _ButtonDialog(tk.Toplevel):
    """A small dialog: message text and a row of buttons, each with an answer.

    Return picks the first button, Escape and the window's close button the
    last.
    """

    def __init__(self, master: tk.Misc, title: str, message: str,
                 buttons: Sequence[tuple[str, str]], on_answer: Callable[[str], None] | None = None,
                 topmost: bool = False) -> None:
        super().__init__(master)
        self.title(title)
        self.answer: str = buttons[-1][1]
        self._on_answer = on_answer
        self._closed = False
        self.resizable(False, False)
        if topmost:
            self.attributes("-topmost", True)
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=message, wraplength=420, justify="left").pack(anchor="w", pady=(0, 12))
        row = ttk.Frame(body)
        row.pack(anchor="e")
        self.buttons: dict[str, ttk.Button] = {}
        for text, answer in buttons:
            button = ttk.Button(row, text=text, command=lambda a=answer: self.choose(a))
            button.pack(side="left", padx=(6, 0))
            self.buttons[answer] = button
        first = buttons[0][1]
        last = buttons[-1][1]
        self.bind("<Return>", lambda e: self.choose(first))
        self.bind("<KP_Enter>", lambda e: self.choose(first))
        self.bind("<Escape>", lambda e: self.choose(last))
        self.protocol("WM_DELETE_WINDOW", lambda: self.choose(last))
        self.buttons[first].focus_set()
        _place_over(self, master)

    def choose(self, answer: str) -> None:
        if self._closed:
            return
        self._closed = True
        self.answer = answer
        self.destroy()
        if self._on_answer is not None:
            self._on_answer(answer)

    def close(self) -> None:
        """Close without answering (playback stopped)."""
        if not self._closed:
            self._closed = True
            self.destroy()


def _place_over(dialog: tk.Toplevel, master: tk.Misc) -> None:
    """Centre the dialog over the master's toplevel when that is visible."""
    dialog.update_idletasks()
    top = master.winfo_toplevel()
    if not top.winfo_viewable():
        return
    x = top.winfo_rootx() + (top.winfo_width() - dialog.winfo_reqwidth()) // 2
    y = top.winfo_rooty() + (top.winfo_height() - dialog.winfo_reqheight()) // 3
    dialog.geometry("+%d+%d" % (max(0, x), max(0, y)))


def _run_modal(dialog: _ButtonDialog) -> str:
    master = dialog.master
    if isinstance(master, tk.Misc) and master.winfo_toplevel().winfo_viewable():
        dialog.transient(master.winfo_toplevel())
    with modal():
        dialog.wait_visibility()
        dialog.grab_set()
        dialog.wait_window()
    return dialog.answer


def ask_unsaved(master: tk.Misc, name: str) -> str:
    """Save / Don't save / Cancel for a document with unsaved changes.

    Returns ``"save"``, ``"discard"`` or ``"cancel"``.
    """
    dialog = _ButtonDialog(master, "Unsaved changes", "Save changes to %s?" % name,
                           (("Save", "save"), ("Don't save", "discard"), ("Cancel", "cancel")))
    return _run_modal(dialog)


def show_message(master: tk.Misc, title: str, message: str) -> None:
    """A modal message with an OK button (errors, About)."""
    _run_modal(_ButtonDialog(master, title, message, (("OK", "ok"),)))


class MessageBoxDialog(_ButtonDialog):
    """The message_box action's dialog: topmost and not modal, so the pump keeps
    running.  Closing it counts as the last button (Cancel or No)."""

    def __init__(self, master: tk.Misc, title: str, text: str, buttons: str,
                 on_answer: Callable[[str], None]) -> None:
        super().__init__(master, title or "Macro Recorder", text, ask_buttons(buttons),
                         on_answer=on_answer, topmost=True)
        self.lift()
        self.focus_force()


def ask_open_path(master: tk.Misc, folder: str) -> str:
    """A macro file to open, or "" when cancelled."""
    with modal():
        path = filedialog.askopenfilename(parent=master, initialdir=folder or None,
                                          filetypes=FILE_TYPES, title="Open")
    return str(path or "")


def ask_save_path(master: tk.Misc, folder: str, name: str) -> str:
    """Where to save, or "" when cancelled."""
    with modal():
        path = filedialog.asksaveasfilename(parent=master, initialdir=folder or None,
                                            initialfile=name, defaultextension=".json",
                                            filetypes=FILE_TYPES, title="Save As")
    return str(path or "")
