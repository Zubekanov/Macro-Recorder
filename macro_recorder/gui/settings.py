"""Persistent settings.

The dataclasses and load/save live here (WP0).  The Settings dialog is added
by the settings-and-CLI work package at the bottom of this module.  Nothing
above the dialog section imports tkinter.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP_DIR_NAME = "macro-recorder"


@dataclass
class PlaybackSettings:
    speed: float = 1.0
    repeat: int = 1                    # 0 = until stopped
    window_timeout_ms: int = 5000
    window_settle_ms: int = 1000
    on_missing: str = "skip"           # skip | halt
    restore_rect: bool = True
    path_mode: str = "recorded"        # default for new documents
    min_spacing_ms: int = 10


@dataclass
class RecordingSettings:
    record_paths: bool = True
    min_move_px: int = 3
    min_wait_ms: int = 100
    click_gap_ms: int = 250
    double_click_ms: int = 400


@dataclass
class HotkeySettings:
    record: str = "ctrl+shift+F9"
    play: str = "ctrl+shift+F5"


@dataclass
class UiSettings:
    geometry: str = "1000x650"
    log_visible: bool = True
    log_height: int = 160
    overlay_enabled: bool = True
    last_folder: str = ""
    recent: list[str] = field(default_factory=list)


RECENT_LIMIT = 10


@dataclass
class Settings:
    playback: PlaybackSettings = field(default_factory=PlaybackSettings)
    recording: RecordingSettings = field(default_factory=RecordingSettings)
    hotkeys: HotkeySettings = field(default_factory=HotkeySettings)
    ui: UiSettings = field(default_factory=UiSettings)

    def add_recent(self, path: str) -> None:
        path = str(Path(path).resolve())
        recent = [p for p in self.ui.recent if p != path]
        self.ui.recent = [path] + recent[:RECENT_LIMIT - 1]

    def remove_recent(self, path: str) -> None:
        self.ui.recent = [p for p in self.ui.recent if p != path]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIR_NAME


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / APP_DIR_NAME


def settings_path() -> Path:
    return config_dir() / "settings.json"


# ---------------------------------------------------------------------------
# Load and save
# ---------------------------------------------------------------------------

def _from_dict(cls: type, data: Any) -> Any:
    """Build a dataclass from a dict, ignoring unknown keys and bad types."""
    if not isinstance(data, dict):
        return cls()
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        if is_dataclass(f.type) if isinstance(f.type, type) else False:
            kwargs[f.name] = _from_dict(f.type, value)
            continue
        default = f.default if f.default is not dataclasses.MISSING else (
            f.default_factory() if f.default_factory is not dataclasses.MISSING else None)
        if isinstance(default, bool):
            if isinstance(value, bool):
                kwargs[f.name] = value
        elif isinstance(default, int):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                kwargs[f.name] = int(value)
        elif isinstance(default, float):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                kwargs[f.name] = float(value)
        elif isinstance(default, str):
            if isinstance(value, str):
                kwargs[f.name] = value
        elif isinstance(default, list):
            if isinstance(value, list):
                kwargs[f.name] = [v for v in value if isinstance(v, str)]
    return cls(**kwargs)


def settings_from_dict(data: Any) -> Settings:
    if not isinstance(data, dict):
        return Settings()
    return Settings(
        playback=_from_dict(PlaybackSettings, data.get("playback")),
        recording=_from_dict(RecordingSettings, data.get("recording")),
        hotkeys=_from_dict(HotkeySettings, data.get("hotkeys")),
        ui=_from_dict(UiSettings, data.get("ui")),
    )


def load_settings(path: Path | None = None) -> Settings:
    """Read settings; an unreadable file is renamed ``.bad`` and defaults are used."""
    path = path or settings_path()
    if not path.exists():
        return Settings()
    try:
        with path.open("r", encoding="utf-8") as f:
            return settings_from_dict(json.load(f))
    except (OSError, ValueError) as e:
        log.warning("Settings file %s is unreadable (%s); using defaults", path, e)
        try:
            path.replace(path.with_suffix(".json.bad"))
        except OSError:
            pass
        return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Write settings atomically."""
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(settings)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
