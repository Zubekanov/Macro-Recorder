"""Key vocabulary shared by the model, the recorder and the injector.

Keys are stored as X keysym names: ``a``, ``A``, ``space``, ``Return``,
``F6``, ``Control_L``, ``KP_1``.  A combo is ``ctrl+shift+s``: modifier
aliases joined by ``+`` with the final key last.  This module is pure Python;
``platform/input.py`` converts names to keycodes.
"""

from __future__ import annotations

# Modifier alias -> canonical keysym name.
MODIFIER_ALIASES: dict[str, str] = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "shift": "Shift_L",
    "alt": "Alt_L",
    "super": "Super_L",
    "win": "Super_L",
    "meta": "Meta_L",
    "altgr": "ISO_Level3_Shift",
}

MODIFIER_KEYSYMS: frozenset[str] = frozenset({
    "Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R",
    "Super_L", "Super_R", "Meta_L", "Meta_R", "ISO_Level3_Shift", "Hyper_L",
    "Hyper_R", "Caps_Lock", "Num_Lock",
})

# Canonical display names for the modifier families.
_MODIFIER_DISPLAY: dict[str, str] = {
    "Control_L": "Ctrl", "Control_R": "Ctrl",
    "Shift_L": "Shift", "Shift_R": "Shift",
    "Alt_L": "Alt", "Alt_R": "Alt",
    "Super_L": "Super", "Super_R": "Super",
    "Meta_L": "Meta", "Meta_R": "Meta",
    "ISO_Level3_Shift": "AltGr",
    "Hyper_L": "Hyper", "Hyper_R": "Hyper",
}

# Keysym names the recorder and injector are expected to resolve.  Printable
# ASCII keysyms are the characters themselves (``a``, ``A``, ``1``) except for
# the names below, plus every Latin-1 keysym name python-xlib's XK knows.
NAMED_KEYS: tuple[str, ...] = (
    "space", "BackSpace", "Tab", "Return", "Escape", "Delete", "Insert",
    "Home", "End", "Page_Up", "Page_Down", "Left", "Right", "Up", "Down",
    "Print", "Scroll_Lock", "Pause", "Menu", "Caps_Lock", "Num_Lock",
    "Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R",
    "Super_L", "Super_R", "Meta_L", "Meta_R", "ISO_Level3_Shift",
    "KP_0", "KP_1", "KP_2", "KP_3", "KP_4", "KP_5", "KP_6", "KP_7", "KP_8",
    "KP_9", "KP_Enter", "KP_Add", "KP_Subtract", "KP_Multiply", "KP_Divide",
    "KP_Decimal", "KP_Home", "KP_End", "KP_Page_Up", "KP_Page_Down",
    "KP_Left", "KP_Right", "KP_Up", "KP_Down", "KP_Insert", "KP_Delete",
    "XF86AudioMute", "XF86AudioLowerVolume", "XF86AudioRaiseVolume",
    "XF86AudioPlay", "XF86AudioNext", "XF86AudioPrev",
) + tuple("F%d" % i for i in range(1, 25))

# Character -> keysym name for the ASCII characters whose keysym is not the
# character itself.
CHAR_KEYSYMS: dict[str, str] = {
    " ": "space", "!": "exclam", '"': "quotedbl", "#": "numbersign",
    "$": "dollar", "%": "percent", "&": "ampersand", "'": "apostrophe",
    "(": "parenleft", ")": "parenright", "*": "asterisk", "+": "plus",
    ",": "comma", "-": "minus", ".": "period", "/": "slash", ":": "colon",
    ";": "semicolon", "<": "less", "=": "equal", ">": "greater",
    "?": "question", "@": "at", "[": "bracketleft", "\\": "backslash",
    "]": "bracketright", "^": "asciicircum", "_": "underscore",
    "`": "grave", "{": "braceleft", "|": "bar", "}": "braceright",
    "~": "asciitilde", "\t": "Tab", "\n": "Return", "\r": "Return",
}
KEYSYM_CHARS: dict[str, str] = {v: k for k, v in CHAR_KEYSYMS.items() if k not in "\t\n\r"}


def is_modifier(name: str) -> bool:
    return name in MODIFIER_KEYSYMS


def normalise_key(name: str) -> str:
    """Map an alias or a single character to its keysym name.

    Raises ValueError for an empty name.
    """
    name = name.strip()
    if not name:
        raise ValueError("Empty key name")
    lower = name.lower()
    if lower in MODIFIER_ALIASES:
        return MODIFIER_ALIASES[lower]
    if len(name) == 1:
        return CHAR_KEYSYMS.get(name, name)
    for known in NAMED_KEYS:
        if known.lower() == lower:
            return known
    return name


def parse_combo(text: str) -> tuple[str, ...]:
    """``"ctrl+shift+s"`` -> ``("Control_L", "Shift_L", "s")``.

    Modifiers come first in the given order; the last element is the key.
    A lone ``+`` is the plus key.  Raises ValueError when empty or when a
    modifier appears after the key.
    """
    text = text.strip()
    if not text:
        raise ValueError("Empty key combination")
    if text == "+":
        return ("plus",)
    parts = [p for p in text.split("+")]
    if parts and parts[-1] == "" and len(parts) >= 2:
        parts = parts[:-2] + ["+"]          # trailing '+' means the plus key
    names = [normalise_key(p) for p in parts if p != ""]
    if not names:
        raise ValueError("Empty key combination")
    for name in names[:-1]:
        if not is_modifier(name):
            raise ValueError("Only modifiers may precede the key in %r" % text)
    return tuple(names)


def format_combo(names: tuple[str, ...] | list[str]) -> str:
    """``("Control_L", "Shift_L", "s")`` -> ``"Ctrl+Shift+S"``."""
    out = []
    for name in names[:-1]:
        out.append(_MODIFIER_DISPLAY.get(name, name))
    key = names[-1]
    out.append(display_key(key))
    return "+".join(out)


def display_key(name: str) -> str:
    """Human text for one keysym name: ``s`` -> ``S``, ``space`` -> ``Space``."""
    if name in _MODIFIER_DISPLAY:
        return _MODIFIER_DISPLAY[name]
    if name in KEYSYM_CHARS:
        return KEYSYM_CHARS[name]
    if len(name) == 1:
        return name.upper()
    return name.replace("_", " ")


def combo_from_names(names: tuple[str, ...]) -> str:
    """Canonical stored form: ``ctrl+shift+s``."""
    aliases = {v: k for k, v in MODIFIER_ALIASES.items()}
    aliases.update({"Control_R": "ctrl", "Shift_R": "shift", "Alt_R": "alt",
                    "Super_R": "super", "Meta_R": "meta"})
    parts = [aliases.get(n, n) for n in names[:-1]] + [names[-1]]
    return "+".join(parts)
