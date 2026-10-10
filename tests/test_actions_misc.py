"""exec_program, message_box, notify and beep play."""

import shlex
import subprocess

import pytest

from macro_recorder.model.actions import misc
from macro_recorder.model.catalogue import END_TARGET, Abort, Flow, Jump
from tests._fake_context import ctx, play, row  # noqa: F401


# ---------------------------------------------------------------------------
# exec_program
# ---------------------------------------------------------------------------

def test_exec_quotes_values_with_spaces_and_quotes(ctx):
    ctx.variables["v"] = 'it\'s "a b"'
    ctx.variables["n"] = 3
    play(ctx, row("exec_program", command="printf %s {v} {n} {{literal}}"))
    command, wait, timeout = ctx.commands[0]
    assert shlex.split(command) == ["printf", "%s", 'it\'s "a b"', "3", "{literal}"]
    assert (wait, timeout) == (False, 0)


def test_exec_without_wait_ignores_exit_variable(ctx):
    assert play(ctx, row("exec_program", command="true", var_name="rc")) is Flow.NEXT
    assert "rc" not in ctx.variables


@pytest.mark.parametrize("code", [0, 3])
def test_exec_wait_stores_exit_code(ctx, code):
    ctx.exit_code = code
    r = row("exec_program", command="sh -c 'exit 3'", wait_exit=True, timeout_ms=2000, var_name="rc")
    assert play(ctx, r) is Flow.NEXT
    assert ctx.variables["rc"] == code
    assert ctx.commands == [("sh -c 'exit 3'", True, 2000.0)]
    assert bool(ctx.logs) == (code != 0)


def test_exec_wait_timeout_is_logged_and_stores_minus_one(ctx):
    ctx.exit_code = None
    ctx.variables["rc"] = 0                 # from an earlier pass that succeeded
    r = row("exec_program", command="sleep 9", wait_exit=True, timeout_ms=100, var_name="rc")
    assert play(ctx, r) is Flow.NEXT
    assert ctx.logs == ["sleep 9 did not exit within 100 ms"]
    assert ctx.variables["rc"] == -1


def test_exec_wait_stopped(ctx):
    ctx.exit_code = None
    ctx.is_stopped = True
    assert play(ctx, row("exec_program", command="sleep 9", wait_exit=True)) is Flow.STOP


def test_exec_launch_failure_aborts(ctx):
    ctx.launch_error = FileNotFoundError("No such file or directory: '/bin/sh'")
    result = play(ctx, row("exec_program", command="ls"))
    assert isinstance(result, Abort) and result.message.startswith("cannot run ls")


@pytest.mark.parametrize("command", ["", "   ", "echo {oops", "echo }"])
def test_exec_bad_command_aborts(ctx, command):
    assert isinstance(play(ctx, row("exec_program", command=command)), Abort)
    assert ctx.commands == []


def test_exec_reserved_exit_variable_aborts_before_running(ctx):
    r = row("exec_program", command="true", wait_exit=True, var_name="op_num")
    assert isinstance(play(ctx, r), Abort)
    assert ctx.commands == []


# ---------------------------------------------------------------------------
# message_box
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("buttons, answer, targets, expected", [
    ("ok", "ok", {}, Flow.NEXT),
    ("ok", "ok", {"target_ok": "next"}, Jump("next")),
    ("ok_cancel", "ok", {}, Flow.NEXT),
    ("ok_cancel", "cancel", {}, Jump(END_TARGET)),
    ("ok_cancel", "cancel", {"target_cancel": None}, Flow.NEXT),
    ("ok_cancel", "cancel", {"target_cancel": "undo"}, Jump("undo")),
    ("yes_no", "yes", {}, Flow.NEXT),
    ("yes_no", "yes", {"target_yes": "y"}, Jump("y")),
    ("yes_no", "no", {}, Flow.NEXT),
    ("yes_no", "no", {"target_no": "n"}, Jump("n")),
    ("yes_no", "cancel", {"target_no": "n"}, Jump("n")),      # closed: counts as No
    ("ok", "cancel", {"target_ok": "a"}, Jump("a")),           # closed: counts as OK
])
def test_message_box_mapping(ctx, buttons, answer, targets, expected):
    ctx.host.answers.append(answer)
    r = row("message_box", text="Go?", buttons=buttons, var_name="answer", **targets)
    assert play(ctx, r) == expected
    assert ctx.variables["answer"] in misc.BUTTON_SETS[buttons]
    assert ctx.reactivations == 0      # the engine's host.ask re-activates


def test_message_box_renders_text_and_title(ctx):
    ctx.variables["n"] = 4
    play(ctx, row("message_box", title="Step", text="Done {n} of {n + 1}", buttons="yes_no"))
    play(ctx, row("message_box", title=None, text="plain"))
    assert ctx.host.asks == [("Step", "Done 4 of 5", "yes_no"), ("", "plain", "ok")]


def test_message_box_stop(ctx):
    ctx.host.answers.append(None)
    r = row("message_box", buttons="ok_cancel", var_name="answer")
    assert play(ctx, r) is Flow.STOP
    assert "answer" not in ctx.variables
    assert ctx.reactivations == 0


def test_message_box_reserved_variable_aborts_before_asking(ctx):
    assert isinstance(play(ctx, row("message_box", var_name="iteration")), Abort)
    assert ctx.host.asks == []


# ---------------------------------------------------------------------------
# notify and beep
# ---------------------------------------------------------------------------

def test_notify_runs_notify_send(ctx, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(misc.subprocess, "run", run)
    ctx.variables["n"] = 2
    assert play(ctx, row("notify", text="Pass {n}")) is Flow.NEXT
    args, kwargs = calls[0]
    assert args == ["notify-send", "Macro Recorder", "Pass 2"]
    assert kwargs["timeout"] == 2.0
    assert ctx.logs == []


@pytest.mark.parametrize("outcome, logged", [
    (FileNotFoundError(2, "No such file or directory", "notify-send"), "No such file"),
    (subprocess.TimeoutExpired("notify-send", 2.0), "timed out"),
    (subprocess.CompletedProcess([], 1, "", "no session bus\n"), "no session bus"),
    (subprocess.CompletedProcess([], 4, "", ""), "exit code 4"),
])
def test_notify_failure_is_logged(ctx, monkeypatch, outcome, logged):
    def run(args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(misc.subprocess, "run", run)
    assert play(ctx, row("notify", text="hi")) is Flow.NEXT
    assert len(ctx.logs) == 1
    assert ctx.logs[0].startswith("Notification failed: ") and logged in ctx.logs[0]


def test_beep_rings_bell(ctx):
    assert play(ctx, row("beep")) is Flow.NEXT
    assert ctx.windows.bells == 1
