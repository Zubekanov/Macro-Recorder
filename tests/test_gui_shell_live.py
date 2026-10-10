"""The real shell window, built off-screen, driven through its commands and pump.

Runs only under Xvfb (``MACRO_RECORDER_LIVE_X=1``).  The platform objects are
the conftest fakes, so nothing records, injects or grabs.
"""

from __future__ import annotations

import time

import pytest

tk = pytest.importorskip("tkinter")

from macro_recorder.gui.main_window import MainWindow
from macro_recorder.gui.playback import Platform, PlatformFactories
from macro_recorder.gui.settings import Settings
from macro_recorder.gui.table import LOCKED_TAG, preset_command
from macro_recorder.model import fileformat
from macro_recorder.model.catalogue import make
from macro_recorder.model.document import MacroDocument, make_row
from macro_recorder.model.protocols import Mode, RawEvent
from macro_recorder.platform.session import OwnWindows
from tests.conftest import FakeHotkeys, FakeInput, FakeListener, FakeScreen, FakeStopKey, FakeWindows

pytestmark = pytest.mark.live_x

TIMEOUT_S = 10.0


class Shell:
    def __init__(self, window: MainWindow, own: OwnWindows) -> None:
        self.window = window
        self.own = own
        self.messages: list[tuple[str, str]] = []
        self.listeners: list[FakeListener] = []
        self.unsaved_answer = "cancel"
        self.prompts: list[str] = []
        window.show_message = lambda title, text: self.messages.append((title, text))
        window.ask_unsaved = self._ask_unsaved

    def _ask_unsaved(self, name: str) -> str:
        self.prompts.append(name)
        return self.unsaved_answer

    def run_until(self, condition) -> None:
        deadline = time.monotonic() + TIMEOUT_S
        while not condition():
            assert time.monotonic() < deadline, "timed out"
            self.window.root.update()
            time.sleep(0.01)

    def ids(self) -> list[int]:
        return [r.id for r in self.window.document.rows]


@pytest.fixture
def shell():
    try:
        root = tk.Tk(className="macro-recorder")
    except tk.TclError as e:
        pytest.skip("no display: %s" % e)
    root.withdraw()
    listeners: list[FakeListener] = []

    def new_listener() -> FakeListener:
        listener = FakeListener()
        listeners.append(listener)
        return listener
    factories = PlatformFactories(windows=FakeWindows, input=FakeInput, screen=FakeScreen,
                                  listener=new_listener, stop_watcher=FakeStopKey,
                                  grabber=FakeHotkeys)
    own = OwnWindows()
    window = MainWindow(root, Settings(), own_windows=own, platform=Platform(factories),
                        save_settings=lambda settings: None)
    rig = Shell(window, own)
    rig.listeners = listeners
    root.update()
    yield rig
    if not window._closing:
        window.close()


def write_macro(path, rows) -> str:
    fileformat.save(MacroDocument(rows), path)
    return str(path)


def wait_row(ms=1, **fields):
    return make_row(make("wait", duration_ms=ms), **fields)


def test_open_select_add_undo(shell, tmp_path):
    w = shell.window
    path = write_macro(tmp_path / "demo.json", [wait_row(), wait_row(label="second"), wait_row()])
    w.open_path(path)
    shell.run_until(lambda: True)
    assert len(w.table.tree.get_children()) == 3
    assert w.root.title() == "demo - Macro Recorder"
    assert w.settings.ui.recent[0].endswith("demo.json")
    first = shell.ids()[0]
    w.table.select([first])
    w.root.update()
    assert w.table.selected_ids() == (first,)
    assert w.commands.run(preset_command("Click"))
    assert w.document.rows[1].type == "mouse_click"
    assert w.table.selected_ids() == (w.document.rows[1].id,)
    assert w.root.title() == "demo* - Macro Recorder"
    assert w.count_label.cget("text") == "4 rows"
    assert w.commands.run("edit.undo")
    assert [r.type for r in w.document.rows] == ["wait"] * 3
    assert len(w.table.tree.get_children()) == 3
    assert w.root.title() == "demo - Macro Recorder"


