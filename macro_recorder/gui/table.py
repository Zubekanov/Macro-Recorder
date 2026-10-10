"""The action table: a Treeview over the document.

The Treeview item id of a row is its row id.  Document changes are applied
incrementally: structural changes insert, delete or move items, then every
row's cells and tags are recomputed and only the items whose text or tags
differ are touched.  Provisional rows shown while recording are not in the
document and use item ids starting with ``p``.
"""

from __future__ import annotations

import itertools
import logging
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import replace
from tkinter import ttk
from typing import Callable, Collection, Sequence

from macro_recorder.gui.commands import CommandRegistry
from macro_recorder.gui.text import visible_controls
from macro_recorder.model import catalogue
from macro_recorder.model.document import MacroDocument, Row
from macro_recorder.model.protocols import Change, Mode

log = logging.getLogger(__name__)

COLUMNS = ("number", "action", "value", "label", "comment")
HEADINGS = {"number": "#", "action": "Action", "value": "Value", "label": "Label",
            "comment": "Comment"}
WIDTHS = {"number": 56, "action": 120, "value": 300, "label": 90, "comment": 150}

GROUP_TINTS = ("#e8f0fe", "#e6f4ea", "#fef7e0", "#f3e8fd", "#e4f7fb", "#fce8ec")
PLAYING_BACKGROUND = "#ffd966"
DISABLED_FOREGROUND = "#8a8a8a"
RECORDING_FOREGROUND = "#a52a2a"
DROP_LINE_COLOUR = "#202020"

BREAKPOINT_MARK = "*"
DISABLED_MARK = "||"
PLAYING_MARK = ">"
PROVISIONAL_MARK = "+"

# Event.state bits that make a key a shortcut rather than a single-key insert.
SHORTCUT_STATE = 0x4 | 0x8 | 0x40          # Control, Mod1 (Alt), Mod4 (Super)
DRAG_THRESHOLD_PX = 5
LOCKED_TAG = "MacroRecorderLockedTable"
# Modes that hide the selection; it comes back by id on return to idle.
SAVING_MODES = (Mode.RECORDING, Mode.PLAYING, Mode.PAUSED)

