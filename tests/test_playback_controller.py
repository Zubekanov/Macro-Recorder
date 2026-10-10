"""PlaybackController and QueueHost with the real player and the conftest fakes.

Host events travel through a real queue and are applied with the shell's
pump rules (``main_window.drain``), exactly as the GUI does, minus Tk.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import replace

import pytest

from macro_recorder.gui.main_window import drain
from macro_recorder.gui.playback import (
    AskRequest, Platform, PlatformFactories, PlaybackCallbacks, PlaybackController, QueueHost,
    ask_buttons,
)
from macro_recorder.gui.settings import Settings
from macro_recorder.model.catalogue import make
from macro_recorder.model.document import MacroDocument, make_row
from macro_recorder.model.protocols import Mode
from tests.conftest import FakeHotkeys, FakeInput, FakeListener, FakeScreen, FakeStopKey, FakeWindows

TIMEOUT_S = 10.0


class Shell:
    """Records what the controller asks of the shell and answers asks on demand."""

    def __init__(self) -> None:
        self.modes: list[Mode] = []
        self.highlights: list[int | None] = []
        self.variables: list[dict] = []
        self.logs: list[str] = []
        self.errors: list[str] = []
        self.asks: list[AskRequest] = []
        self.closed_asks = 0
        self.answer: str | None = None      # answer every ask at once when set

    def callbacks(self) -> PlaybackCallbacks:
        return PlaybackCallbacks(
            set_mode=self.modes.append,
            highlight=self.highlights.append,
            show_variables=lambda v: self.variables.append(dict(v)),
            log=self.logs.append,
            report_error=self.errors.append,
            show_ask=self._ask,
            close_asks=self._close,
        )

    def _ask(self, request: AskRequest) -> None:
        self.asks.append(request)
        if self.answer is not None:
            request.reply(self.answer)

    def _close(self) -> None:
        self.closed_asks += 1


class Rig:
    def __init__(self, rows, settings: Settings | None = None, player_factory=None) -> None:
        self.document = MacroDocument(rows)
        self.settings = settings or Settings()
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        self.inputs: list[FakeInput] = []
        self.windows = FakeWindows()
        kwargs = {} if player_factory is None else {"player": player_factory}
        self.factories = PlatformFactories(
            windows=lambda: self.windows, input=self._new_input, screen=FakeScreen,
            listener=FakeListener, stop_watcher=FakeStopKey, grabber=FakeHotkeys, **kwargs)
        self.platform = Platform(self.factories)
        self.shell = Shell()
        self.controller = PlaybackController(self.document, self.settings, self.platform,
                                             self.post, self.shell.callbacks())
        self.player_logs: list[str] = []

    def _new_input(self) -> FakeInput:
        backend = FakeInput()
        self.inputs.append(backend)
        return backend

    def post(self, kind, payload=None) -> None:
        self.queue.put((kind, payload))

    def pump_once(self) -> None:
        batch = drain(self.queue)
        self.player_logs.extend(batch.logs)
        if batch.row is not None:
            self.controller.handle_row(batch.row)
        for kind, payload in batch.player:
            getattr(self.controller, "handle_" + kind)(payload)

    def pump_until(self, condition) -> None:
        deadline = time.monotonic() + TIMEOUT_S
        while not condition():
            assert time.monotonic() < deadline, "timed out"
            self.pump_once()
            time.sleep(0.005)

    def run_to_end(self) -> None:
        self.pump_until(lambda: not self.controller.running)


def wait_row(ms=1, **fields):
    return make_row(make("wait", duration_ms=ms), **fields)


def test_plays_to_the_end_through_the_queue():
    rows = [wait_row(), make_row(make("var_set", var_name="x", expr="41 + 1")), wait_row()]
    rig = Rig(rows)
    assert rig.controller.start()
    assert rig.shell.modes == [Mode.PLAYING]
    rig.run_to_end()
    assert rig.shell.modes == [Mode.PLAYING, Mode.IDLE]
    assert rig.shell.highlights[-1] is None
    assert set(rig.shell.highlights[:-1]) <= {r.id for r in rows}
    assert rig.shell.errors == []
    assert rig.shell.logs[-1] == "Playback finished"
    assert rig.shell.variables[0] == {}             # cleared at start
    assert rig.shell.variables[-1]["x"] == 42       # snapshot at the end
    assert rig.player_logs                          # the player's own log lines
    assert rig.shell.closed_asks == 1


def test_input_backend_is_built_per_run_and_closed():
    rig = Rig([wait_row()])
    rig.controller.start()
    rig.run_to_end()
    rig.controller.start()
    rig.run_to_end()
    assert len(rig.inputs) == 2


def test_breakpoint_pauses_then_continue_finishes():
    rows = [wait_row(), make_row(make("var_set", var_name="y", expr="7"), breakpoint=True),
            wait_row()]
    rig = Rig(rows)
    rig.controller.start()
    rig.pump_until(lambda: rig.controller.paused)
    assert rig.shell.modes == [Mode.PLAYING, Mode.PAUSED]
    assert rig.shell.highlights[-1] == rows[1].id
    assert rig.controller.play_label == "Continue"
    assert "Paused at row 2" in rig.shell.logs
    rig.controller.continue_()
    assert rig.shell.modes[-1] == Mode.PLAYING
    assert rig.controller.play_label == "Play"
    rig.run_to_end()
    assert rig.shell.modes[-1] == Mode.IDLE
    assert rig.shell.variables[-1]["y"] == 7
    assert rig.shell.errors == []


def test_stop_while_paused():
    rows = [wait_row(breakpoint=True), wait_row()]
    rig = Rig(rows)
    rig.controller.start()
    rig.pump_until(lambda: rig.controller.paused)
    rig.controller.stop()
    rig.run_to_end()
    assert rig.shell.modes == [Mode.PLAYING, Mode.PAUSED, Mode.IDLE]
    assert rig.shell.logs[-1] == "Playback stopped"
    assert rig.shell.errors == []


def test_stop_while_playing():
    rig = Rig([wait_row(5000)])
    rig.controller.start()
    time.sleep(0.05)
    rig.controller.stop()
    rig.run_to_end()
    assert rig.shell.modes[-1] == Mode.IDLE
    assert rig.shell.logs[-1] == "Playback stopped"


def test_message_box_answer_comes_back_through_the_queue():
    rows = [make_row(make("message_box", text="Go on?", buttons="yes_no", var_name="answer")),
            wait_row()]
    rig = Rig(rows)
    rig.shell.answer = "yes"
    rig.controller.start()
    rig.run_to_end()
    assert len(rig.shell.asks) == 1
    request = rig.shell.asks[0]
    assert (request.text, request.buttons) == ("Go on?", "yes_no")
    assert rig.shell.errors == []


def test_stop_during_message_box_cancels_the_ask():
    rows = [make_row(make("message_box", text="Wait")), wait_row()]
    rig = Rig(rows)
    rig.controller.start()
    rig.pump_until(lambda: rig.shell.asks)
    request = rig.shell.asks[0]
    assert not request.done
    rig.controller.stop()
    assert request.done and request.answer is None
    rig.run_to_end()
    assert rig.shell.modes[-1] == Mode.IDLE
    assert rig.shell.closed_asks == 1


def test_error_names_the_row():
    rows = [wait_row(), make_row(make("goto", target="nowhere"))]
    rig = Rig(rows)
    rig.controller.start()
    rig.run_to_end()
    assert len(rig.shell.errors) == 1
    assert rig.shell.errors[0].startswith("Row 2:")
    assert rig.shell.modes[-1] == Mode.IDLE


def test_empty_document_does_not_start():
    rig = Rig([])
    assert not rig.controller.start()
    assert rig.shell.errors == ["There is nothing to play."]
    assert rig.shell.modes == []


def test_invalid_speed_is_reported_and_nothing_runs():
    settings = Settings()
    settings.playback = replace(settings.playback, speed=0)
    rig = Rig([wait_row()], settings)
    assert not rig.controller.start()
    assert rig.shell.errors[0].startswith("Cannot play:")
    assert not rig.controller.running and rig.shell.modes == []


def test_input_backend_failure_is_reported():
    rig = Rig([wait_row()])

    def broken():
        raise OSError("The X server has no XTEST extension")
    rig.factories = replace(rig.factories, input=broken)
    rig.platform.factories = rig.factories
    assert not rig.controller.start()
    assert rig.shell.errors == ["Cannot play: The X server has no XTEST extension"]


def test_finished_always_arrives_when_the_player_raises():
    class NoVariables:
        @staticmethod
        def snapshot() -> dict:
            return {}

    class Exploding:
        def __init__(self, *args, **kwargs) -> None:
            self.variables = NoVariables()

        def play(self) -> None:
            raise RuntimeError("boom")

        def stop(self) -> None:
            pass

    rig = Rig([wait_row()], player_factory=Exploding)
    rig.controller.start()
    rig.run_to_end()
    assert rig.shell.errors == ["boom"]
    assert rig.shell.modes == [Mode.PLAYING, Mode.IDLE]


def test_second_start_while_running_is_refused():
    rig = Rig([wait_row(5000)])
    assert rig.controller.start()
    assert not rig.controller.start()
    rig.controller.stop()
    rig.run_to_end()


def test_queue_host_posts_and_finishes_once():
    posted: list = []
    host = QueueHost(lambda kind, payload: posted.append((kind, payload)))
    host.variables_source = lambda: {"a": 1}
    host.on_row(3)
    host.on_log("line")
    host.on_paused(3, {"v": 2})
    host.on_finished(None)
    host.on_finished("late")
    assert posted == [("row", 3), ("log", "line"), ("paused", (3, {"v": 2})),
                      ("finished", (None, {"a": 1}))]


def test_queue_host_ask_blocks_until_answered():
    posted: list = []
    host = QueueHost(lambda kind, payload: posted.append((kind, payload)))
    result: list = []
    thread = threading.Thread(target=lambda: result.append(host.ask("T", "text", "ok_cancel")))
    thread.start()
    deadline = time.monotonic() + TIMEOUT_S
    while not posted:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    kind, request = posted[0]
    assert kind == "ask" and request.title == "T"
    request.reply("cancel")
    thread.join(TIMEOUT_S)
    assert result == ["cancel"]


def test_queue_host_ask_after_stop_returns_none_without_posting():
    posted: list = []
    host = QueueHost(lambda kind, payload: posted.append((kind, payload)))
    host.stop()
    assert host.ask("T", "text", "ok") is None
    assert posted == []


def test_ask_request_answers_once():
    request = AskRequest("t", "x", "ok")
    request.reply("ok")
    request.cancel()
    assert request.wait() == "ok"


def test_ask_buttons():
    assert ask_buttons("yes_no") == (("Yes", "yes"), ("No", "no"))
    assert ask_buttons("ok_cancel")[-1] == ("Cancel", "cancel")
    assert ask_buttons("bogus") == (("OK", "ok"),)


def test_platform_caches_shared_objects_and_closes_them():
    built: list = []

    class Closable:
        closed = False

        def close(self) -> None:
            self.closed = True

    def make_one():
        obj = Closable()
        built.append(obj)
        return obj

    factories = PlatformFactories(windows=make_one, input=make_one, screen=make_one,
                                  listener=make_one, stop_watcher=make_one, grabber=make_one)
    platform = Platform(factories)
    assert platform.windows() is platform.windows()
    assert platform.new_input() is not platform.new_input()
    platform.grabber()
    platform.close()
    shared = [built[0], built[3]]
    assert all(o.closed for o in shared)
    assert not built[1].closed and not built[2].closed


@pytest.mark.parametrize("answer", ["ok", "cancel"])
def test_ok_cancel_answers_reach_the_player(answer):
    rows = [make_row(make("message_box", text="?", buttons="ok_cancel")),
            make_row(make("var_set", var_name="reached", expr="1"))]
    rig = Rig(rows)
    rig.shell.answer = answer
    rig.controller.start()
    rig.run_to_end()
    # Cancel defaults to End, so the variable is set only after OK.
    assert ("reached" in rig.shell.variables[-1]) == (answer == "ok")


def test_shell_returns_to_idle_when_a_finished_callback_fails():
    rig = Rig([wait_row()])

    def broken_highlight(row_id):
        if row_id is None:
            raise RuntimeError("highlight failed")
    rig.controller._callbacks = replace(rig.controller._callbacks, highlight=broken_highlight)
    rig.controller.start()
    with pytest.raises(RuntimeError):
        rig.run_to_end()
    assert rig.shell.modes[-1] is Mode.IDLE
    assert not rig.controller.running
