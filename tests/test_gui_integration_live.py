"""The real Details panel, overlay and Settings dialog inside the shell.

Runs only under Xvfb (``MACRO_RECORDER_LIVE_X=1``).  The platform objects are
the conftest fakes; the overlay is real and shapes itself on the Xvfb screen.
"""

from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from macro_recorder.gui import settings as settings_module
from macro_recorder.gui.details import DetailsPanel
from macro_recorder.gui.main_window import DETAILS_WIDTH, MainWindow
from macro_recorder.gui.playback import Platform, PlatformFactories
from macro_recorder.gui.settings import Settings, SettingsDialog, open_settings_dialog
from macro_recorder.gui.targeting import Capture
from macro_recorder.model.catalogue import make
from macro_recorder.model.document import make_row
from macro_recorder.model.protocols import Mode
from macro_recorder.platform.session import OwnWindows
from tests.conftest import FakeHotkeys, FakeInput, FakeListener, FakeScreen, FakeStopKey, FakeWindows
from tests.test_gui_shell_live import Shell, show, wait_row

pytestmark = pytest.mark.live_x


class Rig(Shell):
    """A shell whose grabber and saved settings the test can see."""

    def __init__(self, window: MainWindow, own: OwnWindows, grabber: FakeHotkeys,
                 saved: list[Settings]) -> None:
        super().__init__(window, own)
        self.grabber = grabber
        self.saved = saved


def make_rig(settings: Settings) -> Rig:
    try:
        root = tk.Tk(className="macro-recorder")
    except tk.TclError as e:
        pytest.skip("no display: %s" % e)
    root.withdraw()
    grabber = FakeHotkeys(refuse={"ctrl+alt+k"})
    factories = PlatformFactories(windows=FakeWindows, input=FakeInput, screen=FakeScreen,
                                  listener=FakeListener, stop_watcher=FakeStopKey,
                                  grabber=lambda: grabber)
    saved: list[Settings] = []
    own = OwnWindows()
    window = MainWindow(root, settings, own_windows=own, platform=Platform(factories),
                        save_settings=lambda s: saved.append(settings_module.copy_settings(s)))
    window.start_hotkeys()
    shell = Rig(window, own, grabber, saved)
    root.update()
    return shell


@pytest.fixture
def rig():
    shell = make_rig(Settings())
    yield shell
    if not shell.window._closing:
        shell.window.close()


def select(rig: Rig, row_id: int) -> None:
    rig.window.table.select([row_id])
    rig.window.root.update()


def close_toplevels(root) -> None:
    for child in root.winfo_children():
        if isinstance(child, tk.Toplevel) and child.title() in ("Settings", "Capture keys"):
            child.destroy()


def field(panel: DetailsPanel, key: str):
    for widget in panel._row_fields + [w for _s, w in panel._fields]:
        if key in widget.spec.keys:
            return widget
    raise KeyError(key)


class ScriptedTargeting:
    """Targeting sessions that answer with the queued captures, one per session."""

    def __init__(self, captures: list[Capture]) -> None:
        self.captures = list(captures)
        self.started = 0

    def __call__(self, on_done):
        script = self

        class Session:
            def start(self) -> None:
                script.started += 1
                on_done(script.captures.pop(0), None)

            def cancel(self) -> None:
                pass
        return Session()


# ------------------------------------------------------------------ Details panel

def test_selecting_a_row_shows_the_real_details_panel_and_edits_commit(rig):
    w = rig.window
    assert isinstance(w.details, DetailsPanel)
    (row_id,) = w.document.append([wait_row(500)])
    select(rig, row_id)
    duration = field(w.details, "duration_ms")
    duration._vars[0].set("750")
    w.root.update()
    assert w.document.row(row_id).event["duration_ms"] == 750
    assert w.root.title() == "Untitled* - Macro Recorder"
    assert w.commands.run("edit.undo")
    assert field(w.details, "duration_ms").text() == "500"


