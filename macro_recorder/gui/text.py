"""Text helpers shared by the table and the playback log.  Tk-free."""

from __future__ import annotations

_CONTROL_ESCAPES = str.maketrans({"\r": "\\r", "\n": "\\n", "\t": "\\t"})


def visible_controls(text: str) -> str:
    """One line for a cell or a log line: newlines and tabs (in typed text)
    shown as ``\\n``, ``\\r`` and ``\\t``."""
    return text.translate(_CONTROL_ESCAPES)
