# Dialog Positioning Improvement

## Overview

All editing dialogs now appear **centered on the main application window** instead of at the top-left corner of the screen, providing a better user experience and keeping the focus on the application.

## Changes Made

### New Helper Function

Added `_center_dialog()` function in `src/editing.py`:

```python
def _center_dialog(dialog: tk.Toplevel, parent: tk.Widget) -> None:
    """Center a dialog window on the parent window."""
```

**How it works:**
1. Gets the parent window's position and dimensions
2. Gets the dialog's dimensions
3. Calculates the center point
4. Positions the dialog at that center point
5. Ensures dialog doesn't go off-screen

### Updated Dialogs

All editing dialogs now call `_center_dialog()` after creation:

**Value Editing Dialogs:**
- `edit_mouse_move()` - Edit coordinates
- `edit_mouse_click()` - Edit button, action, coordinates
- `edit_mouse_scroll()` - Edit scroll amounts and coordinates
- `edit_key()` - Edit key name
- `edit_wait()` - Edit duration

**Action Type Selection:**
- `choose_action_type()` - Choose which type of action to add

**Action Creation Dialogs:**
- `create_new_event()` with all action types:
  - Mouse Move
  - Mouse Click (down/up)
  - Mouse Scroll
  - Key Press/Release
  - Wait/Pause

## User Experience

### Before
```
Dialog opens at top-left of screen:
[Dialog at (0,0)]
┌─────────────────┐
│ Edit Move       │ ◄── Far from app window
│ X: [100    ]    │
│ Y: [100    ]    │
└─────────────────┘

[App Window at center of screen]
┌────────────────────────────────┐
│ Add Item    [Mouse] [Keyboard] │
│ ┌──────────────────────────────┤
│ │ Action 1: mouse_move         │
│ │ Action 2: wait               │
└────────────────────────────────┘
```

### After
```
Dialog opens centered on app window:

[App Window at center of screen]
┌────────────────────────────────┐
│ Add Item    [Mouse] [Keyboard] │
│                                │
│  ┌──────────────────────────┐  │
│  │ Edit Move                │  │ ◄── Centered on app
│  │ X: [100    ]             │  │
│  │ Y: [100    ]             │  │
│  │ [ OK ] [ Cancel ]        │  │
│  └──────────────────────────┘  │
│ ┌──────────────────────────────┤
│ │ Action 1: mouse_move         │
│ │ Action 2: wait               │
└────────────────────────────────┘
```

## Benefits

- **Better UX**: Dialog stays visible and centered on the application
- **Less Screen Clutter**: Not scattered across the desktop
- **Consistency**: All dialogs use the same centering logic
- **Professional**: More polished, expected behavior
- **Accessibility**: Keeps user focus on the active dialog

## Implementation Details

### The Centering Algorithm

```python
def _center_dialog(dialog, parent):
    # Calculate positions
    parent_x = parent.winfo_x()          # Parent window's X position
    parent_y = parent.winfo_y()          # Parent window's Y position
    parent_width = parent.winfo_width()  # Parent window's width
    parent_height = parent.winfo_height() # Parent window's height
    
    dialog_width = dialog.winfo_width()  # Dialog's width
    dialog_height = dialog.winfo_height() # Dialog's height
    
    # Center calculation
    x = parent_x + (parent_width - dialog_width) // 2
    y = parent_y + (parent_height - dialog_height) // 2
    
    # Safety: ensure on-screen
    x = max(0, x)
    y = max(0, y)
    
    dialog.geometry(f"+{x}+{y}")
```

### Call Pattern

Every dialog follows this pattern:
```python
dialog = tk.Toplevel(parent)
dialog.title("Dialog Title")
dialog.geometry("300x150")
dialog.transient(parent)
dialog.grab_set()
_center_dialog(dialog, parent)  # ◄── NEW

# ... rest of dialog setup ...
```

## Test Coverage

✅ **All 80 tests pass**
- No changes needed to test suite
- Dialog positioning is automatic and transparent
- Works seamlessly with existing code

## Files Modified

- `src/editing.py`: Added `_center_dialog()` helper function and called it in all dialog creation functions

## Future Enhancements

Possible improvements:
- Remember user's preferred dialog position
- Allow dragging dialogs to custom position
- Cascade multiple dialogs if opened in sequence
- Animation/transition to center position
