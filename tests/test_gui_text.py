"""Typed newlines and tabs stay on one line in the table and the playback log."""

from macro_recorder.gui.playback import QueueHost
from macro_recorder.gui.table import row_cells
from macro_recorder.gui.text import visible_controls
from macro_recorder.model.catalogue import make
from macro_recorder.model.document import make_row


def test_visible_controls():
    assert visible_controls("a\nb\tc\r") == "a\\nb\\tc\\r"
    assert visible_controls("plain") == "plain"


def test_table_value_cell_is_one_line():
    row = make_row(make("type_text", text="hi\nthere"))
    assert "\n" not in row_cells(0, row, False)[2]
    assert "hi\\nthere" in row_cells(0, row, False)[2]


def test_player_log_lines_are_one_line():
    posted = []
    QueueHost(lambda kind, payload: posted.append((kind, payload))).on_log("[4] type \"x\n\"")
    assert posted == [("log", "[4] type \"x\\n\"")]
