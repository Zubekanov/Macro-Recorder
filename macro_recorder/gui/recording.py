"""Recording controller: provisional live rows, insert at stop.

Tk-free.  The shell creates one controller, wires its callbacks to the table
and the mode machinery, and calls :meth:`RecordingController.poll` from its
event pump while recording.

Decisions where the design is silent:

- ``show_provisional`` always receives the whole provisional row list.  Rows
  that can no longer change keep their identity (and id) from poll to poll,
  so the table can apply the list as a diff; only the tail is rebuilt.
- When the listener dies the rows recorded so far are still inserted, then
  the error is reported.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Sequence

from macro_recorder.engine.recorder import Recorder, RecorderError
from macro_recorder.gui.settings import Settings
from macro_recorder.model.bundling import StreamBundler, rows_from_stream
from macro_recorder.model.document import MacroDocument, Row

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordingCallbacks:
    """What the controller asks of the shell.  All are called on the caller's thread."""
    show_provisional: Callable[[Sequence[Row]], None]
    clear_provisional: Callable[[], None]
    set_mode: Callable[[str], None]
    report_error: Callable[[str], None]


class RecordingController:
    """Runs one recording at a time into ``document``."""

    def __init__(self, document: MacroDocument, recorder_factory: Callable[[], Recorder],
                 settings: Settings, callbacks: RecordingCallbacks) -> None:
        self._document = document
        self._factory = recorder_factory
        self._settings = settings
        self._callbacks = callbacks
        self._recorder: Recorder | None = None
        self._bundler: StreamBundler | None = None
        self._cursor = 0
        self._shown_version = -1
        self._anchor: int | None = None

    @property
    def recording(self) -> bool:
        return self._recorder is not None

    def start(self, insert_after_row_id: int | None) -> bool:
        """Start recording; the rows go after ``insert_after_row_id`` (None: at the end).

        Returns False and reports the error when the recorder cannot start.
        """
        if self._recorder is not None:
            return False
        recorder = self._factory()
        try:
            recorder.start()
        except RecorderError as e:
            self._callbacks.report_error(str(e))
            return False
        self._recorder = recorder
        self._bundler = StreamBundler(self._settings.recording)
        self._cursor = 0
        self._shown_version = -1
        self._anchor = insert_after_row_id
        self._callbacks.set_mode("recording")
        return True

    def poll(self) -> None:
        """Pull new events into the provisional rows; stop when the recorder asks."""
        recorder, bundler = self._recorder, self._bundler
        if recorder is None or bundler is None:
            return
        if recorder.stop_requested:
            self.stop()
            return
        try:
            events, self._cursor = recorder.events_since(self._cursor)
        except RecorderError as e:
            self.stop()
            self._callbacks.report_error(str(e))
            return
        bundler.feed(events)
        bundler.advance(recorder.now())
        if bundler.version != self._shown_version:
            self._shown_version = bundler.version
            self._callbacks.show_provisional(bundler.provisional())

    def stop(self) -> tuple[int, ...]:
        """Stop, bundle the whole stream and insert it as one undo step.

        Returns the ids of the inserted rows.
        """
        recorder = self._recorder
        if recorder is None:
            return ()
        recorder.stop()
        self._recorder = None
        self._bundler = None
        rows = rows_from_stream(recorder.result(), self._settings.recording)
        self._callbacks.clear_provisional()
        ids: tuple[int, ...] = ()
        if rows:
            ids = self._document.insert(self._insert_index(), rows)
        self._callbacks.set_mode("idle")
        return ids

    def _insert_index(self) -> int:
        if self._anchor is not None:
            try:
                return self._document.index_of(self._anchor) + 1
            except KeyError:
                log.info("Recording: the row to insert after is gone; appending at the end")
        return len(self._document.rows)
