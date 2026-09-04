"""Bounded undo/redo history over opaque snapshots.

The stack never inspects a snapshot; the caller decides what a snapshot is and
how to restore one.  ``push`` records the state *before* a change, ``undo``
and ``redo`` swap the caller's current state for the stored one.
"""

from __future__ import annotations

from typing import Any, Optional


class UndoStack:
    def __init__(self, limit: int = 100) -> None:
        self._limit = limit
        self._undo: list[Any] = []
        self._redo: list[Any] = []

    def push(self, snapshot: Any) -> None:
        """Record ``snapshot`` as the state to return to on the next undo.

        Drops the oldest entry past ``limit`` and clears the redo history,
        since a new change forks away from anything previously undone.
        """
        self._undo.append(snapshot)
        if len(self._undo) > self._limit:
            del self._undo[0]
        self._redo.clear()

    def undo(self, current: Any) -> Optional[Any]:
        """Return the previous snapshot, storing ``current`` for redo.

        Returns None (and stores nothing) when there is nothing to undo.
        """
        if not self._undo:
            return None
        self._redo.append(current)
        return self._undo.pop()

    def redo(self, current: Any) -> Optional[Any]:
        """Return the next snapshot, storing ``current`` for undo."""
        if not self._redo:
            return None
        self._undo.append(current)
        return self._redo.pop()

    def peek(self) -> Optional[Any]:
        """The snapshot the next undo would restore, or None."""
        return self._undo[-1] if self._undo else None

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
