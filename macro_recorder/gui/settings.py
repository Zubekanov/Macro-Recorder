"""Persistent settings and the Settings dialog.

The dataclasses, load and save are Tk-free; the CLI imports them.  The
dialog at the bottom imports tkinter and the platform layer only when it is
opened, so importing this module needs neither.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import tkinter as tk

    from macro_recorder.gui.hotkeys import HotkeyRouter

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
    geometry: str = "1200x720"
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


# ---------------------------------------------------------------------------
# The Settings dialog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NumberSetting:
    """A whole-number field of the dialog; ``section`` is the Settings attribute."""
    section: str
    key: str
    label: str
    minimum: int
    maximum: int


RECORDING_NUMBERS = (
    NumberSetting("recording", "min_move_px", "Minimum movement (px)", 0, 100),
    NumberSetting("recording", "min_wait_ms", "Minimum wait (ms)", 0, 10_000),
    NumberSetting("recording", "click_gap_ms", "Click gap (ms)", 1, 5_000),
    NumberSetting("recording", "double_click_ms", "Double-click interval (ms)", 1, 5_000),
)
WINDOW_NUMBERS = (
    NumberSetting("playback", "window_timeout_ms", "Window timeout (ms)", 0, 600_000),
    NumberSetting("playback", "window_settle_ms", "Window settle (ms)", 0, 60_000),
)
ON_MISSING_CHOICES = ("skip", "halt")
HOTKEY_FIELDS = (("record", "Record or stop"), ("play", "Play or continue"))


def parse_number_setting(text: str, setting: NumberSetting) -> int:
    """A whole number in the setting's range; ValueError with the reason otherwise."""
    stripped = text.strip()
    if stripped.isdigit() and setting.minimum <= int(stripped) <= setting.maximum:
        return int(stripped)
    raise ValueError("%s must be a whole number from %d to %d"
                     % (setting.label, setting.minimum, setting.maximum))


def parse_hotkey(text: str) -> str:
    """A typed or captured hotkey in stored form (``ctrl+shift+F9``).

    Letters are stored in lower case, since a hotkey names a physical key.
    Raises ValueError for text that is not a usable global hotkey.
    """
    from macro_recorder.model.keys import combo_from_names, parse_combo
    from macro_recorder.platform.input import combo_problem
    from macro_recorder.platform.x11 import keysym_from_name

    names = parse_combo(text)
    key = names[-1]
    if len(key) == 1 and key.isalpha():
        names = names[:-1] + (key.lower(),)
    if not keysym_from_name(names[-1]):
        raise ValueError("Unknown key %r" % names[-1])
    combo = combo_from_names(names)
    problem = combo_problem(combo)
    if problem is not None:
        raise ValueError(problem)
    return combo


