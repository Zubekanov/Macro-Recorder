"""The Details panel, its field widgets and the overlay against a real Tk.

Run only under Xvfb: ``MACRO_RECORDER_LIVE_X=1 xvfb-run -a pytest -m live_x``.
The root stays withdrawn; edits are made by setting the fields' StringVars,
which is what typing does.
"""

from __future__ import annotations

import pytest

from macro_recorder.gui.details import DetailsPanel
from macro_recorder.gui.settings import Settings
from macro_recorder.gui.targeting import Capture
from macro_recorder.model.catalogue import all_specs, make, spec_for
from macro_recorder.model.document import MacroDocument, make_row
from macro_recorder.model.protocols import Mode, OverlayShape
from macro_recorder.platform.session import OwnWindows

pytestmark = pytest.mark.live_x


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as e:
        pytest.skip("no display: %s" % e)
    r.withdraw()
    yield r
    r.destroy()


class Harness:
    """A panel over a document, recording shapes and targeting requests."""

    def __init__(self, root, rows):
        self.root = root
        self.document = MacroDocument(rows)
        self.shapes: list = []
        self.requests: list = []
        self.mode = Mode.IDLE
        self.panel = DetailsPanel(root, self.document, Settings(),
                                  lambda keys, deliver: self.requests.append((keys, deliver)),
                                  self.shapes.append, lambda: self.mode)
        self.panel.pack(fill="both", expand=True)

    def show(self, row):
        self.panel.show(row.id)
        self.root.update_idletasks()

    def widget(self, key):
        for widget in self.panel._row_fields:
            if key in widget.spec.keys:
                return widget
        for spec, widget in self.panel._fields:
            if key in spec.keys:
                return widget
        raise KeyError(key)

    def type(self, key, text, index=0):
        self.widget(key)._vars[index].set(text)
        self.root.update_idletasks()

    def row(self, row):
        return self.document.row(row.id)


def path_row():
    return make_row([make("mouse_move", x=10, y=20), make("mouse_move", x=30, y=40, dt=0.1),
                     make("mouse_move", x=50, y=60, dt=0.1)])


# ------------------------------------------------------------------ building

def test_a_panel_builds_for_every_action_type(root):
    rows = [make_row(make(s.type)) for s in all_specs()] + [path_row()]
    h = Harness(root, rows)
    for row in rows:
        h.show(row)
        layout = spec_for(row).layout_for(row)
        assert [s for s, _w in h.panel._fields] == list(layout)
        assert len(h.panel._row_fields) == 4
        assert h.shapes[-1] == spec_for(row).overlay(row)
        assert all(w.state != "invalid" or w.spec.kind.name == "LABEL_TARGET"
                   for _s, w in h.panel._fields)
    h.panel.show(None)
    assert h.panel._fields == [] and h.shapes[-1] is None


def test_show_does_not_commit(root):
    row = make_row(make("mouse_click", x=1.23456, y=2))
    h = Harness(root, [row])
    h.show(row)
    assert h.widget("x").text(0) == "1.235"
    assert not h.document.dirty


# ------------------------------------------------------------------ commits

def test_typing_commits_and_coalesces_into_one_undo_step(root):
    row = make_row(make("wait", duration_ms=500))
    h = Harness(root, [row])
    h.show(row)
    for text in ("7", "75", "750"):
        h.type("duration_ms", text)
    assert h.row(row).event["duration_ms"] == 750
    assert h.document.undo()
    assert h.row(row).event["duration_ms"] == 500
    assert not h.document.can_undo
    assert h.widget("duration_ms").text() == "500"


def test_a_new_field_starts_a_new_undo_step(root):
    row = make_row(make("wait", duration_ms=500))
    h = Harness(root, [row])
    h.show(row)
    h.type("duration_ms", "600")
    h.type("random_max_ms", "900")
    h.document.undo()
    assert h.row(row).event["duration_ms"] == 600
    assert h.row(row).event["random_max_ms"] == 0


def test_selection_change_ends_the_edit_session(root):
    a, b = make_row(make("wait", duration_ms=500)), make_row(make("wait", duration_ms=100))
    h = Harness(root, [a, b])
    h.show(a)
    h.type("duration_ms", "501")
    h.show(b)
    h.show(a)
    h.type("duration_ms", "502")
    h.document.undo()
    assert h.row(a).event["duration_ms"] == 501


def test_invalid_text_is_red_and_reverts(root):
    row = make_row(make("wait", duration_ms=500))
    h = Harness(root, [row])
    h.show(row)
    h.type("duration_ms", "1 +")
    widget = h.widget("duration_ms")
    assert widget.state == "invalid" and widget.pending
    assert h.row(row).event["duration_ms"] == 500
    assert str(widget.inputs[0].cget("style")) == "Invalid.TEntry"
    h.panel.revert_pending()
    assert widget.text() == "500" and widget.state == "ok"


def test_unassigned_variable_is_amber_and_committed(root):
    row = make_row(make("wait", duration_ms=500))
    h = Harness(root, [row])
    h.show(row)
    h.type("duration_ms", "pause * 2")
    assert h.widget("duration_ms").state == "amber"
    assert h.row(row).event["duration_ms"] == "pause * 2"
    setter = make_row(make("var_set", var_name="pause", expr="10"))
    h.document.insert(0, [setter])
    assert h.widget("duration_ms").state == "ok"


