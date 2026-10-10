"""Tk-free logic of the GUI shell: pump ordering, title, recent files, helpers."""

from __future__ import annotations

import dataclasses
import logging
import queue
import threading

import pytest

from macro_recorder import app
from macro_recorder.gui.log_panel import variable_rows
from macro_recorder.gui.main_window import (
    EventBatch, OwnWindowTracker, Stopwatch, _NoGrabber, _NoStopWatcher, clipboard_text,
    display_name, drain, ending_edit, format_elapsed, insertion_index, parse_ms, parse_repeat,
    parse_speed, recent_entries, rows_from_clipboard, shortcut_help, window_title,
)
from macro_recorder.gui.commands import IDLE_ONLY, Command
from macro_recorder.gui.hotkeys import HotkeyActions, HotkeyRouter
from macro_recorder.model.protocols import Mode
from macro_recorder.gui.settings import Settings
from macro_recorder.gui.table import (
    EDIT_MENU, cells, drop_position, number_text, remaining_index, row_tags, shifted_index,
    window_groups,
)
from tests.conftest import FakeStopKey
from macro_recorder.model.catalogue import make
from macro_recorder.model.document import MacroDocument, make_row
from macro_recorder.model.fileformat import FormatError
from macro_recorder.platform.session import OwnWindows


def wait_row(ms=10, **fields):
    return make_row(make("wait", duration_ms=ms), **fields)


def window_row(title="xed", **fields):
    return make_row(make("window_focus", window=title), **fields)


# -- pump ordering ------------------------------------------------------------

def test_drain_keeps_all_logs_the_latest_row_then_player_events_in_order():
    q: queue.SimpleQueue = queue.SimpleQueue()
    call = lambda: None
    for event in [("row", 1), ("log", "a"), ("row", 2), ("paused", (2, {})), ("log", "b"),
                  ("call", call), ("ask", "request"), ("row", 3), ("finished", (None, {}))]:
        q.put(event)
    batch = drain(q)
    assert batch.logs == ["a", "b"]
    assert batch.row == 3
    assert batch.player == [("paused", (2, {})), ("ask", "request"), ("finished", (None, {}))]
    assert batch.calls == [call]
    assert q.empty()


def test_drain_of_an_empty_queue_is_empty():
    assert drain(queue.SimpleQueue()) == EventBatch()


def test_drain_ignores_unknown_kinds(caplog):
    q: queue.SimpleQueue = queue.SimpleQueue()
    q.put(("bogus", 1))
    q.put(("log", "x"))
    with caplog.at_level(logging.ERROR):
        batch = drain(q)
    assert batch.logs == ["x"]
    assert "bogus" in caplog.text


# -- title and dirty ----------------------------------------------------------

def test_title_format():
    assert window_title(None, False) == "Untitled - Macro Recorder"
    assert window_title(None, True) == "Untitled* - Macro Recorder"
    assert window_title("/home/u/macros/login.json", False) == "login - Macro Recorder"
    assert window_title("/home/u/macros/login.json", True) == "login* - Macro Recorder"
    assert display_name("/x/y/a.b.json") == "a.b"


def test_dirty_follows_the_document_revision():
    document = MacroDocument([wait_row()])
    assert not document.dirty
    document.append([wait_row()])
    assert window_title(document.path, document.dirty).startswith("Untitled*")
    document.mark_saved("/tmp/m.json")
    assert window_title(document.path, document.dirty) == "m - Macro Recorder"
    document.undo()
    assert document.dirty
    document.redo()
    assert not document.dirty


# -- recent files ---------------------------------------------------------------

def test_recent_entries_number_and_shorten_home():
    entries = recent_entries(["/home/u/a.json", "/srv/b.json", "/home/u"], home="/home/u")
    assert entries == [("1  ~/a.json", "/home/u/a.json"), ("2  /srv/b.json", "/srv/b.json"),
                       ("3  ~", "/home/u")]


