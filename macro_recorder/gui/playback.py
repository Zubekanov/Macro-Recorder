"""Playback controller: the player thread, the queue host, pause and stop.

Tk-free.  The shell owns the main-thread queue; :class:`QueueHost` is the
``PlayerHost`` the player reports to, posting every callback to that queue,
and :class:`PlaybackController` handles the events when the shell's pump
delivers them on the main thread.  The platform objects come from a
:class:`Platform` built on monkeypatchable factories, so tests run the real
player against the conftest fakes.

Queue events posted by the host: ``("log", line)``, ``("row", row_id)``,
``("paused", (row_id, variables))``, ``("ask", AskRequest)`` and
``("finished", (error, variables))``.  ``finished`` is always the last
event of a run, carrying the exception text when the player failed.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from macro_recorder.engine.player import Player
from macro_recorder.gui.settings import Settings
from macro_recorder.model.document import MacroDocument
from macro_recorder.model.protocols import (
    HotkeyGrabber, InputBackend, InputListener, Mode, Screen, StopKeyWatcher, WindowManager,
)

log = logging.getLogger(__name__)

Post = Callable[[str, Any], None]

ASK_BUTTONS: dict[str, tuple[tuple[str, str], ...]] = {
    "ok": (("OK", "ok"),),
    "ok_cancel": (("OK", "ok"), ("Cancel", "cancel")),
    "yes_no": (("Yes", "yes"), ("No", "no")),
}


def ask_buttons(buttons: str) -> tuple[tuple[str, str], ...]:
    """``(text, answer)`` pairs for a message_box button set; unknown sets show OK."""
    return ASK_BUTTONS.get(buttons, ASK_BUTTONS["ok"])


# ---------------------------------------------------------------------------
# Platform objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlatformFactories:
    """How the shell builds platform objects.  Tests substitute fakes."""
    windows: Callable[[], WindowManager]
    input: Callable[[], InputBackend]
    screen: Callable[[], Screen]
    listener: Callable[[], InputListener]
    stop_watcher: Callable[[], StopKeyWatcher]
    grabber: Callable[[], HotkeyGrabber]
    player: Callable[..., Player] = Player


def default_factories() -> PlatformFactories:
    """The X11 implementations.  The injector and the stop-key watcher share one
    SyntheticKeys record, so the watcher's fallback can ignore injected keys."""
    from macro_recorder.engine.screen import XScreen
    from macro_recorder.platform import input as xinput
    from macro_recorder.platform.windows import create_window_manager

    synthetic = xinput.SyntheticKeys()
    return PlatformFactories(
        windows=create_window_manager,
        input=lambda: xinput.XTestInput(synthetic=synthetic),
        screen=XScreen,
        listener=xinput.XRecordListener,
        stop_watcher=lambda: xinput.XI2StopKeyWatcher(synthetic=synthetic),
        grabber=xinput.XGrabHotkeys,
    )


class Platform:
    """Lazily built, shared platform objects.

    The window manager, screen, stop-key watcher and hotkey grabber are built
    once on first use; an injector and a listener are built fresh for every
    run and closed by their user.
    """

    def __init__(self, factories: PlatformFactories) -> None:
        self.factories = factories
        self._cache: dict[str, Any] = {}

    def _shared(self, name: str) -> Any:
        if name not in self._cache:
            self._cache[name] = getattr(self.factories, name)()
        return self._cache[name]

    def windows(self) -> WindowManager:
        return self._shared("windows")

    def screen(self) -> Screen:
        return self._shared("screen")

    def stop_watcher(self) -> StopKeyWatcher:
        return self._shared("stop_watcher")

    def grabber(self) -> HotkeyGrabber:
        return self._shared("grabber")

    def new_input(self) -> InputBackend:
        return self.factories.input()

    def new_listener(self) -> InputListener:
        return self.factories.listener()

    def close(self) -> None:
        """Close every shared object that has a ``close``."""
        cache, self._cache = self._cache, {}
        for name, obj in cache.items():
            close = getattr(obj, "close", None)
            if close is None:
                continue
            try:
                close()
            except Exception:
                log.exception("Closing the %s failed", name)


# ---------------------------------------------------------------------------
# The host
# ---------------------------------------------------------------------------

class AskRequest:
    """A pending ``host.ask``: the main thread answers it, stop cancels it."""

    def __init__(self, title: str, text: str, buttons: str) -> None:
        self.title = title
        self.text = text
        self.buttons = buttons
        self.answer: str | None = None
        self._done = threading.Event()
        self._lock = threading.Lock()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def reply(self, answer: str | None) -> None:
        """Answer once; later replies are ignored."""
        with self._lock:
            if self._done.is_set():
                return
            self.answer = answer
            self._done.set()

    def cancel(self) -> None:
        self.reply(None)

    def wait(self) -> str | None:
        self._done.wait()
        return self.answer


