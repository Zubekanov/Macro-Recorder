"""Misc actions: run program, message box, notification, beep."""

from __future__ import annotations

import shlex
import subprocess

from macro_recorder.model.actions._common import (
    boolean, choice, duration, fmt_ms, label_target, template, text, var_name,
)
from macro_recorder.model.actions.variables import evaluate, name_problem
from macro_recorder.model.catalogue import (
    END_TARGET, Abort, ActionSpec, FieldKind, FieldSpec, Flow, Jump, Preset, make, register,
)
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType
from macro_recorder.model.protocols import PlayContext

NOTIFY_TIMEOUT_S = 2.0


class TemplateError(ValueError):
    """A command template with an unmatched brace."""


def render_shell(ctx: PlayContext, template_text: str) -> str:
    """Render a command template, shell-quoting the value of each ``{expr}``.

    ``{{`` and ``}}`` are literal braces.  Raises TemplateError for an
    unmatched brace; expression errors propagate from the evaluator.
    """
    out: list[str] = []
    i, n = 0, len(template_text)
    while i < n:
        c = template_text[i]
        if c in "{}" and template_text.startswith(c * 2, i):
            out.append(c)
            i += 2
        elif c == "{":
            close = template_text.find("}", i + 1)
            if close == -1:
                raise TemplateError("unmatched '{' in command")
            out.append(shlex.quote(str(evaluate(ctx, template_text[i + 1:close]))))
            i = close + 1
        elif c == "}":
            raise TemplateError("unmatched '}' in command")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _play_exec(ctx: PlayContext, row: Row) -> Flow | Abort:
    """Run the command; when waiting, log a non-zero exit and store the code."""
    e = row.event
    wait = bool(e["wait_exit"])
    if wait and e["var_name"]:
        problem = name_problem(e["var_name"])
        if problem is not None:
            return problem
    try:
        command = render_shell(ctx, e["command"])
    except TemplateError as exc:
        return Abort(str(exc))
    if not command.strip():
        return Abort("Run Program has no command")
    timeout_ms = ctx.resolve(e["timeout_ms"]) if wait else 0
    try:
        code = ctx.run_command(command, wait, timeout_ms)
    except OSError as exc:
        return Abort("cannot run %s: %s" % (command, exc))
    if not wait:
        return Flow.NEXT
    if code is None:
        if ctx.stopped():
            return Flow.STOP
        ctx.log("%s did not exit within %s" % (command, fmt_ms(e["timeout_ms"])))
        return Flow.NEXT
    if code != 0:
        ctx.log("%s exited with code %d" % (command, code))
    if e["var_name"]:
        ctx.variables[e["var_name"]] = code
    return Flow.NEXT


EXEC_PROGRAM = register(ActionSpec(
    type=EventType.EXEC_PROGRAM.value, name="Run Program", category="Misc",
    fields=(
        FieldSpec(keys=("command",), label="Command", kind=FieldKind.COMMAND, default="",
                  help="Runs through /bin/sh; {expr} values are shell-quoted"),
        boolean("wait_exit", "Wait for exit", False),
        duration("timeout_ms", "Timeout", 0, visible_when=lambda v: bool(v.get("wait_exit")),
                 help="0 waits forever"),
        var_name("var_name", "Exit code variable", "", visible_when=lambda v: bool(v.get("wait_exit"))),
    ),
    presets=(Preset("Run program", None, lambda: make("exec_program", command="")),),
    describe=lambda row: "run %s%s" % (row.event["command"] or "?", " and wait" if row.event["wait_exit"] else ""),
    play=_play_exec,
))

BUTTON_SETS = {
    "ok": ("ok",),
    "ok_cancel": ("ok", "cancel"),
    "yes_no": ("yes", "no"),
}


def _describe_box(row: Row) -> str:
    e = row.event
    return 'message "%s" [%s]' % (e["text"], "/".join(BUTTON_SETS[e["buttons"]]))


def _play_box(ctx: PlayContext, row: Row) -> Flow | Jump | Abort:
    """Ask the host and go to the answer's target; None from the host means stopped.

    An answer outside the row's button set (the dialog was closed) counts as
    the set's last button: OK, Cancel or No.  The engine's ``host.ask``
    releases held input and re-activates the window context around the
    dialog, so the action does neither.
    """
    e = row.event
    if e["var_name"]:
        problem = name_problem(e["var_name"])
        if problem is not None:
            return problem
    buttons = BUTTON_SETS[e["buttons"]]
    answer = ctx.host.ask(e["title"] or "", ctx.render(e["text"]), e["buttons"])
    if answer is None:
        return Flow.STOP
    if answer not in buttons:
        answer = buttons[-1]
    if e["var_name"]:
        ctx.variables[e["var_name"]] = answer
    target = e["target_" + answer]
    return Jump(target) if target else Flow.NEXT


MESSAGE_BOX = register(ActionSpec(
    type=EventType.MESSAGE_BOX.value, name="Message Box", category="Misc",
    fields=(
        text("title", "Title", ""),
        template("text", "Text", "Continue?"),
        choice("buttons", "Buttons", ("ok", "ok_cancel", "yes_no"), "ok"),
        label_target("target_ok", "OK goes to", None, none_label="(next row)",
                     visible_when=lambda v: v.get("buttons") in ("ok", "ok_cancel")),
        label_target("target_cancel", "Cancel goes to", END_TARGET, none_label="(next row)",
                     visible_when=lambda v: v.get("buttons") == "ok_cancel"),
        label_target("target_yes", "Yes goes to", None, none_label="(next row)",
                     visible_when=lambda v: v.get("buttons") == "yes_no"),
        label_target("target_no", "No goes to", None, none_label="(next row)",
                     visible_when=lambda v: v.get("buttons") == "yes_no"),
        var_name("var_name", "Answer variable", ""),
    ),
    presets=(Preset("Message box", None, lambda: make("message_box", text="Continue?")),),
    describe=_describe_box, play=_play_box,
))


def _play_notify(ctx: PlayContext, row: Row) -> Flow:
    """Show a desktop notification; a failure is logged and playback goes on."""
    message = ctx.render(row.event["text"])
    try:
        result = subprocess.run(["notify-send", "Macro Recorder", message],
                                capture_output=True, text=True, timeout=NOTIFY_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        ctx.log("Notification failed: %s" % exc)
        return Flow.NEXT
    if result.returncode != 0:
        ctx.log("Notification failed: %s" % (result.stderr.strip() or "exit code %d" % result.returncode))
    return Flow.NEXT


NOTIFY = register(ActionSpec(
    type=EventType.NOTIFY.value, name="Notification", category="Misc",
    fields=(template("text", "Text", "Macro Recorder"),),
    presets=(Preset("Notification", None, lambda: make("notify", text="Macro Recorder")),),
    describe=lambda row: 'notify "%s"' % row.event["text"],
    play=_play_notify,
))


def _play_beep(ctx: PlayContext, row: Row) -> Flow:
    ctx.windows.bell()
    return Flow.NEXT


BEEP = register(ActionSpec(
    type=EventType.BEEP.value, name="Beep", category="Misc",
    fields=(),
    presets=(Preset("Beep", None, lambda: make("beep")),),
    describe=lambda row: "beep", play=_play_beep,
))
