"""Turning a Tk key press into a stored key name or key combination.

Tk-free.  The Settings dialog's hotkey Capture buttons and the Details
panel's key fields read the next key press through Tk and pass its keysym
and state here.  Tk on X reports X keysym names, which is what the model
stores.
"""

from __future__ import annotations

from macro_recorder.model.keys import combo_from_names, is_modifier

# Tk event.state bits on X11.
SHIFT_MASK = 0x1
CONTROL_MASK = 0x4
MOD1_MASK = 0x8         # Alt
MOD4_MASK = 0x40        # Super

# Modifier bits in the order a stored combo lists them (ctrl+shift+alt+super).
_STATE_MODIFIERS = ((CONTROL_MASK, "Control_L"), (SHIFT_MASK, "Shift_L"),
                    (MOD1_MASK, "Alt_L"), (MOD4_MASK, "Super_L"))


def key_from_event(keysym: str, state: int, single: bool) -> str | None:
    """The key (``single``) or combination a key press stands for, in stored form.

    A single key is the keysym as pressed, modifiers included.  A combination
    waits for a key that is not a modifier (None until then) and adds the
    modifiers held in ``state``; a letter pressed with Shift is stored in
    lower case, since Shift is already part of the combination.  None also
    for a key Tk cannot name.
    """
    if not keysym or keysym == "??":
        return None
    if single:
        return keysym
    if is_modifier(keysym):
        return None
    modifiers = tuple(name for mask, name in _STATE_MODIFIERS if state & mask)
    key = keysym
    if state & SHIFT_MASK and len(key) == 1 and key.isalpha():
        key = key.lower()
    return combo_from_names(modifiers + (key,))
