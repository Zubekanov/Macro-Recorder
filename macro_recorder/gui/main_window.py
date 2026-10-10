"""The GUI shell: menus, ribbon tabs, footer, modes and the event-queue pump.

Worker threads (player, recorder hotkeys, targeting, logging) never touch
Tk.  They :meth:`MainWindow.post` ``(kind, payload)`` to one
``queue.SimpleQueue``; a main-thread pump drains it every 50 ms while not
idle and every 200 ms when idle.  Each tick appends all log lines, applies
only the most recent active-row report, then handles paused, ask and
finished in arrival order, then runs marshalled calls.

The helpers above :class:`MainWindow` are Tk-free and unit-tested.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import queue
import re
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable, Sequence

from macro_recorder.engine.recorder import Recorder
from macro_recorder.gui import dialogs
from macro_recorder.gui import settings as settings_module
from macro_recorder.gui.commands import IDLE_ONLY, Command, CommandRegistry
from macro_recorder.gui.details import DetailsPanel
from macro_recorder.gui.hotkeys import HotkeyActions, HotkeyRouter
from macro_recorder.gui.log_panel import LogPanel
from macro_recorder.gui.overlay import Overlay
from macro_recorder.gui.playback import (
    AskRequest, Platform, PlaybackCallbacks, PlaybackController,
)
from macro_recorder.gui.recording import RecordingCallbacks, RecordingController
from macro_recorder.gui.settings import ON_MISSING_CHOICES, Settings
from macro_recorder.gui.table import ActionTable, preset_command, shifted_index
from macro_recorder.gui.targeting import Capture, TargetingSession
from macro_recorder.model import catalogue, fileformat, validate
from macro_recorder.model.catalogue import CATEGORIES, FILTER_GROUPS
from macro_recorder.model.document import PATH_MODES, MacroDocument, Row, make_row
from macro_recorder.model.keys import format_combo, parse_combo
from macro_recorder.model.protocols import Change, Mode, OverlayShape
from macro_recorder.platform.session import OwnWindows, SessionProblem

log = logging.getLogger(__name__)

APP_NAME = "Macro Recorder"
IDLE_PUMP_MS = 200
BUSY_PUMP_MS = 50
DETAILS_WIDTH = 400
MIN_LAYOUT_WIDTH = 100     # narrower Configure events come before the first real layout
CLIPBOARD_KEY = "macro_recorder_rows"
VIEW_MODES = frozenset(Mode) - {Mode.PAUSED}
_WHOLE_NUMBER = re.compile(r"[0-9]+")
FILTER_LABELS = {
    "mouse_move": "Skip mouse moves", "mouse_click": "Skip mouse clicks",
    "mouse_scroll": "Skip mouse scrolls", "keyboard": "Skip keyboard", "wait": "Skip waits",
}
_ROW_ERROR = re.compile(r"^Row (\d+):")


# ---------------------------------------------------------------------------
# Tk-free helpers
# ---------------------------------------------------------------------------

def display_name(path: str | None) -> str:
    """The document's name for the title and prompts: the file stem or Untitled."""
    return Path(path).stem if path else "Untitled"


def window_title(path: str | None, dirty: bool) -> str:
    """``name* - Macro Recorder``; the asterisk marks unsaved changes."""
    return "%s%s - %s" % (display_name(path), "*" if dirty else "", APP_NAME)


def recent_entries(recent: Sequence[str], home: str | None = None) -> list[tuple[str, str]]:
    """``(menu label, path)`` for the Recent menu: numbered, home shown as ``~``."""
    home = home if home is not None else str(Path.home())
    out = []
    for number, path in enumerate(recent[:settings_module.RECENT_LIMIT], 1):
        shown = path
        if home and (path == home or path.startswith(home.rstrip("/") + "/")):
            shown = "~" + path[len(home.rstrip("/")):]
        out.append(("%d  %s" % (number, shown), path))
    return out


@dataclass
class EventBatch:
    """One pump tick's worth of queue events, sorted by the pump's rules."""
    logs: list[str] = field(default_factory=list)
    row: int | None = None
    player: list[tuple[str, Any]] = field(default_factory=list)   # paused, ask, finished
    calls: list[Callable[[], None]] = field(default_factory=list)


PLAYER_EVENTS = ("paused", "ask", "finished")


def drain(events: "queue.SimpleQueue[tuple[str, Any]]") -> EventBatch:
    """Empty the queue: every log line, the latest row report, then the player
    events and calls in arrival order."""
    batch = EventBatch()
    while True:
        try:
            kind, payload = events.get_nowait()
        except queue.Empty:
            return batch
        if kind == "log":
            batch.logs.append(str(payload))
        elif kind == "row":
            batch.row = payload
        elif kind in PLAYER_EVENTS:
            batch.player.append((kind, payload))
        elif kind == "call":
            batch.calls.append(payload)
        else:
            log.error("Unknown queue event %r", kind)


class Stopwatch:
    """Elapsed time of a recording or a playback, excluding pauses."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started: float | None = None
        self._total = 0.0

    def start(self) -> None:
        self._total = 0.0
        self._started = self._clock()

    def pause(self) -> None:
        if self._started is not None:
            self._total += self._clock() - self._started
            self._started = None

    def resume(self) -> None:
        if self._started is None:
            self._started = self._clock()

    def elapsed(self) -> float:
        running = self._clock() - self._started if self._started is not None else 0.0
        return self._total + running


def format_elapsed(seconds: float) -> str:
    """``m:ss.t`` (``h:mm:ss`` from an hour)."""
    seconds = max(0.0, seconds)
    if seconds >= 3600:
        whole = int(seconds)
        return "%d:%02d:%02d" % (whole // 3600, whole // 60 % 60, whole % 60)
    tenths = int(seconds * 10)
    return "%d:%02d.%d" % (tenths // 600, tenths // 10 % 60, tenths % 10)


def parse_speed(text: str) -> float | None:
    """Playback speed: a number above 0, up to 100."""
    try:
        value = float(text.strip())
    except ValueError:
        return None
    return value if 0 < value <= 100 else None


def parse_repeat(text: str) -> int | None:
    """Repeat count: a whole number, 0 meaning until stopped; blank is invalid."""
    text = text.strip()
    return int(text) if _WHOLE_NUMBER.fullmatch(text) else None


def parse_ms(text: str) -> int | None:
    """A whole number of milliseconds, 0 or more."""
    text = text.strip()
    return int(text) if _WHOLE_NUMBER.fullmatch(text) else None


def clipboard_text(rows: Sequence[Row]) -> str:
    """Rows in the file format, as the clipboard carries them."""
    return json.dumps({CLIPBOARD_KEY: fileformat.rows_to_dicts(rows)})


def rows_from_clipboard(text: str) -> list[Row] | None:
    """Rows from clipboard text, or None when the text is not copied rows.

    Raises FormatError when it is copied rows that do not read back.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or CLIPBOARD_KEY not in data:
        return None
    return fileformat.rows_from_dicts(data[CLIPBOARD_KEY])


