"""Misc actions: run program, message box, notification, beep."""

from __future__ import annotations

from macro_recorder.model.actions._common import (
    boolean, choice, duration, label_target, template, text, var_name,
)
from macro_recorder.model.catalogue import END_TARGET, ActionSpec, FieldKind, FieldSpec, Preset, make, register
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType

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
))

BUTTON_SETS = {
    "ok": ("ok",),
    "ok_cancel": ("ok", "cancel"),
    "yes_no": ("yes", "no"),
}


def _describe_box(row: Row) -> str:
    e = row.event
    return 'message "%s" [%s]' % (e["text"], "/".join(BUTTON_SETS[e["buttons"]]))


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
    describe=_describe_box,
))

NOTIFY = register(ActionSpec(
    type=EventType.NOTIFY.value, name="Notification", category="Misc",
    fields=(template("text", "Text", "Macro Recorder"),),
    presets=(Preset("Notification", None, lambda: make("notify", text="Macro Recorder")),),
    describe=lambda row: 'notify "%s"' % row.event["text"],
))

BEEP = register(ActionSpec(
    type=EventType.BEEP.value, name="Beep", category="Misc",
    fields=(),
    presets=(Preset("Beep", None, lambda: make("beep")),),
    describe=lambda row: "beep",
))
