"""MacroDocument: rows, undo, labels, sessions, split."""

import pytest

from macro_recorder.model import catalogue
from macro_recorder.model.catalogue import make
from macro_recorder.model.document import MacroDocument, Row, label_problem, make_row
from macro_recorder.model.events import MacroEvent
from macro_recorder.model.protocols import Change


def wait(ms=500, **fields):
    return make_row(make("wait", duration_ms=ms), **fields)


def goto(target, **fields):
    return make_row(make("goto", target=target), **fields)


def path(points, dt=0.01):
    events = tuple(make("mouse_move", x=x, y=y, dt=0.0 if i == 0 else dt)
                   for i, (x, y) in enumerate(points))
    return make_row(events)


def test_catalogue_has_every_event_type():
    from macro_recorder.model.events import EVENT_TYPE_VALUES
    assert {s.type for s in catalogue.all_specs()} == EVENT_TYPE_VALUES
    for spec in catalogue.all_specs():
        for preset in spec.presets:
            ev = preset.make()
            assert ev.type == spec.type
            assert set(ev.params) == set(spec.keys)


def test_make_rejects_unknown_keys():
    with pytest.raises(ValueError, match="Unknown field"):
        make("wait", bogus=1)


def test_row_rejects_mixed_multi_event():
    with pytest.raises(ValueError):
        Row(id=1, events=(make("wait"), make("goto", target="End")))


def test_insert_delete_move_undo_redo():
    doc = MacroDocument()
    changes = []
    doc.subscribe(changes.append)
    a, b, c = wait(1), wait(2), wait(3)
    ids = doc.append([a, b, c])
    assert [r.event["duration_ms"] for r in doc.rows] == [1, 2, 3]
    assert ids != (a.id, b.id, c.id)          # fresh ids on insert
    assert changes[-1] == Change("inserted", ids)
    assert doc.dirty

    doc.move([ids[2]], 0)
    assert [r.event["duration_ms"] for r in doc.rows] == [3, 1, 2]
    doc.delete([ids[0]])
    assert [r.event["duration_ms"] for r in doc.rows] == [3, 2]

    assert doc.undo()
    assert [r.event["duration_ms"] for r in doc.rows] == [3, 1, 2]
    assert doc.undo()
    assert [r.event["duration_ms"] for r in doc.rows] == [1, 2, 3]
    assert doc.undo()
    assert doc.rows == ()
    assert not doc.dirty
    assert not doc.undo()
    assert doc.redo()
    assert len(doc.rows) == 3
    assert doc.dirty


def test_saved_revision_tracks_undo():
    doc = MacroDocument()
    doc.append([wait(1)])
    doc.mark_saved("/tmp/x.json")
    assert not doc.dirty
    doc.append([wait(2)])
    assert doc.dirty
    doc.undo()
    assert not doc.dirty
    doc.redo()
    assert doc.dirty


def test_edit_session_coalesces_one_field():
    doc = MacroDocument()
    (rid,) = doc.append([wait(1)])
    doc.begin_edit(rid, "duration_ms")
    for ms in (10, 100, 1000):
        row = doc.row(rid)
        doc.replace_row(row.__class__(**{**row.__dict__, "events": (row.event.with_params(duration_ms=ms),)}))
    assert doc.row(rid).event["duration_ms"] == 1000
    doc.end_edit()
    assert doc.undo()
    assert doc.row(rid).event["duration_ms"] == 1
    assert doc.redo()
    assert doc.row(rid).event["duration_ms"] == 1000


def test_session_breaks_on_other_field_and_other_command():
    doc = MacroDocument()
    (rid,) = doc.append([wait(1)])
    doc.begin_edit(rid, "duration_ms")
    doc.replace_row(doc.row(rid).__class__(**{**doc.row(rid).__dict__,
                                              "events": (doc.row(rid).event.with_params(duration_ms=5),)}))
    doc.begin_edit(rid, "random_max_ms")
    doc.replace_row(doc.row(rid).__class__(**{**doc.row(rid).__dict__,
                                              "events": (doc.row(rid).event.with_params(random_max_ms=9),)}))
    doc.undo()
    assert doc.row(rid).event["duration_ms"] == 5
    assert doc.row(rid).event["random_max_ms"] == 0