def test_details_are_locked_while_recording(rig):
    w = rig.window
    (row_id,) = w.document.append([wait_row(500)])
    select(rig, row_id)
    entry = field(w.details, "duration_ms").inputs[0]
    assert w.commands.run("record.start")
    w.root.update()
    assert entry.instate(["disabled"])
    assert w.commands.run("record.stop")
    w.root.update()
    assert not field(w.details, "duration_ms").inputs[0].instate(["disabled"])


def test_pick_fills_a_point_and_its_colour_through_the_shell(rig):
    w = rig.window
    (row_id,) = w.document.append([make_row(make("wait_pixel", x=1, y=1))])
    select(rig, row_id)
    w.targeting_factory = ScriptedTargeting([Capture(40, 50, "#12AB34")])
    pick = [b for b in field(w.details, "x").inputs if b.winfo_class() == "TButton"][0]
    pick.invoke()
    rig.run_until(lambda: w.mode is Mode.IDLE and w.document.row(row_id).event["x"] == 40)
    event = w.document.row(row_id).event
    assert (event["x"], event["y"], event["colour"]) == (40, 50, "#12ab34")


def test_region_pick_runs_a_second_targeting_session(rig):
    w = rig.window
    (row_id,) = w.document.append([make_row(make("ocr_read", x=1, y=2, x2=3, y2=4))])
    select(rig, row_id)
    script = ScriptedTargeting([Capture(10, 20, "#000000"), Capture(300, 200, "#000000")])
    w.targeting_factory = script
    w.details._context.request_targeting(field(w.details, "x").spec)
    rig.run_until(lambda: script.started == 2 and w.mode is Mode.IDLE
                  and w.document.row(row_id).event["x2"] == 300)
    event = w.document.row(row_id).event
    assert (event["x"], event["y"], event["x2"], event["y2"]) == (10, 20, 300, 200)
    w.document.undo()
    assert w.document.row(row_id).event["x"] == 1


