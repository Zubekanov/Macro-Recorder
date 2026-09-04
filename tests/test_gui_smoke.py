"""Builds the real tkinter app off-screen and drives one editing session.

Skipped when no display is available.  Nothing here records or plays, so no
global hooks are installed; the screen overlay is replaced with a stub.
"""

import tkinter as tk

import pytest

from macro_recorder.event_types import EventType
from macro_recorder.macro import MacroEvent, MacroGroup, WindowRect, load_macro
from macro_recorder.run import MacroRecorderApp


class _NoOverlay:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def app():
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip("no display: %s" % e)
    root.withdraw()
    app = MacroRecorderApp(root)
    app._overlay = _NoOverlay()
    yield app
    root.update()
    root.destroy()


def _groups():
    return [
        MacroGroup(window=None, recorded_rect=None,
                   events=[MacroEvent(type=EventType.KEY_PRESS, ts=0.0, key="a")]),
        MacroGroup(window="App", recorded_rect=WindowRect(100, 50, 400, 300), events=[
            MacroEvent(type=EventType.MOUSE_CLICK, ts=0.0, x=10, y=10, button="left", pressed=True),
            MacroEvent(type=EventType.MOUSE_CLICK, ts=0.05, x=10, y=10, button="left", pressed=False),
            MacroEvent(type=EventType.WAIT, ts=0.1, duration=0.2),
        ]),
    ]


def rows(app):
    return list(app.table.get_children())


def select(app, iid):
    app.table.selection_set(iid)
    app.root.update()
    assert app._selected_iid == iid


def test_load_edit_undo_move_save(app, tmp_path):
    app._load_groups_to_table(_groups())
    app.root.update()
    assert len(rows(app)) == 4            # key, window, click, wait
    assert app.action_count == 4
    assert app.table.item(rows(app)[2], "values")[2] == "left click @ (110, 60)"

    # Add a wait after the last row, then edit it through the Details panel.
    select(app, rows(app)[-1])
    app._add_action_row("wait")
    assert len(rows(app)) == 5
    wait_iid = rows(app)[-1]
    select(app, wait_iid)
    entry = app._detail_widgets["duration"]
    entry.delete(0, tk.END)
    entry.insert(0, "750")
    app._sync_detail_to_table()
    assert app._row_events[wait_iid][0].duration == pytest.approx(0.75)
    assert "750 ms" in app.table.item(wait_iid, "values")[2]

    # The window row exposes title, match mode and launch command.
    select(app, rows(app)[1])
    assert app._detail_widgets["window"].get() == "App"
    app._detail_widgets["launch"].insert(0, "app.exe")
    app._sync_detail_to_table()

    # Delete, undo, redo.
    select(app, wait_iid)
    app._delete_row()
    assert len(rows(app)) == 4
    app._undo_action()
    assert len(rows(app)) == 5
    assert app._row_events[rows(app)[-1]][0].duration == pytest.approx(0.75)
    app._redo_action()
    assert len(rows(app)) == 4

    # Move the last row up one place.
    last = rows(app)[-1]
    select(app, last)
    app._move_rows(-1)
    assert rows(app).index(last) == 2

    # Save and reload: the edited launch command survives, row order is kept.
    path = tmp_path / "m.json"
    app._do_save(str(path))
    groups = load_macro(path)
    assert [g.window for g in groups] == [None, "App"]
    assert groups[1].launch == "app.exe"
    assert [e.type for e in groups[1].events] == [EventType.WAIT, EventType.MOUSE_CLICK, EventType.MOUSE_CLICK]
    assert (groups[1].events[1].x, groups[1].events[1].y) == (10, 10)


def test_details_edits_on_one_row_are_one_undo_step(app):
    app._load_groups_to_table(_groups())
    app.root.update()
    wait_iid = rows(app)[-1]
    select(app, wait_iid)
    entry = app._detail_widgets["duration"]
    for text in ("3", "30", "300"):
        entry.delete(0, tk.END)
        entry.insert(0, text)
        app._sync_detail_to_table()
    assert app._row_events[wait_iid][0].duration == pytest.approx(0.3)
    app._undo_action()
    assert app._row_events[rows(app)[-1]][0].duration == pytest.approx(0.2)


def test_playback_settings_errors_name_the_field(app):
    app._speed_var.set("speed_x")
    with pytest.raises(ValueError, match="Speed"):
        app._read_playback_settings()
    app._speed_var.set("2*0.5")
    app._repeat_var.set("-1")
    with pytest.raises(ValueError, match="Repeat"):
        app._read_playback_settings()
    app._repeat_var.set("0")
    assert app._read_playback_settings() == (1.0, 0, 5.0)