def test_coord_pair_validates_both_halves_together(root):
    row = make_row(make("mouse_click", x=10, y=20))
    h = Harness(root, [row])
    h.show(row)
    h.type("x", "left")
    assert h.widget("x").state == "invalid"
    assert h.row(row).event["x"] == 10
    h.type("y", "top", index=1)
    assert (h.row(row).event["x"], h.row(row).event["y"]) == ("left", "top")


def move_focus(root, source, target):
    """Focus ``target`` and send ``source`` the FocusOut a click or Tab would."""
    root.deiconify()
    target.focus_force()
    root.update()
    source.event_generate("<FocusOut>")
    root.update()


def test_moving_between_pair_entries_keeps_red_text(root):
    row = make_row(make("mouse_scroll"))
    h = Harness(root, [row])
    h.show(row)
    widget = h.widget("x")
    x_entry, y_entry = widget.inputs[0], widget.inputs[1]
    h.type("x", "500")
    assert widget.state == "invalid"
    move_focus(root, x_entry, y_entry)
    assert widget.text(0) == "500"
    h.type("y", "300", index=1)
    assert (h.row(row).event["x"], h.row(row).event["y"]) == (500, 300)
    h.type("x", "")
    move_focus(root, x_entry, y_entry)
    h.type("y", "", index=1)
    assert (h.row(row).event["x"], h.row(row).event["y"]) == (None, None)


def test_leaving_the_field_reverts_red_text(root):
    row = make_row(make("mouse_click", x=10, y=20))
    h = Harness(root, [row])
    h.show(row)
    widget = h.widget("x")
    h.type("x", "left")
    move_focus(root, widget.inputs[0], h.widget("delay_ms").inputs[0])
    assert widget.text(0) == "10" and widget.state == "ok"


def test_undo_reverts_red_text(root):
    row = make_row(make("wait", duration_ms=500))
    h = Harness(root, [row])
    h.show(row)
    h.type("random_max_ms", "700")
    h.type("duration_ms", "oops +")
    h.document.undo()
    assert h.widget("duration_ms").text() == "500"
    assert h.widget("random_max_ms").text() == "0"


def test_path_start_translates_every_point(root):
    row = path_row()
    h = Harness(root, [row])
    h.show(row)
    h.type("x", "15")
    assert [e["x"] for e in h.row(row).events] == [15, 35, 55]
    assert h.shapes[-1] == OverlayShape("path", ((15.0, 20.0), (35.0, 40.0), (55.0, 60.0)))


def test_choice_bool_and_visibility(root):
    row = make_row(make("mouse_click"))
    h = Harness(root, [row])
    h.show(row)
    count = h.widget("count")
    assert count.frame.grid_info()
    h.type("action", "down")
    assert h.row(row).event["action"] == "down"
    assert not count.frame.grid_info()


def test_enabled_when_disables_the_widget(root):
    row = make_row(make("type_text", text="hi"))
    h = Harness(root, [row])
    h.show(row)
    humanise = h.widget("humanise")
    assert humanise.inputs[0].instate(["disabled"])
    h.type("char_delay_ms", "20")
    assert not humanise.inputs[0].instate(["disabled"])
    assert h.widget("duration_ms").inputs[0].instate(["disabled"])


def test_row_fields(root):
    target = make_row(make("wait"), label="top")
    h = Harness(root, [target])
    h.show(target)
    h.type("enabled", "0")
    assert not h.row(target).enabled
    h.type("delay_ms", "40")
    assert h.row(target).delay_ms == 40
    assert [w.spec.keys for w in h.panel._row_fields] == [("enabled",), ("delay_ms",)]


def test_label_target_lists_labels_and_shows_a_missing_one_red(root):
    top = make_row(make("wait"), label="top")
    jumper = make_row(make("goto", target="gone"))
    h = Harness(root, [top, jumper])
    h.show(jumper)
    widget = h.widget("target")
    assert list(widget.combo.cget("values")) == ["Start", "top", "End", "gone"]
    assert widget.state == "invalid"
    h.type("target", "top")
    assert h.row(jumper).event["target"] == "top"
    assert widget.state == "ok"


def test_key_combo_keeps_the_typed_text(root):
    row = make_row(make("key_combo", keys="a"))
    h = Harness(root, [row])
    h.show(row)
    h.type("keys", "Ctrl+Shift+s")
    assert h.row(row).event["keys"] == "ctrl+shift+s"
    assert h.widget("keys").text() == "Ctrl+Shift+s"


def test_colour_field_and_swatch(root):
    row = make_row(make("wait_pixel"))
    h = Harness(root, [row])
    h.show(row)
    h.type("colour", "#FF0000")
    assert h.row(row).event["colour"] == "#ff0000"
    h.type("colour", "#ff00")
    assert h.widget("colour").state == "invalid"


