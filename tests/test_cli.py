"""The command line, with the platform factories replaced by the conftest fakes."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from macro_recorder import cli
from macro_recorder.model import fileformat
from macro_recorder.model.catalogue import make
from macro_recorder.model.document import MacroDocument, make_row
from macro_recorder.model.protocols import RawEvent
from tests.conftest import FakeInput, FakeListener, FakeScreen, FakeStopKey, FakeWindows, window


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def save(tmp_path, rows, name="macro.json", **kwargs):
    path = tmp_path / name
    fileformat.save(MacroDocument(rows, **kwargs), path)
    return str(path)


@pytest.fixture
def x11_session(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("WINDOWID", raising=False)


class Platform:
    """The fakes the factories hand out, for assertions."""

    def __init__(self) -> None:
        self.input = FakeInput()
        self.windows = FakeWindows([window(0x100, "xed", 100, 80, 900, 600)])
        self.screen = FakeScreen()
        self.watcher = FakeStopKey()
        self.listener: FakeListener = FakeListener()
        self.input_closed = False
        original_close = self.input.close

        def close():
            self.input_closed = True
            original_close()
        self.input.close = close


@pytest.fixture
def platform(monkeypatch, x11_session):
    fakes = Platform()
    monkeypatch.setattr(cli, "make_input", lambda: fakes.input)
    monkeypatch.setattr(cli, "make_window_manager", lambda: fakes.windows)
    monkeypatch.setattr(cli, "make_screen", lambda: fakes.screen)
    monkeypatch.setattr(cli, "make_stop_watcher", lambda input: fakes.watcher)
    monkeypatch.setattr(cli, "make_listener", lambda: fakes.listener)
    return fakes


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def test_parser_play_options():
    args = cli.build_parser().parse_args(
        ["play", "m.json", "--speed", "2.5", "--repeat", "0", "--window-timeout", "800",
         "--on-missing", "halt", "--no-filter", "--verbose"])
    assert args.run is cli.cmd_play
    assert (args.file, args.speed, args.repeat, args.window_timeout) == ("m.json", 2.5, 0, 800)
    assert args.on_missing == "halt" and args.no_filter and args.verbose
    settings = cli.playback_settings(args)
    assert settings.playback.speed == 2.5
    assert settings.playback.repeat == 0
    assert settings.playback.window_timeout_ms == 800
    assert settings.playback.on_missing == "halt"


def test_parser_play_defaults_keep_default_settings():
    args = cli.build_parser().parse_args(["play", "m.json"])
    assert not args.no_filter and not args.verbose
    assert cli.playback_settings(args) == cli.Settings()


@pytest.mark.parametrize("argv", [
    ["play", "m.json", "--speed", "0"],
    ["play", "m.json", "--speed", "fast"],
    ["play", "m.json", "--repeat", "-1"],
    ["play", "m.json", "--on-missing", "goto"],
    ["record", "--stop-key", "ctrl+F6"],
    ["record", "--stop-key", "ctrl"],
    ["record", "--stop-key", "Shift_R"],
    ["frobnicate"],
    [],
])
def test_parser_rejects(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def test_parser_record_stop_key_is_normalised():
    args = cli.build_parser().parse_args(["record", "out.json", "--stop-key", "escape"])
    assert args.output == "out.json"
    assert args.stop_key == "Escape"
    assert cli.build_parser().parse_args(["record"]).stop_key == "F6"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

LIST_ROWS = [
    make_row(make("wait", duration_ms=200), label="top"),
    make_row(make("window_focus", window="xed")),
    make_row(make("mouse_click", x=10, y=20), delay_ms=60, comment="press OK"),
    make_row(make("key_combo", keys="ctrl+s"), enabled=False, breakpoint=True),
    make_row(make("window_focus", window="Firefox"), enabled=False),
    make_row(make("wait", duration_ms=100), breakpoint=True),
]


def test_list_shows_rows_and_groups(tmp_path, capsys):
    path = save(tmp_path, LIST_ROWS)
    assert cli.main(["list", path]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "1               top  Wait: 200 ms",
        "",
        "2                    Window Focus: xed",
        "3        60 ms         Mouse Click: left click at (10, 20)  # press OK",
        "4 ||*                  Key: Ctrl+S",
        "",
        "5 ||                 Window Focus: Firefox",
        "6   *                Wait: 100 ms",
    ]


def test_list_brackets_filtered_rows(tmp_path, capsys):
    path = save(tmp_path, LIST_ROWS, filter=("mouse_click",))
    assert cli.main(["list", path]) == 0
    assert capsys.readouterr().out.splitlines() == [
        " 1                top  Wait: 200 ms",
        "",
        " 2                     Window Focus: xed",
        "[3]        60 ms         Mouse Click: left click at (10, 20)  # press OK",
        " 4  ||*                  Key: Ctrl+S",
        "",
        " 5  ||                 Window Focus: Firefox",
        " 6    *                Wait: 100 ms",
    ]


def test_list_without_labels_has_no_label_column(tmp_path, capsys):
    path = save(tmp_path, [make_row(make("wait", duration_ms=200))])
    assert cli.main(["list", path]) == 0
    assert capsys.readouterr().out == "1               Wait: 200 ms\n"


def test_list_keeps_typed_newlines_on_one_line(capsys):
    example = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__))),
                           "examples", "xed-demo.json")
    assert cli.main(["list", example]) == 0
    lines = capsys.readouterr().out.splitlines()
    (typed,) = [line for line in lines if "Type Text" in line]
    assert 'Macro Recorder\\n"' in typed
    assert all(line == "" or line.lstrip()[0].isdigit() or line.lstrip()[0] == "["
               for line in lines)


def test_example_targets_only_xed_windows(capsys):
    from macro_recorder.platform.windows import title_matches
    example = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__))),
                           "examples", "xed-demo.json")
    assert cli.main(["validate", example]) == 0
    row = fileformat.load(example).rows[0]
    pattern, mode = row.event["window"], row.event["match_mode"]
    assert title_matches("Unsaved Document 1", pattern, mode)      # xed on Mint 22.3
    assert title_matches("Unsaved Document 1 - xed", pattern, mode)
    assert title_matches("notes.txt (~/Documents) - xed", pattern, mode)
    for other in ("Fixed bugs - Mozilla Firefox", "xed-demo.json - GitHub", "Mixed results"):
        assert not title_matches(other, pattern, mode), other


def test_list_escapes_a_comment_with_a_newline(tmp_path, capsys):
    path = save(tmp_path, [make_row(make("wait", duration_ms=200), comment="two\nlines")])
    assert cli.main(["list", path]) == 0
    assert capsys.readouterr().out == "1               Wait: 200 ms  # two\\nlines\n"


def test_list_into_a_closed_pipe_exits_quietly(tmp_path):
    path = save(tmp_path, [make_row(make("wait", duration_ms=i + 1)) for i in range(5000)])
    code = "import sys; from macro_recorder import cli; sys.exit(cli.main(['list', sys.argv[1]]))"
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
    proc = subprocess.Popen([sys.executable, "-c", code, path], cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdout.readline()
    proc.stdout.close()                             # as `| head -1` does
    err = proc.stderr.read().decode()
    assert proc.wait(10) == 141
    assert "Traceback" not in err and "BrokenPipe" not in err


def test_list_bad_file(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text('{"version": 2, "rows": []}')
    assert cli.main(["list", str(path)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ") and "version must be 3" in err


def test_list_missing_file(tmp_path, capsys):
    assert cli.main(["list", str(tmp_path / "none.json")]) == 1
    assert capsys.readouterr().err.startswith("Error: cannot read ")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def test_validate_clean(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    path = save(tmp_path, [make_row(make("wait", duration_ms=200))])
    assert cli.main(["validate", path]) == 0
    assert capsys.readouterr().out == "%s: no problems\n" % path


def test_validate_problems_and_warnings(tmp_path, capsys):
    path = save(tmp_path, [
        make_row(make("goto", target="nowhere")),
        make_row(make("var_set", var_name="x", expr="y + 1")),
    ])
    assert cli.main(["validate", path]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("Row 1: ") and "nowhere" in lines[0]
    assert lines[1].startswith("warning: Row 2: ") and "'y'" in lines[1]


def test_validate_only_warnings_exits_0(tmp_path, capsys):
    path = save(tmp_path, [make_row(make("var_set", var_name="x", expr="y + 1"))])
    assert cli.main(["validate", path]) == 0
    assert capsys.readouterr().out.startswith("warning: Row 1: ")


def test_validate_structural_problems(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text('{"version": 3, "rows": [{"events": [{"type": "nope"}]}]}')
    assert cli.main(["validate", str(path)]) == 1
    assert capsys.readouterr().out == 'Row 1: unknown event type "nope"\n'


def test_list_and_validate_do_not_import_the_platform(tmp_path):
    path = save(tmp_path, [make_row(make("window_focus", window="xed")),
                           make_row(make("mouse_click", x=10, y=20))])
    code = "\n".join([
        "import sys",
        "from macro_recorder import cli",
        "assert cli.main(['list', sys.argv[1]]) == 0",
        "assert cli.main(['validate', sys.argv[1]]) == 0",
        "loaded = {'Xlib', 'macro_recorder.platform.input', 'macro_recorder.platform.windows',",
        "          'macro_recorder.platform.x11', 'tkinter'} & set(sys.modules)",
        "assert not loaded, loaded",
    ])
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    subprocess.run([sys.executable, "-c", code, path], check=True, env=env,
                   cwd=os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__))),
                   stdout=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Session refusal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env", [
    {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"},
    {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
    {},
])
@pytest.mark.parametrize("argv", [["record", "out.json"], ["play", "m.json"]])
def test_session_refusal(env, argv, monkeypatch, capsys, platform):
    for name in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert cli.main(argv) == 3                      # 2 is argparse's usage error
    assert capsys.readouterr().err.startswith("Error: ")
    assert platform.input.actions == []


# ---------------------------------------------------------------------------
# play
# ---------------------------------------------------------------------------

def test_play_runs_the_macro(tmp_path, capsys, platform):
    path = save(tmp_path, [make_row(make("mouse_click", x=10, y=20))])
    assert cli.main(["play", path]) == 0
    assert ("press_button", "left") in platform.input.actions
    assert ("release_button", "left") in platform.input.actions
    assert platform.input_closed
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_play_verbose_prints_log_lines(tmp_path, capsys, platform):
    path = save(tmp_path, [make_row(make("wait", duration_ms=1))])
    assert cli.main(["play", path, "--verbose"]) == 0
    assert "[1] 1 ms" in capsys.readouterr().out


def test_play_refuses_invalid_document(tmp_path, capsys, platform):
    path = save(tmp_path, [make_row(make("goto", target="nowhere"))])
    assert cli.main(["play", path]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("Row 1: ")
    assert err[-1].startswith("Error: ") and "1 problem(s)" in err[-1]
    assert platform.input.actions == []


def test_play_ignores_errors_on_rows_that_do_not_play(tmp_path, capsys, platform):
    path = save(tmp_path, [
        make_row(make("goto", target="nowhere"), enabled=False),
        make_row(make("wait", duration_ms=-5)),
        make_row(make("mouse_click", x=10, y=20)),
    ], filter=("wait",))
    assert cli.main(["play", path]) == 0
    assert ("press_button", "left") in platform.input.actions
    assert capsys.readouterr().err == ""
    assert cli.main(["play", path, "--verbose"]) == 0
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("warning: Row 1: ") and err[0].endswith("(row does not play)")
    assert err[1].startswith("warning: Row 2: ")


def test_play_refuses_errors_on_rows_unfiltered_by_no_filter(tmp_path, capsys, platform):
    path = save(tmp_path, [make_row(make("wait", duration_ms=-5))], filter=("wait",))
    assert cli.main(["play", path, "--no-filter"]) == 1
    assert capsys.readouterr().err.startswith("Row 1: ")


def test_play_refuses_label_errors_on_disabled_rows(tmp_path, capsys, platform):
    path = save(tmp_path, [
        make_row(make("wait", duration_ms=1), label="again"),
        make_row(make("wait", duration_ms=1), label="again", enabled=False),
    ])
    assert cli.main(["play", path]) == 1
    assert capsys.readouterr().err.startswith("Row 2: ")


def test_play_reports_runtime_error(tmp_path, capsys, platform):
    path = save(tmp_path, [
        make_row(make("wait", duration_ms=1)),
        make_row(make("var_set", var_name="x", expr="1 / 0")),
    ])
    assert cli.main(["play", path]) == 1
    assert capsys.readouterr().err.startswith("Error: Row 2: ")


def test_play_filter_and_no_filter(tmp_path, platform):
    path = save(tmp_path, [make_row(make("mouse_click", x=10, y=20))], filter=("mouse_click",))
    assert cli.main(["play", path]) == 0
    assert ("press_button", "left") not in platform.input.actions
    assert cli.main(["play", path, "--no-filter"]) == 0
    assert ("press_button", "left") in platform.input.actions


def test_play_ignores_breakpoints(tmp_path, platform):
    path = save(tmp_path, [make_row(make("mouse_click", x=10, y=20), breakpoint=True)])
    assert cli.main(["play", path]) == 0
    assert ("press_button", "left") in platform.input.actions


def test_play_repeat(tmp_path, platform):
    path = save(tmp_path, [make_row(make("mouse_click", x=10, y=20))])
    assert cli.main(["play", path, "--repeat", "3"]) == 0
    assert platform.input.actions.count(("press_button", "left")) == 3


def test_play_stop_key_is_watched(tmp_path, capsys, platform):
    path = save(tmp_path, [make_row(make("wait", duration_ms=60000))])
    timer = threading.Timer(0.2, lambda: platform.watcher.press())
    timer.start()
    started = time.monotonic()
    assert cli.main(["play", path]) == 130          # cut short, as with Ctrl+C
    assert time.monotonic() - started < 5
    assert "Stopped" in capsys.readouterr().err


def test_play_esc_under_repeat_forever_exits_0(tmp_path, capsys, platform):
    path = save(tmp_path, [make_row(make("wait", duration_ms=50))])
    timer = threading.Timer(0.3, lambda: platform.watcher.press())
    timer.start()
    assert cli.main(["play", path, "--repeat", "0"]) == 0
    assert "Stopped" in capsys.readouterr().err


def test_play_ctrl_c_stops_the_player(tmp_path, capsys, monkeypatch, platform):
    path = save(tmp_path, [make_row(make("key_press", key="a")),
                           make_row(make("wait", duration_ms=60000))])

    def interrupted(thread):
        time.sleep(0.2)
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "_wait_for", interrupted)
    assert cli.main(["play", path]) == 130
    assert platform.input.held_keys == set()
    assert platform.input_closed
    assert "Stopped" in capsys.readouterr().err


def test_play_second_ctrl_c_waits_for_the_player(tmp_path, capsys, monkeypatch, platform):
    path = save(tmp_path, [make_row(make("key_press", key="a")),
                           make_row(make("wait", duration_ms=60000))])
    interrupts = [0]
    closed_while_running = []
    real_slice = cli._join_slice

    def join_slice(thread):
        if interrupts[0] < 2:
            time.sleep(0.2)
            interrupts[0] += 1
            raise KeyboardInterrupt
        real_slice(thread)
    monkeypatch.setattr(cli, "_join_slice", join_slice)
    original_close = platform.input.close

    def close():
        closed_while_running.append(any(t.name == "player" and t.is_alive()
                                        for t in threading.enumerate()))
        original_close()
    platform.input.close = close
    assert cli.main(["play", path]) == 130
    assert interrupts[0] == 2
    assert closed_while_running == [False]
    assert platform.input.held_keys == set()
    assert ("release_key", "a") in platform.input.actions


def test_play_platform_failure(tmp_path, capsys, monkeypatch, platform):
    def broken():
        raise OSError("The X server has no XTEST extension")
    monkeypatch.setattr(cli, "make_input", broken)
    path = save(tmp_path, [make_row(make("wait", duration_ms=1))])
    assert cli.main(["play", path]) == 1
    assert capsys.readouterr().err == "Error: The X server has no XTEST extension\n"


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------

class FakePopen:
    """Stands in for zenity: answers with ``code`` after ``delay`` seconds."""

    code = 0
    delay = 0.0
    commands: list[list[str]] = []

    def __init__(self, command, **kwargs):
        FakePopen.commands.append(command)
        self._done = time.monotonic() + FakePopen.delay
        self.terminated = False

    def wait(self, timeout=None):
        if self.terminated:
            return -15
        if timeout is not None and time.monotonic() + timeout < self._done:
            time.sleep(timeout)
            raise subprocess.TimeoutExpired("zenity", timeout)
        return FakePopen.code

    def terminate(self):
        self.terminated = True


@pytest.fixture
def zenity(monkeypatch):
    FakePopen.code, FakePopen.delay, FakePopen.commands = 0, 0.0, []
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/zenity")
    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    return FakePopen


@pytest.mark.parametrize("buttons, code, answer", [
    ("ok", 0, "ok"), ("ok", 1, "ok"),
    ("ok_cancel", 0, "ok"), ("ok_cancel", 1, "cancel"),
    ("yes_no", 0, "yes"), ("yes_no", 1, "no"),
])
def test_ask_zenity(zenity, buttons, code, answer):
    zenity.code = code
    assert cli.CliHost().ask("Title", "<b>Text</b>", buttons) == answer
    command = zenity.commands[0]
    assert command[0] == "/usr/bin/zenity"
    assert "--no-markup" in command
    assert command[command.index("--text") + 1] == "<b>Text</b>"
    assert command[command.index("--title") + 1] == "Title"
    if buttons == "ok":
        assert "--info" in command
    else:
        assert "--question" in command
        labels = {"ok_cancel": ("OK", "Cancel"), "yes_no": ("Yes", "No")}[buttons]
        assert command[command.index("--ok-label") + 1] == labels[0]
        assert command[command.index("--cancel-label") + 1] == labels[1]


def test_ask_zenity_closed_with_the_stop_key_is_not_an_answer(zenity):
    """Esc closes zenity a moment before the player's stop flag is set."""
    zenity.code = 1
    stop = threading.Event()
    host = cli.CliHost()
    host.attach(stop.is_set)
    threading.Timer(0.03, stop.set).start()
    assert host.ask("", "Continue?", "yes_no") is None