def insertion_index(document: MacroDocument, selected: Sequence[int]) -> int:
    """Where new rows go: below the last selected row, or at the end."""
    if selected:
        index = document.row_index()
        positions = [index[i] for i in selected if i in index]
        if positions:
            return max(positions) + 1
    return len(document.rows)


def ending_edit(document: MacroDocument, command: Command) -> Command:
    """``command`` changed to end the document's open edit session first, so
    no command merges into a Details edit's undo step."""
    run = command.run

    def run_ending_edit(*args: Any) -> Any:
        document.end_edit()
        return run(*args)
    return dataclasses.replace(command, run=run_ending_edit)


def shortcut_help(settings: Settings) -> str:
    """The Help tab's shortcut text."""
    inserts = ", ".join("%s %s" % (p.insert_key, p.name)
                        for _s, p in catalogue.presets() if p.insert_key)

    def combo(text: str) -> str:
        try:
            return format_combo(parse_combo(text))
        except ValueError:
            return text
    return "\n".join((
        "Ctrl+N New, Ctrl+O Open, Ctrl+S Save, Ctrl+Shift+S Save As",
        "Ctrl+Z Undo, Ctrl+Y Redo, F5 Play or Continue, F9 Record or Stop",
        "In the table: Ctrl+X Cut, Ctrl+C Copy, Ctrl+V Paste, Ctrl+A Select all, "
        "Ctrl+E Toggle enabled, F8 Toggle breakpoint, Delete, Alt+Up and Alt+Down Move, "
        "Return Edit label, F2 Edit comment",
        "Insert keys in the table: %s" % inserts,
        "Anywhere: %s Record or Stop, %s Play or Continue; F6 stops recording, "
        "Escape stops playback" % (combo(settings.hotkeys.record), combo(settings.hotkeys.play)),
    ))


class OwnWindowTracker:
    """Registers the application's toplevels in ``OwnWindows``.

    ``resolve`` turns Tk's ``winfo_id`` into the X window that carries the
    toplevel (Tk's wrapper).  The resolved id is remembered by widget path,
    because a destroyed widget can no longer be asked.
    """

    def __init__(self, own_windows: OwnWindows, resolve: Callable[[int], int]) -> None:
        self._own = own_windows
        self._resolve = resolve
        self._ids: dict[str, int] = {}

    def mapped(self, path: str, inner_xid: int) -> None:
        if path in self._ids:
            return
        try:
            xid = self._resolve(inner_xid)
        except Exception as e:
            log.debug("Resolving the X window of %s failed: %s", path, e)
            xid = inner_xid
        self._ids[path] = xid
        self._own.add(xid)

    def destroyed(self, path: str) -> None:
        xid = self._ids.pop(path, None)
        if xid is not None:
            self._own.remove(xid)

    def registered(self) -> dict[str, int]:
        return dict(self._ids)


class X11ToplevelIds:
    """``resolve`` for :class:`OwnWindowTracker` over one lazily opened X connection."""

    def __init__(self) -> None:
        self._display: Any = None

    def __call__(self, inner_xid: int) -> int:
        from macro_recorder.platform import x11
        if self._display is None:
            self._display = x11.open_display()
        return x11.tk_toplevel_xid(self._display, inner_xid)

    def close(self) -> None:
        display, self._display = self._display, None
        if display is not None:
            display.close()


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