def test_single_key_insert_binding_respects_modifiers(shell):
    w = shell.window
    tree = w.table.tree
    assert tree.bind("<KeyPress-c>") and tree.bind("<KeyPress-C>")
    assert tree.bind("<Control-c>") and tree.bind("<Control-C>")
    assert w.root.bind("<Control-z>") and w.root.bind("<F5>")
    assert not tree.bind("<Control-z>")


def test_alt_up_moves_the_selection(shell):
    w = shell.window
    w.document.append([wait_row(1), wait_row(2), wait_row(3)])
    ids = shell.ids()
    w.table.select([ids[2]])
    w.root.update()
    assert w.table.tree.bind("<Alt-Up>")
    assert w.commands.run("edit.move_up")
    assert shell.ids() == [ids[0], ids[2], ids[1]]
    assert w.table.selected_ids() == (ids[2],)
    assert [w.table.tree.set(str(i), "number") for i in shell.ids()] == ["1", "2", "3"]
    assert w.commands.run("edit.move_down")
    assert shell.ids() == ids


def test_inline_label_edit_validates_and_commits(shell):
    w = shell.window
    w.root.deiconify()
    w.document.append([wait_row(label="taken"), wait_row()])
    target = shell.ids()[1]
    w.root.update()
    w.table.begin_label_edit(target)
    w.root.update()
    entry = w.table.editor_entry
    assert entry is not None
    entry.delete(0, "end")
    entry.insert(0, "Taken")
    w.root.update()
    assert str(entry.cget("style")) == "Invalid.TEntry"
    entry.event_generate("<Return>")
    w.root.update()
    assert w.table.editing                      # invalid: stays open
    entry.delete(0, "end")
    entry.insert(0, "loop")
    w.root.update()
    assert str(entry.cget("style")) == "TEntry"
    entry.event_generate("<Return>")
    w.root.update()
    assert not w.table.editing
    assert w.document.row(target).label == "loop"
    assert w.table.tree.set(str(target), "label") == "loop"


def test_document_change_cancels_the_inline_editor(shell):
    w = shell.window
    w.root.deiconify()
    w.document.append([wait_row(), wait_row()])
    w.root.update()
    w.table.begin_comment_edit(shell.ids()[0])
    w.root.update()
    assert w.table.editing
    w.document.append([wait_row()])
    assert not w.table.editing


def test_modes_set_widget_states(shell):
    w = shell.window
    w.document.append([wait_row()])
    w.table.select(shell.ids())
    w.root.update()
    assert w.record_button.instate(["!disabled"])
    assert w.play_stop_button.instate(["disabled"])
    w.set_mode(Mode.PLAYING)
    assert w.play_stop_button.instate(["!disabled"])
    assert w.record_button.instate(["disabled"])
    assert w.play_button.instate(["disabled"])
    assert w.speed_box.instate(["disabled"])
    assert str(w.table.tree.cget("selectmode")) == "none"
    assert w.table.tree.bindtags()[0] == LOCKED_TAG
    assert w.table.selected_ids() == ()
    assert not w.commands.run("edit.delete")
    w.set_mode(Mode.PAUSED)
    assert w.play_button.instate(["!disabled"])
    assert w.commands.enabled("play.stop")
    assert not w.commands.enabled("file.save")
    w.set_mode(Mode.IDLE)
    assert str(w.table.tree.cget("selectmode")) == "extended"
    assert w.table.selected_ids() == tuple(shell.ids())
    assert w.speed_box.instate(["!disabled"])


def test_session_problem_disables_record_and_play():
    try:
        root = tk.Tk(className="macro-recorder")
    except tk.TclError as e:
        pytest.skip("no display: %s" % e)
    root.withdraw()
    from macro_recorder.platform.session import SessionProblem
    window = MainWindow(root, Settings(), own_windows=OwnWindows(), platform=None,
                        session_problem=SessionProblem("wayland", "Wayland"),
                        save_settings=lambda settings: None)
    try:
        root.update()
        assert window.record_button.instate(["disabled"])
        assert window.play_button.instate(["disabled"])
        assert not window.commands.enabled("record.toggle")
        assert window.commands.enabled("file.open")
    finally:
        window.close()


