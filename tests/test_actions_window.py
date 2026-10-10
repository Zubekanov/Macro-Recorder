"""window_focus play: the engine's enter_window owns the focus logic."""

import pytest

from macro_recorder.model.catalogue import Abort, Flow, Jump
from tests._fake_context import ctx, play, row  # noqa: F401


@pytest.mark.parametrize("result", [Flow.NEXT, Jump("retry"), Abort("window xed not found")])
def test_window_focus_returns_enter_window_result(ctx, result):
    r = row("window_focus", window="xed")
    ctx.enter_result = result
    assert play(ctx, r) == result
    assert ctx.entered == [r]


def test_window_focus_touches_nothing_else(ctx):
    play(ctx, row("window_focus", window="xed", launch="xed"))
    assert ctx.windows.calls == []
    assert ctx.input.actions == []
    assert ctx.commands == []
