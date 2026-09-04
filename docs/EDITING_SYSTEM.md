# Action and Value Editing System

A comprehensive system for editing macro actions and values through intuitive popup dialogs.

## Features

### 1. Value Editing (Double-Click Any Value)

Double-click on any value in the "Value" column to open the appropriate editor dialog for that action type:

- **Mouse Move**: Edit X and Y coordinates
- **Mouse Click**: Edit button (left/right/middle), action (down/up), and coordinates
- **Mouse Scroll**: Edit horizontal (dx) and vertical (dy) scroll amounts and coordinates
- **Key Press/Release**: Edit the key to press/release
- **Wait/Pause**: Edit duration in seconds or milliseconds
- **Label**: Double-click the Label column to edit action labels (simple text field)

### 2. Add New Actions by Category

The Record tab's "Add Item" group displays action categories as buttons with dropdown menus:

**Mouse Category:**
- Click the "Mouse" button or its dropdown arrow (▼)
- Select from:
  - Move
  - Click Down
  - Click Up
  - Scroll

**Keyboard Category:**
- Click the "Keyboard" button or its dropdown arrow (▼)
- Select from:
  - Key Press
  - Key Release

**Wait Category:**
- Click the "Wait" button or its dropdown arrow (▼)
- Select from:
  - Wait/Pause

When you select an action type:
1. An editor dialog opens for setting initial values
2. The new action is inserted at the end of the macro (or after the currently selected row)
3. Timestamps are automatically assigned and normalized

### 3. Type-Specific Editors

#### Mouse Move
- X coordinate (integer)
- Y coordinate (integer)
- Applied immediately to update the overlay visualization

#### Mouse Click
- Button selection: left, right, or middle
- Action: down (press) or up (release)
- X and Y coordinates

#### Mouse Scroll
- Horizontal scroll (dx): positive = right, negative = left
- Vertical scroll (dy): positive = down, negative = up
- X and Y coordinates (center of scroll)

#### Key Press / Key Release
- Key name (e.g., "a", "Return", "Key.ctrl", "Key.shift")
- Supports both simple keys and special keys from pynput notation

#### Wait/Pause
- Duration in seconds (e.g., 0.5 for 500ms)
- Unit selector: seconds or milliseconds
- Automatically converts milliseconds to seconds internally

## Implementation Details

### Files Modified

**`src/editing.py`** (new)
- Pure dialog functions independent of the main app
- Functions return edited events or None if cancelled
- Dialog types:
  - `edit_mouse_move(parent, event) -> MacroEvent | None`
  - `edit_mouse_click(parent, event) -> MacroEvent | None`
  - `edit_mouse_scroll(parent, event) -> MacroEvent | None`
  - `edit_key(parent, event) -> MacroEvent | None`
  - `edit_wait(parent, event) -> MacroEvent | None`
  - `choose_action_type(parent) -> str | None`
  - `create_new_event(parent, action_type, ts) -> MacroEvent | None`

**`src/run.py`**
- Updated `_on_cell_double_click()` to dispatch to appropriate editor based on column and event type
- Added category buttons with dropdowns in Record tab options bar (Mouse, Keyboard, Wait)
- Added `_show_action_menu()` method to display category-specific dropdown menus
- Updated `_add_action_row()` method to accept action type directly from menu selection

### Workflow

**Editing Existing Values:**
1. Double-click any value in the "Value" column
2. Appropriate editor opens
3. Modify the value(s)
4. Click OK to save or Cancel to discard
5. Table updates with new formatted value

**Adding New Actions by Category:**
1. Click a category button (Mouse, Keyboard, Wait) or its dropdown arrow
2. Dropdown menu shows specific action types for that category
3. Click the desired action type
4. Editor dialog opens for that action type
5. Fill in action-specific details
6. Click OK to add
7. New action is inserted after selected row (or at end)
8. Timestamps are automatically normalized

**Editing Labels:**
1. Double-click any value in the "Label" column
2. Inline text editor appears
3. Type or edit the label text
4. Press Enter or click away to save

## User Interface

### Record Tab Options Bar

```
┌──────────────────────────────────────────────────────────────────┐
│ Add Item                                                         │
│ ┌────────┐ ┌─────────┐ ┌──────┐                                 │
│ │ Mouse  │ │Keyboard │ │ Wait │    Debug   Comment             │
│ │   ▼    │ │    ▼    │ │  ▼   │                                 │
│ └────────┘ └─────────┘ └──────┘                                 │
└──────────────────────────────────────────────────────────────────┘
```

**Mouse Category:**
- Click "Mouse" or dropdown arrow to see:
  - Move
  - Click Down
  - Click Up
  - Scroll

**Keyboard Category:**
- Click "Keyboard" or dropdown arrow to see:
  - Key Press
  - Key Release

**Wait Category:**
- Click "Wait" or dropdown arrow to see:
  - Wait/Pause

**Other:**
- **Debug**: Insert debug row (existing feature)
- **Comment**: Insert comment row (existing feature)

### Mouse Category Dropdown

```
Click "Mouse" or "▼" button shows:

┌──────────────────┐
│ Move             │
│ Click Down       │
│ Click Up         │
│ Scroll           │
└──────────────────┘
```

### Event Editor Example (Mouse Click)

```
┌──────────────────────────────────────────┐
│ New Mouse Click                          │
├──────────────────────────────────────────┤
│ Button:        [left    ▼]               │
│ X coordinate:  [100            ]         │
│ Y coordinate:  [100            ]         │
├──────────────────────────────────────────┤
│ [ OK  ]  [ Cancel ]                      │
└──────────────────────────────────────────┘
```

### Event Editor Example (Mouse Click)

```
┌──────────────────────────────────────────┐
│ Edit Click                               │
├──────────────────────────────────────────┤
│ Button:        [left    ▼]               │
│ Action:        [down    ▼]               │
│ X coordinate:  [100            ]         │
│ Y coordinate:  [100            ]         │
├──────────────────────────────────────────┤
│ [ OK  ]  [ Cancel ]                      │
└──────────────────────────────────────────┘
```

## Validation

Each editor validates inputs before accepting:

- **Numeric fields** (coordinates, durations): Must be valid numbers
- **Duration**: Must be positive
- **Keys**: Cannot be empty
- **Buttons**: Limited to left, right, middle

Invalid input shows an error dialog with helpful message.

## Integration with Existing System

- Editors use `dataclasses.replace()` to create modified events without mutation
- Edited events are stored back in `_row_events[iid]`
- Table display is automatically updated with new formatted values
- Timestamps are normalized after adding new actions
- New actions inherit the appropriate timestamp based on previous events

## Testing

Tests are included in `test_editing.py`:
- Verify editing functions are callable
- Verify MacroEvent objects can be created with correct attributes
- Validate action type selection and event creation

Run tests with:
```bash
python -m unittest test_editing -v
```

## Future Enhancements

Possible improvements:
- Keyboard shortcuts for editing (e.g., Ctrl+E)
- Batch editing multiple rows
- Copy/paste values between rows
- Template actions for common sequences
- Smart coordinate suggestions based on window
