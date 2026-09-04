import collections
import copy
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pyautogui
from macro_recorder.bundling import bundle_events
from macro_recorder.event_types import EventType
from macro_recorder.expressions import ExpressionError, evaluate
from macro_recorder.macro import MacroEvent, MacroGroup, load_macro, save_macro
from macro_recorder.overlay_renderer import OverlayRenderer
from macro_recorder.table_model import TableModel
from macro_recorder.recorder import Recorder
from macro_recorder.player import (
    DYNAMIC_VALUES,
    GOTO_END,
    GOTO_START,
    MacroExecutionError,
    Player,
    WindowNotFoundError,
)


TABS = ["Record", "Playback", "Help"]
TABLE_COLUMNS = ("num", "Action", "Value", "Label")
TABLE_COL_WIDTHS = {"num": 36, "Action": 160, "Value": 200, "Label": 150}

# Mouse-move events separated by more than this gap are treated as distinct
# movement segments and get a 'wait' display row inserted between them.
_MOVE_BUNDLE_GAP_S: float = 0.5  # 500 ms

# Mouse click down/up pairs within this gap are bundled into a single "click" row.
_CLICK_BUNDLE_GAP_S: float = 0.25  # 250 ms

# How often to poll the recorder for new events during live recording.
_POLL_INTERVAL_MS: int = 100        # 10 Hz

# Playback row-highlighting pump.  The player reports the row it is about to
# run on every instruction; those rows are queued (deduped against the last
# one) and a main-thread pump paints one per tick.  This shows fast execution
# as flicker through the hit rows (a useful "this row ran" indicator) while a
# row the player dwells on (a wait) is the only thing hit during its dwell, so
# it stays highlighted.  The bounded queue keeps a tight loop from lagging.
_HIGHLIGHT_PUMP_MS: int = 50
_ACTIVE_QUEUE_MAX: int = 64
# Cap on buffered log lines awaiting paint, and on lines kept in the panel.
_LOG_QUEUE_MAX: int = 5000
_LOG_PANEL_MAX_LINES: int = 5000

# Light, gentle background colours assigned per distinct window scope.
_WINDOW_PALETTE = [
    "#d4e8d4", "#d4d8e8", "#e8d4d4", "#e8e8d4",
    "#d4e8e8", "#e8d4e8", "#dce8d4", "#d4dde8",
]

# Screen-overlay drawing (selection preview for mouse actions).
# Readable labels for the table's Action column (display only; the underlying
# event type drives all logic).  Keyed by EventType, but plain-string types
# from loaded files look up fine too (str-enum equality).
_ACTION_DISPLAY_NAMES = {
    EventType.MOUSE_MOVE: "Move Mouse",
    EventType.MOUSE_MOVE_TIMED: "Move Point-to-Point",
    EventType.MOUSE_CLICK: "Mouse Click",
    EventType.MOUSE_SCROLL: "Scroll",
    EventType.KEY_PRESS: "Key Press",
    EventType.KEY_RELEASE: "Key Release",
    EventType.TYPE_TEXT: "Type Text",
    EventType.WAIT: "Wait",
    EventType.WINDOW_FOCUS: "Window Focus",
    EventType.VAR_SET: "Set Variable",
    EventType.GOTO: "Go To",
    EventType.GOTO_IF: "Conditional Go To",
    EventType.OCR_READ: "Read Screen (OCR)",
    EventType.MATCH_IMAGE: "Wait for Image",
    EventType.MATCH_TEXT: "Wait for Text",
}


def _action_label(event_type) -> str:
    """Readable Action-column name for an event type."""
    return _ACTION_DISPLAY_NAMES.get(event_type, str(event_type))


def _goto_target_text(ev) -> str:
    """Readable goto destination: an instruction number (``#N``) or a label."""
    if ev.target_index is not None:
        return f"#{ev.target_index}"
    return str(ev.target)


def _format_duration_label(duration) -> str:
    """Human label for a duration field.

    Numeric durations are stored in seconds and shown in ms (or s if >=1000ms);
    expression (string) durations are entered in ms and shown verbatim.
    """
    if isinstance(duration, str):
        return f"{duration} ms"
    if duration is None:
        return "0 ms"
    ms = duration * 1000
    return f"{ms:.0f} ms" if ms < 1000 else f"{duration:.3f} s"


def _duration_field_text(duration) -> str:
    """Initial text for an editable 'Duration (ms)' entry.

    Numeric durations (stored in seconds) are shown in ms; expression durations
    (stored as a ms-expression string) are shown verbatim.
    """
    if isinstance(duration, str):
        return duration
    return f"{(duration or 0) * 1000:.0f}"


# Default timings used when inserting actions and re-anchoring timestamps.
_DEFAULT_MOVE_DURATION_S = 0.5    # mouse_move_timed default travel time
_DEFAULT_WAIT_DURATION_S = 0.5    # wait/pause default
_CLICK_PAIR_GAP_S = 0.05          # gap between synthesized click down and up
_DEFAULT_ACTION_SPACING_S = 0.1   # fallback spacing after the last row
_MIN_ACTION_SPACING_S = 0.05      # min spacing given to instant actions


class MacroRecorderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Macro Recorder")
        self.root.geometry("900x600")
        self.root.minsize(500, 350)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Tab / footer state
        self.active_tab: str = TABS[0]
        self.action_count: int = 0
        self.elapsed_time: str = "00:00:00"

        # Drag state
        self._drag_iids: list[str] = []   # rows being dragged (captured at press)
        self._drag_start_y: int = 0
        self._dragging: bool = False      # True once a press has become a real drag

        # UI-free model holding row→events and per-row time ranges.
        # row_events maps treeview iid → list of MacroEvents: mouse-move runs are
        # bundled into a single row (many events → one iid); all other event types
        # are stored as a one-element list.  Coordinates in the table are always
        # ABSOLUTE; conversion to/from window-relative happens only at
        # load/save/play boundaries.  _row_events below is a read-only view
        # onto this model.
        self._model = TableModel()
        self._current_file: str | None = None

        # Maps a window title → its assigned colour tag name (e.g. "win_0").
        self._window_color_map: dict[str, str] = {}

        # Recording / playback live state
        self._recorder: Recorder | None = None
        self._player: Player | None = None
        self._recording: bool = False
        self._playing: bool = False
        self._poll_cursor: int = 0   # events from current recording already shown
        self._clipboard: list[list[MacroEvent]] = []
        self._playing_iid: str | None = None  # currently highlighted row during playback
        # id(event) -> row iid, built per playback so the player's active-event
        # reports map back to table rows by identity.
        self._event_to_iid: dict[int, str] = {}
        # Highlight pump: the player thread enqueues hit rows here (deduped
        # against the last); a main-thread pump paints one per tick.
        self._active_queue: collections.deque = collections.deque(maxlen=_ACTIVE_QUEUE_MAX)
        self._last_enqueued = object()   # sentinel != any iid/None, for dedup
        self._pump_after_id: str | None = None
        self._debug_timer: str | None = None  # timer for debug highlighting output
        # Output log: the player thread pushes formatted lines here; the main-
        # thread highlight pump drains them into the (collapsible) log panel.
        self._log_queue: collections.deque = collections.deque(maxlen=_LOG_QUEUE_MAX)
        self._log_visible: bool = True

        # Screen overlay that previews the selected action.
        self._overlay = OverlayRenderer(root)

        # Playback config (created before _build_ui so options bar can bind them)
        self._speed_var = tk.StringVar(value="1.0")
        self._repeat_var = tk.StringVar(value="1")
        self._timeout_var = tk.StringVar(value="5.0")
        self._missing_window_var = tk.StringVar(value="skip")
        self._debug_highlight_var = tk.BooleanVar(value=False)

        # Widget references (assigned in _build_options_bar / _build_*)
        self.tab_buttons: dict[str, tk.Button] = {}
        self.options_frames: dict[str, tk.Frame] = {}
        self.table: ttk.Treeview
        self.action_count_label: tk.Label
        self.timer_label: tk.Label
        self._record_btn: tk.Button
        self._stop_record_btn: tk.Button
        self._play_btn: tk.Button
        self._stop_play_btn: tk.Button
        self._details_frame: tk.LabelFrame
        self._details_content: tk.Frame
        self._details_scroll_frame: tk.Frame
        self._detail_widgets: dict[str, tk.Widget] = {}
        self._selected_iid: str | None = None  # Currently selected row in details panel

        self._build_ui()

    # Read-only view onto the model's state, so existing call sites that do
    # self._row_events[...] / .get / .clear / .pop keep working unchanged.
    @property
    def _row_events(self) -> dict[str, list[MacroEvent]]:
        return self._model.row_events

    # ------------------------------------------------------------------ build

    def _build_ui(self) -> None:
        self._build_tab_bar()
        self._build_options_bar()
        self._build_footer()    # footer before table so it anchors to bottom
        self._build_table()

    def _build_tab_bar(self) -> None:
        bar = tk.Frame(self.root)
        bar.pack(side=tk.TOP, fill=tk.X)

        # File button with dropdown menu
        file_btn = tk.Button(bar, text="File", relief=tk.FLAT, padx=10, pady=4,
                             command=self._show_file_menu)
        file_btn.pack(side=tk.LEFT)
        self.tab_buttons["File"] = file_btn

        # Regular tab buttons
        for name in TABS:
            btn = tk.Button(
                bar, text=name, relief=tk.FLAT, padx=10, pady=4,
                command=lambda n=name: self._select_tab(n),
            )
            btn.pack(side=tk.LEFT)
            self.tab_buttons[name] = btn
        self._apply_tab_style(TABS[0])

    def _build_options_bar(self) -> None:
        container = tk.Frame(self.root, bd=1, relief=tk.GROOVE)
        container.pack(side=tk.TOP, fill=tk.X)

        for name in TABS:
            frame = tk.Frame(container, pady=2)

            if name == "Record":
                add_group = tk.LabelFrame(frame, text="Add Item", padx=4, pady=1)
                add_group.pack(side=tk.LEFT, padx=6, pady=2)

                # One button per category; clicking it opens the action menu.
                # The ▼ in the label signals the dropdown affordance.
                for category, label, width in (
                    ("mouse", "Mouse ▼", 9),
                    ("keyboard", "Keyboard ▼", 10),
                    ("wait", "Wait ▼", 8),
                    ("variable", "Variable ▼", 10),
                    ("control", "Control ▼", 9),
                    ("match", "Match ▼", 8),
                ):
                    btn = tk.Button(add_group, text=label, width=width, relief=tk.RAISED)
                    btn.config(command=lambda c=category, b=btn: self._show_action_menu(c, b))
                    btn.pack(side=tk.LEFT, padx=2)

                ttk.Separator(frame, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=2)

                rec_group = tk.LabelFrame(frame, text="Recording", padx=4, pady=1)
                rec_group.pack(side=tk.LEFT, pady=2)
                self._record_btn = tk.Button(rec_group, text="▶ Record", command=self._start_recording)
                self._record_btn.pack(side=tk.LEFT, padx=(0, 4))
                ttk.Separator(rec_group, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, pady=1)
                self._stop_record_btn = tk.Button(
                    rec_group, text="■ Stop", command=self._stop_recording, state=tk.DISABLED,
                )
                self._stop_record_btn.pack(side=tk.LEFT, padx=(4, 0))

            elif name == "Playback":
                play_group = tk.LabelFrame(frame, text="Controls", padx=4, pady=1)
                play_group.pack(side=tk.LEFT, padx=6, pady=2)
                self._play_btn = tk.Button(play_group, text="▶ Play", command=self._start_playback)
                self._play_btn.pack(side=tk.LEFT, padx=(0, 4))
                ttk.Separator(play_group, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, pady=1)
                self._stop_play_btn = tk.Button(
                    play_group, text="■ Stop", command=self._stop_playback, state=tk.DISABLED,
                )
                self._stop_play_btn.pack(side=tk.LEFT, padx=(4, 8))
                ttk.Separator(play_group, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, pady=1)
                tk.Label(play_group, text="Speed:").pack(side=tk.LEFT, padx=(8, 2))
                tk.Entry(play_group, textvariable=self._speed_var, width=5).pack(side=tk.LEFT, padx=(0, 8))
                ttk.Separator(play_group, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, pady=1)
                tk.Label(play_group, text="Repeat:").pack(side=tk.LEFT, padx=(8, 2))
                tk.Entry(play_group, textvariable=self._repeat_var, width=4).pack(side=tk.LEFT)
                tk.Label(play_group, text="(0=∞)", foreground="gray").pack(side=tk.LEFT, padx=(2, 0))

                win_group = tk.LabelFrame(frame, text="Windows", padx=4, pady=1)
                win_group.pack(side=tk.LEFT, padx=6, pady=2)
                tk.Label(win_group, text="Timeout (s):").pack(side=tk.LEFT, padx=(0, 2))
                tk.Entry(win_group, textvariable=self._timeout_var, width=5).pack(side=tk.LEFT, padx=(0, 8))
                ttk.Separator(win_group, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, pady=1)
                tk.Label(win_group, text="If missing:").pack(side=tk.LEFT, padx=(8, 2))
                tk.OptionMenu(win_group, self._missing_window_var, "skip", "halt").pack(side=tk.LEFT)
                ttk.Separator(win_group, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=1)
                tk.Checkbutton(win_group, text="Debug highlight", variable=self._debug_highlight_var).pack(side=tk.LEFT)

            elif name == "Help":
                tk.Button(
                    frame, text="Debug Help",
                    command=lambda: print("[debug] tab=Help"),
                ).pack(side=tk.LEFT, padx=6)

            self.options_frames[name] = frame

        self.options_frames[TABS[0]].pack(fill=tk.X)

    def _build_table(self) -> None:
        # A vertical paned window lets the user drag the boundary between the
        # table/details area (top) and the output log (bottom).
        self._main_paned = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        self._main_paned.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        outer = tk.Frame(self._main_paned)
        self._main_paned.add(outer, weight=5)

        # Left side: table
        table_frame = tk.Frame(outer)
        table_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        style = ttk.Style()
        style.configure("Macro.Treeview", fieldbackground="#f2f2f2")
        self.table = ttk.Treeview(
            table_frame, style="Macro.Treeview", columns=TABLE_COLUMNS,
            show="headings", selectmode="extended",
        )
        self.table.heading("num", text="")
        for col in TABLE_COLUMNS[1:]:
            self.table.heading(col, text=col)
        for col, width in TABLE_COL_WIDTHS.items():
            self.table.column(col, width=width, stretch=(col != "num"), minwidth=30)

        vsb = ttk.Scrollbar(table_frame, orient="vertical",   command=self.table.yview)
        hsb = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT,  fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.table.pack(fill=tk.BOTH, expand=True)

        self.table.tag_configure("row_even", background="#ffffff")
        self.table.tag_configure("row_odd",  background="#f2f2f2")
        # Note: Using Tkinter's selection highlighting instead of tags for playback highlight
        # (tag background colors don't render reliably in Treeview)

        self.table.bind("<Double-1>",        self._on_cell_double_click)
        self.table.bind("<ButtonPress-1>",   self._on_drag_start)
        self.table.bind("<B1-Motion>",       self._on_drag_motion)
        self.table.bind("<ButtonRelease-1>", self._on_drag_release)
        self.table.bind("<Delete>",           lambda _: self._delete_row())
        self.table.bind("<Control-c>",        lambda _: self._copy_rows())
        self.table.bind("<Control-v>",        lambda _: self._paste_rows())
        self.table.bind("<<TreeviewSelect>>", lambda _: self._on_selection_change())
        # Note: the overlay auto-hides when the application loses focus — that is
        # owned by OverlayRenderer itself, so it covers every kind of drawing.

        # Right side: details panel
        self._build_details_panel(outer)

        # Bottom: collapsible/resizable output log pane
        self._build_log_panel(self._main_paned)

    def _build_log_panel(self, paned: ttk.PanedWindow) -> None:
        """Build the collapsible, resizable output-log pane at the bottom."""
        self._log_frame = tk.Frame(paned)

        header = tk.Frame(self._log_frame)
        header.pack(side=tk.TOP, fill=tk.X)
        tk.Label(header, text="Output Log", font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=6)
        tk.Button(header, text="Clear", relief=tk.FLAT, command=self._clear_log).pack(side=tk.RIGHT, padx=4)
        self._log_autoscroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(header, text="Auto-scroll", variable=self._log_autoscroll_var).pack(side=tk.RIGHT)

        body = tk.Frame(self._log_frame)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._log_text = tk.Text(body, height=8, wrap=tk.NONE, state=tk.DISABLED,
                                 font=("Consolas", 9), background="#1e1e1e",
                                 foreground="#d4d4d4", insertbackground="#d4d4d4")
        vsb = ttk.Scrollbar(body, orient="vertical", command=self._log_text.yview)
        hsb = ttk.Scrollbar(body, orient="horizontal", command=self._log_text.xview)
        self._log_text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self._log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        paned.add(self._log_frame, weight=1)

    def _build_details_panel(self, parent: tk.Frame) -> None:
        """Build the right-side details panel for editing selected actions."""
        self._details_frame = tk.LabelFrame(parent, text="Details", padx=10, pady=10, width=320)
        self._details_frame.pack(side=tk.RIGHT, fill=tk.BOTH, padx=5, pady=5)
        self._details_frame.pack_propagate(False)

        # Container for editable fields (will be populated on selection)
        self._details_content = tk.Frame(self._details_frame)
        self._details_content.pack(fill=tk.BOTH, expand=True)

        # Store references to detail input widgets
        self._detail_widgets = {}

        # Label field (always present)
        tk.Label(self._details_content, text="Label:", font=("Arial", 9, "bold")).grid(row=0, column=0, sticky="w", pady=5)
        self._detail_widgets["label"] = tk.Entry(self._details_content, width=25)
        self._detail_widgets["label"].grid(row=0, column=1, sticky="ew", padx=5, pady=5)
        self._detail_widgets["label"].bind("<KeyRelease>", lambda _: self._sync_detail_to_table())

        # Separator
        ttk.Separator(self._details_content, orient="horizontal").grid(row=1, column=0, columnspan=2, sticky="ew", pady=5)

        # Details section (populated based on action type)
        self._details_scroll_frame = tk.Frame(self._details_content)
        self._details_scroll_frame.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=5)
        self._details_content.grid_rowconfigure(2, weight=1)
        self._details_content.grid_columnconfigure(1, weight=1)

        # Message when nothing is selected
        self._no_selection_label = tk.Label(
            self._details_scroll_frame,
            text="Select an action\nto edit details",
            foreground="gray",
            font=("Arial", 10)
        )
        self._no_selection_label.pack(expand=True)

    def _build_footer(self) -> None:
        footer = tk.Frame(self.root, bd=1, relief=tk.SUNKEN)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        self.action_count_label = tk.Label(footer, text="0 actions", anchor="w", padx=6)
        self.action_count_label.pack(side=tk.LEFT)
        self._log_toggle_btn = tk.Button(footer, text="▾ Hide Log", relief=tk.FLAT,
                                         padx=6, command=self._toggle_log_panel)
        self._log_toggle_btn.pack(side=tk.LEFT)
        self.timer_label = tk.Label(footer, text="00:00:00", anchor="e", padx=6)
        self.timer_label.pack(side=tk.RIGHT)

    # --------------------------------------------------------------- tab logic

    def _select_tab(self, name: str) -> None:
        self.options_frames[self.active_tab].pack_forget()
        self.active_tab = name
        self.options_frames[name].pack(fill=tk.X)
        self._apply_tab_style(name)

    def _apply_tab_style(self, active: str) -> None:
        for name, btn in self.tab_buttons.items():
            btn.config(relief=tk.SUNKEN if name == active else tk.FLAT)

    def _show_file_menu(self) -> None:
        """Show a dropdown menu for File operations."""
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Open", command=self._open_file)
        menu.add_command(label="Save", command=self._save_file)
        menu.add_command(label="Save As", command=self._save_file_as)

        # Position menu below the File button
        file_btn = self.tab_buttons["File"]
        x = file_btn.winfo_rootx()
        y = file_btn.winfo_rooty() + file_btn.winfo_height()
        menu.post(x, y)

    # -------------------------------------------------------------- file ops

    def _open_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Open Macro",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            groups = load_macro(path)
        except Exception as e:
            messagebox.showerror("Open failed", str(e))
            return
        self._load_groups_to_table(groups)
        self._current_file = path
        self.root.title(f"Macro Recorder — {Path(path).name}")

    def _save_file(self) -> None:
        if self._current_file:
            self._do_save(self._current_file)
        else:
            self._save_file_as()

    def _save_file_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save Macro",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self._do_save(path)
        self._current_file = path
        self.root.title(f"Macro Recorder — {Path(path).name}")

    def _do_save(self, path: str) -> None:
        try:
            save_macro(self._get_groups_from_table(), path)
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    # ---------------------------------------------------------- recording

    def _start_recording(self) -> None:
        if self._recording:
            return
        self._recording = True
        self._poll_cursor = 0
        self._recorder = Recorder(stop_key="Key.f6")
        self._record_btn.config(state=tk.DISABLED)
        self._stop_record_btn.config(state=tk.NORMAL)
        self.action_count_label.config(text="Recording… press F6 to stop")

        def _run() -> None:
            groups = self._recorder.start()  # blocks until F6 or _stop_recording()
            self.root.after(0, lambda: self._on_recording_complete(groups))

        threading.Thread(target=_run, daemon=True).start()
        self._poll_recorder()

    def _stop_recording(self) -> None:
        if self._recorder:
            self._recorder.stop()

    def _on_recording_complete(self, groups: list[MacroGroup]) -> None:
        self._recording = False
        self._recorder = None
        self._record_btn.config(state=tk.NORMAL)
        self._stop_record_btn.config(state=tk.DISABLED)
        # Rebuild authoritatively from the final grouped data (replaces the
        # provisional rows shown live during polling).
        self._load_groups_to_table(groups)
        self._poll_cursor = 0

    def _poll_recorder(self) -> None:
        """Called every _POLL_INTERVAL_MS on the main thread during recording."""
        if not self._recording or self._recorder is None:
            return
        self._sync_recording_events(self._recorder.get_events())
        self.root.after(_POLL_INTERVAL_MS, self._poll_recorder)

    def _sync_recording_events(self, snapshot: list[MacroEvent]) -> None:
        """Append events in snapshot beyond _poll_cursor to the table.

        If the last existing row is an open mouse-move group whose final
        timestamp is close enough to the first new event, that row is removed
        and its events are prepended to the incoming batch so the group is
        re-rendered as a single continuous row.
        """
        new_events = snapshot[self._poll_cursor:]
        if not new_events:
            return

        # Merge with or split from the last row if it is a mouse-move group
        children = self.table.get_children()
        if children:
            last_iid = children[-1]
            last_group = self._row_events.get(last_iid)
            if last_group and last_group[0].type == EventType.MOUSE_MOVE and new_events[0].type == EventType.MOUSE_MOVE:
                gap = new_events[0].ts - last_group[-1].ts
                if gap <= _MOVE_BUNDLE_GAP_S:
                    # Extend: undo the last row and prepend its events
                    self.table.delete(last_iid)
                    del self._row_events[last_iid]
                    new_events = list(last_group) + new_events
                    self._poll_cursor = max(0, self._poll_cursor - len(last_group))
                else:
                    # Gap detected across a poll boundary: insert a wait event now
                    # so _append_events_to_table doesn't need to look back at the table.
                    wait_ev = MacroEvent(type=EventType.WAIT, ts=last_group[-1].ts, duration=gap)
                    wait_iid = self.table.insert("", tk.END, values=("", _action_label(EventType.WAIT), self._format_value(wait_ev), ""))
                    self._row_events[wait_iid] = [wait_ev]

        self._append_events_to_table(new_events)
        self._poll_cursor = len(snapshot)

        # Scroll to the newest row
        children = self.table.get_children()
        if children:
            self.table.see(children[-1])

    # ---------------------------------------------------------- playback

    def _start_playback(self) -> None:
        if self._playing:
            return
        # Build the playback groups and the event→row map together, so the
        # player's active-event reports (including windowed coordinate copies)
        # map back to table rows by identity.
        self._event_to_iid = {}
        groups = self._build_playback_groups(self._event_to_iid)
        if not groups or all(not g.events for g in groups):
            messagebox.showinfo("Nothing to play", "No events in the table.")
            return
        # Speed and Repeat may be plain numbers or expressions.  They are
        # evaluated once, here, against an empty variable store (variables only
        # gain values during playback), so a constant expression like "2*3"
        # works but a variable reference resolves to 0.
        try:
            speed = float(evaluate(self._speed_var.get(), {}))
            repeat = int(evaluate(self._repeat_var.get(), {}))
            window_timeout = float(self._timeout_var.get())
        except (ValueError, ExpressionError):
            messagebox.showerror(
                "Invalid settings",
                "Speed and Repeat must be numbers/expressions and Timeout a number.",
            )
            return

        self._playing = True
        self._active_queue.clear()
        self._last_enqueued = object()
        self._log_queue.clear()
        self._append_log_lines(["— Playback started —"])
        self._player = Player(
            speed=speed,
            repeat=repeat,
            window_timeout=window_timeout,
            on_missing_window=self._missing_window_var.get(),
            on_active_event=self._on_active_event,
            on_log=self._on_log,
        )
        self._play_btn.config(state=tk.DISABLED)
        self._stop_play_btn.config(state=tk.NORMAL)
        self._pump_highlight()   # main-thread pump that drains the active queue

        # Start debug output if enabled
        if self._debug_highlight_var.get():
            self._debug_print_highlight_state()

        def _run() -> None:
            error: str | None = None
            try:
                self._player.play(groups)  # blocks until done or stopped
            except (WindowNotFoundError, MacroExecutionError) as e:
                error = str(e)
            self.root.after(0, lambda: self._on_playback_complete(error))

        threading.Thread(target=_run, daemon=True).start()

    def _stop_playback(self) -> None:
        if self._player:
            self._player.stop()

    def _on_playback_complete(self, error: str | None = None) -> None:
        self._playing = False
        self._player = None
        # Stop the highlight pump and debug output timer.
        if self._pump_after_id:
            self.root.after_cancel(self._pump_after_id)
            self._pump_after_id = None
        if self._debug_timer:
            self.root.after_cancel(self._debug_timer)
            self._debug_timer = None
        self._clear_playing_highlight()
        # Flush any log lines that arrived after the pump's last tick.
        self._drain_log_queue()
        self._append_log_lines(["— Playback %s —" % ("halted" if error else "finished")])
        self._play_btn.config(state=tk.NORMAL)
        self._stop_play_btn.config(state=tk.DISABLED)
        if error:
            messagebox.showerror("Playback halted", error)

    def _on_active_event(self, event) -> None:
        """Called from the player thread for each instruction it runs.

        Enqueues the instruction's row (deduped against the previous one) for the
        main-thread pump to paint.  Does no Tk work itself, so it is safe to call
        from the player thread.  Consecutive reports of the same row (e.g. the
        many events of one mouse-move bundle) collapse to a single entry.
        """
        iid = self._event_to_iid.get(id(event)) if event else None
        if iid == self._last_enqueued:
            return
        self._last_enqueued = iid
        self._active_queue.append(iid)

    def _pump_highlight(self) -> None:
        """Main-thread: paint one queued row per tick, then reschedule.

        Draining one row per tick turns fast execution into visible flicker
        through the hit rows; when the queue empties (e.g. during a wait's dwell)
        the last-painted row simply stays highlighted.
        """
        if not self._playing:
            self._pump_after_id = None
            return
        if self._active_queue:
            iid = self._active_queue.popleft()
            if iid != self._playing_iid:
                self._update_highlight(iid)
        self._drain_log_queue()
        self._pump_after_id = self.root.after(_HIGHLIGHT_PUMP_MS, self._pump_highlight)

    # ------------------------------------------------------------- output log

    def _toggle_log_panel(self) -> None:
        """Show or hide the output-log pane (collapse into the paned window)."""
        self._log_visible = not self._log_visible
        if self._log_visible:
            self._main_paned.add(self._log_frame, weight=1)
            self._log_toggle_btn.config(text="▾ Hide Log")
        else:
            self._main_paned.forget(self._log_frame)
            self._log_toggle_btn.config(text="▸ Show Log")

    def _on_log(self, message: str) -> None:
        """Called from the player thread: buffer one log line for the main thread."""
        self._log_queue.append(message)

    def _drain_log_queue(self) -> None:
        """Main-thread: flush all buffered log lines into the panel."""
        if not self._log_queue:
            return
        lines = []
        while self._log_queue:
            lines.append(self._log_queue.popleft())
        self._append_log_lines(lines)

    def _append_log_lines(self, lines: list[str]) -> None:
        """Append lines to the (read-only) log Text, trimming old history."""
        self._log_text.config(state=tk.NORMAL)
        self._log_text.insert(tk.END, "\n".join(lines) + "\n")
        # Keep the buffer bounded so a long run doesn't grow without limit.
        excess = int(self._log_text.index("end-1c").split(".")[0]) - _LOG_PANEL_MAX_LINES
        if excess > 0:
            self._log_text.delete("1.0", "%d.0" % (excess + 1))
        if self._log_autoscroll_var.get():
            self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self._log_text.config(state=tk.NORMAL)
        self._log_text.delete("1.0", tk.END)
        self._log_text.config(state=tk.DISABLED)

    def _update_highlight(self, new_iid: str | None) -> None:
        """Update highlighting to a new row using Tkinter's selection mechanism.

        Since Treeview tag background colors don't render reliably, we use the
        built-in selection highlighting which is guaranteed to work.
        """
        # Use selection to highlight the row (this is reliable in Tkinter)
        if new_iid and new_iid in self._row_events:
            self.table.selection_set(new_iid)
            self.table.see(new_iid)  # Scroll to make visible
        else:
            # Clear selection when not playing
            self.table.selection_remove(self.table.selection())

        self._playing_iid = new_iid

    def _clear_playing_highlight(self) -> None:
        """Clear the playing highlight and restore normal row colour."""
        self._update_highlight(None)

    def _debug_print_highlight_state(self) -> None:
        """Print which row is currently highlighted (debug aid)."""
        if not self._playing:
            return

        print(f"\n[Highlight State] playing_iid={self._playing_iid}")
        for idx, iid in enumerate(self.table.get_children()):
            values = self.table.item(iid, "values")
            row_desc = f"{values[1]}@{values[2][:30]}" if len(values) > 2 else str(values)
            marker = "→ ACTIVE" if iid == self._playing_iid else "  "
            print(f"  {marker} Row {idx} ({iid}): {row_desc}")

        self._debug_timer = self.root.after(500, self._debug_print_highlight_state)

    # ---------------------------------------------------- events ↔ table

    def _load_groups_to_table(self, groups: list[MacroGroup]) -> None:
        """Replace the table contents with the given groups.

        Groups are flattened to a single event stream: each windowed group is
        preceded by a synthesized window_focus row carrying its title and rect,
        and its relative coordinates are converted back to absolute for display.
        """
        for iid in self.table.get_children():
            self.table.delete(iid)
        self._row_events.clear()
        self._window_color_map.clear()
        self._append_events_to_table(self._model.groups_to_flat_events(groups))
        # _append_events_to_table renumbers; normalize re-anchors timestamps.
        self._normalize_timestamps()

    def _append_events_to_table(self, events: list[MacroEvent]) -> None:
        """Append events to the end of the table without clearing existing rows.

        Bundling (consecutive moves, click down/up pairs, same-direction
        scrolls) and synthetic wait-row synthesis are handled by the pure
        ``bundle_events`` helper; this method only renders the resulting
        row-groups.  Events are expected to carry ABSOLUTE coordinates.
        """
        for group in bundle_events(events, _MOVE_BUNDLE_GAP_S, _CLICK_BUNDLE_GAP_S):
            first = group[0]
            value = self._format_row_value(group)
            iid = self.table.insert("", tk.END, values=("", _action_label(first.type), value, first.label or ""))
            self._row_events[iid] = group

        self._renumber_rows()
        self.set_action_count(sum(1 for iid in self.table.get_children() if iid in self._row_events))

    def _format_row_value(self, group: list[MacroEvent]) -> str:
        """Return the Value-column text for a (possibly bundled) row-group."""
        first = group[0]
        if first.type == EventType.MOUSE_MOVE and len(group) > 1:
            last = group[-1]
            return f"({first.x}, {first.y}) → ({last.x}, {last.y})"
        if first.type == EventType.MOUSE_CLICK and len(group) == 2 and first.pressed and not group[1].pressed:
            return f"{first.button} click @ ({first.x}, {first.y})"
        if first.type == EventType.MOUSE_SCROLL and len(group) > 1:
            total_dy = sum(ev.dy for ev in group if ev.dy)
            total_dx = sum(ev.dx for ev in group if ev.dx)
            if total_dy != 0:
                direction = "down" if total_dy > 0 else "up"
                return f"scroll {direction} ({abs(total_dy)})"
            if total_dx != 0:
                direction = "right" if total_dx > 0 else "left"
                return f"scroll {direction} ({abs(total_dx)})"
        return self._format_value(first)

    def _get_groups_from_table(self) -> list[MacroGroup]:
        """Reconstruct MacroGroups from the table in current row order (for save)."""
        order = list(self.table.get_children())
        labels = self._gather_labels(order)
        return self._model.rows_to_groups(order, labels)

    def _build_playback_groups(self, event_to_iid: dict[int, str]) -> list[MacroGroup]:
        """Like _get_groups_from_table, but also fills event_to_iid for highlighting."""
        order = list(self.table.get_children())
        labels = self._gather_labels(order)
        return self._model.rows_to_groups(order, labels, event_to_iid)

    def _gather_labels(self, order: list[str]) -> dict[str, str | None]:
        """Collect the edited Label-column text for each row."""
        labels: dict[str, str | None] = {}
        for iid in order:
            vals = self.table.item(iid, "values")
            labels[iid] = vals[3] if len(vals) > 3 and vals[3] else None
        return labels

    @staticmethod
    def _format_value(ev: MacroEvent) -> str:
        """Return a compact human-readable string for the Value column."""
        if ev.type == EventType.MOUSE_MOVE:
            return f"({ev.x}, {ev.y})"
        if ev.type == EventType.MOUSE_MOVE_TIMED:
            # dx/dy hold the absolute To-coordinate.
            return f"({ev.x}, {ev.y}) → ({ev.dx}, {ev.dy}) in {_format_duration_label(ev.duration)}"
        if ev.type == EventType.MOUSE_CLICK:
            return f"{ev.button} {'down' if ev.pressed else 'up'} @ ({ev.x}, {ev.y})"
        if ev.type == EventType.MOUSE_SCROLL:
            dx = f"{ev.dx:+d}" if isinstance(ev.dx, int) else ev.dx
            dy = f"{ev.dy:+d}" if isinstance(ev.dy, int) else ev.dy
            return f"({dx}, {dy}) @ ({ev.x}, {ev.y})"
        if ev.type in (EventType.KEY_PRESS, EventType.KEY_RELEASE):
            return ev.key or ""
        if ev.type == EventType.TYPE_TEXT:
            base = f'type "{ev.expr}"'
            if ev.char_delay is not None:
                return f"{base} ({_format_duration_label(ev.char_delay)}/char)"
            if ev.duration is not None:
                return f"{base} (over {_format_duration_label(ev.duration)})"
            return base
        if ev.type == EventType.WINDOW_FOCUS:
            return ev.window or ""
        if ev.type == EventType.VAR_SET:
            return f"{ev.var_name} = {ev.expr}"
        if ev.type == EventType.OCR_READ:
            return f"{ev.var_name} = OCR({ev.x},{ev.y} → {ev.dx},{ev.dy})"
        if ev.type == EventType.MATCH_IMAGE:
            name = Path(ev.image_path).name if ev.image_path else "?"
            return f'{ev.var_name} = wait image "{name}"'
        if ev.type == EventType.MATCH_TEXT:
            return f'{ev.var_name} = wait text "{ev.expr}"'
        if ev.type == EventType.GOTO:
            return f"goto {_goto_target_text(ev)}"
        if ev.type == EventType.GOTO_IF:
            return f"if ({ev.expr}) goto {_goto_target_text(ev)}"
        if ev.type == EventType.WAIT and ev.duration is not None:
            return _format_duration_label(ev.duration)
        return ""

    def _label_options(self, current_target: str | None = None) -> list[str]:
        """Return goto-target choices ordered by program position.

        ``Start of program`` first, then each filled-in label in the order its
        row occurs in the table, then ``End of program`` last.
        """
        opts = [GOTO_START]
        for iid in self.table.get_children():
            vals = self.table.item(iid, "values")
            label = vals[3] if len(vals) > 3 else ""
            if label and label not in opts:
                opts.append(label)
        opts.append(GOTO_END)
        # Keep a stale/renamed target selectable, positioned just before End.
        if current_target and current_target not in opts:
            opts.insert(len(opts) - 1, current_target)
        return opts

    # ---------------------------------------------------------- goto target fields

    _GOTO_LABEL_MODE = "Label"
    _GOTO_INDEX_MODE = "Instruction #"

    def _make_goto_target_fields(self, event: MacroEvent) -> None:
        """Build the goto destination fields: a mode selector plus, depending on
        the mode, a label dropdown XOR an instruction-number entry."""
        index_mode = event.target_index is not None
        self._make_combobox_field(
            "Target type:", [self._GOTO_LABEL_MODE, self._GOTO_INDEX_MODE], "goto_mode",
            self._GOTO_INDEX_MODE if index_mode else self._GOTO_LABEL_MODE,
            on_change=self._on_goto_mode_change)
        if index_mode:
            self._make_entry_field("Instruction #:", event.target_index, "target_index")
        else:
            self._make_combobox_field("Go to:", self._label_options(event.target),
                                      "target", event.target or GOTO_END)

    def _on_goto_mode_change(self, _event=None) -> None:
        """Switch a goto row between label and instruction-number targeting,
        then rebuild the panel so the matching field is shown."""
        if not self._selected_iid or self._selected_iid not in self._row_events:
            return
        event = self._row_events[self._selected_iid][0]
        mode = self._detail_widgets["goto_mode"].get()
        if mode == self._GOTO_INDEX_MODE:
            if event.target_index is None:
                event.target_index = 1
            event.target = None
        else:
            event.target_index = None
            if not event.target:
                event.target = GOTO_END
        self._show_details_panel(self._selected_iid)
        self._sync_detail_to_table()

    def _sync_goto_target(self, event: MacroEvent) -> None:
        """Read the goto destination widgets back into the event (label XOR #)."""
        mode = self._detail_widgets.get("goto_mode")
        if mode is not None and mode.get() == self._GOTO_INDEX_MODE:
            if "target_index" in self._detail_widgets:
                txt = self._detail_widgets["target_index"].get().strip()
                if txt.isdigit():
                    event.target_index = int(txt)
            event.target = None
        else:
            if "target" in self._detail_widgets:
                event.target = self._detail_widgets["target"].get()
            event.target_index = None

    # -------------------------------------------------------------- row actions

    def _show_action_menu(self, category: str, parent_widget: tk.Widget) -> None:
        """Show a popup menu with action types for the given category."""
        menu = tk.Menu(parent_widget, tearoff=False)

        if category == "mouse":
            menu.add_command(label="Move", command=lambda: self._add_action_row("mouse_move"))
            menu.add_command(label="Move Point-to-Point", command=lambda: self._add_action_row("mouse_move_timed"))
            menu.add_command(label="Click", command=lambda: self._add_action_row("mouse_click"))
            menu.add_command(label="Click Down", command=lambda: self._add_action_row("mouse_click_down"))
            menu.add_command(label="Click Up", command=lambda: self._add_action_row("mouse_click_up"))
            menu.add_command(label="Scroll", command=lambda: self._add_action_row("mouse_scroll"))

        elif category == "keyboard":
            menu.add_command(label="Key Press", command=lambda: self._add_action_row("key_press"))
            menu.add_command(label="Key Release", command=lambda: self._add_action_row("key_release"))
            menu.add_command(label="Type Text", command=lambda: self._add_action_row("type_text"))

        elif category == "wait":
            menu.add_command(label="Wait/Pause", command=lambda: self._add_action_row("wait"))

        elif category == "variable":
            menu.add_command(label="Set Variable", command=lambda: self._add_action_row("var_set"))
            menu.add_command(label="Read Screen (OCR)", command=lambda: self._add_action_row("ocr_read"))

        elif category == "control":
            menu.add_command(label="Go To", command=lambda: self._add_action_row("goto"))
            menu.add_command(label="Conditional Go To", command=lambda: self._add_action_row("goto_if"))

        elif category == "match":
            menu.add_command(label="Wait for Image", command=lambda: self._add_action_row("match_image"))
            menu.add_command(label="Wait for Text", command=lambda: self._add_action_row("match_text"))

        # Get widget position and show menu
        x = parent_widget.winfo_rootx()
        y = parent_widget.winfo_rooty() + parent_widget.winfo_height()
        menu.post(x, y)

    def _add_action_row(self, action_type: str) -> None:
        """Add a new action of the specified type with default values.

        Parameters
        ----------
        action_type:
            Action type string (e.g., "mouse_move", "key_press", "wait")
        """
        # Get the next timestamp (after the last event in the table)
        next_ts = 0.0
        children = self.table.get_children()
        if children:
            last_iid = children[-1]
            last_group = self._row_events.get(last_iid, [])
            if last_group:
                last_event = last_group[-1]
                next_ts = last_event.ts + (last_event.duration or _DEFAULT_ACTION_SPACING_S)

        # Create default event based on type
        new_event = None
        if action_type == "mouse_move":
            new_event = MacroEvent(type=EventType.MOUSE_MOVE, ts=next_ts, x=100, y=100)
        elif action_type == "mouse_move_timed":
            # For timed moves dx/dy hold the absolute To-coordinate (not a delta).
            new_event = MacroEvent(type=EventType.MOUSE_MOVE_TIMED, ts=next_ts, x=100, y=100, dx=200, dy=200, duration=_DEFAULT_MOVE_DURATION_S)
        elif action_type == "mouse_click":
            # Create both down and up events together
            new_events = [
                MacroEvent(type=EventType.MOUSE_CLICK, ts=next_ts, button="left", pressed=True, x=100, y=100),
                MacroEvent(type=EventType.MOUSE_CLICK, ts=next_ts + _CLICK_PAIR_GAP_S, button="left", pressed=False, x=100, y=100)
            ]
            # Insert as bundle
            sel = self.table.selection()
            index = self.table.index(sel[0]) + 1 if sel else tk.END
            value = "left @ (100, 100)"
            iid = self.table.insert("", index, values=("", _action_label(EventType.MOUSE_CLICK), value, ""))
            self._row_events[iid] = new_events
            self._refresh_after_mutation()
            self.table.selection_set(iid)
            self.table.see(iid)
            self.set_action_count(sum(1 for iid in self.table.get_children() if iid in self._row_events))
            return
        elif action_type == "mouse_click_down":
            new_event = MacroEvent(type=EventType.MOUSE_CLICK, ts=next_ts, button="left", pressed=True, x=100, y=100)
        elif action_type == "mouse_click_up":
            new_event = MacroEvent(type=EventType.MOUSE_CLICK, ts=next_ts, button="left", pressed=False, x=100, y=100)
        elif action_type == "mouse_scroll":
            new_event = MacroEvent(type=EventType.MOUSE_SCROLL, ts=next_ts, dx=0, dy=3, x=100, y=100)
        elif action_type == "key_press":
            new_event = MacroEvent(type=EventType.KEY_PRESS, ts=next_ts, key="a")
        elif action_type == "key_release":
            new_event = MacroEvent(type=EventType.KEY_RELEASE, ts=next_ts, key="a")
        elif action_type == "type_text":
            # expr holds an f-string-style template, e.g. "Result: {x}".
            new_event = MacroEvent(type=EventType.TYPE_TEXT, ts=next_ts, expr="text")
        elif action_type == "wait":
            new_event = MacroEvent(type=EventType.WAIT, ts=next_ts, duration=_DEFAULT_WAIT_DURATION_S)
        elif action_type == "var_set":
            new_event = MacroEvent(type=EventType.VAR_SET, ts=next_ts, var_name="x", expr="0")
        elif action_type == "ocr_read":
            # x,y and dx,dy are two opposite corners (any orientation).
            new_event = MacroEvent(type=EventType.OCR_READ, ts=next_ts, x=100, y=100, dx=300, dy=200, var_name="text")
        elif action_type == "match_image":
            new_event = MacroEvent(type=EventType.MATCH_IMAGE, ts=next_ts, x=100, y=100, dx=400, dy=300,
                                   image_path="", tolerance=0.9, duration=5.0, var_name="found")
        elif action_type == "match_text":
            new_event = MacroEvent(type=EventType.MATCH_TEXT, ts=next_ts, x=100, y=100, dx=400, dy=300,
                                   expr="expected", tolerance=0.8, duration=5.0, var_name="found")
        elif action_type == "goto":
            new_event = MacroEvent(type=EventType.GOTO, ts=next_ts, target=GOTO_END)
        elif action_type == "goto_if":
            new_event = MacroEvent(type=EventType.GOTO_IF, ts=next_ts, expr="x > 0", target=GOTO_END)

        if not new_event:
            return

        # Insert the new event into the table
        sel = self.table.selection()
        index = self.table.index(sel[0]) + 1 if sel else tk.END

        # Format value for display
        value = self._format_value(new_event)
        iid = self.table.insert("", index, values=("", _action_label(new_event.type), value, ""))
        self._row_events[iid] = [new_event]

        self._refresh_after_mutation()
        self.table.selection_set(iid)
        self.table.see(iid)
        self.set_action_count(sum(1 for iid in self.table.get_children() if iid in self._row_events))

    def _delete_row(self) -> None:
        sel = self.table.selection()
        if not sel:
            return
        focus = self.table.next(sel[-1]) or self.table.prev(sel[0])
        for iid in sel:
            self.table.delete(iid)
            self._row_events.pop(iid, None)
        self._refresh_after_mutation()
        if focus:
            try:
                self.table.selection_set(focus)
            except tk.TclError:
                pass
        self.set_action_count(sum(1 for iid in self.table.get_children() if iid in self._row_events))

    def _copy_rows(self) -> None:
        sel = self.table.selection()
        if not sel:
            return
        self._clipboard = [
            copy.deepcopy(self._row_events[iid])
            for iid in sel
            if iid in self._row_events   # skip display-only wait rows
        ]

    def _paste_rows(self) -> None:
        if not self._clipboard:
            return
        sel = self.table.selection()
        insert_at = self.table.index(sel[-1]) + 1 if sel else len(self.table.get_children())

        new_iids: list[str] = []
        for offset, group in enumerate(self._clipboard):
            first = group[0]
            if first.type == EventType.MOUSE_MOVE and len(group) > 1:
                last = group[-1]
                value = f"({first.x}, {first.y}) → ({last.x}, {last.y})"
            else:
                value = self._format_value(first)
            iid = self.table.insert("", insert_at + offset, values=("", _action_label(first.type), value, first.label or ""))
            self._row_events[iid] = group
            new_iids.append(iid)

        self._refresh_after_mutation()
        self.table.selection_set(new_iids)
        self.table.see(new_iids[-1])
        self.set_action_count(sum(1 for iid in self.table.get_children() if iid in self._row_events))

    def _move_row_up(self) -> None:
        sel = self.table.selection()
        if not sel:
            return
        iid = sel[0]
        prev = self.table.prev(iid)
        if prev:
            self.table.move(iid, "", self.table.index(prev))
            self._refresh_after_mutation()

    def _move_row_down(self) -> None:
        sel = self.table.selection()
        if not sel:
            return
        iid = sel[0]
        nxt = self.table.next(iid)
        if nxt:
            self.table.move(iid, "", self.table.index(nxt))
            self._refresh_after_mutation()

    @staticmethod
    def _adjust_colour(hex_colour: str, factor: float) -> str:
        """Adjust brightness of a hex colour by a factor (0.7=darker, 1.3=lighter)."""
        hex_colour = hex_colour.lstrip("#")
        r, g, b = int(hex_colour[0:2], 16), int(hex_colour[2:4], 16), int(hex_colour[4:6], 16)
        r = max(0, min(255, int(r * factor)))
        g = max(0, min(255, int(g * factor)))
        b = max(0, min(255, int(b * factor)))
        return f"#{r:02x}{g:02x}{b:02x}"

    def _get_window_color_tag(self, title: str) -> tuple[str, str]:
        """Return (creating if needed) the even/odd colour tags for a window title.

        Returns (even_tag, odd_tag) for alternating rows within the window scope.
        """
        tag_base = self._window_color_map.get(title)
        if tag_base is None:
            idx = len(self._window_color_map)
            tag_base = f"win_{idx}"
            colour = _WINDOW_PALETTE[idx % len(_WINDOW_PALETTE)]
            colour_light = self._adjust_colour(colour, 1.05)  # 5% lighter for subtle alternation
            self.table.tag_configure(f"{tag_base}_even", background=colour)
            self.table.tag_configure(f"{tag_base}_odd", background=colour_light)
            self._window_color_map[title] = tag_base
        return (f"{tag_base}_even", f"{tag_base}_odd")

    def _renumber_rows(self) -> None:
        i = 1
        current_win_tags: tuple[str, str] | None = None
        win_row_count = 0  # track row count within current window scope
        for idx, iid in enumerate(self.table.get_children()):
            vals = list(self.table.item(iid, "values"))
            group = self._row_events.get(iid)
            is_focus = group is not None and group[0].type == EventType.WINDOW_FOCUS

            if is_focus:
                current_win_tags = self._get_window_color_tag(group[0].window or "")
                win_row_count = 0
                # window_focus rows use neutral stripe coloring, not window-specific
                tag = "row_even" if idx % 2 == 0 else "row_odd"
            elif current_win_tags:
                # Within a window scope: alternate the window's even/odd shades
                tag = current_win_tags[win_row_count % 2]
                win_row_count += 1
            else:
                # Outside any window context: use global alternation
                tag = "row_even" if idx % 2 == 0 else "row_odd"

            if group:
                vals[0] = i
                i += 1
            else:
                vals[0] = ""

            self.table.item(iid, values=vals, tags=(tag,))

    def _refresh_after_mutation(self) -> None:
        """Re-sync derived state after any row insert/delete/reorder.

        Renumbers the visible rows, then re-anchors timestamps to the new
        visual order.
        """
        self._renumber_rows()
        self._normalize_timestamps()

    def _normalize_timestamps(self) -> None:
        """Re-anchor timestamps to the current visual row order (delegates to model)."""
        self._model.normalize_timestamps(list(self.table.get_children()),
                                         _MIN_ACTION_SPACING_S)

    # -------------------------------------------------------------- drag reorder

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_iids = []
        self._dragging = False
        if self.table.identify_region(event.x, event.y) != "cell":
            return
        clicked = self.table.identify_row(event.y)
        if not clicked:
            return
        # Capture the selection now, before Treeview's own click handler may
        # collapse it.  If the pressed row is part of a multi-selection, drag the
        # whole block (in program order); otherwise drag just the pressed row.
        selection = set(self.table.selection())
        if clicked in selection and len(selection) > 1:
            self._drag_iids = [i for i in self.table.get_children() if i in selection]
        else:
            self._drag_iids = [clicked]
        self._drag_start_y = event.y

    def _on_drag_motion(self, event: tk.Event) -> str | None:
        if not self._drag_iids or abs(event.y - self._drag_start_y) < 4:
            return None
        self._dragging = True
        self.table.config(cursor="fleur")
        # Keep the dragged rows highlighted (and suppress Treeview drag-select).
        if set(self.table.selection()) != set(self._drag_iids):
            self.table.selection_set(self._drag_iids)
        return "break"

    def _on_drag_release(self, event: tk.Event) -> str | None:
        if not self._drag_iids:
            return None
        self.table.config(cursor="")
        if not self._dragging:
            self._drag_iids = []
            return None   # a plain click — let normal selection happen

        drag_set = set(self._drag_iids)
        full = list(self.table.get_children())
        remaining = [i for i in full if i not in drag_set]
        drag_ordered = [i for i in full if i in drag_set]

        target = self.table.identify_row(event.y)
        if target in drag_set:
            insert_at = None                      # dropped within the block → no move
        elif not target:
            insert_at = len(remaining)            # dropped past the last row → end
        else:
            insert_at = remaining.index(target)
            bbox = self.table.bbox(target)        # drop after target if in its lower half
            if bbox and event.y > bbox[1] + bbox[3] / 2:
                insert_at += 1

        if insert_at is not None:
            new_order = remaining[:insert_at] + drag_ordered + remaining[insert_at:]
            if new_order != full:
                for idx, iid in enumerate(new_order):
                    self.table.move(iid, "", idx)
                self._refresh_after_mutation()

        self.table.selection_set(self._drag_iids)
        self._drag_iids = []
        self._dragging = False
        return "break"

    # ---------------------------------------------------------- inline editing

    def _on_cell_double_click(self, event: tk.Event) -> None:
        region = self.table.identify_region(event.x, event.y)
        if region != "cell":
            return
        iid = self.table.identify_row(event.y)
        col_id = self.table.identify_column(event.x)
        col_index = int(col_id[1:]) - 1
        col_name = TABLE_COLUMNS[col_index]

        if col_name == "num":
            return

        group = self._row_events.get(iid)
        if not group:
            return

        # Label column — quick inline text editing.  All other columns are
        # edited via the persistent side panel (see _show_details_panel).
        if col_name == "Label":
            vals = list(self.table.item(iid, "values"))
            x, y, w, h = self.table.bbox(iid, col_id)
            entry = tk.Entry(self.table, width=w // 8)
            entry.place(x=x, y=y, width=w, height=h)
            entry.insert(0, vals[col_index])
            entry.select_range(0, tk.END)
            entry.focus_set()

            def _commit(_=None) -> None:
                vals = list(self.table.item(iid, "values"))
                vals[col_index] = entry.get()
                self.table.item(iid, values=vals)
                entry.destroy()

            entry.bind("<Return>", _commit)
            entry.bind("<FocusOut>", _commit)
            entry.bind("<Escape>", lambda _: entry.destroy())

    # ---------------------------------------------------------- state helpers

    def set_action_count(self, count: int) -> None:
        self.action_count = count
        self.action_count_label.config(
            text=f"{count} action{'s' if count != 1 else ''}",
        )

    def set_elapsed_time(self, time_str: str) -> None:
        self.elapsed_time = time_str
        self.timer_label.config(text=time_str)

    # ---------------------------------------------------------- screen overlay

    def _on_selection_change(self) -> None:
        sel = self.table.selection()
        if len(sel) != 1:
            self._overlay.hide()
            self._show_details_panel(None)
            return
        iid = sel[0]
        group = self._row_events.get(iid)
        if not group:
            self._overlay.hide()
            self._show_details_panel(None)
            return

        # Show details panel and overlay preview for this row.
        self._show_details_panel(iid)
        self._draw_overlay_for_group(group)

    def _draw_overlay_for_group(self, group: list[MacroEvent]) -> None:
        """Draw the on-screen preview for a row's action (or hide it).

        Only previews when coordinates are concrete numbers — expression-valued
        coordinates (variables) have no value until playback.  Called on
        selection and again whenever the details panel edits the row, so the
        preview tracks coordinate edits and targeting captures live.
        """
        t = group[0].type
        numeric = all(isinstance(ev.x, (int, float)) and isinstance(ev.y, (int, float))
                      for ev in group if ev.x is not None)
        if t == EventType.MOUSE_MOVE and numeric:
            if len(group) == 1:
                self._overlay.draw_target(group[0])      # manual single move: dot
            else:
                self._overlay.draw_move(group)           # recorded bundle: trajectory
        elif t == EventType.MOUSE_MOVE_TIMED and numeric:
            self._overlay.draw_timed_move(group[0])
        elif t == EventType.MOUSE_CLICK and numeric:
            self._overlay.draw_click(group[0])
        elif t in (EventType.OCR_READ, EventType.MATCH_IMAGE, EventType.MATCH_TEXT) \
                and group[0].monitor is None and numeric \
                and isinstance(group[0].dx, (int, float)) and isinstance(group[0].dy, (int, float)):
            self._overlay.draw_region(group[0])
        else:
            self._overlay.hide()

    # ---------------------------------------------------------- details panel helpers

    def _make_entry_field(self, label_text: str, initial_value, widget_key: str) -> tk.Entry:
        """Create Label + Entry field pair and register widget."""
        tk.Label(self._details_scroll_frame, text=label_text, font=("Arial", 9)).grid(
            row=self._detail_row, column=0, sticky="w", pady=3)
        entry = tk.Entry(self._details_scroll_frame, width=15)
        entry.insert(0, str(initial_value or ""))
        entry.grid(row=self._detail_row, column=1, sticky="ew", padx=5)
        entry.bind("<KeyRelease>", lambda _: self._sync_detail_to_table())
        self._detail_widgets[widget_key] = entry
        self._detail_row += 1
        return entry

    def _make_file_field(self, label_text: str, initial_value, widget_key: str) -> tk.Entry:
        """Create Label + Entry + Browse button for choosing a file path."""
        tk.Label(self._details_scroll_frame, text=label_text, font=("Arial", 9)).grid(
            row=self._detail_row, column=0, sticky="w", pady=3)
        frame = tk.Frame(self._details_scroll_frame)
        frame.grid(row=self._detail_row, column=1, sticky="ew", padx=5)
        entry = tk.Entry(frame, width=13)
        entry.insert(0, str(initial_value or ""))
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<KeyRelease>", lambda _: self._sync_detail_to_table())
        self._detail_widgets[widget_key] = entry

        def browse() -> None:
            path = filedialog.askopenfilename(
                title="Choose reference image",
                filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif"), ("All files", "*.*")],
            )
            if path:
                entry.delete(0, tk.END)
                entry.insert(0, path)
                self._sync_detail_to_table()

        tk.Button(frame, text="…", width=2, command=browse).pack(side=tk.LEFT, padx=2)
        self._detail_row += 1
        return entry

    @staticmethod
    def _parse_tolerance(text: str, default: float) -> float:
        """Parse a 0–1 tolerance/confidence entry, falling back to ``default``."""
        try:
            return float(text)
        except (ValueError, TypeError):
            return default

    def _sync_match_region_fields(self, ev: MacroEvent) -> None:
        """Read the shared region widgets (corners + monitor) into the event."""
        w = self._detail_widgets
        if "x1" in w:
            ev.x = self._parse_field(w["x1"].get())
        if "y1" in w:
            ev.y = self._parse_field(w["y1"].get())
        if "x2" in w:
            ev.dx = self._parse_field(w["x2"].get())
        if "y2" in w:
            ev.dy = self._parse_field(w["y2"].get())
        if "monitor" in w:
            txt = w["monitor"].get().strip()
            ev.monitor = int(txt) if txt.isdigit() else None

    def _make_combobox_field(self, label_text: str, values: list, widget_key: str,
                             initial_value: str, on_change=None) -> ttk.Combobox:
        """Create Label + readonly Combobox field pair.

        ``on_change`` (if given) is called instead of the default table sync when
        the selection changes — used where a choice rebuilds the panel."""
        tk.Label(self._details_scroll_frame, text=label_text, font=("Arial", 9)).grid(
            row=self._detail_row, column=0, sticky="w", pady=3)
        combo = ttk.Combobox(self._details_scroll_frame, values=values, width=12, state="readonly")
        combo.set(initial_value or (values[0] if values else ""))
        combo.grid(row=self._detail_row, column=1, sticky="ew", padx=5)
        handler = on_change if on_change is not None else (lambda _: self._sync_detail_to_table())
        combo.bind("<<ComboboxSelected>>", handler)
        self._detail_widgets[widget_key] = combo
        self._detail_row += 1
        return combo

    def _make_dynamic_values_help(self) -> None:
        """Read-only helper text listing the live values usable in expressions by
        name (e.g. ``rt_ms``).  They are reserved and cannot be assigned to.

        Names are padded to a common width and rendered in a fixed-width font so
        the ``—`` separators line up into a column."""
        width = max(len(name) for name, _desc in DYNAMIC_VALUES)
        lines = ["Dynamic values — type the name in an expression:"]
        lines += [f"{name.ljust(width)} — {desc}" for name, desc in DYNAMIC_VALUES]
        tk.Label(self._details_scroll_frame, text="\n".join(lines), font=("Consolas", 8),
                 foreground="gray", justify=tk.LEFT).grid(
            row=self._detail_row, column=0, columnspan=2, sticky="w", pady=(8, 3))
        self._detail_row += 1

    def _make_check_entry_row(self, label_text: str, var: "tk.BooleanVar", entry_key: str,
                              value_text: str) -> tuple[tk.Checkbutton, tk.Entry]:
        """Create a 'Checkbutton + Entry' row; returns (checkbutton, entry)."""
        chk = tk.Checkbutton(self._details_scroll_frame, text=label_text, variable=var, font=("Arial", 9))
        chk.grid(row=self._detail_row, column=0, sticky="w", pady=3)
        entry = tk.Entry(self._details_scroll_frame, width=12)
        entry.insert(0, value_text)
        entry.grid(row=self._detail_row, column=1, sticky="ew", padx=5)
        entry.bind("<KeyRelease>", lambda _: self._sync_detail_to_table())
        self._detail_widgets[entry_key] = entry
        self._detail_row += 1
        return chk, entry

    def _make_text_timing_options(self, event: MacroEvent) -> None:
        """Two mutually-exclusive Type Text timing options: total duration XOR per-char delay."""
        use_dur = tk.BooleanVar(value=event.duration is not None)
        use_delay = tk.BooleanVar(value=event.char_delay is not None)
        self._detail_widgets["use_duration"] = use_dur
        self._detail_widgets["use_delay"] = use_delay

        dur_chk, dur_entry = self._make_check_entry_row(
            "Over (ms):", use_dur, "duration",
            _duration_field_text(event.duration) if event.duration is not None else "")
        delay_chk, delay_entry = self._make_check_entry_row(
            "Delay/char (ms):", use_delay, "char_delay",
            _duration_field_text(event.char_delay) if event.char_delay is not None else "")

        def refresh():
            dur_entry.config(state="normal" if use_dur.get() else "disabled")
            delay_entry.config(state="normal" if use_delay.get() else "disabled")

        def on_dur():
            if use_dur.get():
                use_delay.set(False)   # XOR
            refresh()
            self._sync_detail_to_table()

        def on_delay():
            if use_delay.get():
                use_dur.set(False)     # XOR
            refresh()
            self._sync_detail_to_table()

        dur_chk.config(command=on_dur)
        delay_chk.config(command=on_delay)
        refresh()

    def _make_coordinate_pair(self, pair_label: str, x_key: str, y_key: str, initial_x, initial_y, targeting_key: str | None = None) -> None:
        """Create horizontal x,y coordinate pair with optional targeting button."""
        tk.Label(self._details_scroll_frame, text=pair_label, font=("Arial", 9)).grid(
            row=self._detail_row, column=0, sticky="w", pady=3)
        frame = tk.Frame(self._details_scroll_frame)
        frame.grid(row=self._detail_row, column=1, sticky="ew", padx=5)

        # Targeting button first, anchored right, so it always keeps its full
        # width; the expanding entry fields only fill the space that remains
        # (otherwise a narrow pane squishes/clips the button).
        if targeting_key:
            tk.Button(frame, text="⊙", width=2, height=1, font=("Arial", 12),
                     padx=0, pady=0, bd=1,
                     command=lambda: self._unified_targeting(targeting_key)).pack(side=tk.RIGHT, padx=2, expand=False)

        # X field
        entry_x = tk.Entry(frame, width=8)
        entry_x.insert(0, str(initial_x or 0))
        entry_x.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry_x.bind("<KeyRelease>", lambda _: self._sync_detail_to_table())
        self._detail_widgets[x_key] = entry_x

        # Comma separator
        tk.Label(frame, text=",", font=("Arial", 9)).pack(side=tk.LEFT, padx=2)

        # Y field
        entry_y = tk.Entry(frame, width=8)
        entry_y.insert(0, str(initial_y or 0))
        entry_y.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry_y.bind("<KeyRelease>", lambda _: self._sync_detail_to_table())
        self._detail_widgets[y_key] = entry_y

        self._detail_row += 1

    # ---------------------------------------------------------- details panel

    def _show_details_panel(self, iid: str | None) -> None:
        """Display edit fields in the side panel for the selected row."""
        # Clear previous widgets (except label field)
        for widget in self._details_scroll_frame.winfo_children():
            widget.destroy()
        self._detail_widgets = {}

        self._selected_iid = iid

        # Show "no selection" message if nothing selected
        if not iid or iid not in self._row_events:
            self._no_selection_label = tk.Label(
                self._details_scroll_frame,
                text="Select an action\nto edit details",
                foreground="gray",
                font=("Arial", 10)
            )
            self._no_selection_label.pack(expand=True)

            # Clear label field
            if "label" in self._detail_widgets:
                self._detail_widgets["label"].delete(0, tk.END)
            return

        group = self._row_events[iid]
        first_event = group[0]

        # Update label field
        if "label" in self._detail_widgets:
            self._detail_widgets["label"].delete(0, tk.END)
            self._detail_widgets["label"].insert(0, first_event.label or "")

        # Build detail fields based on event type
        self._detail_row = 0

        if first_event.type == EventType.MOUSE_MOVE and len(group) == 1:
            self._make_coordinate_pair("Target:", "x", "y", first_event.x or 0, first_event.y or 0, targeting_key="x,y")

        elif first_event.type == EventType.MOUSE_MOVE_TIMED:
            # dx/dy hold the absolute To-coordinate (not a delta).
            self._make_coordinate_pair("From:", "x1", "y1", first_event.x, first_event.y, targeting_key="from")
            self._make_coordinate_pair("To:", "x2", "y2", first_event.dx, first_event.dy, targeting_key="to")
            self._make_entry_field("Duration (ms):", _duration_field_text(first_event.duration), "duration")

        elif first_event.type == EventType.MOUSE_CLICK:
            self._make_combobox_field("Button:", ["left", "right", "middle"], "button", first_event.button or "left")
            self._make_combobox_field("Action:", ["down", "up"], "pressed", "down" if first_event.pressed else "up")
            self._make_coordinate_pair("Position:", "x", "y", first_event.x or 0, first_event.y or 0, targeting_key="x,y")

        elif first_event.type == EventType.MOUSE_SCROLL:
            self._make_entry_field("DX:", first_event.dx or 0, "dx")
            self._make_entry_field("DY:", first_event.dy or 0, "dy")
            self._make_entry_field("X:", first_event.x or 0, "x")
            self._make_entry_field("Y:", first_event.y or 0, "y")

        elif first_event.type in (EventType.KEY_PRESS, EventType.KEY_RELEASE):
            self._make_entry_field("Key:", first_event.key or "", "key")

        elif first_event.type == EventType.TYPE_TEXT:
            self._make_entry_field("Text:", first_event.expr or "", "expr")
            self._make_text_timing_options(first_event)

        elif first_event.type == EventType.WAIT:
            self._make_entry_field("Duration (ms):", _duration_field_text(first_event.duration), "duration")

        elif first_event.type == EventType.VAR_SET:
            self._make_entry_field("Variable:", first_event.var_name or "", "var_name")
            self._make_entry_field("Expression:", first_event.expr or "", "expr")
            self._make_dynamic_values_help()

        elif first_event.type == EventType.OCR_READ:
            # x,y and dx,dy are two opposite corners (any orientation); the region
            # is the bounding box between them.
            self._make_entry_field("Variable:", first_event.var_name or "", "var_name")
            self._make_coordinate_pair("Corner 1:", "x1", "y1", first_event.x, first_event.y, targeting_key="from")
            self._make_coordinate_pair("Corner 2:", "x2", "y2", first_event.dx, first_event.dy, targeting_key="to")

        elif first_event.type == EventType.MATCH_IMAGE:
            self._make_entry_field("Found var:", first_event.var_name or "", "var_name")
            self._make_file_field("Image file:", first_event.image_path or "", "image_path")
            self._make_coordinate_pair("Corner 1:", "x1", "y1", first_event.x, first_event.y, targeting_key="from")
            self._make_coordinate_pair("Corner 2:", "x2", "y2", first_event.dx, first_event.dy, targeting_key="to")
            self._make_entry_field("Monitor (blank=area):", first_event.monitor if first_event.monitor is not None else "", "monitor")
            self._make_entry_field("Confidence (0-1):", first_event.tolerance if first_event.tolerance is not None else "", "tolerance")
            self._make_entry_field("Timeout (ms):", _duration_field_text(first_event.duration), "duration")
            self._make_entry_field("Capture X var:", first_event.capture_var or "", "capture_var")
            self._make_entry_field("Capture Y var:", first_event.capture_var_y or "", "capture_var_y")

        elif first_event.type == EventType.MATCH_TEXT:
            self._make_entry_field("Found var:", first_event.var_name or "", "var_name")
            self._make_entry_field("Expected text:", first_event.expr or "", "expr")
            self._make_coordinate_pair("Corner 1:", "x1", "y1", first_event.x, first_event.y, targeting_key="from")
            self._make_coordinate_pair("Corner 2:", "x2", "y2", first_event.dx, first_event.dy, targeting_key="to")
            self._make_entry_field("Monitor (blank=area):", first_event.monitor if first_event.monitor is not None else "", "monitor")
            self._make_entry_field("Tolerance (0-1):", first_event.tolerance if first_event.tolerance is not None else "", "tolerance")
            self._make_entry_field("Timeout (ms):", _duration_field_text(first_event.duration), "duration")
            self._make_entry_field("Capture text var:", first_event.capture_var or "", "capture_var")

        elif first_event.type == EventType.GOTO:
            self._make_goto_target_fields(first_event)

        elif first_event.type == EventType.GOTO_IF:
            self._make_entry_field("Condition:", first_event.expr or "", "expr")
            self._make_goto_target_fields(first_event)

        self._details_scroll_frame.grid_columnconfigure(1, weight=1)

    @staticmethod
    def _parse_field(text: str):
        """Parse a numeric-or-expression field.

        Returns an int or float when the text is numeric, otherwise the trimmed
        string, which is treated as a variable expression and evaluated at
        playback time (so coordinates etc. may reference variables).
        """
        text = text.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            return text

    @staticmethod
    def _parse_duration(text: str):
        """Parse a 'Duration (ms)' entry.

        A numeric entry is converted ms→seconds and stored as a number; an
        expression is stored verbatim (a ms-expression resolved at playback).
        """
        value = MacroRecorderApp._parse_field(text)
        if isinstance(value, (int, float)):
            return value / 1000.0
        return value

    def _sync_detail_to_table(self) -> None:
        """Sync changes from the details panel back to the stored event and table display."""
        if not self._selected_iid or self._selected_iid not in self._row_events:
            return

        group = self._row_events[self._selected_iid]
        first_event = group[0]

        # Update label
        if "label" in self._detail_widgets:
            new_label = self._detail_widgets["label"].get()
            first_event.label = new_label if new_label else None

        # Update event-specific fields
        try:
            if first_event.type == EventType.MOUSE_MOVE and len(group) == 1:
                if "x" in self._detail_widgets:
                    first_event.x = self._parse_field(self._detail_widgets["x"].get())
                if "y" in self._detail_widgets:
                    first_event.y = self._parse_field(self._detail_widgets["y"].get())

            elif first_event.type == EventType.MOUSE_MOVE_TIMED:
                # From → x/y, To (absolute) → dx/dy; all may be expressions.
                if "x1" in self._detail_widgets:
                    first_event.x = self._parse_field(self._detail_widgets["x1"].get())
                if "y1" in self._detail_widgets:
                    first_event.y = self._parse_field(self._detail_widgets["y1"].get())
                if "x2" in self._detail_widgets:
                    first_event.dx = self._parse_field(self._detail_widgets["x2"].get())
                if "y2" in self._detail_widgets:
                    first_event.dy = self._parse_field(self._detail_widgets["y2"].get())
                if "duration" in self._detail_widgets:
                    first_event.duration = self._parse_duration(self._detail_widgets["duration"].get())

            elif first_event.type == EventType.MOUSE_CLICK:
                if "button" in self._detail_widgets:
                    first_event.button = self._detail_widgets["button"].get()
                if "pressed" in self._detail_widgets:
                    first_event.pressed = self._detail_widgets["pressed"].get() == "down"
                if "x" in self._detail_widgets:
                    first_event.x = self._parse_field(self._detail_widgets["x"].get())
                if "y" in self._detail_widgets:
                    first_event.y = self._parse_field(self._detail_widgets["y"].get())

            elif first_event.type == EventType.MOUSE_SCROLL:
                if "dx" in self._detail_widgets:
                    first_event.dx = self._parse_field(self._detail_widgets["dx"].get())
                if "dy" in self._detail_widgets:
                    first_event.dy = self._parse_field(self._detail_widgets["dy"].get())
                if "x" in self._detail_widgets:
                    first_event.x = self._parse_field(self._detail_widgets["x"].get())
                if "y" in self._detail_widgets:
                    first_event.y = self._parse_field(self._detail_widgets["y"].get())

            elif first_event.type in (EventType.KEY_PRESS, EventType.KEY_RELEASE):
                if "key" in self._detail_widgets:
                    first_event.key = self._detail_widgets["key"].get()

            elif first_event.type == EventType.TYPE_TEXT:
                if "expr" in self._detail_widgets:
                    # Stored verbatim — it's a template, not a parsed value.
                    first_event.expr = self._detail_widgets["expr"].get()
                # Timing: at most one of duration / char_delay is active (XOR).
                use_dur = self._detail_widgets.get("use_duration")
                use_delay = self._detail_widgets.get("use_delay")
                if use_dur is not None and use_dur.get():
                    txt = self._detail_widgets["duration"].get().strip()
                    first_event.duration = self._parse_duration(txt) if txt else None
                    first_event.char_delay = None
                elif use_delay is not None and use_delay.get():
                    txt = self._detail_widgets["char_delay"].get().strip()
                    first_event.char_delay = self._parse_duration(txt) if txt else None
                    first_event.duration = None
                else:
                    first_event.duration = None
                    first_event.char_delay = None

            elif first_event.type == EventType.WAIT:
                if "duration" in self._detail_widgets:
                    first_event.duration = self._parse_duration(self._detail_widgets["duration"].get())

            elif first_event.type == EventType.VAR_SET:
                if "var_name" in self._detail_widgets:
                    first_event.var_name = self._detail_widgets["var_name"].get()
                if "expr" in self._detail_widgets:
                    first_event.expr = self._detail_widgets["expr"].get()

            elif first_event.type == EventType.OCR_READ:
                if "var_name" in self._detail_widgets:
                    first_event.var_name = self._detail_widgets["var_name"].get()
                if "x1" in self._detail_widgets:
                    first_event.x = self._parse_field(self._detail_widgets["x1"].get())
                if "y1" in self._detail_widgets:
                    first_event.y = self._parse_field(self._detail_widgets["y1"].get())
                if "x2" in self._detail_widgets:
                    first_event.dx = self._parse_field(self._detail_widgets["x2"].get())
                if "y2" in self._detail_widgets:
                    first_event.dy = self._parse_field(self._detail_widgets["y2"].get())

            elif first_event.type == EventType.MATCH_IMAGE:
                if "var_name" in self._detail_widgets:
                    first_event.var_name = self._detail_widgets["var_name"].get()
                if "image_path" in self._detail_widgets:
                    first_event.image_path = self._detail_widgets["image_path"].get()
                self._sync_match_region_fields(first_event)
                if "tolerance" in self._detail_widgets:
                    first_event.tolerance = self._parse_tolerance(self._detail_widgets["tolerance"].get(), 0.9)
                if "duration" in self._detail_widgets:
                    first_event.duration = self._parse_duration(self._detail_widgets["duration"].get())
                if "capture_var" in self._detail_widgets:
                    first_event.capture_var = self._detail_widgets["capture_var"].get() or None
                if "capture_var_y" in self._detail_widgets:
                    first_event.capture_var_y = self._detail_widgets["capture_var_y"].get() or None

            elif first_event.type == EventType.MATCH_TEXT:
                if "var_name" in self._detail_widgets:
                    first_event.var_name = self._detail_widgets["var_name"].get()
                if "expr" in self._detail_widgets:
                    first_event.expr = self._detail_widgets["expr"].get()
                self._sync_match_region_fields(first_event)
                if "tolerance" in self._detail_widgets:
                    first_event.tolerance = self._parse_tolerance(self._detail_widgets["tolerance"].get(), 0.8)
                if "duration" in self._detail_widgets:
                    first_event.duration = self._parse_duration(self._detail_widgets["duration"].get())
                if "capture_var" in self._detail_widgets:
                    first_event.capture_var = self._detail_widgets["capture_var"].get() or None

            elif first_event.type == EventType.GOTO:
                self._sync_goto_target(first_event)

            elif first_event.type == EventType.GOTO_IF:
                if "expr" in self._detail_widgets:
                    first_event.expr = self._detail_widgets["expr"].get()
                self._sync_goto_target(first_event)

        except (ValueError, tk.TclError):
            # Ignore conversion errors while user is typing
            return

        # Update table display
        value = self._format_value(first_event)
        vals = list(self.table.item(self._selected_iid, "values"))
        vals[2] = value  # Value column is always index 2
        vals[3] = first_event.label or ""  # Label column is always index 3
        self.table.item(self._selected_iid, values=vals)

        # Redraw the on-screen preview so it tracks edits / targeting captures.
        self._draw_overlay_for_group(group)

    def _unified_targeting(self, targeting_key: str) -> None:
        """Unified targeting mode for any coordinate pair.

        Uses pynput to listen for global mouse clicks, same as the recorder.

        Parameters
        ----------
        targeting_key:
            One of: "from" (x1,y1), "to" (x2,y2), or "x,y" (single pair)
        """
        from pynput import mouse

        # Parse targeting_key to determine which coordinate fields to update
        if targeting_key == "from":
            coord_keys = ("x1", "y1")
        elif targeting_key == "to":
            coord_keys = ("x2", "y2")
        else:
            # Handle "x,y" format
            coord_keys = tuple(targeting_key.split(","))

        # Create instruction window in top-left corner so it doesn't block clicks
        instr_window = tk.Toplevel(self.root)
        instr_window.title("Targeting Mode")
        instr_window.geometry("300x80+10+10")
        instr_window.attributes("-topmost", True)

        tk.Label(instr_window, text="Click the target on screen", font=("Arial", 11, "bold"), foreground="blue").pack(pady=5)
        tk.Label(instr_window, text="(the click is captured, not sent — Esc to cancel)",
                 font=("Arial", 9), foreground="blue").pack()

        # Precompute the instruction window's screen rect on the main thread so
        # the hook callback (other thread) never has to query Tk.
        self.root.update_idletasks()
        iwx, iwy = instr_window.winfo_rootx(), instr_window.winfo_rooty()
        iww, iwh = instr_window.winfo_width(), instr_window.winfo_height()

        def _on_instr_window(px, py) -> bool:
            return iwx <= px <= iwx + iww and iwy <= py <= iwy + iwh

        targeting_active = [True]
        listener = [None]
        captured = [None]   # (x, y) recorded from the first button-down

        # Win32 mouse-button messages (down/up for left, right, middle).
        _DOWN_MSGS = {0x0201, 0x0204, 0x0207}
        _BUTTON_MSGS = _DOWN_MSGS | {0x0202, 0x0205, 0x0208}

        def cleanup():
            """Apply the captured point (if any) and tear down targeting."""
            targeting_active[0] = False
            if listener[0] is not None:
                listener[0].stop()
            if (captured[0] is not None and self._selected_iid
                    and self._selected_iid in self._row_events):
                x_key, y_key = coord_keys
                cx, cy = captured[0]
                if x_key in self._detail_widgets:
                    self._detail_widgets[x_key].delete(0, tk.END)
                    self._detail_widgets[x_key].insert(0, str(cx))
                if y_key in self._detail_widgets:
                    self._detail_widgets[y_key].delete(0, tk.END)
                    self._detail_widgets[y_key].insert(0, str(cy))
                self._sync_detail_to_table()
            try:
                instr_window.destroy()
            except Exception:
                pass

        def win32_event_filter(msg, data):
            """Windows hook filter: capture the click coords and swallow it.

            Capturing happens here (not in on_click) because suppressing an
            event stops pynput from dispatching it to on_click.  Calling
            suppress_event() raises to signal suppression, so nothing after it
            runs and it must not be wrapped in try/except.
            """
            if not targeting_active[0] or msg not in _BUTTON_MSGS:
                return
            px, py = data.pt.x, data.pt.y
            if _on_instr_window(px, py):
                return  # let clicks on the instruction window through (e.g. close)
            if msg in _DOWN_MSGS and captured[0] is None:
                captured[0] = (int(px), int(py))
                # Finish slightly later so the matching button-up is swallowed too.
                self.root.after(120, cleanup)
            if listener[0] is not None:
                listener[0].suppress_event()   # consumes the event (raises)

        def on_click(x_pos, y_pos, button, pressed):
            """Fallback capture for non-Windows, where the filter never runs."""
            if not targeting_active[0] or not pressed or _on_instr_window(x_pos, y_pos):
                return True
            if captured[0] is None:
                captured[0] = (int(x_pos), int(y_pos))
                self.root.after(0, cleanup)
            return False

        # Esc cancels (keyboard isn't suppressed by the mouse filter).
        instr_window.bind("<Escape>", lambda _: cleanup())
        instr_window.protocol("WM_DELETE_WINDOW", cleanup)
        instr_window.focus_force()

        # Start global mouse listener; the filter consumes the click on Windows.
        listener[0] = mouse.Listener(on_click=on_click, win32_event_filter=win32_event_filter)
        listener[0].start()

    def _on_close(self) -> None:
        if self._recording and self._recorder:
            self._recorder.stop()
        if self._playing and self._player:
            self._player.stop()
        self._overlay.hide()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MacroRecorderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