def test_playback_through_the_real_pump(shell):
    w = shell.window
    rows = [wait_row(), make_row(make("var_set", var_name="n", expr="5"), breakpoint=True),
            wait_row()]
    w.document.append(rows)
    ids = shell.ids()
    w.table.select([ids[0]])
    w.root.update()
    assert w.commands.run("play.play")
    assert w.mode is Mode.PLAYING
    shell.run_until(lambda: w.mode is Mode.PAUSED)
    assert w._play_text.get() == "Continue"
    assert w.table.tree.set(str(ids[1]), "number").startswith(">")
    assert "playing" in w.table.tree.item(str(ids[1]), "tags")
    assert w.commands.run("play.play")
    shell.run_until(lambda: w.mode is Mode.IDLE)
    assert w._play_text.get() == "Play"
    assert ("n", "5") in w.log_panel.variables()
    assert "Playback finished" in w.log_panel.text()
    assert w.table.selected_ids() == (ids[0],)
    assert not w.table.tree.set(str(ids[1]), "number").startswith(">")
    assert shell.messages == []


def test_message_box_through_the_real_pump(shell):
    w = shell.window
    w.document.append([make_row(make("message_box", text="Go?", buttons="yes_no")), wait_row()])
    w.commands.run("play.play")
    shell.run_until(lambda: w._asks)
    dialog = w._asks[0]
    assert set(dialog.buttons) == {"yes", "no"}
    dialog.choose("yes")
    shell.run_until(lambda: w.mode is Mode.IDLE)
    assert shell.messages == []


def test_stop_closes_a_pending_message_box(shell):
    w = shell.window
    w.document.append([make_row(make("message_box", text="Go?")), wait_row()])
    w.commands.run("play.play")
    shell.run_until(lambda: w._asks)
    w.commands.run("play.stop")
    shell.run_until(lambda: w.mode is Mode.IDLE)
    assert w._asks == []
    assert "Playback stopped" in w.log_panel.text()


def test_playback_error_is_reported_and_names_the_row(shell):
    w = shell.window
    w.document.append([wait_row(), make_row(make("goto", target="missing"))])
    w.commands.run("play.play")
    shell.run_until(lambda: w.mode is Mode.IDLE)
    shell.run_until(lambda: shell.messages)
    title, text = shell.messages[0]
    assert title == "Playback stopped" and text.startswith("Row 2:")
    assert w.table.selected_ids() == (shell.ids()[1],)


def test_unsaved_prompt_on_new(shell, tmp_path):
    w = shell.window
    w.document.append([wait_row()])
    shell.unsaved_answer = "cancel"
    w.commands.run("file.new")
    assert shell.prompts == ["Untitled"] and len(w.document.rows) == 1
    shell.unsaved_answer = "discard"
    w.commands.run("file.new")
    assert w.document.rows == () and not w.document.dirty
    assert w.root.title() == "Untitled - Macro Recorder"


def test_unsaved_prompt_save_writes_the_file(shell, tmp_path):
    w = shell.window
    path = write_macro(tmp_path / "keep.json", [wait_row()])
    w.open_path(path)
    w.document.append([wait_row()])
    shell.unsaved_answer = "save"
    w.commands.run("file.new")
    assert shell.prompts == ["keep"]
    assert len(fileformat.load(path).rows) == 2
    assert w.document.rows == ()


def test_missing_recent_file_is_reported_and_dropped(shell, tmp_path):
    w = shell.window
    path = write_macro(tmp_path / "gone.json", [wait_row()])
    w.open_path(path)
    (tmp_path / "gone.json").unlink()
    w.commands.run("file.new")
    w.commands.run("file.open_recent", path)
    shell.run_until(lambda: shell.messages)
    assert shell.messages[0][0] == "Open"
    assert all(not p.endswith("gone.json") for p in w.settings.ui.recent)


def test_paste_round_trip(shell):
    w = shell.window
    w.document.append([wait_row(label="a"), wait_row()])
    ids = shell.ids()
    w.table.select(ids)
    w.commands.run("edit.copy")
    w.table.select([ids[1]])
    w.commands.run("edit.paste")
    assert len(w.document.rows) == 4
    assert w.document.rows[2].label == ""          # duplicate label cleared
    assert "already used" in w.log_panel.text()