def test_rename_label_rewrites_targets_and_rejects_duplicates():
    doc = MacroDocument()
    a, b = doc.append([wait(1, label="top"), goto("top")])
    doc.rename_label(a, "loop")
    assert doc.row(b).event["target"] == "loop"
    with pytest.raises(ValueError, match="already used"):
        doc.rename_label(b, "LOOP")
    with pytest.raises(ValueError, match="reserved"):
        doc.rename_label(b, "start")
    assert label_problem("1abc") is not None
    assert label_problem("ok-label_2") is None


def test_paste_clears_duplicate_labels():
    doc = MacroDocument()
    doc.append([wait(1, label="top")])
    (pasted,) = doc.append([wait(2, label="top")])
    assert doc.row(pasted).label == ""


def test_split_path_keeps_metadata_and_turns_gaps_into_delays():
    doc = MacroDocument()
    row = path([(0, 0), (1, 1), (2, 2)], dt=0.2)
    (rid,) = doc.append([Row(**{**row.__dict__, "label": "p", "comment": "c", "breakpoint": True, "delay_ms": 7})])
    ids = doc.split(rid)
    rows = doc.rows
    assert len(rows) == 3 and ids == tuple(r.id for r in rows)
    assert rows[0].id == rid and rows[0].label == "p" and rows[0].comment == "c" and rows[0].breakpoint
    assert rows[0].delay_ms == 7
    assert rows[1].delay_ms == 200 and rows[1].label == "" and not rows[1].breakpoint
    assert all(r.event.dt == 0.0 for r in rows)
    doc.undo()
    assert len(doc.rows) == 1 and doc.rows[0].is_path


def test_filter_and_path_mode_are_undoable_and_dirty():
    doc = MacroDocument()
    doc.set_filter(["wait", "bogus", "mouse_move"])
    assert doc.filter == ("mouse_move", "wait")
    assert doc.dirty
    doc.set_path_mode("spline")
    doc.undo()
    assert doc.path_mode == "recorded" and doc.filter == ("mouse_move", "wait")
    with pytest.raises(ValueError):
        doc.set_path_mode("zigzag")


def test_plays_respects_enabled_and_filter():
    doc = MacroDocument()
    (w, g) = doc.append([wait(1), goto("End")])
    assert doc.plays(doc.row(w))
    doc.set_filter(["wait"])
    assert not doc.plays(doc.row(w))
    assert doc.plays(doc.row(g))
    doc.set_enabled([g], False)
    assert not doc.plays(doc.row(g))
    doc.toggle_breakpoint([g])
    assert doc.row(g).breakpoint


def test_governing_window():
    doc = MacroDocument()
    win = make_row(make("window_focus", window="xed"))
    ids = doc.append([wait(1), win, wait(2), wait(3)])
    assert doc.governing_window(0) is None
    assert doc.governing_window(3).id == ids[1]
    doc.set_enabled([ids[1]], False)
    assert doc.governing_window(3) is None


def test_reset_clears_history():
    doc = MacroDocument()
    doc.append([wait(1)])
    doc.reset([wait(9)], path="/tmp/new.json")
    assert not doc.can_undo and not doc.dirty and doc.path == "/tmp/new.json"
    assert doc.rows[0].event["duration_ms"] == 9


def test_event_equality_and_with_params():
    a = make("wait", duration_ms=5)
    b = a.with_params(duration_ms=5)
    assert a == b and hash(a) == hash(b)
    assert a.with_params(duration_ms=6) != a
    assert isinstance(a, MacroEvent)