class MainWindow:
    """The application window over one document."""

    def __init__(self, root: tk.Tk, settings: Settings, *, own_windows: OwnWindows,
                 platform: Platform | None, session_problem: SessionProblem | None = None,
                 resolve_xid: Callable[[int], int] | None = None,
                 save_settings: Callable[[Settings], None] = settings_module.save_settings) -> None:
        self.root = root
        self.settings = settings
        self.queue: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        path_mode = settings.playback.path_mode if settings.playback.path_mode in PATH_MODES else "recorded"
        self.document = MacroDocument(path_mode=path_mode)
        self.mode = Mode.IDLE
        self.commands = CommandRegistry()
        self.platform = platform
        self.session_problem = session_problem
        self._own_windows = own_windows
        self._save_settings = save_settings
        self._tracker = OwnWindowTracker(own_windows, resolve_xid or (lambda xid: xid))
        self._resolver = resolve_xid
        self._router: HotkeyRouter | None = None
        self._hotkeys_grabbed = False
        self._overlay: Overlay | None = None
        self._shape: OverlayShape | None = None
        self._paused_row: int | None = None
        self._asks: list[dialogs.MessageBoxDialog] = []
        self._targeting: TargetingSession | None = None
        self._last_inserted: tuple[int, ...] = ()
        self._stopwatch = Stopwatch()
        self._pump_job: str | None = None
        self._pump_interval = IDLE_PUMP_MS
        self._closing = False
        self._menu_entries: dict[str, list[tuple[int, str]]] = {}
        self._buttons: list[tuple[ttk.Widget, str]] = []
        self._category_buttons: list[tuple[ttk.Menubutton, str]] = []
        self._idle_widgets: list[ttk.Widget] = []
        self._playback_problems: dict[str, str] = {}
        self.targeting_factory: Callable[..., TargetingSession] = TargetingSession
        self.capture_key: Callable[[bool], str | None] = (
            lambda single: dialogs.capture_key(self.root, single))
        self.ask_unsaved: Callable[[str], str] = lambda name: dialogs.ask_unsaved(self.root, name)
        self.show_message: Callable[[str, str], None] = (
            lambda title, text: dialogs.show_message(self.root, title, text))

        self.playback: PlaybackController | None = None
        self.recording: RecordingController | None = None
        if platform is not None:
            self.playback = PlaybackController(self.document, settings, platform, self.post,
                                               self._playback_callbacks())
            self.recording = RecordingController(self.document, self._new_recorder, settings,
                                                 self._recording_callbacks())

        self._register_commands()
        self._build_ui()
        self._bind_shortcuts()
        self._track_toplevels()
        if session_problem is not None or platform is None:
            for name in ("record.start", "record.toggle", "play.play"):
                self.commands.set_available(name, False)
        self.commands.subscribe(self._refresh_widgets)
        self.document.subscribe(self._on_document_change)
        self.root.protocol("WM_DELETE_WINDOW", self.exit)
        self._refresh_widgets()
        self._update_title()
        self._update_footer()
        self._schedule_pump()

    # -- marshalling ---------------------------------------------------------

    def post(self, kind: str, payload: Any = None) -> None:
        """Queue an event for the main thread.  Safe from any thread."""
        self.queue.put((kind, payload))

    def _schedule_pump(self) -> None:
        if self._closing:
            return
        self._pump_interval = IDLE_PUMP_MS if self.mode is Mode.IDLE else BUSY_PUMP_MS
        self._pump_job = self.root.after(self._pump_interval, self._pump)

    def _reschedule_pump(self) -> None:
        wanted = IDLE_PUMP_MS if self.mode is Mode.IDLE else BUSY_PUMP_MS
        if self._pump_job is not None and wanted != self._pump_interval:
            self.root.after_cancel(self._pump_job)
            self._pump_job = None
            self._schedule_pump()

    def _pump(self) -> None:
        self._pump_job = None
        try:
            self.process(drain(self.queue))
            if self.mode is Mode.RECORDING and self.recording is not None:
                self.recording.poll()
            self._update_elapsed()
        finally:
            self._schedule_pump()

    def process(self, batch: EventBatch) -> None:
        """Apply one batch in the pump's order."""
        if batch.logs:
            self.log_panel.append(batch.logs)
        playback = self.playback
        if batch.row is not None and playback is not None:
            playback.handle_row(batch.row)
        for kind, payload in batch.player:
            if playback is None:
                continue
            try:
                if kind == "paused":
                    playback.handle_paused(payload)
                elif kind == "ask":
                    playback.handle_ask(payload)
                else:
                    playback.handle_finished(payload)
            except Exception:
                log.exception("Handling the player's %r event failed", kind)
        for call in batch.calls:
            try:
                call()
            except Exception:
                log.exception("A queued call failed")

    # -- modes ---------------------------------------------------------------

    def set_mode(self, mode: Mode) -> None:
        """Switch modes: end edit sessions, lock or unlock the editors, update
        commands, hotkeys, overlay, stopwatch and pump rate."""
        previous = self.mode
        if mode == previous:
            return
        self.document.end_edit()
        self.mode = mode
        if mode is not Mode.IDLE:
            self.table.cancel_inline_edit()
            self.details.revert_pending()
        self.details.set_locked(mode is not Mode.IDLE)
        self.table.set_mode(mode)
        self.commands.set_mode(mode)
        if self._router is not None:
            self._router.mode_changed(mode)
        if mode in (Mode.RECORDING, Mode.PLAYING) and previous in (Mode.IDLE, Mode.TARGETING):
            self._stopwatch.start()
        elif mode is Mode.PAUSED:
            self._stopwatch.pause()
        elif mode is Mode.PLAYING:
            self._stopwatch.resume()
        elif mode is Mode.IDLE:
            self._stopwatch.pause()
        if mode is not Mode.PAUSED:
            self._paused_row = None
        if previous is Mode.RECORDING and mode is Mode.IDLE and self._last_inserted:
            self.table.select(self._last_inserted)
        self._play_text.set(self.playback.play_label if self.playback is not None else "Play")
        for widget in self._idle_widgets:
            widget.state(["!disabled"] if mode is Mode.IDLE else ["disabled"])
        self._update_overlay()
        self._update_elapsed()
        self._reschedule_pump()

    # -- controllers ---------------------------------------------------------

    def _playback_callbacks(self) -> PlaybackCallbacks:
        return PlaybackCallbacks(
            set_mode=self.set_mode,
            highlight=self._highlight,
            show_variables=lambda variables: self.log_panel.show_variables(variables),
            log=lambda line: self.log_panel.append([line]),
            report_error=self._report_run_error,
            show_ask=self._show_ask,
            close_asks=self._close_asks,
        )

    def _recording_callbacks(self) -> RecordingCallbacks:
        return RecordingCallbacks(
            show_provisional=lambda rows: self.table.show_provisional(rows),
            clear_provisional=lambda: self.table.clear_provisional(),
            set_mode=lambda mode: self.set_mode(Mode(mode)),
            report_error=lambda message: self._report_error("Recording", message),
        )

    def _new_recorder(self) -> Recorder:
        assert self.platform is not None
        return Recorder(self.platform.new_listener(), self.platform.windows(), self._own_windows,
                        record_combo=self.settings.hotkeys.record)

    def _highlight(self, row_id: int | None) -> None:
        self.table.highlight(row_id)
        if self.mode is Mode.PAUSED:
            self._paused_row = row_id
            self._update_overlay()

    def _show_ask(self, request: AskRequest) -> None:
        def answered(answer: str) -> None:
            if dialog in self._asks:
                self._asks.remove(dialog)
            request.reply(answer)
        dialog = dialogs.MessageBoxDialog(self.root, request.title, request.text, request.buttons,
                                          on_answer=answered, on_stop=self.stop_playback)
        self._asks.append(dialog)

    def _close_asks(self) -> None:
        asks, self._asks = self._asks, []
        for dialog in asks:
            dialog.close()

    def _report_run_error(self, message: str) -> None:
        """Show the error that ended a run and select the row it names."""
        match = _ROW_ERROR.match(message)
        if match:
            index = int(match.group(1)) - 1
            if 0 <= index < len(self.document.rows):
                self.table.select([self.document.rows[index].id])
        self._report_error("Playback stopped", message)

    def _report_error(self, title: str, message: str) -> None:
        self.log_panel.append(["%s: %s" % (title, message)])
        self.root.after_idle(lambda: self.show_message(title, message))

    # -- commands ------------------------------------------------------------

    def _register_commands(self) -> None:
        idle = IDLE_ONLY
        sel = lambda: bool(self.table.selected_ids())
        single = lambda: len(self.table.selected_ids()) == 1
        reg = self._register
        reg(Command("file.new", "New", self.new_document, idle, "Ctrl+N"))
        reg(Command("file.open", "Open...", self.open_document, idle, "Ctrl+O"))
        reg(Command("file.open_recent", "Open recent", self.open_path, idle))
        reg(Command("file.save", "Save", self.save, idle, "Ctrl+S"))
        reg(Command("file.save_as", "Save As...", self.save_as, idle, "Ctrl+Shift+S"))
        reg(Command("file.settings", "Settings...", self.open_settings, idle))
        reg(Command("file.exit", "Exit", self.exit, idle))
        reg(Command("edit.undo", "Undo", self.undo, idle, "Ctrl+Z",
                    when=lambda: self.document.can_undo))
        reg(Command("edit.redo", "Redo", self.redo, idle, "Ctrl+Y",
                    when=lambda: self.document.can_redo))
        reg(Command("edit.cut", "Cut", self.cut, idle, "Ctrl+X", when=sel))
        reg(Command("edit.copy", "Copy", self.copy, idle, "Ctrl+C", when=sel))
        reg(Command("edit.paste", "Paste", self.paste, idle, "Ctrl+V"))
        reg(Command("edit.delete", "Delete", self.delete, idle, "Delete", when=sel))
        reg(Command("edit.split", "Split path", self.split, idle,
                    when=lambda: any(self.document.row(i).is_path for i in self.table.selected_ids())))
        reg(Command("edit.toggle_enabled", "Toggle enabled", self.toggle_enabled, idle, "Ctrl+E",
                    when=sel))
        reg(Command("edit.toggle_breakpoint", "Toggle breakpoint", self.toggle_breakpoint,
                    frozenset({Mode.IDLE, Mode.PAUSED}), "F8",
                    when=lambda: bool(self._breakpoint_targets())))
        reg(Command("edit.select_all", "Select All", self.select_all, idle, "Ctrl+A",
                    when=lambda: bool(self.document.rows)))
        reg(Command("edit.move_up", "Move up", lambda: self.move_selection(-1), idle, "Alt+Up",
                    when=sel))
        reg(Command("edit.move_down", "Move down", lambda: self.move_selection(1), idle,
                    "Alt+Down", when=sel))
        reg(Command("edit.edit_label", "Edit label", self.edit_label, idle, "Return", when=single))
        reg(Command("edit.edit_comment", "Edit comment", self.edit_comment, idle, "F2",
                    when=single))
        for _spec, preset in catalogue.presets():
            reg(Command(preset_command(preset.name), preset.name,
                        lambda p=preset: self.insert_rows([make_row(p.make())]), idle,
                        preset.insert_key))
        reg(Command("record.start", "Record", self.start_recording, idle))
        reg(Command("record.stop", "Stop", self.stop_recording, frozenset({Mode.RECORDING})))
        reg(Command("record.toggle", "Record or Stop", self.toggle_recording,
                    frozenset({Mode.IDLE, Mode.RECORDING}), "F9"))
        reg(Command("play.play", "Play", self.play_or_continue,
                    frozenset({Mode.IDLE, Mode.PAUSED}), "F5"))
        reg(Command("play.stop", "Stop", self.stop_playback,
                    frozenset({Mode.PLAYING, Mode.PAUSED})))
        reg(Command("view.log", "Output log", self.toggle_log, VIEW_MODES))
        reg(Command("view.overlay", "Overlay", self.toggle_overlay, VIEW_MODES))
        reg(Command("help.shortcuts", "Keyboard shortcuts", self.show_shortcuts, idle))
        reg(Command("help.about", "About", self.show_about, idle))

    def _register(self, command: Command) -> None:
        self.commands.register(ending_edit(self.document, command))

    # -- UI ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = self.root
        root.title(window_title(None, False))
        try:
            root.geometry(self.settings.ui.geometry)
        except tk.TclError:
            log.warning("Ignoring the saved window geometry %r", self.settings.ui.geometry)
        style = ttk.Style(root)
        style.configure("Invalid.TEntry", foreground="red")
        style.configure("Invalid.TSpinbox", foreground="red")

        header = ttk.Frame(root, padding=(4, 4, 4, 0))
        header.pack(side="top", fill="x")
        self._build_menus(header)
        self.ribbon = ttk.Notebook(root)
        self.ribbon.pack(side="top", fill="x", padx=4)
        self._build_record_tab()
        self._build_playback_tab()
        self._build_view_tab()
        self._build_help_tab()

        self._build_footer()

        self.main_pane = ttk.PanedWindow(root, orient="vertical")
        self.main_pane.pack(side="top", fill="both", expand=True, padx=4, pady=4)
        self.upper_pane = upper = ttk.PanedWindow(self.main_pane, orient="horizontal")
        self.table = ActionTable(upper, self.document, self.commands, self._on_select)
        self.details = DetailsPanel(upper, self.document, self.settings, self.request_targeting,
                                    self._on_shape, lambda: self.mode, self.request_key_capture)
        upper.add(self.table, weight=3)
        upper.add(self.details, weight=1)
        self.main_pane.add(upper, weight=1)
        self._sash_binding: str | None = upper.bind("<Configure>", self._place_details_sash,
                                                    add="+")
        self.log_panel = LogPanel(self.main_pane)
        if self.settings.ui.log_visible:
            self.main_pane.add(self.log_panel, weight=0)
            root.after_idle(self._restore_log_height)
        if self.platform is not None:
            self._overlay = Overlay(root, self._own_windows, self.settings.ui.overlay_enabled)

    def _build_menus(self, parent: ttk.Frame) -> None:
        file_button = ttk.Menubutton(parent, text="File")
        self.file_menu = tk.Menu(file_button, tearoff=False,
                                 postcommand=lambda: self._refresh_menu("file"))
        file_button.configure(menu=self.file_menu)
        self.recent_menu = tk.Menu(self.file_menu, tearoff=False)
        self._menu_entries["file"] = []
        self._menu_item("file", self.file_menu, "file.new")
        self._menu_item("file", self.file_menu, "file.open")
        self.file_menu.add_cascade(label="Recent", menu=self.recent_menu)
        self._recent_index = self.file_menu.index("end")
        self._menu_item("file", self.file_menu, "file.save")
        self._menu_item("file", self.file_menu, "file.save_as")
        self.file_menu.add_separator()
        self._menu_item("file", self.file_menu, "file.settings")
        self.file_menu.add_separator()
        self._menu_item("file", self.file_menu, "file.exit")
        file_button.pack(side="left")

        edit_button = ttk.Menubutton(parent, text="Edit")
        self.edit_menu = tk.Menu(edit_button, tearoff=False,
                                 postcommand=lambda: self._refresh_menu("edit"))
        edit_button.configure(menu=self.edit_menu)
        self._menu_entries["edit"] = []
        for name in ("edit.undo", "edit.redo", None, "edit.cut", "edit.copy", "edit.paste",
                     "edit.delete", None, "edit.split", "edit.toggle_enabled",
                     "edit.toggle_breakpoint", None, "edit.select_all"):
            if name is None:
                self.edit_menu.add_separator()
            else:
                self._menu_item("edit", self.edit_menu, name)
        edit_button.pack(side="left", padx=(4, 0))

    def _menu_item(self, key: str, menu: tk.Menu, name: str) -> None:
        command = self.commands.get(name)
        menu.add_command(label=command.label, accelerator=command.accelerator or "",
                         command=lambda: self.commands.run(name))
        self._menu_entries[key].append((menu.index("end"), name))

    def _refresh_menu(self, key: str) -> None:
        menu = self.file_menu if key == "file" else self.edit_menu
        for index, name in self._menu_entries[key]:
            menu.entryconfigure(index, state="normal" if self.commands.enabled(name) else "disabled")
        if key == "file":
            self._refresh_recent_menu()

    def _refresh_recent_menu(self) -> None:
        self.recent_menu.delete(0, "end")
        entries = recent_entries(self.settings.ui.recent)
        enabled = self.commands.enabled("file.open_recent")
        for label, path in entries:
            self.recent_menu.add_command(
                label=label, command=lambda p=path: self.commands.run("file.open_recent", p),
                state="normal" if enabled else "disabled")
        self.file_menu.entryconfigure(self._recent_index,
                                      state="normal" if entries and enabled else "disabled")

    def _button(self, parent: tk.Misc, name: str, text: str | None = None,
                textvariable: tk.StringVar | None = None) -> ttk.Button:
        command = self.commands.get(name)
        button = ttk.Button(parent, text=text or command.label, textvariable=textvariable,
                            command=lambda: self.commands.run(name))
        self._buttons.append((button, name))
        return button

    def _build_record_tab(self) -> None:
        tab = ttk.Frame(self.ribbon, padding=4)
        self.ribbon.add(tab, text="Record and Edit")
        self.add_buttons: dict[str, ttk.Menubutton] = {}
        by_category: dict[str, list] = {}
        for spec, preset in catalogue.presets():
            by_category.setdefault(spec.category, []).append(preset)
        for category in CATEGORIES:
            presets = by_category.get(category)
            if not presets:
                continue
            button = ttk.Menubutton(tab, text=category)
            menu = tk.Menu(button, tearoff=False)
            for preset in presets:
                name = preset_command(preset.name)
                menu.add_command(label=preset.name, accelerator=preset.insert_key or "",
                                 command=lambda n=name: self.commands.run(n))
            button.configure(menu=menu)
            button.pack(side="left", padx=(0, 2))
            self.add_buttons[category] = button
            self._category_buttons.append((button, preset_command(presets[0].name)))
        ttk.Separator(tab, orient="vertical").pack(side="left", fill="y", padx=6)
        self.record_button = self._button(tab, "record.start")
        self.record_button.pack(side="left")
        self.record_stop_button = self._button(tab, "record.stop")
        self.record_stop_button.pack(side="left", padx=(2, 0))

    def _build_playback_tab(self) -> None:
        tab = ttk.Frame(self.ribbon, padding=4)
        self.ribbon.add(tab, text="Playback")
        playback = self.settings.playback
        self._play_text = tk.StringVar(self.root, value="Play")
        self.play_button = self._button(tab, "play.play", textvariable=self._play_text)
        self.play_button.pack(side="left")
        self.play_stop_button = self._button(tab, "play.stop")
        self.play_stop_button.pack(side="left", padx=(2, 0))
        ttk.Separator(tab, orient="vertical").pack(side="left", fill="y", padx=(6, 0))
        # Two rows, so the tab fits a window about 900 px wide.
        options = ttk.Frame(tab)
        options.pack(side="left", fill="y")
        first = ttk.Frame(options)
        first.pack(side="top", anchor="w")
        second = ttk.Frame(options)
        second.pack(side="top", anchor="w", pady=(4, 0))

        self.speed_var = tk.StringVar(self.root, value=_number_text(playback.speed))
        self.speed_box = self._labelled_spinbox(first, "Speed", self.speed_var, 0.25, 100, 0.25, 5)
        self.speed_var.trace_add("write", lambda *a: self._apply_playback_field(
            "speed", self.speed_var, self.speed_box, parse_speed,
            "Speed must be a number above 0, up to 100"))
        self.repeat_var = tk.StringVar(self.root, value=str(playback.repeat))
        self.repeat_box = self._labelled_spinbox(first, "Repeat", self.repeat_var, 0, 100000, 1, 6)
        self.repeat_var.trace_add("write", lambda *a: self._apply_playback_field(
            "repeat", self.repeat_var, self.repeat_box, parse_repeat,
            "Repeat must be a whole number; 0 repeats until stopped"))

        self.filter_button = ttk.Menubutton(first, text="Filter")
        self.filter_menu = tk.Menu(self.filter_button, tearoff=False)
        self.filter_vars: dict[str, tk.BooleanVar] = {}
        for group in FILTER_GROUPS:
            var = tk.BooleanVar(self.root, value=group in self.document.filter)
            self.filter_vars[group] = var
            self.filter_menu.add_checkbutton(label=FILTER_LABELS[group], variable=var,
                                             command=self._apply_filter)
        self.filter_button.configure(menu=self.filter_menu)
        self.filter_button.pack(side="left", padx=(12, 0))
        self._idle_widgets.append(self.filter_button)

        ttk.Label(first, text="Mouse path").pack(side="left", padx=(12, 2))
        self.path_mode_var = tk.StringVar(self.root, value=self.document.path_mode)
        self.path_mode_box = ttk.Combobox(first, textvariable=self.path_mode_var, values=PATH_MODES,
                                          state="readonly", width=9)
        self.path_mode_box.bind("<<ComboboxSelected>>", lambda e: self._apply_path_mode())
        self.path_mode_box.pack(side="left")
        self._idle_widgets.append(self.path_mode_box)

        self.timeout_var = tk.StringVar(self.root, value=str(playback.window_timeout_ms))
        ttk.Label(second, text="Window timeout (ms)").pack(side="left", padx=(12, 2))
        self.timeout_entry = ttk.Entry(second, textvariable=self.timeout_var, width=7)
        self.timeout_entry.pack(side="left")
        self._idle_widgets.append(self.timeout_entry)
        self.timeout_var.trace_add("write", lambda *a: self._apply_playback_field(
            "window_timeout_ms", self.timeout_var, self.timeout_entry, parse_ms,
            "Window timeout must be a whole number of milliseconds"))

        ttk.Label(second, text="If window missing").pack(side="left", padx=(12, 2))
        on_missing = playback.on_missing if playback.on_missing in ON_MISSING_CHOICES else "skip"
        self.on_missing_var = tk.StringVar(self.root, value=on_missing)
        self.on_missing_box = ttk.Combobox(second, textvariable=self.on_missing_var,
                                           values=ON_MISSING_CHOICES, state="readonly", width=6)
        self.on_missing_box.bind("<<ComboboxSelected>>", lambda e: setattr(
            self.settings.playback, "on_missing", self.on_missing_var.get()))
        self.on_missing_box.pack(side="left")
        self._idle_widgets.append(self.on_missing_box)

        self.restore_var = tk.BooleanVar(self.root, value=playback.restore_rect)
        self.restore_check = ttk.Checkbutton(
            second, text="Restore windows", variable=self.restore_var,
            command=lambda: setattr(self.settings.playback, "restore_rect", self.restore_var.get()))
        self.restore_check.pack(side="left", padx=(12, 0))
        self._idle_widgets.append(self.restore_check)

    def _labelled_spinbox(self, parent: tk.Misc, label: str, var: tk.StringVar, low: float,
                          high: float, step: float, width: int) -> ttk.Spinbox:
        ttk.Label(parent, text=label).pack(side="left", padx=(12, 2))
        box = ttk.Spinbox(parent, textvariable=var, from_=low, to=high, increment=step,
                          width=width)
        box.pack(side="left")
        self._idle_widgets.append(box)
        return box

    def _apply_playback_field(self, key: str, var: tk.StringVar, widget: ttk.Widget,
                              parse: Callable[[str], Any], problem: str) -> None:
        value = parse(var.get())
        invalid_style = "Invalid.TSpinbox" if isinstance(widget, ttk.Spinbox) else "Invalid.TEntry"
        normal_style = "TSpinbox" if isinstance(widget, ttk.Spinbox) else "TEntry"
        if value is None:
            self._playback_problems[key] = problem
            widget.configure(style=invalid_style)
            return
        self._playback_problems.pop(key, None)
        widget.configure(style=normal_style)
        setattr(self.settings.playback, key, value)

    def _apply_filter(self) -> None:
        if self.mode is not Mode.IDLE:
            self._sync_playback_tab()
            return
        self.document.set_filter(g for g, var in self.filter_vars.items() if var.get())

    def _apply_path_mode(self) -> None:
        mode = self.path_mode_var.get()
        if self.mode is not Mode.IDLE or mode not in PATH_MODES:
            self._sync_playback_tab()
            return
        self.document.set_path_mode(mode)

    def _sync_playback_tab(self) -> None:
        """Show the document's filter and path mode."""
        for group, var in self.filter_vars.items():
            if var.get() != (group in self.document.filter):
                var.set(group in self.document.filter)
        if self.path_mode_var.get() != self.document.path_mode:
            self.path_mode_var.set(self.document.path_mode)

    def _sync_settings_widgets(self) -> None:
        """Show the playback settings (after the Settings dialog)."""
        playback = self.settings.playback
        self.speed_var.set(_number_text(playback.speed))
        self.repeat_var.set(str(playback.repeat))
        self.timeout_var.set(str(playback.window_timeout_ms))
        self.on_missing_var.set(playback.on_missing)
        self.restore_var.set(playback.restore_rect)

    def _build_view_tab(self) -> None:
        tab = ttk.Frame(self.ribbon, padding=4)
        self.ribbon.add(tab, text="View")
        self.log_var = tk.BooleanVar(self.root, value=self.settings.ui.log_visible)
        self.overlay_var = tk.BooleanVar(self.root, value=self.settings.ui.overlay_enabled)
        ttk.Checkbutton(tab, text="Output log", variable=self.log_var,
                        command=lambda: self._view_toggle("view.log", self.log_var)
                        ).pack(side="left")
        ttk.Checkbutton(tab, text="Overlay", variable=self.overlay_var,
                        command=lambda: self._view_toggle("view.overlay", self.overlay_var)
                        ).pack(side="left", padx=(8, 0))

    def _view_toggle(self, name: str, var: tk.BooleanVar) -> None:
        """A checkbutton already flipped its variable; flip it back and run the command."""
        var.set(not var.get())
        self.commands.run(name)

    def _build_help_tab(self) -> None:
        tab = ttk.Frame(self.ribbon, padding=4)
        self.ribbon.add(tab, text="Help")
        self._button(tab, "help.shortcuts").pack(side="left")
        self._button(tab, "help.about").pack(side="left", padx=(2, 0))

    def _build_footer(self) -> None:
        footer = ttk.Frame(self.root, padding=(6, 2))
        footer.pack(side="bottom", fill="x")
        self.count_label = ttk.Label(footer, text="")
        self.count_label.pack(side="left")
        self.elapsed_label = ttk.Label(footer, text=format_elapsed(0))
        self.elapsed_label.pack(side="left", padx=(16, 0))
        ttk.Checkbutton(footer, text="Log", variable=self.log_var,
                        command=lambda: self._view_toggle("view.log", self.log_var)
                        ).pack(side="right")

    def _refresh_widgets(self) -> None:
        """Take every button's state from its command."""
        for button, name in self._buttons:
            button.state(["!disabled"] if self.commands.enabled(name) else ["disabled"])
        for button, name in self._category_buttons:
            button.state(["!disabled"] if self.commands.enabled(name) else ["disabled"])

    def _update_title(self) -> None:
        self.root.title(window_title(self.document.path, self.document.dirty))

    def _update_footer(self) -> None:
        count = len(self.document.rows)
        self.count_label.configure(text="%d row%s" % (count, "" if count == 1 else "s"))

    def _update_elapsed(self) -> None:
        self.elapsed_label.configure(text=format_elapsed(self._stopwatch.elapsed()))

    def _place_details_sash(self, event: tk.Event) -> None:
        """On the first real layout, give the Details panel its width (at most
        half of a narrow window); the table takes the rest.  Later the user's
        sash position stands."""
        if event.width < MIN_LAYOUT_WIDTH or self._sash_binding is None:
            return
        self.upper_pane.unbind("<Configure>", self._sash_binding)
        self._sash_binding = None
        self.upper_pane.sashpos(0, max(event.width - DETAILS_WIDTH, event.width // 2))

    def _restore_log_height(self) -> None:
        self.root.update_idletasks()
        height = self.main_pane.winfo_height()
        if height > self.settings.ui.log_height + 80 and len(self.main_pane.panes()) > 1:
            self.main_pane.sashpos(0, height - self.settings.ui.log_height)

    # -- shortcuts and own windows -------------------------------------------

    def _bind_shortcuts(self) -> None:
        root = self.root
        pairs = {"n": "file.new", "o": "file.open", "y": "edit.redo"}
        for letter, name in pairs.items():
            for key in (letter, letter.upper()):
                root.bind("<Control-%s>" % key, lambda e, n=name: self._shortcut(n))
        for key in ("s", "S"):
            root.bind("<Control-%s>" % key, lambda e: self._shortcut(
                "file.save_as" if int(e.state) & 0x1 else "file.save"))
        for key in ("z", "Z"):
            root.bind("<Control-%s>" % key, lambda e: self._shortcut(
                "edit.redo" if int(e.state) & 0x1 else "edit.undo"))
        root.bind("<F5>", lambda e: self._shortcut("play.play"))
        root.bind("<F9>", lambda e: self._shortcut("record.toggle", "F9"))

    def _shortcut(self, name: str, *args: Any) -> str:
        self.commands.run(name, *args)
        return "break"

    def _track_toplevels(self) -> None:
        """Register every toplevel (dialogs and menus included) in OwnWindows."""
        def mapped(event: tk.Event) -> None:
            widget = event.widget
            if isinstance(widget, (tk.Tk, tk.Toplevel, tk.Menu)):
                self._tracker.mapped(str(widget), int(widget.winfo_id()))

        def destroyed(event: tk.Event) -> None:
            if isinstance(event.widget, (tk.Tk, tk.Toplevel, tk.Menu)):
                self._tracker.destroyed(str(event.widget))
        self.root.bind_all("<Map>", mapped, add="+")
        self.root.bind_all("<Destroy>", destroyed, add="+")

    # -- selection, document changes, overlay ------------------------------

    def _on_select(self, ids: tuple[int, ...]) -> None:
        self.document.end_edit()
        self.details.show(ids[0] if len(ids) == 1 else None)
        if len(ids) != 1:
            self._shape = None
            self._update_overlay()
        self.commands.refresh()

    def _on_document_change(self, change: Change) -> None:
        if change.kind == "inserted":
            self._last_inserted = change.ids
        if change.kind in ("reset", "meta"):
            self._sync_playback_tab()
        self._update_title()
        self._update_footer()
        self.commands.refresh()

    def _on_shape(self, shape: OverlayShape | None) -> None:
        """The Details panel's shape for the selected row.  It is kept for idle
        and shown only then; recording, playing and targeting keep the overlay
        hidden."""
        self._shape = shape
        if self.mode is Mode.IDLE:
            self._update_overlay()

    def _update_overlay(self) -> None:
        """Show the selected row's marks when idle, the next row's when paused,
        nothing otherwise.  The overlay itself hides while the application has
        no focus."""
        overlay = self._overlay
        if overlay is None:
            return
        if self.mode is Mode.PAUSED:
            overlay.show(self._row_shape(self._paused_row))
        elif self.mode is Mode.IDLE:
            overlay.show(self._shape)
        else:
            overlay.hide()

    def _row_shape(self, row_id: int | None) -> OverlayShape | None:
        if row_id is None:
            return None
        try:
            row = self.document.row(row_id)
            return catalogue.spec_for(row).overlay(row)
        except Exception as e:
            log.debug("No overlay for row %s: %s", row_id, e)
            return None

    # -- file commands -------------------------------------------------------

    def confirm_discard(self) -> bool:
        """Save / Don't save / Cancel when the document has unsaved changes.
        True when the caller may go on."""
        if not self.document.dirty:
            return True
        answer = self.ask_unsaved(display_name(self.document.path))
        if answer == "save":
            return self.save()
        return answer == "discard"

    def new_document(self) -> None:
        if not self.confirm_discard():
            return
        self.details.show(None)
        path_mode = self.settings.playback.path_mode
        self.document.reset(path_mode=path_mode if path_mode in PATH_MODES else "recorded")

    def open_document(self) -> None:
        if not self.confirm_discard():
            return
        path = dialogs.ask_open_path(self.root, self.settings.ui.last_folder)
        if path:
            self._load(path)

    def open_path(self, path: str) -> None:
        """Open a recent file."""
        if not self.confirm_discard():
            return
        self._load(path)

    def _load(self, path: str) -> bool:
        try:
            loaded = fileformat.load(path)
        except FileNotFoundError:
            self.settings.remove_recent(str(Path(path).resolve()))
            self._report_error("Open", "%s no longer exists." % path)
            return False
        except fileformat.FormatError as e:
            self._report_error("Open", str(e))
            return False
        except OSError as e:
            self._report_error("Open", "Cannot read %s: %s" % (path, e.strerror or e))
            return False
        self.details.show(None)
        self.document.reset(loaded.rows, loaded.filter, loaded.path_mode,
                            path=os.path.abspath(path))
        self._remember_path(path)
        problems = validate.check(self.document)
        if problems:
            self.log_panel.append(["%s: %s" % (display_name(path), p) for p in problems])
        return True

    def save(self) -> bool:
        if self.document.path is None:
            return self.save_as()
        return self._write(self.document.path)

    def save_as(self) -> bool:
        folder = (os.path.dirname(self.document.path) if self.document.path
                  else self.settings.ui.last_folder)
        name = Path(self.document.path).name if self.document.path else "macro.json"
        path = dialogs.ask_save_path(self.root, folder, name)
        if not path:
            return False
        return self._write(path)

    def _write(self, path: str) -> bool:
        try:
            fileformat.save(self.document, path)
        except fileformat.FormatError as e:
            self._report_error("Save", str(e))
            return False
        except OSError as e:
            self._report_error("Save", "Cannot write %s: %s" % (path, e.strerror or e))
            return False
        self.document.mark_saved(os.path.abspath(path))
        self._remember_path(path)
        self._update_title()
        return True

    def _remember_path(self, path: str) -> None:
        self.settings.add_recent(path)
        self.settings.ui.last_folder = os.path.dirname(os.path.abspath(path))

    def open_settings(self) -> None:
        """The Settings dialog; the grabbed hotkeys are released while it is open."""
        router = self._router if self._hotkeys_grabbed else None
        if settings_module.open_settings_dialog(self.root, self.settings, router,
                                                save=self._save_settings):
            self._sync_settings_widgets()

    def exit(self) -> None:
        """Stop whatever runs, offer to save, then close."""
        if self._targeting is not None:
            self._targeting.cancel()
        if self.mode is Mode.RECORDING and self.recording is not None:
            self.recording.stop()
        if self.playback is not None and self.playback.running:
            self.playback.stop()
            self.playback.join(2.0)
        if not self.confirm_discard():
            return
        self.close()

    def close(self) -> None:
        """Save the settings, release platform resources and destroy the window."""
        self._closing = True
        if self._pump_job is not None:
            self.root.after_cancel(self._pump_job)
            self._pump_job = None
        ui = self.settings.ui
        try:
            ui.geometry = self.root.wm_geometry()
            ui.log_visible = bool(self.log_var.get())
            if ui.log_visible and len(self.main_pane.panes()) > 1:
                ui.log_height = max(60, self.main_pane.winfo_height() - self.main_pane.sashpos(0))
        except tk.TclError as e:
            log.debug("Reading the window layout failed: %s", e)
        try:
            self._save_settings(self.settings)
        except OSError as e:
            log.error("Saving the settings failed: %s", e)
        if self._router is not None:
            self._router.stop()
        if self._overlay is not None:
            self._overlay.close()
        if self.platform is not None:
            self.platform.close()
        close = getattr(self._resolver, "close", None)
        if close is not None:
            close()
        self.root.destroy()

    # -- edit commands -------------------------------------------------------

    def insert_rows(self, rows: Sequence[Row]) -> tuple[int, ...]:
        """Insert below the selected row (or at the end) and select the new rows."""
        index = insertion_index(self.document, self.table.selected_ids())
        ids = self.document.insert(index, rows)
        self.table.select(ids)
        self.table.tree.focus_set()
        return ids

    def undo(self) -> None:
        self.details.revert_pending()
        self.document.undo()

    def redo(self) -> None:
        self.details.revert_pending()
        self.document.redo()

    def copy(self) -> None:
        rows = [self.document.row(i) for i in self.table.selected_ids()]
        if rows:
            self.root.clipboard_clear()
            self.root.clipboard_append(clipboard_text(rows))

    def cut(self) -> None:
        self.copy()
        self.delete()

    def paste(self) -> None:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            self.root.bell()
            return
        try:
            rows = rows_from_clipboard(text)
        except fileformat.FormatError as e:
            self._report_error("Paste", str(e))
            return
        if not rows:
            self.root.bell()
            return
        ids = self.insert_rows(rows)
        for row, row_id in zip(rows, ids):
            if row.label and self.document.row(row_id).label != row.label:
                self.log_panel.append(["Pasted row %d: label %r is already used and was cleared"
                                       % (self.document.index_of(row_id) + 1, row.label)])

    def delete(self) -> None:
        ids = self.table.selected_ids()
        if not ids:
            return
        first = self.document.index_of(ids[0])
        self.document.delete(ids)
        rows = self.document.rows
        if rows:
            self.table.select([rows[min(first, len(rows) - 1)].id])

    def split(self) -> None:
        for row_id in self.table.selected_ids():
            if self.document.row(row_id).is_path:
                self.document.split(row_id, self.settings.recording.min_wait_ms)

    def toggle_enabled(self) -> None:
        ids = self.table.selected_ids()
        if ids:
            rows = [self.document.row(i) for i in ids]
            self.document.set_enabled(ids, not all(r.enabled for r in rows))

    def _breakpoint_targets(self) -> tuple[int, ...]:
        """The selection when idle; the row about to run when paused."""
        if self.mode is Mode.PAUSED:
            return (self._paused_row,) if self._paused_row is not None else ()
        return self.table.selected_ids()

    def toggle_breakpoint(self) -> None:
        ids = self._breakpoint_targets()
        if ids:
            rows = [self.document.row(i) for i in ids]
            on = not all(r.breakpoint for r in rows)
            self.document.set_breakpoint(ids, on)
            if self.mode is Mode.PAUSED and self.playback is not None:
                for row_id in ids:
                    self.playback.set_breakpoint(row_id, on)

    def select_all(self) -> None:
        self.table.select([r.id for r in self.document.rows])

    def move_selection(self, delta: int) -> None:
        ids = self.table.selected_ids()
        if not ids:
            return
        index = self.document.row_index()
        target = shifted_index([index[i] for i in ids], len(self.document.rows), delta)
        self.document.move(ids, target)
        self.table.select(ids)

    def edit_label(self) -> None:
        ids = self.table.selected_ids()
        if len(ids) == 1:
            self.table.begin_label_edit(ids[0])

    def edit_comment(self) -> None:
        ids = self.table.selected_ids()
        if len(ids) == 1:
            self.table.begin_comment_edit(ids[0])

    # -- record, play, target ------------------------------------------------

    def start_recording(self, start_key: str | None = None) -> None:
        if self.recording is None:
            return
        ids = self.table.selected_ids()
        self._last_inserted = ()
        self.recording.start(ids[-1] if ids else None, start_key)

    def stop_recording(self) -> None:
        if self.recording is not None:
            self.recording.stop()

    def toggle_recording(self, start_key: str | None = None) -> None:
        if self.mode is Mode.RECORDING:
            self.stop_recording()
        else:
            self.start_recording(start_key)

    def play_or_continue(self) -> None:
        if self.playback is None:
            return
        if self.mode is Mode.PAUSED:
            self.playback.continue_()
            return
        if self._playback_problems:
            self._report_error("Play", "\n".join(self._playback_problems.values()))
            return
        self.playback.start()

    def stop_playback(self) -> None:
        if self.playback is not None:
            self.playback.stop()

    def request_targeting(self, keys: tuple[str, ...],
                          deliver: Callable[[Capture], None]) -> None:
        """Run a targeting session for the Details panel's ``keys``; ``deliver``
        receives the :class:`Capture` on the main thread, back in idle, so it
        may request the next capture (a region's second corner)."""
        if self.mode is not Mode.IDLE or self.platform is None or self._targeting is not None:
            self.root.bell()
            return
        session_box: list[TargetingSession] = []

        def done(capture: Capture | None, error: str | None) -> None:
            self.post("call", lambda: self._targeting_done(session_box[0], capture, error, deliver))
        session = self.targeting_factory(done)
        session_box.append(session)
        self._targeting = session
        self.set_mode(Mode.TARGETING)
        try:
            session.start()
        except Exception as e:
            self._targeting = None
            self.set_mode(Mode.IDLE)
            self._report_error("Targeting", str(e))

    def _targeting_done(self, session: TargetingSession, capture: Capture | None,
                        error: str | None, deliver: Callable[[Capture], None]) -> None:
        if self._targeting is not session:
            return
        self._targeting = None
        self.set_mode(Mode.IDLE)
        if error:
            self._report_error("Targeting", error)
        elif capture is not None:
            deliver(capture)

    def request_key_capture(self, single: bool, deliver: Callable[[str], None]) -> None:
        """Read a key (``single``) or a combination for the Details panel; the
        global hotkeys are released meanwhile so their combos can be captured."""
        if self.mode is not Mode.IDLE:
            self.root.bell()
            return
        router = self._router if self._hotkeys_grabbed else None
        if router is not None:
            router.suspend()
        try:
            key = self.capture_key(single)
        finally:
            if router is not None:
                router.resume()
        if key is not None:
            deliver(key)

    # -- view and help -------------------------------------------------------

    def toggle_log(self) -> None:
        visible = not self.log_var.get()
        self.log_var.set(visible)
        self.settings.ui.log_visible = visible
        if visible:
            self.main_pane.add(self.log_panel, weight=0)
            self.root.after_idle(self._restore_log_height)
        else:
            if len(self.main_pane.panes()) > 1:
                self.settings.ui.log_height = max(
                    60, self.main_pane.winfo_height() - self.main_pane.sashpos(0))
            self.main_pane.forget(self.log_panel)

    def toggle_overlay(self) -> None:
        enabled = not self.overlay_var.get()
        self.overlay_var.set(enabled)
        self.settings.ui.overlay_enabled = enabled
        if self._overlay is not None:
            self._overlay.set_enabled(enabled)
            self._update_overlay()

    def show_shortcuts(self) -> None:
        self.show_message("Keyboard shortcuts", shortcut_help(self.settings))

    def show_about(self) -> None:
        name = ("%s %s" % (APP_NAME, _version())).strip()
        self.show_message("About %s" % APP_NAME,
                          "%s\nRecords, edits and replays mouse and keyboard macros on X11." % name)

    # -- hotkeys -------------------------------------------------------------

    def start_hotkeys(self) -> None:
        """Grab the global hotkeys and prepare the stop keys; failures go to the log.

        The grabber and the stop-key watcher are built independently, so
        Escape still stops playback when the hotkeys cannot be grabbed.
        """
        if self.platform is None or self._router is not None:
            return
        grabber: Any
        watcher: Any
        try:
            grabber = self.platform.grabber()
            self._hotkeys_grabbed = True
        except Exception as e:
            log.warning("Global hotkeys are unavailable: %s", e)
            grabber = _NoGrabber()
        try:
            watcher = self.platform.stop_watcher()
        except Exception as e:
            log.warning("No stop key: Escape will not stop playback and F6 will not stop "
                        "recording: %s", e)
            watcher = _NoStopWatcher(str(e))
        actions = HotkeyActions(
            start_record=lambda: self.post("call", lambda: self.commands.run(
                "record.start", _combo_key(self.settings.hotkeys.record))),
            stop_record=lambda trigger: self.post("call", lambda: self.commands.run("record.stop")),
            play=lambda: self.post("call", lambda: self.commands.run("play.play")),
            resume=lambda: self.post("call", lambda: self.commands.run("play.play")),
            stop_play=lambda: self.post("call", lambda: self.commands.run("play.stop")),
        )
        self._router = HotkeyRouter(grabber, watcher, self.settings, lambda: self.mode, actions,
                                    is_blocked=dialogs.modal_active)
        if self._hotkeys_grabbed:
            self._router.start()
        self._router.mode_changed(self.mode)


class _NoGrabber:
    """Stands in for the hotkey grabber when it cannot be built, so the router
    still arms the stop keys.  The router's :meth:`start` is not called on it."""

    def grab(self, combo: str, callback: Callable[[], None]) -> bool:
        return False

    def ungrab_all(self) -> None:
        pass

    def suspend(self) -> None:
        pass

    def resume(self) -> list[str]:
        return []

    def close(self) -> None:
        pass


class _NoStopWatcher:
    """Stands in for the stop-key watcher when it cannot be built; arming it
    fails, which the router logs on every attempt."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def start(self, key: str, callback: Callable[[], None]) -> None:
        raise OSError(self._reason)

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


def _combo_key(combo: str) -> str | None:
    """The final key of a combo (``F9`` for ``ctrl+shift+F9``), or None."""
    try:
        return parse_combo(combo)[-1]
    except ValueError:
        return None


def _number_text(value: float) -> str:
    return ("%g" % value) if value != int(value) else str(int(value))


def _version() -> str:
    from importlib import metadata
    try:
        return metadata.version("macro-recorder")
    except metadata.PackageNotFoundError:
        return ""