def test_recent_entries_do_not_shorten_a_sibling_of_home():
    assert recent_entries(["/home/user2/a.json"], home="/home/u") == [
        ("1  /home/user2/a.json", "/home/user2/a.json")]


def test_recent_list_is_most_recent_first_and_capped(tmp_path):
    settings = Settings()
    paths = [tmp_path / ("m%d.json" % i) for i in range(12)]
    for p in paths:
        settings.add_recent(str(p))
    settings.add_recent(str(paths[3]))
    entries = recent_entries(settings.ui.recent, home="")
    assert len(entries) == 10
    assert entries[0][1] == str(paths[3].resolve())
    assert entries[1][1] == str(paths[11].resolve())
    settings.remove_recent(str(paths[3].resolve()))
    assert recent_entries(settings.ui.recent, home="")[0][1] == str(paths[11].resolve())


# -- elapsed time ---------------------------------------------------------------

def test_stopwatch_excludes_pauses():
    t = [0.0]
    watch = Stopwatch(lambda: t[0])
    watch.start()
    t[0] = 2.0
    watch.pause()
    t[0] = 10.0
    assert watch.elapsed() == 2.0
    watch.resume()
    t[0] = 11.5
    assert watch.elapsed() == 3.5
    watch.start()
    assert watch.elapsed() == 0.0


def test_format_elapsed():
    assert format_elapsed(0) == "0:00.0"
    assert format_elapsed(61.27) == "1:01.2"
    assert format_elapsed(3725) == "1:02:05"
    assert format_elapsed(-1) == "0:00.0"


# -- playback fields --------------------------------------------------------------

@pytest.mark.parametrize("text, value", [("1", 1.0), (" 2.5 ", 2.5), ("0", None), ("-1", None),
                                         ("abc", None), ("", None), ("101", None)])
def test_parse_speed(text, value):
    assert parse_speed(text) == value


@pytest.mark.parametrize("text, value", [("0", 0), ("3", 3), ("", None), (" ", None),
                                         ("1.5", None), ("-1", None), ("x", None),
                                         ("\u00b2", None), ("\u0663", None)])
def test_parse_repeat_blank_is_invalid_and_zero_means_until_stopped(text, value):
    assert parse_repeat(text) == value


def test_parse_ms():
    assert parse_ms("5000") == 5000
    assert parse_ms("") is None and parse_ms("1.5") is None
    assert parse_ms("\u00b2") is None and parse_ms("\u0663") is None


# -- commands and edit sessions ----------------------------------------------------

def test_commands_end_the_open_edit_session_before_running():
    row = wait_row(10)
    document = MacroDocument([row])
    document.begin_edit(row.id, "comment")
    document.replace_row(dataclasses.replace(row, comment="edited"))
    command = ending_edit(document, Command(
        "edit.toggle_enabled", "Toggle enabled",
        lambda: document.set_enabled([row.id], False), IDLE_ONLY))
    command.run()
    document.undo()
    assert document.row(row.id).enabled
    assert document.row(row.id).comment == "edited"


def test_context_menu_has_the_edit_menu_items_in_order():
    names = [item[0] for item in EDIT_MENU if item is not None]
    edit_menu = ["edit.undo", "edit.redo", "edit.cut", "edit.copy", "edit.paste", "edit.delete",
                 "edit.split", "edit.toggle_enabled", "edit.toggle_breakpoint",
                 "edit.select_all"]
    assert [n for n in names if n in edit_menu] == edit_menu


def test_cells_survive_a_row_that_cannot_be_described(monkeypatch, caplog):
    from macro_recorder.model import catalogue
    row = wait_row(10)
    spec = catalogue.spec_for(row)

    def broken(r):
        raise ValueError("bad")
    monkeypatch.setattr(catalogue, "spec_for", lambda r: dataclasses.replace(spec, describe=broken))
    with caplog.at_level(logging.WARNING):
        assert cells("+", row, "Recorded row 1")[2] == "?"
    assert "Recorded row 1" in caplog.text


# -- stop keys without a grabber ---------------------------------------------------