def test_toplevels_register_in_own_windows(shell):
    w = shell.window
    w.root.deiconify()
    shell.run_until(lambda: w.root.winfo_ismapped())
    w.root.update()
    assert shell.own.ids()
    top = tk.Toplevel(w.root)
    shell.run_until(lambda: top.winfo_ismapped())
    w.root.update()
    count = len(shell.own.ids())
    top.destroy()
    w.root.update()
    assert len(shell.own.ids()) == count - 1


def test_targeting_round_trip(shell):
    w = shell.window
    delivered: list = []

    class FakeSession:
        def __init__(self, on_done):
            self.on_done = on_done

        def start(self):
            from macro_recorder.gui.targeting import Capture
            self.on_done(Capture(10, 20, "#ff0000"), None)

        def cancel(self):
            pass

    w.targeting_factory = FakeSession
    w.request_targeting(("x", "y"), delivered.append)
    assert w.mode is Mode.TARGETING
    shell.run_until(lambda: delivered)
    assert delivered == [{"x": 10, "y": 20, "colour": "#ff0000"}]
    assert w.mode is Mode.IDLE


# -- review follow-ups: Escape in message_box, recording, drag, locked keys ---------

def show(shell) -> None:
    """Map the window and give the table focus, so generated events arrive."""
    w = shell.window
    w.root.deiconify()
    shell.run_until(lambda: w.root.winfo_ismapped())
    w.table.tree.focus_force()
    w.root.update()


def row_centre(tree, iid: str) -> tuple[int, int]:
    x, y, width, height = tree.bbox(iid)
    return x + width // 2, y + height // 2


def test_escape_in_the_message_box_stops_playback(shell):
    w = shell.window
    w.document.append([
        make_row(make("message_box", text="Go?", buttons="yes_no", target_no="no_branch")),
        wait_row(),
        make_row(make("var_set", var_name="hit", expr="1"), label="no_branch"),
    ])
    w.commands.run("play.play")
    shell.run_until(lambda: w._asks)
    dialog = w._asks[0]
    dialog.focus_force()
    w.root.update()
    dialog.event_generate("<Escape>")
    shell.run_until(lambda: w.mode is Mode.IDLE)
    assert "Playback stopped" in w.log_panel.text()
    assert "hit" not in dict(w.log_panel.variables())
    assert not dialog.winfo_exists()


def test_recording_shows_provisional_rows_then_inserts_and_selects(shell):
    w = shell.window
    w.document.append([wait_row(1), wait_row(2)])
    first, second = shell.ids()
    w.table.select([first])
    w.root.update()
    assert w.commands.run("record.start")
    assert w.mode is Mode.RECORDING
    assert str(w.table.tree.cget("selectmode")) == "none"
    listener = shell.listeners[-1]
    t = time.monotonic()
    listener.feed(RawEvent("key_press", t, key="a"), RawEvent("key_release", t + 0.05, key="a"))
    tree = w.table.tree

    def provisional() -> list[str]:
        return [i for i in tree.get_children() if i.startswith("p")]
    shell.run_until(lambda: any(tree.set(i, "action") == "Key" for i in provisional()))
    children = tree.get_children()
    assert children[0] == str(first)
    assert children[1].startswith("p")              # after the selected row
    assert children[-1] == str(second)
    assert all("recording" in tree.item(i, "tags") for i in provisional())
    assert w.commands.run("record.stop")
    assert w.mode is Mode.IDLE
    assert provisional() == []
    rows = w.document.rows
    inserted = tuple(r.id for r in rows[1:-1])
    assert inserted and rows[0].id == first and rows[-1].id == second
    assert any(r.type == "key_combo" for r in rows[1:-1])
    assert w.table.selected_ids() == inserted
    assert w.commands.run("edit.undo")              # one undo step
    assert shell.ids() == [first, second]


def test_record_toggle_key_starts_and_stops_recording(shell):
    show(shell)
    w = shell.window
    w.table.tree.event_generate("<F9>")
    w.root.update()
    assert w.mode is Mode.RECORDING
    w.commands.run("record.toggle")
    assert w.mode is Mode.IDLE


