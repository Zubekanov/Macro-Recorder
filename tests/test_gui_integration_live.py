"""The real Details panel, overlay and Settings dialog inside the shell.

Runs only under Xvfb (``MACRO_RECORDER_LIVE_X=1``).  The platform objects are
the conftest fakes; the overlay is real and shapes itself on the Xvfb screen.
"""

from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from macro_recorder.gui import settings as settings_module
from macro_recorder.gui.details import DetailsPanel
from macro_recorder.gui.main_window import MainWindow
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


@pytest.fixture
def rig():
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
    window = MainWindow(root, Settings(), own_windows=own, platform=Platform(factories),
                        save_settings=lambda s: saved.append(settings_module.copy_settings(s)))
    window.start_hotkeys()
    shell = Rig(window, own, grabber, saved)
    root.update()
    yield shell
    if not window._closing:
        window.close()


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