def _router(grabber, watcher, mode, stops):
    actions = HotkeyActions(start_record=lambda: None, stop_record=lambda t: stops.append(t),
                            play=lambda: None, resume=lambda: None,
                            stop_play=lambda: stops.append("play"))
    return HotkeyRouter(grabber, watcher, Settings(), lambda: mode[0], actions)


def test_escape_still_stops_playback_without_a_grabber():
    watcher = FakeStopKey()
    stops: list[str] = []
    mode = [Mode.PLAYING]
    router = _router(_NoGrabber(), watcher, mode, stops)
    router.mode_changed(Mode.PLAYING)
    watcher.press()
    assert stops == ["play"]


def test_missing_stop_watcher_is_logged_on_arming(caplog):
    router = _router(_NoGrabber(), _NoStopWatcher("no XInput2"), [Mode.PLAYING], [])
    with caplog.at_level(logging.WARNING):
        router.mode_changed(Mode.PLAYING)
    assert "Stop key Escape unavailable: no XInput2" in caplog.text


# -- clipboard and insertion ------------------------------------------------------

def test_clipboard_round_trip_gives_fresh_ids():
    rows = [wait_row(20, label="top", comment="c"), window_row()]
    back = rows_from_clipboard(clipboard_text(rows))
    assert [r.type for r in back] == ["wait", "window_focus"]
    assert back[0].label == "top" and back[0].comment == "c"
    assert {r.id for r in back}.isdisjoint({r.id for r in rows})


def test_clipboard_ignores_foreign_text_and_rejects_bad_rows():
    assert rows_from_clipboard("hello") is None
    assert rows_from_clipboard('{"other": 1}') is None
    with pytest.raises(FormatError):
        rows_from_clipboard('{"macro_recorder_rows": [{"events": [{"type": "nope"}]}]}')


def test_insertion_index_is_below_the_last_selected_row():
    rows = [wait_row(), wait_row(), wait_row()]
    document = MacroDocument(rows)
    assert insertion_index(document, ()) == 3
    assert insertion_index(document, (rows[0].id,)) == 1
    assert insertion_index(document, (rows[1].id, rows[0].id)) == 2
    assert insertion_index(document, (999,)) == 3


def test_shortcut_help_names_the_hotkeys_and_insert_keys():
    text = shortcut_help(Settings())
    assert "Ctrl+Shift+F9 Record or Stop" in text
    assert "Ctrl+Shift+F5 Play or Continue" in text
    assert "C Click" in text and "W Wait" in text
    assert text.isascii()


# -- own windows --------------------------------------------------------------------

def test_tracker_registers_resolved_ids_and_unregisters_by_path():
    own = OwnWindows()
    tracker = OwnWindowTracker(own, lambda inner: inner + 1000)
    tracker.mapped(".", 5)
    tracker.mapped(".", 5)                  # mapped again: no second lookup needed
    tracker.mapped(".!toplevel", 7)
    assert own.ids() == {1005, 1007}
    tracker.destroyed(".!toplevel")
    tracker.destroyed(".unknown")
    assert own.ids() == {1005}
    assert tracker.registered() == {".": 1005}


def test_tracker_falls_back_to_the_inner_id_when_resolving_fails():
    own = OwnWindows()

    def broken(inner):
        raise OSError("no display")
    tracker = OwnWindowTracker(own, broken)
    tracker.mapped(".", 42)
    assert own.contains(42)


# -- table helpers ------------------------------------------------------------------

def test_window_groups_end_at_disabled_window_rows():
    rows = [wait_row(), window_row("a"), wait_row(), window_row("b", enabled=False), wait_row(),
            window_row("c"), wait_row()]
    groups = window_groups(rows)
    assert groups == {rows[1].id: 0, rows[2].id: 0, rows[5].id: 1, rows[6].id: 1}


def test_number_text_markers():
    row = wait_row()
    assert number_text(0, row, False) == "1"
    assert number_text(2, row, True) == "[3]"
    assert number_text(0, wait_row(breakpoint=True, enabled=False), False) == "*|| 1"
    assert number_text(4, wait_row(breakpoint=True), False, playing=True) == ">* 5"