def test_details_sash_is_placed_once_even_on_a_narrow_window():
    settings = Settings()
    settings.ui.geometry = "700x500"
    shell = make_rig(settings)
    w = shell.window
    try:
        show(shell)
        shell.run_until(lambda: w._sash_binding is None)
        width = w.upper_pane.winfo_width()
        assert abs(w.upper_pane.sashpos(0) - width // 2) <= 8
        w.upper_pane.sashpos(0, 250)
        w.root.geometry("1400x700")
        w.root.update()
        shell.run_until(lambda: w.upper_pane.winfo_width() > 1000)
        # The panes share the extra width by weight; the sash is not reset to width - 400.
        assert w.upper_pane.sashpos(0) < w.upper_pane.winfo_width() - DETAILS_WIDTH - 100
    finally:
        w.close()


def test_key_combo_capture_suspends_the_hotkeys(rig):
    w = rig.window
    (row_id,) = w.document.append([make_row(make("key_combo", keys="a"))])
    select(rig, row_id)
    seen = []

    def capture(single: bool) -> str:
        seen.append((single, rig.grabber.suspended))
        return "ctrl+shift+s"
    w.capture_key = capture
    button = [b for b in field(w.details, "keys").inputs if b.winfo_class() == "TButton"][0]
    assert button.cget("text") == "Capture"
    button.invoke()
    w.root.update()
    assert seen == [(False, True)]
    assert not rig.grabber.suspended
    assert w.document.row(row_id).event["keys"] == "ctrl+shift+s"
    assert field(w.details, "keys").text() == "Ctrl+Shift+S"


def test_key_capture_dialog_reads_one_key_press(rig):
    show(rig)
    w = rig.window
    result = []

    def press() -> None:
        dialog = [c for c in w.root.winfo_children() if isinstance(c, tk.Toplevel)
                  and c.title() == "Capture keys"][0]
        dialog.event_generate("<KeyPress>", keysym="Control_L", state=0)
        dialog.event_generate("<KeyPress>", keysym="S", state=0x5)
    w.root.after(200, press)
    w.root.after(5000, lambda: close_toplevels(w.root))    # a failed press must not hang
    result.append(w.capture_key(False))
    assert result == ["ctrl+shift+s"]


# ------------------------------------------------------------------ overlay

def test_overlay_follows_the_selection_and_hides_while_recording_and_playing(rig):
    show(rig)
    w = rig.window
    click_id, wait_id = w.document.append([make_row(make("mouse_click", x=200, y=150)),
                                           wait_row(1)])
    select(rig, click_id)
    overlay = w._overlay
    assert overlay is not None and overlay._visible
    select(rig, wait_id)
    assert not overlay._visible
    select(rig, click_id)
    assert overlay._visible
    assert w.commands.run("record.start")
    w.root.update()
    assert not overlay._visible
    w.details.show(click_id)
    assert not overlay._visible
    assert w.commands.run("record.stop")
    w.root.update()
    assert w.commands.run("play.play")
    w.root.update()
    assert not overlay._visible
    rig.run_until(lambda: w.mode is Mode.IDLE)
    w.root.update()
    assert overlay._visible


def test_overlay_stays_hidden_while_another_application_has_the_focus(rig):
    show(rig)
    w = rig.window
    (click_id,) = w.document.append([make_row(make("mouse_click", x=200, y=150))])
    w.document.append([wait_row(1)])
    select(rig, click_id)
    overlay = w._overlay
    assert overlay._visible
    other = tk.Tk()             # a separate Tk application stands in for the target
    try:
        other.geometry("200x100+900+600")
        other.update()
        assert w.commands.run("play.play")
        other.focus_force()
        rig.run_until(lambda: w.mode is Mode.IDLE and w.root.focus_get() is None)
        w.root.update()
        assert not overlay._visible
        w.table.tree.focus_force()
        rig.run_until(lambda: overlay._visible)
    finally:
        other.destroy()


def test_breakpoint_cleared_while_paused_applies_to_the_running_playback(rig):
    w = rig.window
    w.settings.playback.repeat = 2
    w.repeat_var.set("2")
    (first, _second) = w.document.append([wait_row(1, breakpoint=True), wait_row(1)])
    assert w.commands.run("play.play")
    rig.run_until(lambda: w.mode is Mode.PAUSED)
    assert w.commands.run("edit.toggle_breakpoint")
    assert not w.document.row(first).breakpoint
    assert w.commands.run("play.play")
    rig.run_until(lambda: w.mode is Mode.IDLE)
    assert w.log_panel.text().count("Paused at row 1") == 1
    assert "Playback finished" in w.log_panel.text()


# ------------------------------------------------------------------ Settings dialog

def open_dialog(rig: Rig, act) -> bool:
    """Open File > Settings and run ``act(dialog)`` once it is shown."""
    w = rig.window
    original = settings_module.SettingsDialog
    shown = []
    errors: list[BaseException] = []

    def run_act(dialog: SettingsDialog) -> None:
        try:
            act(dialog)
        except BaseException as e:      # Tk would only print it
            errors.append(e)
            dialog.cancel()

    class Recording(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            shown.append(self)
            self.window.after(100, lambda: run_act(self))
            self.window.after(5000, self.cancel)    # a stuck act must not hang the run
    settings_module.SettingsDialog = Recording
    try:
        assert w.commands.run("file.settings")
    finally:
        settings_module.SettingsDialog = original
    if errors:
        raise errors[0]
    assert shown
    return shown[0].saved


def test_settings_ok_saves_and_applies(rig):
    w = rig.window

    def act(dialog: SettingsDialog) -> None:
        assert rig.grabber.suspended
        dialog.number_vars["min_wait_ms"].set("150")
        dialog.number_vars["window_timeout_ms"].set("7000")
        dialog.on_missing_var.set("halt")
        dialog.record_paths_var.set(False)
        dialog.hotkey_vars["record"].set("Ctrl+Alt+R")
        dialog.ok_button.invoke()
    assert open_dialog(rig, act)
    settings = w.settings
    assert settings.recording.min_wait_ms == 150 and not settings.recording.record_paths
    assert settings.playback.window_timeout_ms == 7000 and settings.playback.on_missing == "halt"
    assert settings.hotkeys.record == "ctrl+alt+r"
    assert rig.saved and rig.saved[-1].hotkeys.record == "ctrl+alt+r"
    assert w.timeout_var.get() == "7000" and w.on_missing_var.get() == "halt"
    assert w.commands.run("help.shortcuts")
    assert "Ctrl+Alt+R Record or Stop" in rig.messages[-1][1]
    assert not rig.grabber.suspended
    assert set(rig.grabber.grabbed) == {"ctrl+alt+r", "ctrl+shift+F5"}


def test_settings_cancel_discards(rig):
    w = rig.window

    def act(dialog: SettingsDialog) -> None:
        dialog.number_vars["min_wait_ms"].set("999")
        dialog.hotkey_vars["play"].set("F12")
        dialog.cancel_button.invoke()
    assert not open_dialog(rig, act)
    assert w.settings.recording.min_wait_ms == 100
    assert w.settings.hotkeys.play == "ctrl+shift+F5"
    assert rig.saved == []
    assert not rig.grabber.suspended
    assert set(rig.grabber.grabbed) == {"ctrl+shift+F9", "ctrl+shift+F5"}


def test_settings_problems_stay_in_the_dialog(rig):
    messages = []

    def act(dialog: SettingsDialog) -> None:
        dialog.number_vars["click_gap_ms"].set("fast")
        dialog.hotkey_vars["play"].set("ctrl+alt+F1")
        dialog.ok_button.invoke()
        messages.append(dialog.message.cget("text"))
        assert str(dialog.entries["click_gap_ms"].cget("style")) == "Invalid.TEntry"
        dialog.hotkey_vars["play"].set("Ctrl+Shift+F9")
        dialog.number_vars["click_gap_ms"].set("250")
        dialog.ok_button.invoke()
        messages.append(dialog.message.cget("text"))
        dialog.cancel_button.invoke()
    assert not open_dialog(rig, act)
    assert "Click gap (ms) must be a whole number" in messages[0]
    assert "reserved by the X server" in messages[0]
    assert messages[1] == "Record and Play need different keys"
    assert rig.saved == []


def test_settings_capture_reads_the_next_key_press(rig):
    show(rig)

    def act(dialog: SettingsDialog) -> None:
        dialog.capture_buttons["play"].invoke()
        dialog.window.event_generate("<KeyPress>", keysym="Shift_L", state=0)
        assert dialog.capturing == "play"
        dialog.window.event_generate("<KeyPress>", keysym="Return", state=0x4)
        assert dialog.capturing is None
        assert dialog.hotkey_vars["play"].get() == "Ctrl+Return"
        dialog.ok_button.invoke()
    assert open_dialog(rig, act)
    assert rig.window.settings.hotkeys.play == "ctrl+Return"


def test_settings_reports_a_refused_grab_and_a_second_ok_keeps_it(rig):
    messages = []

    def act(dialog: SettingsDialog) -> None:
        dialog.hotkey_vars["record"].set("Ctrl+Alt+K")
        dialog.ok_button.invoke()
        messages.append(dialog.message.cget("text"))
        assert rig.grabber.suspended
        dialog.ok_button.invoke()
    assert open_dialog(rig, act)
    assert "Record or stop: Ctrl+Alt+K is in use by another application" in messages[0]
    assert rig.window.settings.hotkeys.record == "ctrl+alt+k"


def test_settings_shows_hotkeys_refused_at_startup(rig):
    rig.window.settings.hotkeys.record = "ctrl+alt+k"
    rig.window._router.start()              # as at startup
    seen = []

    def act(dialog: SettingsDialog) -> None:
        seen.append((dialog.message.cget("text"),
                     str(dialog.entries["record"].cget("style")),
                     str(dialog.entries["play"].cget("style"))))
        dialog.ok_button.invoke()           # the same refusal again: kept without asking
    assert open_dialog(rig, act)
    text, record_style, play_style = seen[0]
    assert "Record or stop: Ctrl+Alt+K is in use by another application" in text
    assert record_style == "Invalid.TEntry" and play_style != "Invalid.TEntry"


def test_return_on_the_focused_cancel_button_cancels(rig):
    show(rig)

    def act(dialog: SettingsDialog) -> None:
        dialog.number_vars["min_wait_ms"].set("999")
        dialog.cancel_button.focus_force()
        dialog.window.update()
        dialog.cancel_button.event_generate("<Return>")
    assert not open_dialog(rig, act)
    assert rig.window.settings.recording.min_wait_ms == 100
    assert rig.saved == []


def test_open_settings_dialog_without_a_router_saves_atomically(rig, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    settings = Settings()
    root = rig.window.root

    def finish() -> None:
        dialog = [c for c in root.winfo_children() if isinstance(c, tk.Toplevel)
                  and c.title() == "Settings"][0]
        dialog.event_generate("<Return>")
    root.after(200, finish)
    root.after(5000, lambda: close_toplevels(root))
    assert open_settings_dialog(root, settings, None)
    loaded = settings_module.load_settings()
    assert loaded == settings
    assert not list(tmp_path.glob("macro-recorder/.settings-*"))


# ------------------------------------------------------------------ final review fixes

def test_drop_in_the_heading_of_a_scrolled_table_goes_before_the_first_visible_row(rig):
    show(rig)
    w = rig.window
    tree = w.table.tree
    w.document.append([wait_row(i) for i in range(1, 81)])
    w.root.update()
    tree.yview_moveto(0.5)
    w.root.update()
    index = {str(r.id): n for n, r in enumerate(w.document.rows)}
    first, last = w.table._visible_rows(index)
    assert index[first] > 0 and index[last] < 79
    assert not tree.bbox(tree.prev(first)) and not tree.bbox(tree.next(last))
    position, _line = w.table._drop_at(2)                       # in the heading
    assert position == index[first]
    position, _line = w.table._drop_at(tree.winfo_height() + 60)  # well below the table
    assert position == index[last] + 1


def test_context_menu_keeps_its_grab(rig, monkeypatch):
    show(rig)
    w = rig.window
    w.document.append([wait_row(1)])
    w.root.update()
    menu = w.table._context_menu
    posted = []
    monkeypatch.setattr(menu, "tk_popup", lambda x, y: posted.append((x, y)))
    monkeypatch.setattr(menu, "grab_release", lambda: pytest.fail("the menu grab was released"))
    tree = w.table.tree
    x, y, width, height = tree.bbox(str(w.document.rows[0].id))
    tree.event_generate("<Button-3>", x=x + 5, y=y + height // 2, rootx=50, rooty=60)
    w.root.update()
    assert posted


def test_choosers_count_as_modal(rig, monkeypatch, tmp_path):
    from macro_recorder.gui import dialogs, fields
    w = rig.window
    seen = []
    monkeypatch.setattr(fields.filedialog, "askopenfilename",
                        lambda **kw: seen.append(dialogs.modal_active()) or "")
    monkeypatch.setattr(fields.colorchooser, "askcolor",
                        lambda **kw: seen.append(dialogs.modal_active()) or (None, None))
    rows = w.document.append([make_row(make("match_image")), make_row(make("exec_program")),
                              make_row(make("wait_pixel"))])
    for row_id, key in zip(rows, ("image_path", "command", "colour")):
        select(rig, row_id)
        widget = field(w.details, key)
        (widget._browse if key != "colour" else widget._choose)()
    assert seen == [True, True, True]
    assert not dialogs.modal_active()


def test_typed_inline_comment_counts_before_the_unsaved_prompt(rig):
    w = rig.window
    (row_id,) = w.document.append([wait_row(1)])
    w.document.mark_saved()
    w.root.update()
    w.table.begin_comment_edit(row_id)
    w.root.update()
    w.table._editor.var.set("typed but not committed")
    rig.unsaved_answer = "cancel"
    assert w.commands.run("file.new")
    assert rig.prompts                               # the typed text made it dirty
    assert w.document.row(row_id).comment == "typed but not committed"


def test_return_presses_the_focused_dialog_button(rig):
    from macro_recorder.gui.dialogs import _ButtonDialog
    show(rig)
    root = rig.window.root
    answers = []
    dialog = _ButtonDialog(root, "Unsaved", "Save?", [("Save", "save"), ("Don't save", "discard"),
                                                       ("Cancel", "cancel")], answers.append)
    dialog.wait_visibility()
    dialog.buttons["discard"].focus_force()
    root.update()
    dialog.event_generate("<Return>")
    root.update()
    assert answers == ["discard"]
    dialog = _ButtonDialog(root, "Unsaved", "Save?", [("Save", "save"), ("Cancel", "cancel")],
                           answers.append)
    dialog.wait_visibility()
    dialog.focus_force()
    root.update()
    dialog.event_generate("<Return>")
    root.update()
    assert answers == ["discard", "save"]           # no button focused: the first


def test_save_as_to_another_folder_updates_the_details_folder(rig, tmp_path):
    w = rig.window
    (row_id,) = w.document.append([make_row(make("match_image"))])
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    assert w._write(str(first / "m.json"))
    select(rig, row_id)
    assert w.details._context.document_dir == str(first)
    assert w._write(str(second / "m.json"))
    assert w.details._context.document_dir == str(second)


def test_saved_log_height_is_restored_and_kept():
    settings = Settings()
    settings.ui.geometry = "1200x720"
    settings.ui.log_height = 300
    shell = make_rig(settings)
    w = shell.window
    try:
        show(shell)
        shell.run_until(lambda: w._log_placed)
        w.root.update()
        assert abs(w.main_pane.winfo_height() - w.main_pane.sashpos(0) - 300) <= 8
    finally:
        w.close()
    assert abs(settings.ui.log_height - 300) <= 8


def test_log_height_is_not_overwritten_when_never_shown():
    settings = Settings()
    settings.ui.log_height = 300
    shell = make_rig(settings)
    shell.window.close()                            # never mapped
    assert settings.ui.log_height == 300


def test_hidden_log_follows_new_lines_once_shown():
    settings = Settings()
    settings.ui.log_visible = False
    shell = make_rig(settings)
    w = shell.window
    try:
        show(shell)
        w.log_panel.append(["line %d" % i for i in range(40)])
        w.root.update()
        w.commands.run("view.log")
        shell.run_until(lambda: w.log_panel._text.winfo_ismapped())
        w.root.update()
        assert w.log_panel._text.yview()[1] == 1.0
        w.log_panel.append(["newest"])
        w.root.update()
        assert w.log_panel._text.yview()[1] == 1.0
        w.log_panel._scroll("moveto", 0)            # the user scrolls up: stop following
        w.log_panel.append(["later"])
        w.root.update()
        assert w.log_panel._text.yview()[0] == 0.0
    finally:
        w.close()


@pytest.mark.parametrize("drag", ["<B1-Motion>", "<B2-Motion>"])
def test_dragging_the_log_away_from_the_end_stops_following(rig, drag):
    show(rig)
    panel = rig.window.log_panel
    panel.append(["line %d" % i for i in range(200)])
    rig.window.root.update()
    panel._text.yview_moveto(0)                    # where the drag scrolled to
    panel._text.event_generate(drag, x=5, y=5)
    rig.window.root.update()
    panel.append(["newest"])
    rig.window.root.update()
    assert panel._text.yview()[1] < 1.0


def test_f8_toggles_the_breakpoint_while_paused(rig):
    show(rig)
    w = rig.window
    (first, _second) = w.document.append([wait_row(1, breakpoint=True), wait_row(1)])
    assert w.commands.run("play.play")
    rig.run_until(lambda: w.mode is Mode.PAUSED)
    w.table.tree.event_generate("<F8>")
    w.root.update()
    assert not w.document.row(first).breakpoint
    assert w.commands.run("play.stop")
    rig.run_until(lambda: w.mode is Mode.IDLE)