def test_ask_zenity_closed_by_stop(zenity):
    zenity.delay = 30.0
    stop = threading.Event()
    host = cli.CliHost()
    host.attach(stop.is_set)
    threading.Timer(0.2, stop.set).start()
    started = time.monotonic()
    assert host.ask("", "Continue?", "yes_no") is None
    assert time.monotonic() - started < 5


def test_ask_fallback_answers_first_button(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.CliHost().ask("Check", "Is it done?", "yes_no") == "yes"
    out = capsys.readouterr().out
    assert out == "Check\nIs it done?\nAnswered yes (no zenity)\n"


def test_ask_fallback_when_zenity_cannot_start(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/zenity")

    def broken(*args, **kwargs):
        raise OSError("exec format error")
    monkeypatch.setattr(cli.subprocess, "Popen", broken)
    assert cli.CliHost().ask("", "Go?", "ok_cancel") == "ok"
    assert capsys.readouterr().out.endswith("Answered ok (no zenity)\n")


def test_play_message_box_fallback(tmp_path, capsys, monkeypatch, platform):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    path = save(tmp_path, [
        make_row(make("message_box", text="Ready?", buttons="yes_no", var_name="answer")),
        make_row(make("if_else", expr="answer == 'yes'", target="End")),
        make_row(make("mouse_click", x=1, y=1)),
    ])
    assert cli.main(["play", path]) == 0
    assert "Answered yes" in capsys.readouterr().out
    assert ("press_button", "left") not in platform.input.actions


def test_host_prints_final_error(capsys):
    host = cli.CliHost()
    host.on_row(1)
    host.on_log("hidden")
    host.on_finished("Row 3: boom")
    assert host.error == "Row 3: boom"
    out = capsys.readouterr()
    assert out.out == "" and out.err == "Error: Row 3: boom\n"


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------

class ScriptedListener(FakeListener):
    """Feeds its events as soon as it starts; optionally dies afterwards."""

    def __init__(self, events, die=False):
        super().__init__()
        self.events = events
        self.die = die

    def start(self, callback):
        super().start(callback)
        t0 = time.monotonic()
        self.feed(*[e.__class__(**{**e.__dict__, "time": t0 + e.time}) for e in self.events])
        if self.die:
            self.alive = False


CLICK = [
    RawEvent("button_press", 0.1, x=10, y=20, button="left"),
    RawEvent("button_release", 0.15, x=10, y=20, button="left"),
]


def test_record_saves_until_stop_key(tmp_path, capsys, platform):
    platform.listener = ScriptedListener(CLICK + [RawEvent("key_press", 0.5, key="F6")])
    out = tmp_path / "rec.json"
    assert cli.main(["record", str(out)]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == "Recording... press F6 to stop"
    document = fileformat.load(out)
    types = [r.type for r in document.rows]
    assert "mouse_click" in types and "key_combo" not in types
    assert printed[-1] == "Saved %d rows to %s" % (len(document.rows), out)


def test_record_custom_stop_key(tmp_path, capsys, platform):
    platform.listener = ScriptedListener(CLICK + [RawEvent("key_press", 0.5, key="F6"),
                                                  RawEvent("key_release", 0.6, key="F6"),
                                                  RawEvent("key_press", 0.9, key="Pause")])
    out = tmp_path / "rec.json"
    assert cli.main(["record", str(out), "--stop-key", "Pause"]) == 0
    assert "press Pause to stop" in capsys.readouterr().out
    keys = [r.event["keys"] for r in fileformat.load(out).rows if r.type == "key_combo"]
    assert keys == ["F6"]


def test_record_listener_death_saves_and_exits_1(tmp_path, capsys, platform):
    platform.listener = ScriptedListener(CLICK, die=True)
    out = tmp_path / "rec.json"
    assert cli.main(["record", str(out)]) == 1
    captured = capsys.readouterr()
    assert "Saved" in captured.out
    assert captured.err.startswith("Error: The input listener stopped unexpectedly")
    assert any(r.type == "mouse_click" for r in fileformat.load(out).rows)


def test_record_ctrl_c_saves_and_exits_1(tmp_path, capsys, monkeypatch, platform):
    platform.listener = ScriptedListener(CLICK)

    def interrupt(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "_sleep", interrupt)
    out = tmp_path / "rec.json"
    assert cli.main(["record", str(out)]) == 1
    assert capsys.readouterr().err.startswith("Error: recording interrupted")
    assert any(r.type == "mouse_click" for r in fileformat.load(out).rows)
    assert not platform.listener.alive


def test_record_default_output(tmp_path, capsys, monkeypatch, platform):
    monkeypatch.chdir(tmp_path)
    platform.listener = ScriptedListener([RawEvent("key_press", 0.1, key="F6")])
    assert cli.main(["record"]) == 0
    files = list(tmp_path.glob("recording-*.json"))
    assert len(files) == 1
    assert fileformat.load(files[0]).rows[0].type == "window_focus"


def test_record_refuses_missing_output_folder(tmp_path, capsys, platform):
    out = tmp_path / "missing" / "rec.json"
    assert cli.main(["record", str(out)]) == 1
    assert capsys.readouterr().err.startswith("Error: cannot write %s: folder " % out)
    assert platform.listener._callback is None
    assert not out.exists()


def test_record_refuses_folder_as_output(tmp_path, capsys, platform):
    assert cli.main(["record", str(tmp_path)]) == 1
    assert "it is a folder" in capsys.readouterr().err


def test_record_refuses_unknown_stop_key(tmp_path, capsys, platform):
    assert cli.main(["record", str(tmp_path / "rec.json"), "--stop-key", "Fsix"]) == 1
    assert capsys.readouterr().err == "Error: Fsix is not a key name\n"
    assert platform.listener._callback is None


def test_record_failed_save_falls_back(tmp_path, capsys, monkeypatch, platform):
    monkeypatch.chdir(tmp_path)
    platform.listener = ScriptedListener(CLICK + [RawEvent("key_press", 0.5, key="F6")])
    out = tmp_path / "rec.json"
    real_save = fileformat.save

    def save_fails_for_out(document, path):
        if str(path) == str(out):
            raise OSError("disk full")
        real_save(document, path)
    monkeypatch.setattr(cli.fileformat, "save", save_fails_for_out)
    assert cli.main(["record", str(out)]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: cannot save %s: disk full" % out)
    files = list(tmp_path.glob("recording-*.json"))
    assert len(files) == 1
    assert captured.out.splitlines()[-1].endswith(files[0].name)
    assert any(r.type == "mouse_click" for r in fileformat.load(files[0]).rows)


def test_record_treats_the_terminal_as_its_own_window(tmp_path, monkeypatch, platform):
    monkeypatch.setenv("WINDOWID", str(0x100))
    platform.listener = ScriptedListener(CLICK + [RawEvent("key_press", 0.5, key="F6")])
    out = tmp_path / "rec.json"
    assert cli.main(["record", str(out)]) == 0
    assert all(r.type != "window_focus" for r in fileformat.load(out).rows)


def test_record_start_failure(tmp_path, capsys, monkeypatch, platform):
    class Broken(FakeListener):
        def start(self, callback):
            raise OSError("RECORD extension missing")
    platform.listener = Broken()
    assert cli.main(["record", str(tmp_path / "rec.json")]) == 1
    assert "RECORD extension missing" in capsys.readouterr().err
    assert not (tmp_path / "rec.json").exists()


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------

@pytest.mark.live_x
def test_live_play_with_real_platform(tmp_path):
    """Plays a small macro through XTest under Xvfb."""
    path = save(tmp_path, [make_row(make("mouse_move", x=50, y=60)),
                           make_row(make("wait", duration_ms=10))])
    assert cli.main(["play", path]) == 0