def test_row_tags():
    assert row_tags(wait_row(), None, False) == ()
    assert row_tags(wait_row(), 7, False) == ("group1",)
    assert row_tags(wait_row(enabled=False), None, True) == ("disabled",)
    assert row_tags(wait_row(), None, True) == ("filtered",)


def test_drop_position():
    assert drop_position(5, 2, True, False) == 2
    assert drop_position(5, 2, False, False) == 3
    assert drop_position(5, None, False, True) == 0
    assert drop_position(5, None, False, False) == 5


@pytest.mark.parametrize("moving, position, order", [
    ([0], 3, [1, 2, 0, 3, 4]),
    ([3], 0, [3, 0, 1, 2, 4]),
    ([1, 2], 5, [0, 3, 4, 1, 2]),
    ([4], 5, [0, 1, 2, 3, 4]),
])
def test_drop_moves_rows_to_the_line(moving, position, order):
    rows = [wait_row(i) for i in range(5)]
    document = MacroDocument(rows)
    document.move([rows[i].id for i in moving], remaining_index(position, moving))
    assert [rows.index(r) for r in document.rows] == order


@pytest.mark.parametrize("selected, delta, order", [
    ([2], -1, [0, 2, 1, 3, 4]),
    ([2], 1, [0, 1, 3, 2, 4]),
    ([0], -1, [0, 1, 2, 3, 4]),
    ([4], 1, [0, 1, 2, 3, 4]),
    ([1, 2], 1, [0, 3, 1, 2, 4]),
    ([1, 2], -1, [1, 2, 0, 3, 4]),
])
def test_alt_arrows_shift_the_selection(selected, delta, order):
    rows = [wait_row(i) for i in range(5)]
    document = MacroDocument(rows)
    document.move([rows[i].id for i in selected], shifted_index(selected, 5, delta))
    assert [rows.index(r) for r in document.rows] == order


def test_variable_rows_sorted_and_quoted():
    assert variable_rows({"b": 2, "A": "x", "c": 1.5}) == [("A", "'x'"), ("b", "2"), ("c", "1.5")]


# -- app: logging and error hooks -----------------------------------------------------

def test_queue_log_handler_posts_warnings_and_up():
    posted: list = []
    logger = logging.getLogger("macro_recorder.test_handler")
    handler = app.QueueLogHandler(lambda kind, payload: posted.append((kind, payload)))
    logger.addHandler(handler)
    try:
        logger.info("quiet")
        logger.warning("Hotkey %s not available", "F9")
        logger.error("broken")
    finally:
        logger.removeHandler(handler)
    assert posted == [("log", "Warning: Hotkey F9 not available"), ("log", "Error: broken")]


def test_configure_logging_writes_to_the_cache_dir(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    try:
        path = app.configure_logging(tmp_path / "macro-recorder")
        logging.getLogger("macro_recorder.test").info("hello")
        for h in root.handlers:
            h.flush()
        assert path == tmp_path / "macro-recorder" / "app.log"
        assert "hello" in path.read_text()
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()
        root.setLevel(level)


def test_error_hooks_log_and_surface(caplog, monkeypatch):
    class FakeRoot:
        report_callback_exception = None

    reports: list = []
    posted: list = []
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    root = FakeRoot()
    app.install_error_hooks(root, reports.append, lambda kind, payload: posted.append((kind, payload)))
    with caplog.at_level(logging.ERROR):
        try:
            raise ValueError("bad value")
        except ValueError as e:
            root.report_callback_exception(type(e), e, e.__traceback__)
        thread = threading.Thread(target=lambda: 1 / 0, name="worker")
        thread.start()
        thread.join()
    assert reports == ["ValueError: bad value"]
    assert len(posted) == 1 and posted[0][0] == "call"
    posted[0][1]()
    assert reports[-1] == "ZeroDivisionError: division by zero"
    assert "Unhandled error in thread worker" in caplog.text
