"""Screen-point capture for the Details panel's targeting buttons.

Tk-free: :class:`TargetingSession` runs the platform capture on a thread and
reports through ``on_done``, which the shell marshals to the main thread.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

from macro_recorder.platform.input import TargetingError, capture_point

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class Capture:
    x: int
    y: int
    colour: str         # "#rrggbb"


CaptureFunction = Callable[[float, Callable[[], bool]], "tuple[int, int, str] | None"]


class TargetingSession:
    """One capture.  ``on_done(capture, error)`` is called exactly once, on the worker thread.

    ``capture`` is None when the user cancelled, :meth:`cancel` was called or
    the grab failed; ``error`` is the message for a failed grab.
    """

    def __init__(self, on_done: Callable[[Capture | None, str | None], None],
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 capture: CaptureFunction = capture_point) -> None:
        self._on_done = on_done
        self._timeout_s = timeout_s
        self._capture = capture
        self._cancelled = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("A targeting session runs once")
        self._thread = threading.Thread(target=self._run, name="targeting", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        result: Capture | None = None
        error: str | None = None
        try:
            point = self._capture(self._timeout_s, self._cancelled.is_set)
            if point is not None and not self._cancelled.is_set():
                result = Capture(*point)
        except TargetingError as e:
            error = str(e)
        except OSError as e:
            error = "Targeting failed: %s" % e
        except Exception as e:
            log.exception("Targeting failed")
            error = "Targeting failed: %s" % e
        self._on_done(result, error)