def test_drag_moves_rows_to_the_drop_line(shell):
    show(shell)
    w = shell.window
    tree = w.table.tree
    w.document.append([wait_row(1), wait_row(2), wait_row(3), wait_row(4)])
    w.root.update()
    ids = shell.ids()
    x, y = row_centre(tree, str(ids[0]))
    _x, top, _width, height = tree.bbox(str(ids[2]))
    target_y = top + height - 2                     # lower half of the third row
    tree.event_generate("<ButtonPress-1>", x=x, y=y)
    w.root.update()
    assert w.table.selected_ids() == (ids[0],)
    for step in range(y, target_y + 1, 3):
        tree.event_generate("<B1-Motion>", x=x, y=step, state=0x100)
        w.root.update()
    tree.event_generate("<ButtonRelease-1>", x=x, y=target_y, state=0x100)
    w.root.update()
    assert shell.ids() == [ids[1], ids[2], ids[0], ids[3]]
    assert w.table.selected_ids() == (ids[0],)
    assert w.commands.run("edit.undo")
    assert shell.ids() == ids


def test_locked_table_ignores_keys_and_clicks(shell):
    show(shell)
    w = shell.window
    tree = w.table.tree
    w.document.append([wait_row(1), wait_row(2), wait_row(3)])
    ids = shell.ids()
    w.table.select([ids[0]])
    w.root.update()
    w.set_mode(Mode.RECORDING)
    tree.focus_force()
    w.root.update()
    before = w.document.rows
    for sequence in ("<Delete>", "<Control-a>", "<Down>", "<Alt-Down>", "<KeyPress-c>"):
        tree.event_generate(sequence)
        w.root.update()
    x, y = row_centre(tree, str(ids[2]))
    tree.event_generate("<ButtonPress-1>", x=x, y=y)
    tree.event_generate("<ButtonRelease-1>", x=x, y=y)
    w.root.update()
    assert w.document.rows == before
    assert tree.selection() == ()
    w.commands.run("record.stop")
    assert w.table.selected_ids() == (ids[0],)


def test_root_shortcuts_fire_from_the_table(shell):
    show(shell)
    w = shell.window
    tree = w.table.tree
    w.document.append([wait_row(1)])
    w.root.update()
    tree.event_generate("<Control-z>")
    w.root.update()
    assert w.document.rows == ()
    tree.event_generate("<Control-y>")
    w.root.update()
    assert len(w.document.rows) == 1
    tree.event_generate("<F5>")
    w.root.update()
    assert w.mode in (Mode.PLAYING, Mode.IDLE)
    shell.run_until(lambda: w.mode is Mode.IDLE)
    assert "Playback finished" in w.log_panel.text()


def test_double_click_on_the_number_column_toggles_the_breakpoint(shell):
    show(shell)
    w = shell.window
    tree = w.table.tree
    w.document.append([wait_row(1), wait_row(2)])
    w.root.update()
    target = shell.ids()[1]
    x, y, _width, height = tree.bbox(str(target), "number")
    tree.event_generate("<Double-Button-1>", x=x + 5, y=y + height // 2)
    w.root.update()
    assert w.document.row(target).breakpoint
    assert tree.set(str(target), "number").startswith("*")
    tree.event_generate("<Double-Button-1>", x=x + 5, y=y + height // 2)
    w.root.update()
    assert not w.document.row(target).breakpoint


def test_toggle_from_the_menu_does_not_merge_into_an_edit_session(shell):
    w = shell.window
    w.document.append([wait_row(1)])
    row = w.document.rows[0]
    w.table.select([row.id])
    w.document.begin_edit(row.id, "comment")
    from dataclasses import replace
    w.document.replace_row(replace(row, comment="edited"))
    assert w.commands.run("edit.toggle_enabled")
    assert w.commands.run("edit.undo")
    assert w.document.row(row.id).enabled
    assert w.document.row(row.id).comment == "edited"


def test_context_menu_lists_undo_redo_and_select_all(shell):
    w = shell.window
    w.document.append([wait_row(1)])
    w.table._build_context_menu()
    menu = w.table._context_menu
    labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
              if menu.type(i) == "command"]
    assert {"Undo", "Redo", "Select All"} <= set(labels)