EDIT_MENU = (
    ("edit.undo", "Undo"), ("edit.redo", "Redo"), None,
    ("edit.cut", "Cut"), ("edit.copy", "Copy"), ("edit.paste", "Paste"),
    ("edit.delete", "Delete"), None, ("edit.split", "Split path"),
    ("edit.toggle_enabled", "Toggle enabled"), ("edit.toggle_breakpoint", "Toggle breakpoint"),
    None, ("edit.select_all", "Select All"),
    None, ("edit.edit_label", "Edit label"), ("edit.edit_comment", "Edit comment"),
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def window_groups(rows: Sequence[Row]) -> dict[int, int]:
    """Row id -> window group number for rows inside a group.

    A group starts at an enabled window row and runs to the next window row;
    a disabled window row ends the previous group and starts none.
    """
    out: dict[int, int] = {}
    group: int | None = None
    count = 0
    for row in rows:
        if row.is_window:
            if row.enabled:
                group = count
                count += 1
            else:
                group = None
        if group is not None:
            out[row.id] = group
    return out


def number_text(index: int, row: Row, filtered: bool, playing: bool = False) -> str:
    """The number column: markers for playing, breakpoint and disabled, then the
    1-based number, bracketed when the filter skips the row."""
    marks = ""
    if playing:
        marks += PLAYING_MARK
    if row.breakpoint:
        marks += BREAKPOINT_MARK
    if not row.enabled:
        marks += DISABLED_MARK
    number = "[%d]" % (index + 1) if filtered else str(index + 1)
    return "%s %s" % (marks, number) if marks else number


def row_cells(index: int, row: Row, filtered: bool, playing: bool = False) -> tuple[str, ...]:
    """The five cells of a document row."""
    return cells(number_text(index, row, filtered, playing), row, "Row %d" % (index + 1))


def cells(number: str, row: Row, where: str) -> tuple[str, ...]:
    """The cells of ``row`` with ``number`` in the number column; a row that
    cannot be described shows ``?`` and is logged under ``where``."""
    spec = catalogue.spec_for(row)
    try:
        value = spec.describe(row)
    except Exception as e:          # a bad row must not break the table
        log.warning("%s: cannot describe the row: %s", where, e)
        value = "?"
    return (number, spec.name, visible_controls(value), row.label, _one_line(row.comment))


def row_tags(row: Row, group: int | None, filtered: bool) -> tuple[str, ...]:
    tags: list[str] = []
    if group is not None:
        tags.append("group%d" % (group % len(GROUP_TINTS)))
    if not row.enabled:
        tags.append("disabled")
    elif filtered:
        tags.append("filtered")
    return tuple(tags)


def drop_position(count: int, hovered: int | None, upper_half: bool, above_first: bool,
                  visible: tuple[int, int] | None = None) -> int:
    """Insertion position in the full list for a drop.

    Before the hovered row in its upper half, after it in its lower half.
    With no hovered row the drop goes to the nearest visible edge: before
    the first visible row when the pointer is above it, else after the last
    visible row.  ``visible`` is ``(first, last)`` visible indexes; without
    it the whole list counts as visible (0 above, ``count`` below).
    """
    if hovered is not None:
        return hovered if upper_half else hovered + 1
    if visible is None or count == 0:
        return 0 if above_first else count
    first, last = visible
    return first if above_first else last + 1


def remaining_index(position: int, moving: Collection[int]) -> int:
    """A full-list insertion position -> the index among the rows that do not move,
    as ``MacroDocument.move`` takes it."""
    return position - sum(1 for i in moving if i < position)


def shifted_index(indices: Collection[int], count: int, delta: int) -> int:
    """The ``MacroDocument.move`` index that shifts the selected rows (full-list
    ``indices``) one place up (``delta`` -1) or down (+1)."""
    ordered = sorted(indices)
    if delta < 0:
        return max(0, ordered[0] - 1)
    unselected_before_last = ordered[-1] - (len(ordered) - 1)
    return min(count - len(ordered), unselected_before_last + 1)


def _one_line(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# The widget
# ---------------------------------------------------------------------------

class _InlineEditor:
    """An Entry placed over a Label or Comment cell."""

    def __init__(self, entry: ttk.Entry, var: tk.StringVar, row_id: int, column: str) -> None:
        self.entry = entry
        self.var = var
        self.row_id = row_id
        self.column = column


class ActionTable(ttk.Frame):
    """Treeview over the document with drag reorder, inline editing and a context menu."""

    def __init__(self, master: tk.Misc, document: MacroDocument, commands: CommandRegistry,
                 on_select: Callable[[tuple[int, ...]], None]) -> None:
        super().__init__(master)
        self._document = document
        self._commands = commands
        self._on_select = on_select
        self._mode = Mode.IDLE
        self._cache: dict[int, tuple[tuple, tuple[tuple[str, ...], tuple[str, ...]]]] = {}
        self._playing: int | None = None
        self._saved_selection: tuple[int, ...] | None = None
        self._editor: _InlineEditor | None = None
        self._last_scroll: tuple[str, str] | None = None
        self._press: tuple[int, str] | None = None      # (y, iid) of a button press on a row
        self._press_keeps_selection = False
        self._drag_position: int | None = None
        self._provisional: list[Row] = []
        self._provisional_iids: list[str] = []
        self._provisional_base: int | None = None
        self._provisional_ids = itertools.count(1)

        self.tree = ttk.Treeview(self, columns=COLUMNS, show="headings", selectmode="extended")
        for column in COLUMNS:
            self.tree.heading(column, text=HEADINGS[column], anchor="w")
            self.tree.column(column, width=WIDTHS[column], anchor="w",
                             stretch=column in ("value", "comment"))
        self._scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self._on_yscroll)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self._scrollbar.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        italic = tkfont.nametofont("TkDefaultFont", root=self).copy()
        italic.configure(slant="italic")
        self._italic = italic
        for i, colour in enumerate(GROUP_TINTS):
            self.tree.tag_configure("group%d" % i, background=colour)
        self.tree.tag_configure("disabled", foreground=DISABLED_FOREGROUND)
        self.tree.tag_configure("filtered", font=italic)
        self.tree.tag_configure("recording", foreground=RECORDING_FOREGROUND)
        self.tree.tag_configure("playing", background=PLAYING_BACKGROUND)

        style = ttk.Style(self)
        style.configure("Invalid.TEntry", foreground="red")

        self._drop_line = tk.Frame(self.tree, height=2, background=DROP_LINE_COLOUR)
        self._context_menu = tk.Menu(self, tearoff=False)
        self._bind()
        self._normal_tags = self.tree.bindtags()
        self._locked_tags = (LOCKED_TAG, str(self.tree.winfo_toplevel()), "all")
        self._bind_locked_class()
        document.subscribe(self._on_change)
        self._rebuild()

    # -- public API ----------------------------------------------------------

    def selected_ids(self) -> tuple[int, ...]:
        """Selected row ids in document order (provisional rows excluded)."""
        index = self._document.row_index()
        ids = [int(i) for i in self.tree.selection() if i.isdigit() and int(i) in index]
        return tuple(sorted(ids, key=index.__getitem__))

    def select(self, ids: Sequence[int]) -> None:
        """Select rows by id, focus and show the first."""
        iids = [str(i) for i in ids if self.tree.exists(str(i))]
        self.tree.selection_set(iids)
        if iids:
            self.tree.focus(iids[0])
            self.tree.see(iids[0])

    def set_mode(self, mode: Mode) -> None:
        """Lock the table in every mode but idle; save and clear the selection
        while recording or playing and restore it on return to idle."""
        previous, self._mode = self._mode, mode
        self.cancel_inline_edit()
        self._end_drag()
        if mode is Mode.IDLE:
            self.tree.configure(selectmode="extended")
            self.tree.bindtags(self._normal_tags)
        else:
            self.tree.configure(selectmode="none")
            self.tree.bindtags(self._locked_tags)
        if mode in SAVING_MODES and previous not in SAVING_MODES:
            self._saved_selection = self.selected_ids()
            self.tree.selection_set(())
        elif mode is Mode.IDLE and self._saved_selection is not None:
            saved, self._saved_selection = self._saved_selection, None
            self.highlight(None)
            self.select(saved)

    def highlight(self, row_id: int | None) -> None:
        """Give ``row_id`` exactly the ``playing`` tag and the ``>`` marker; the
        previous row gets its computed look back."""
        previous, self._playing = self._playing, row_id
        if previous is not None and previous != row_id:
            self._refresh_row(previous)
        if row_id is not None and self.tree.exists(str(row_id)):
            self._refresh_row(row_id)
            self.tree.see(str(row_id))

    def show_provisional(self, rows: Sequence[Row]) -> None:
        """Show the live recording rows after the selection (or at the end).

        Rows that are the same objects as last time keep their items.
        """
        if self._provisional_base is None:
            saved = self._saved_selection or ()
            selected = [str(i) for i in saved if self.tree.exists(str(i))]
            children = self.tree.get_children()
            self._provisional_base = (max(children.index(i) for i in selected) + 1
                                      if selected else len(children))
        keep = 0
        while (keep < len(rows) and keep < len(self._provisional)
               and rows[keep] is self._provisional[keep]):
            keep += 1
        stale = self._provisional_iids[keep:]
        if stale:
            self.tree.delete(*stale)
        del self._provisional_iids[keep:]
        for offset, row in enumerate(rows[keep:], keep):
            iid = "p%d" % next(self._provisional_ids)
            self.tree.insert("", self._provisional_base + offset, iid=iid, tags=("recording",),
                             values=cells(PROVISIONAL_MARK, row, "Recorded row %d" % (offset + 1)))
            self._provisional_iids.append(iid)
        self._provisional = list(rows)
        if self._provisional_iids:
            self.tree.see(self._provisional_iids[-1])

    def clear_provisional(self) -> None:
        if self._provisional_iids:
            self.tree.delete(*self._provisional_iids)
        self._provisional = []
        self._provisional_iids = []
        self._provisional_base = None

    def begin_label_edit(self, row_id: int) -> None:
        self._begin_edit(row_id, "label")

    def begin_comment_edit(self, row_id: int) -> None:
        self._begin_edit(row_id, "comment")

    def cancel_inline_edit(self) -> None:
        editor, self._editor = self._editor, None
        if editor is not None:
            editor.entry.destroy()

    @property
    def editing(self) -> bool:
        return self._editor is not None

    @property
    def editor_entry(self) -> ttk.Entry | None:
        """The open inline editor's Entry (for tests)."""
        return self._editor.entry if self._editor is not None else None

    # -- document changes ----------------------------------------------------

    def _on_change(self, change: Change) -> None:
        self.cancel_inline_edit()
        if change.kind == "reset":
            self._rebuild()
            return
        if change.kind == "inserted":
            index = self._document.row_index()
            for row_id in sorted(change.ids, key=index.__getitem__):
                self._insert_item(index[row_id], self._document.rows[index[row_id]])
        elif change.kind == "removed":
            gone = [str(i) for i in change.ids if self.tree.exists(str(i))]
            if gone:
                self.tree.delete(*gone)
            for row_id in change.ids:
                self._cache.pop(row_id, None)
            if self._playing in change.ids:
                self._playing = None
        elif change.kind == "moved":
            self._reorder()
        self._sync()

    def _reorder(self) -> None:
        """Move items into document order, front to back, touching only misplaced ones."""
        current = [i for i in self.tree.get_children() if not i.startswith("p")]
        for position, row in enumerate(self._document.rows):
            iid = str(row.id)
            if current[position] != iid:
                self.tree.move(iid, "", position)
                current.remove(iid)
                current.insert(position, iid)

    def _rebuild(self) -> None:
        selected = self.selected_ids()
        self.tree.delete(*[i for i in self.tree.get_children() if not i.startswith("p")])
        self._cache.clear()
        if self._playing is not None and not self._exists_in_document(self._playing):
            self._playing = None
        for i, row in enumerate(self._document.rows):
            self._insert_item(i, row)
        self._sync()
        keep = [i for i in selected if self.tree.exists(str(i))]
        if keep:
            self.tree.selection_set([str(i) for i in keep])

    def _insert_item(self, index: int, row: Row) -> None:
        self.tree.insert("", index, iid=str(row.id), values=("",) * len(COLUMNS))

    def _exists_in_document(self, row_id: int) -> bool:
        return row_id in self._document.row_index()

    def _apply_look(self, index: int, row: Row, groups: dict[int, int]) -> None:
        """Update one item when anything its look depends on changed.

        Rows are immutable, so the row object, its position, filter state,
        playing state and group decide the look; the text is only rebuilt
        when one of them differs from the cached inputs.
        """
        filtered = row.enabled and not self._document.plays(row)
        playing = row.id == self._playing
        group = groups.get(row.id)
        inputs = (row, index, filtered, playing, group)
        cached = self._cache.get(row.id)
        if cached is not None and cached[0][0] is row and cached[0][1:] == inputs[1:]:
            return
        cells = row_cells(index, row, filtered, playing)
        tags = ("playing",) if playing else row_tags(row, group, filtered)
        if cached is None or cached[1] != (cells, tags):
            self.tree.item(str(row.id), values=cells, tags=tags)
        self._cache[row.id] = (inputs, (cells, tags))

    def _sync(self) -> None:
        """Recompute every row and touch only the items that changed."""
        rows = self._document.rows
        groups = window_groups(rows)
        for i, row in enumerate(rows):
            self._apply_look(i, row, groups)

    def _refresh_row(self, row_id: int) -> None:
        index = self._document.row_index()
        if row_id in index:
            i = index[row_id]
            self._apply_look(i, self._document.rows[i], window_groups(self._document.rows))

    # -- bindings ------------------------------------------------------------

    def _bind(self) -> None:
        tree = self.tree
        tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        tree.bind("<ButtonPress-1>", self._on_press)
        tree.bind("<B1-Motion>", self._on_motion)
        tree.bind("<ButtonRelease-1>", self._on_release)
        tree.bind("<Double-Button-1>", self._on_double)
        tree.bind("<Button-3>", self._on_context)
        tree.bind("<Configure>", lambda e: self.cancel_inline_edit())
        shortcuts = {
            "x": "edit.cut", "c": "edit.copy", "v": "edit.paste", "a": "edit.select_all",
            "e": "edit.toggle_enabled",
        }
        for letter, name in shortcuts.items():
            for key in (letter, letter.upper()):
                tree.bind("<Control-%s>" % key, lambda e, n=name: self._run(n))
        plain = {"<Delete>": "edit.delete", "<F8>": "edit.toggle_breakpoint",
                 "<Alt-Up>": "edit.move_up", "<Alt-Down>": "edit.move_down",
                 "<Return>": "edit.edit_label", "<KP_Enter>": "edit.edit_label",
                 "<F2>": "edit.edit_comment"}
        for sequence, name in plain.items():
            tree.bind(sequence, lambda e, n=name: self._run(n))
        for _spec, preset in catalogue.presets():
            if not preset.insert_key:
                continue
            name = preset_command(preset.name)
            for key in {preset.insert_key.lower(), preset.insert_key.upper()}:
                tree.bind("<KeyPress-%s>" % key, lambda e, n=name: self._insert_key(e, n))

    def _bind_locked_class(self) -> None:
        """The locked table only scrolls."""
        tree = self.tree
        tree.bind_class(LOCKED_TAG, "<MouseWheel>",
                        lambda e: e.widget.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        tree.bind_class(LOCKED_TAG, "<Button-4>", lambda e: e.widget.yview_scroll(-1, "units"))
        tree.bind_class(LOCKED_TAG, "<Button-5>", lambda e: e.widget.yview_scroll(1, "units"))

    def _run(self, name: str) -> str:
        self._commands.run(name)
        return "break"

    def _insert_key(self, event: tk.Event, name: str) -> str | None:
        if int(event.state) & SHORTCUT_STATE:
            return None
        self._commands.run(name)
        return "break"

    def _on_tree_select(self, _event: tk.Event) -> None:
        if self._mode is not Mode.IDLE:
            return
        self._on_select(self.selected_ids())

    def _on_yscroll(self, first: str, last: str) -> None:
        if self._last_scroll is not None and self._last_scroll != (first, last):
            self.cancel_inline_edit()
        self._last_scroll = (first, last)
        self._scrollbar.set(first, last)

    # -- mouse: drag reorder and breakpoints -------------------------------

    def _on_press(self, event: tk.Event) -> str | None:
        region = self.tree.identify_region(event.x, event.y)
        if region in ("separator", "heading"):
            self.cancel_inline_edit()
            self._press = None
            return None
        iid = self.tree.identify_row(event.y)
        if not iid or iid.startswith("p"):
            self._press = None
            return None
        self._press = (event.y, iid)
        self._drag_position = None
        selection = self.tree.selection()
        modifiers = int(event.state) & (0x1 | 0x4)
        # Keep a multi-row selection when pressing on one of its rows, so it can be dragged.
        self._press_keeps_selection = not modifiers and len(selection) > 1 and iid in selection
        if self._press_keeps_selection:
            self.tree.focus_set()
            return "break"
        return None

    def _on_motion(self, event: tk.Event) -> str | None:
        if self._press is None:
            return None
        if self._drag_position is None and abs(event.y - self._press[0]) < DRAG_THRESHOLD_PX:
            return "break" if self._press_keeps_selection else None
        self.cancel_inline_edit()
        height = self.tree.winfo_height()
        if event.y < 8:
            self.tree.yview_scroll(-1, "units")
        elif event.y > height - 8:
            self.tree.yview_scroll(1, "units")
        self._drag_position, line_y = self._drop_at(event.y)
        self._drop_line.place(x=0, y=max(0, line_y - 1), relwidth=1.0)
        self._drop_line.lift()
        return "break"

    def _drop_at(self, y: int) -> tuple[int, int]:
        """(full-list position, line y) for a pointer y inside the tree."""
        rows = [i for i in self.tree.get_children() if not i.startswith("p")]
        index = {iid: n for n, iid in enumerate(rows)}
        hovered = self.tree.identify_row(y)
        box = self.tree.bbox(hovered) if hovered in index else ""
        if box:
            _x, top, _width, height = box
            upper = y < top + height / 2
            position = drop_position(len(rows), index[hovered], upper, False)
            return position, top if upper else top + height
        # Nothing visible under the pointer (the heading, or below the last
        # row): drop at the nearest visible edge, not at the far end of a
        # scrolled table.
        first, last = self._visible_rows(index)
        if first is None or last is None:
            return 0, 0
        first_box, last_box = self.tree.bbox(first), self.tree.bbox(last)
        above = y < first_box[1]
        position = drop_position(len(rows), None, False, above, (index[first], index[last]))
        return position, first_box[1] if above else last_box[1] + last_box[3]

    def _visible_rows(self, index: dict[str, int]) -> tuple[str | None, str | None]:
        """The first and last document rows with a visible bounding box."""
        height = self.tree.winfo_height()
        first = last = None
        for y in range(0, height, 2):
            iid = self.tree.identify_row(y)
            if iid in index and self.tree.bbox(iid):
                first = iid
                break
        for y in range(height - 1, -1, -2):
            iid = self.tree.identify_row(y)
            if iid in index and self.tree.bbox(iid):
                last = iid
                break
        return first, last

    def _on_release(self, event: tk.Event) -> str | None:
        press, position = self._press, self._drag_position
        self._end_drag()
        if press is None:
            return None
        if position is None:
            if self._press_keeps_selection:
                self.tree.selection_set(press[1])
                self.tree.focus(press[1])
            return None
        ids = self.selected_ids()
        if not ids:
            return "break"
        index = self._document.row_index()
        target = remaining_index(position, [index[i] for i in ids])
        self._document.move(ids, target)
        self.select(ids)
        return "break"

    def _end_drag(self) -> None:
        self._press = None
        self._drag_position = None
        self._drop_line.place_forget()

    def _on_double(self, event: tk.Event) -> str | None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        iid = self.tree.identify_row(event.y)
        if not iid or not iid.isdigit():
            return None
        column = self.tree.column(self.tree.identify_column(event.x), "id")
        row_id = int(iid)
        if column == "number":
            self.tree.selection_set(iid)
            self._commands.run("edit.toggle_breakpoint")
        elif column == "label":
            self.begin_label_edit(row_id)
        elif column == "comment":
            self.begin_comment_edit(row_id)
        return "break"

    # -- context menu --------------------------------------------------------

    def _on_context(self, event: tk.Event) -> str:
        iid = self.tree.identify_row(event.y)
        if iid and iid.isdigit() and iid not in self.tree.selection():
            self.tree.selection_set(iid)
            self.tree.focus(iid)
        self.tree.focus_set()
        self._build_context_menu()
        # On X11 tk_popup returns once the menu is posted, holding a global
        # grab that unposts the menu on a click elsewhere: no grab_release.
        self._context_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _build_context_menu(self) -> None:
        menu = self._context_menu
        for child in menu.winfo_children():
            child.destroy()
        menu.delete(0, "end")
        add = tk.Menu(menu, tearoff=False)
        by_category: dict[str, tk.Menu] = {}
        for spec, preset in catalogue.presets():
            sub = by_category.get(spec.category)
            if sub is None:
                sub = tk.Menu(add, tearoff=False)
                by_category[spec.category] = sub
                add.add_cascade(label=spec.category, menu=sub)
            name = preset_command(preset.name)
            sub.add_command(label=preset.name, accelerator=preset.insert_key or "",
                            command=lambda n=name: self._commands.run(n),
                            state="normal" if self._commands.enabled(name) else "disabled")
        menu.add_cascade(label="Add", menu=add)
        menu.add_separator()
        for item in EDIT_MENU:
            if item is None:
                menu.add_separator()
                continue
            name, label = item
            command = self._commands.get(name)
            menu.add_command(label=label, accelerator=command.accelerator or "",
                             command=lambda n=name: self._commands.run(n),
                             state="normal" if self._commands.enabled(name) else "disabled")

    # -- inline editing ------------------------------------------------------

    def _begin_edit(self, row_id: int, column: str) -> None:
        self.cancel_inline_edit()
        if self._mode is not Mode.IDLE or not self._exists_in_document(row_id):
            return
        iid = str(row_id)
        self.tree.see(iid)
        self.tree.update_idletasks()
        box = self.tree.bbox(iid, column)
        if not box:
            return
        x, y, width, height = box
        row = self._document.row(row_id)
        var = tk.StringVar(self, value=row.label if column == "label" else row.comment)
        entry = ttk.Entry(self.tree, textvariable=var)
        entry.place(x=x, y=y, width=max(width, 80), height=height)
        editor = _InlineEditor(entry, var, row_id, column)
        self._editor = editor
        if column == "label":
            var.trace_add("write", lambda *a: self._validate_label(editor))
        entry.bind("<Return>", lambda e: self._commit_edit(editor, keep_open_if_invalid=True))
        entry.bind("<KP_Enter>", lambda e: self._commit_edit(editor, keep_open_if_invalid=True))
        entry.bind("<Escape>", lambda e: self._cancel_from_entry(editor))
        entry.bind("<FocusOut>", lambda e: self._commit_edit(editor, keep_open_if_invalid=False))
        entry.focus_set()
        entry.select_range(0, "end")
        entry.icursor("end")

    def commit_inline_edit(self) -> None:
        """Commit the open inline editor as Return would; an invalid label is
        dropped rather than kept open.  Nothing happens when none is open."""
        editor = self._editor
        if editor is not None:
            self._commit_edit(editor, keep_open_if_invalid=False)

    def _validate_label(self, editor: _InlineEditor) -> None:
        if self._editor is not editor:
            return
        problem = self._document.rename_problem(editor.row_id, editor.var.get().strip())
        editor.entry.configure(style="Invalid.TEntry" if problem else "TEntry")

    def _cancel_from_entry(self, editor: _InlineEditor) -> str:
        if self._editor is editor:
            self.cancel_inline_edit()
            self.tree.focus_set()
        return "break"

    def _commit_edit(self, editor: _InlineEditor, keep_open_if_invalid: bool) -> str | None:
        if self._editor is not editor:
            return None
        text = editor.var.get().strip() if editor.column == "label" else editor.var.get()
        if not self._exists_in_document(editor.row_id):
            self.cancel_inline_edit()
            return "break"
        row = self._document.row(editor.row_id)
        self._document.end_edit()
        if editor.column == "label":
            problem = self._document.rename_problem(editor.row_id, text)
            if problem:
                log.warning("Label not changed: %s", problem)
                if keep_open_if_invalid:
                    self.bell()
                    return "break"
                self.cancel_inline_edit()
                return "break"
            self.cancel_inline_edit()
            self.tree.focus_set()
            if text != row.label:
                self._document.rename_label(row.id, text)
        else:
            self.cancel_inline_edit()
            self.tree.focus_set()
            if text != row.comment:
                self._document.replace_row(replace(row, comment=text))
        return "break"


def preset_command(preset_name: str) -> str:
    """The command name that inserts a preset."""
    return "add:" + preset_name