def test_region_modes(root):
    row = make_row(make("ocr_read", x=1, y=2, x2=30, y2=40))
    h = Harness(root, [row])
    h.show(row)
    h.type("x", "Monitor")
    assert h.row(row).event["monitor"] == 1
    h.type("x", "2", index=5)
    assert h.row(row).event["monitor"] == 2
    h.type("x", "Rectangle")
    event = h.row(row).event
    assert (event["x"], event["y"], event["x2"], event["y2"], event["monitor"]) == (1, 2, 30, 40, None)


# ------------------------------------------------------------------ targeting and locking

def test_targeting_fills_position_and_colour_in_one_step(root):
    row = make_row(make("wait_pixel", x=1, y=1))
    h = Harness(root, [row])
    h.show(row)
    pick = [w for w in h.widget("x").inputs if w.winfo_class() == "TButton"][0]
    pick.invoke()
    keys, deliver = h.requests[-1]
    assert keys == ("x", "y", "colour")
    deliver(Capture(5, 6, "#ABCDEF"))
    event = h.row(row).event
    assert (event["x"], event["y"], event["colour"]) == (5, 6, "#abcdef")
    h.document.undo()
    assert h.row(row).event["colour"] == "#ffffff"


def test_region_targeting_takes_two_captures(root):
    row = make_row(make("ocr_read", x=1, y=2, x2=3, y2=4, monitor=2))
    h = Harness(root, [row])
    h.show(row)
    h.panel._context.request_targeting(h.widget("x").spec)
    keys, deliver = h.requests[-1]
    assert keys == ("x", "y")
    deliver(Capture(10, 20, "#000000"))
    assert len(h.requests) == 1
    root.update()
    keys, deliver = h.requests[-1]
    assert keys == ("x2", "y2")
    deliver(Capture(300, 200, "#000000"))
    event = h.row(row).event
    assert (event["x"], event["y"], event["x2"], event["y2"], event["monitor"]) == (10, 20, 300, 200, None)


def test_targeting_reverts_red_text(root):
    row = make_row(make("wait_pixel", x=1, y=1))
    h = Harness(root, [row])
    h.show(row)
    h.type("timeout_ms", "1 +")
    h.panel._context.request_targeting(h.widget("x").spec)
    assert h.widget("timeout_ms").state != "invalid"


def test_locked_panel_disables_and_refuses_edits(root):
    row = make_row(make("wait", duration_ms=500))
    h = Harness(root, [row])
    h.show(row)
    h.type("duration_ms", "1 +")
    h.mode = Mode.PLAYING
    h.panel.set_locked(True)
    widget = h.widget("duration_ms")
    assert widget.text() == "500"
    assert widget.inputs[0].instate(["disabled"])
    h.type("duration_ms", "900")
    assert h.row(row).event["duration_ms"] == 500
    h.mode = Mode.IDLE
    h.panel.set_locked(False)
    assert not widget.inputs[0].instate(["disabled"])


def test_deleting_the_shown_row_clears_the_panel(root):
    row = make_row(make("wait"))
    h = Harness(root, [row])
    h.show(row)
    h.document.delete([row.id])
    assert h.panel._fields == [] and h.shapes[-1] is None


def test_destroy_unsubscribes(root):
    row = make_row(make("wait"))
    h = Harness(root, [row])
    h.show(row)
    h.panel.destroy()
    h.document.delete([row.id])


# ------------------------------------------------------------------ overlay

def covers(rects, x, y):
    return any(rx <= x < rx + rw and ry <= y < ry + rh for rx, ry, rw, rh in rects)


def test_overlay_is_shaped_to_its_marks_and_click_through(root):
    from macro_recorder.gui.overlay import Overlay
    from macro_recorder.platform import x11

    own = OwnWindows()
    overlay = Overlay(root, own, enabled=True)
    overlay.show(OverlayShape("click", ((200, 150),)))
    root.update()
    assert overlay._xid is not None, "the SHAPE extension is expected under Xvfb"
    xid = overlay._xid
    assert xid == int(overlay._window.wm_frame(), 16)     # the wrapper Tk maps, not its child
    bounding = x11.get_window_shape(overlay._display, xid, x11.SHAPE_BOUNDING)
    assert covers(bounding, 200, 150)
    assert not covers(bounding, 600, 600)
    assert x11.get_window_shape(overlay._display, xid, x11.SHAPE_INPUT) == []
    assert own.contains(xid, include_overlay=True) and not own.contains(xid)
    assert overlay._window.winfo_ismapped()

    overlay.set_enabled(False)
    root.update()
    assert not overlay._window.winfo_ismapped()
    overlay.set_enabled(True)
    root.update()
    assert overlay._window.winfo_ismapped()

    overlay.show(OverlayShape("rect", ((100, 100), (400, 300))))
    root.update()
    bounding = x11.get_window_shape(overlay._display, xid, x11.SHAPE_BOUNDING)
    assert covers(bounding, 250, 100) and not covers(bounding, 250, 200)

    overlay.hide()
    root.update()
    assert not overlay._window.winfo_ismapped()
    overlay.close()
    assert not own.contains(xid, include_overlay=True)
    assert overlay._display is None and overlay._window is None