class QueueHost:
    """``PlayerHost`` that posts to the shell's queue.  Called on the player thread."""

    def __init__(self, post: Post) -> None:
        self._post = post
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._pending: list[AskRequest] = []
        self.variables_source: Callable[[], Mapping[str, Any]] | None = None
        self.finished_posted = False

    def on_row(self, row_id: int) -> None:
        self._post("row", row_id)

    def on_log(self, line: str) -> None:
        self._post("log", line)

    def on_paused(self, row_id: int, variables: Mapping[str, Any]) -> None:
        self._post("paused", (row_id, dict(variables)))

    def on_finished(self, error: str | None) -> None:
        if self.finished_posted:
            return
        self.finished_posted = True
        self._post("finished", (error, self._variables()))

    def ask(self, title: str, text: str, buttons: str) -> str | None:
        """Block until the main thread answers or playback stops (None)."""
        request = AskRequest(title, text, buttons)
        with self._lock:
            if self._stopped.is_set():
                return None
            self._pending.append(request)
        self._post("ask", request)
        answer = request.wait()
        with self._lock:
            if request in self._pending:
                self._pending.remove(request)
        return None if self._stopped.is_set() else answer

    def stop(self) -> None:
        """Cancel every pending and future ``ask``."""
        with self._lock:
            self._stopped.set()
            pending = list(self._pending)
        for request in pending:
            request.cancel()

    def _variables(self) -> dict[str, Any]:
        if self.variables_source is None:
            return {}
        try:
            return dict(self.variables_source())
        except Exception as e:
            log.warning("Reading the variables after playback failed: %s", e)
            return {}


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlaybackCallbacks:
    """What the controller asks of the shell.  All are called on the main thread."""
    set_mode: Callable[[Mode], None]
    highlight: Callable[[int | None], None]
    show_variables: Callable[[Mapping[str, Any]], None]
    log: Callable[[str], None]
    report_error: Callable[[str], None]
    show_ask: Callable[[AskRequest], None]
    close_asks: Callable[[], None]


class PlaybackController:
    """Runs one playback at a time on a worker thread."""

    def __init__(self, document: MacroDocument, settings: Settings, platform: Platform,
                 post: Post, callbacks: PlaybackCallbacks) -> None:
        self._document = document
        self._settings = settings
        self._platform = platform
        self._post = post
        self._callbacks = callbacks
        self._player: Player | None = None
        self._host: QueueHost | None = None
        self._thread: threading.Thread | None = None
        self._paused = False
        self._stop_requested = False

    @property
    def running(self) -> bool:
        return self._player is not None

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def play_label(self) -> str:
        """The Play button's text: Continue while paused."""
        return "Continue" if self._paused else "Play"

    def start(self) -> bool:
        """Build a player over the document and run it.  False (reported) when it cannot start."""
        if self._player is not None:
            return False
        if not self._document.rows:
            self._callbacks.report_error("There is nothing to play.")
            return False
        host = QueueHost(self._post)
        try:
            backend = self._platform.new_input()
        except Exception as e:
            log.exception("Playback: the input backend failed")
            self._callbacks.report_error("Cannot play: %s" % e)
            return False
        try:
            player = self._platform.factories.player(
                self._document, self._settings, host, backend,
                self._platform.windows(), self._platform.screen())
        except Exception as e:
            _close_quietly(backend)
            log.exception("Playback: the player could not be built")
            self._callbacks.report_error("Cannot play: %s" % e)
            return False
        host.variables_source = player.variables.snapshot
        self._player, self._host = player, host
        self._paused = False
        self._stop_requested = False
        self._callbacks.show_variables({})
        self._callbacks.set_mode(Mode.PLAYING)
        self._thread = threading.Thread(target=self._run, args=(player, host, backend),
                                        name="player", daemon=True)
        self._thread.start()
        return True

    def continue_(self) -> None:
        """Resume after a breakpoint."""
        if self._player is None or not self._paused:
            return
        self._paused = False
        self._callbacks.set_mode(Mode.PLAYING)
        self._player.resume()

    def stop(self) -> None:
        """Ask the player to stop; ``finished`` follows through the queue."""
        if self._player is None:
            return
        self._stop_requested = True
        if self._host is not None:
            self._host.stop()
        self._player.stop()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- queue events (main thread) ------------------------------------------

    def handle_row(self, row_id: int) -> None:
        if self._player is not None and not self._paused:
            self._callbacks.highlight(row_id)

    def handle_paused(self, payload: tuple[int, Mapping[str, Any]]) -> None:
        if self._player is None:
            return
        row_id, variables = payload
        self._paused = True
        self._callbacks.set_mode(Mode.PAUSED)
        self._callbacks.highlight(row_id)
        self._callbacks.show_variables(variables)
        self._callbacks.log("Paused at row %s" % self._row_number(row_id))

    def handle_ask(self, request: AskRequest) -> None:
        if self._player is None or request.done:
            return
        self._callbacks.show_ask(request)

    def handle_finished(self, payload: tuple[str | None, Mapping[str, Any]]) -> None:
        """Tidy up after a run.  The shell returns to idle even when a callback fails."""
        error, variables = payload
        thread = self._thread
        self._player = self._host = self._thread = None
        self._paused = False
        stop_requested, self._stop_requested = self._stop_requested, False
        if thread is not None:
            thread.join(1.0)
        try:
            self._callbacks.close_asks()
            self._callbacks.highlight(None)
        finally:
            self._callbacks.set_mode(Mode.IDLE)
        self._callbacks.show_variables(variables)
        if error:
            self._callbacks.report_error(error)
        else:
            self._callbacks.log("Playback stopped" if stop_requested else "Playback finished")

    # -- worker thread -------------------------------------------------------

    @staticmethod
    def _run(player: Player, host: QueueHost, backend: InputBackend) -> None:
        """The thread body: ``finished`` always arrives, with any exception text."""
        error: str | None = None
        try:
            player.play()
        except BaseException as e:      # noqa: BLE001 - reported through the queue
            log.exception("Playback thread failed")
            error = str(e) or type(e).__name__
        finally:
            _close_quietly(backend)
            if not host.finished_posted:
                host.on_finished(error or "Playback ended without a result")

    def _row_number(self, row_id: int) -> str:
        try:
            return str(self._document.index_of(row_id) + 1)
        except KeyError:
            return "?"


def _close_quietly(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        log.exception("Closing %r failed", obj)