def parse_hotkeys(texts: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """``({name: stored combo}, {name: problem})`` for the Record and Play fields."""
    from macro_recorder.gui.hotkeys import same_combo

    combos: dict[str, str] = {}
    problems: dict[str, str] = {}
    for name, label in HOTKEY_FIELDS:
        try:
            combos[name] = parse_hotkey(texts[name])
        except ValueError as e:
            problems[name] = "%s: %s" % (label, e)
    if "record" in combos and "play" in combos and same_combo(combos["record"], combos["play"]):
        problems["play"] = "Record and Play need different keys"
    return combos, problems


def apply_settings(target: Settings, source: Settings) -> None:
    """Copy the dialog's sections (Recording, Window, Hotkeys) into ``target`` in place."""
    for f in fields(RecordingSettings):
        setattr(target.recording, f.name, getattr(source.recording, f.name))
    for key in ("window_timeout_ms", "window_settle_ms", "on_missing", "restore_rect"):
        setattr(target.playback, key, getattr(source.playback, key))
    for f in fields(HotkeySettings):
        setattr(target.hotkeys, f.name, getattr(source.hotkeys, f.name))


def copy_settings(settings: Settings) -> Settings:
    """An independent copy, so the dialog can save a candidate without touching the original."""
    return settings_from_dict(asdict(settings))


class SettingsDialog:
    """The modal Settings dialog over ``settings``.

    The hotkey router, when given, is suspended while the dialog is open so
    the Capture buttons can read the current combos, and grabs again on
    close.  OK first tries the new combos; refused ones are shown and a
    second OK keeps them.  It then saves a copy of the settings atomically
    and, only once that worked, applies it.  ``saved`` tells which happened.
    """

    def __init__(self, master: tk.Misc, settings: Settings, router: HotkeyRouter | None,
                 save: Callable[[Settings], None]) -> None:
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import ttk

        from macro_recorder.gui.dialogs import place_over
        from macro_recorder.gui.hotkeys import display_combo

        self._settings = settings
        self._router = router
        self._save = save
        self.saved = False
        self.capturing: str | None = None
        self._accepted_refusals: dict[str, str] | None = None
        self.window = window = tk.Toplevel(master)
        window.title("Settings")
        window.resizable(False, False)
        style = ttk.Style(window)
        style.configure("Invalid.TEntry", foreground="red")
        style.configure("Problem.TLabel", foreground="#c00000")
        body = ttk.Frame(window, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        # One label column width for all three boxes, so their fields line up.
        font = tkfont.nametofont("TkDefaultFont", root=window)
        labels = [s.label for s in RECORDING_NUMBERS + WINDOW_NUMBERS]
        labels += ["If window missing"] + [label for _n, label in HOTKEY_FIELDS]
        label_width = max(font.measure(text) for text in labels) + 16

        self.number_vars: dict[str, tk.StringVar] = {}
        self.entries: dict[str, ttk.Entry] = {}
        recording = ttk.LabelFrame(body, text="Recording", padding=(10, 6))
        recording.grid(row=0, column=0, sticky="ew")
        self.record_paths_var = tk.BooleanVar(window, value=settings.recording.record_paths)
        ttk.Checkbutton(recording, text="Record mouse paths", variable=self.record_paths_var
                        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=2)
        for row, setting in enumerate(RECORDING_NUMBERS, 1):
            self._number_row(recording, row, setting)

        window_box = ttk.LabelFrame(body, text="Window", padding=(10, 6))
        window_box.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for row, setting in enumerate(WINDOW_NUMBERS):
            self._number_row(window_box, row, setting)
        ttk.Label(window_box, text="If window missing").grid(row=2, column=0, sticky="w",
                                                            pady=2, padx=(0, 12))
        on_missing = settings.playback.on_missing
        self.on_missing_var = tk.StringVar(
            window, value=on_missing if on_missing in ON_MISSING_CHOICES else ON_MISSING_CHOICES[0])
        self.on_missing_box = ttk.Combobox(window_box, textvariable=self.on_missing_var,
                                           values=ON_MISSING_CHOICES, state="readonly", width=8)
        self.on_missing_box.grid(row=2, column=1, sticky="w", pady=2)
        self.restore_var = tk.BooleanVar(window, value=settings.playback.restore_rect)
        ttk.Checkbutton(window_box, text="Restore window position and size",
                        variable=self.restore_var).grid(row=3, column=0, columnspan=2,
                                                        sticky="w", pady=2)

        hotkeys = ttk.LabelFrame(body, text="Hotkeys", padding=(10, 6))
        hotkeys.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        hotkeys.columnconfigure(1, weight=1)
        self.hotkey_vars: dict[str, tk.StringVar] = {}
        self.capture_buttons: dict[str, ttk.Button] = {}
        for row, (name, label) in enumerate(HOTKEY_FIELDS):
            ttk.Label(hotkeys, text=label).grid(row=row, column=0, sticky="w", pady=2,
                                                padx=(0, 12))
            var = tk.StringVar(window, value=display_combo(getattr(settings.hotkeys, name)))
            entry = ttk.Entry(hotkeys, textvariable=var, width=20)
            entry.grid(row=row, column=1, sticky="ew", pady=2)
            button = ttk.Button(hotkeys, text="Capture",
                                command=lambda n=name: self.start_capture(n))
            button.grid(row=row, column=2, padx=(6, 0), pady=2)
            self.hotkey_vars[name] = var
            self.entries[name] = entry
            self.capture_buttons[name] = button

        for box in (recording, window_box, hotkeys):
            box.columnconfigure(0, minsize=label_width)
        self.message = ttk.Label(body, text="", justify="left", wraplength=360)
        self.message.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.message.grid_remove()
        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, sticky="e", pady=(10, 0))
        self.ok_button = ttk.Button(buttons, text="OK", command=self.ok)
        self.ok_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self.cancel)
        self.cancel_button.pack(side="left", padx=(6, 0))

        window.bind("<KeyPress>", self._on_key)
        for sequence in ("<Return>", "<KP_Enter>"):
            window.bind(sequence, self._on_return)
        window.bind("<Escape>", lambda e: self._on_key(e) if self.capturing else self.cancel())
        window.protocol("WM_DELETE_WINDOW", self.cancel)
        if router is not None and router.failures:
            self._show_refusals(router.failures)
        place_over(window, master)

    def _number_row(self, parent: tk.Misc, row: int, setting: NumberSetting) -> None:
        import tkinter as tk
        from tkinter import ttk

        value = getattr(getattr(self._settings, setting.section), setting.key)
        var = tk.StringVar(self.window, value=str(value))
        ttk.Label(parent, text=setting.label).grid(row=row, column=0, sticky="w", pady=2,
                                                   padx=(0, 12))
        entry = ttk.Entry(parent, textvariable=var, width=8)
        entry.grid(row=row, column=1, sticky="w", pady=2)
        self.number_vars[setting.key] = var
        self.entries[setting.key] = entry

    # -- running -------------------------------------------------------------

    def run(self) -> bool:
        """Show the dialog modally; True when OK saved the settings."""
        from macro_recorder.gui.dialogs import run_modal

        if self._router is not None:
            self._router.suspend()
        try:
            run_modal(self.window, focus=self.entries[RECORDING_NUMBERS[0].key])
        finally:
            if not self.saved and self._router is not None:
                self._regrab()
        return self.saved

    def _regrab(self) -> dict[str, str]:
        """Grab the combos now in the settings; returns the refused ones."""
        assert self._router is not None
        self._router.start()
        return self._router.resume()

    # -- capture -------------------------------------------------------------

    def start_capture(self, name: str) -> None:
        """Read the next key press on the dialog into the ``name`` hotkey field."""
        self.capturing = name
        self._show("Press the key combination for %s" % dict(HOTKEY_FIELDS)[name], problem=False)
        self.window.focus_set()

    def _on_key(self, event: Any) -> str | None:
        from macro_recorder.gui.hotkeys import display_combo
        from macro_recorder.gui.keycapture import key_from_event

        name = self.capturing
        if name is None:
            return None
        combo = key_from_event(event.keysym, int(event.state), single=False)
        if combo is None:
            return "break"
        self.capturing = None
        self.hotkey_vars[name].set(display_combo(combo))
        self._show("", problem=False)
        self.entries[name].focus_set()
        return "break"

    def _on_return(self, event: Any) -> str | None:
        """Return presses the focused button, or OK; while capturing it is the combo."""
        if self.capturing:
            return self._on_key(event)
        focused = self.window.focus_get()
        if focused is not None and focused.winfo_class() == "TButton":
            focused.invoke()
        else:
            self.ok()
        return "break"

    # -- OK and Cancel -------------------------------------------------------

    def collect(self) -> tuple[Settings, dict[str, str]]:
        """The settings the fields describe, and ``{field key: problem}``."""
        candidate = copy_settings(self._settings)
        problems: dict[str, str] = {}
        for setting in RECORDING_NUMBERS + WINDOW_NUMBERS:
            try:
                value = parse_number_setting(self.number_vars[setting.key].get(), setting)
            except ValueError as e:
                problems[setting.key] = str(e)
                continue
            setattr(getattr(candidate, setting.section), setting.key, value)
        candidate.recording.record_paths = bool(self.record_paths_var.get())
        candidate.playback.on_missing = self.on_missing_var.get()
        candidate.playback.restore_rect = bool(self.restore_var.get())
        combos, hotkey_problems = parse_hotkeys({n: v.get() for n, v in self.hotkey_vars.items()})
        problems.update(hotkey_problems)
        for name, combo in combos.items():
            setattr(candidate.hotkeys, name, combo)
        return candidate, problems

    def ok(self) -> None:
        """Validate, check the grabs, save, apply and close; problems stay in the dialog."""
        self.capturing = None
        candidate, problems = self.collect()
        for key, entry in self.entries.items():
            entry.configure(style="Invalid.TEntry" if key in problems else "TEntry")
        if problems:
            self._show("\n".join(problems.values()))
            return
        if self._router is not None and not self._hotkeys_accepted(candidate):
            return
        try:
            self._save(candidate)
        except OSError as e:
            self._show("Cannot save the settings: %s" % (e.strerror or e))
            return
        apply_settings(self._settings, candidate)
        self.saved = True
        if self._router is not None:
            self._regrab()
        self.window.destroy()

    def _hotkeys_accepted(self, candidate: Settings) -> bool:
        """Try the candidate's combos; False (and shown) when some are refused anew."""
        assert self._router is not None
        current = self._settings.hotkeys
        self._settings.hotkeys = candidate.hotkeys
        try:
            refused = self._regrab()
            self._router.suspend()
        finally:
            self._settings.hotkeys = current
        if not refused or refused == self._accepted_refusals:
            return True
        self._show_refusals(refused)
        return False

    def _show_refusals(self, refused: dict[str, str]) -> None:
        """Mark the refused hotkey fields; OK with the same refusals keeps them."""
        self._accepted_refusals = refused
        for name in refused:
            self.entries[name].configure(style="Invalid.TEntry")
        lines = ["%s: %s" % (dict(HOTKEY_FIELDS)[name], text) for name, text in refused.items()]
        self._show("\n".join(lines + ["Press OK again to keep these hotkeys."]))

    def cancel(self) -> None:
        """Close without saving; the router grabs the unchanged combos again."""
        self.window.destroy()

    def _show(self, text: str, problem: bool = True) -> None:
        """Show ``text`` above the buttons (red for a problem); blank hides the line."""
        self.message.configure(text=text, style="Problem.TLabel" if problem else "TLabel")
        if text:
            self.message.grid()
        else:
            self.message.grid_remove()


def open_settings_dialog(master: tk.Misc, settings: Settings, router: HotkeyRouter | None,
                         save: Callable[[Settings], None] = save_settings) -> bool:
    """Edit the Recording, Window and Hotkeys settings in a modal dialog.

    True when OK saved them (atomically, through ``save``) and applied them
    to ``settings``; Cancel leaves both untouched.  ``router`` is suspended
    while the dialog is open.
    """
    return SettingsDialog(master, settings, router, save).run()
