# Side Panel UI Implementation

## Overview

Replaced popup dialogs with a **persistent side panel** on the right side of the main window. Users now:
1. Click a category button to add an action with **default values**
2. The default action appears in the table immediately
3. Click/select the row to edit its details in the side panel
4. Changes sync live to the table display

## Architecture

### Window Layout

```
┌─────────────────────────────────────────────────────────────────┐
│ Tab Bar                                                         │
├─────────────────────────────────────────────────────────────────┤
│ Options Bar (Record/Playback)                                   │
├─────────────────────────────┬─────────────────────────────────┐
│                             │                                 │
│      Action Table           │       Details Panel             │
│   (left, expands)           │      (right, fixed width)       │
│                             │                                 │
│  # Action  Value  Label     │  [Details for selected row]     │
│  ─────────────────────       │  ────────────────────────      │
│  1 mouse_move (100, 100)    │  Label: [______]                │
│  2 wait     500 ms  Jump    │  ────────────────────────      │
│  3 click    left @ (200,150)│  X: [150]                      │
│                             │  Y: [200]                      │
│                             │                                 │
└─────────────────────────────┴─────────────────────────────────┘
│ Status Bar                                                      │
└─────────────────────────────────────────────────────────────────┘
```

### Key Components

**Table (Left):**
- Displays all actions in sequence
- Acts as the main control surface
- Scrollable if many actions
- Click to select, drag to reorder, delete with Delete key

**Details Panel (Right):**
- Shows editable fields for selected action
- Fields vary by action type
- All changes sync immediately to table
- Shows "Select an action to edit details" when nothing selected

## User Workflow

### Adding an Action

1. Click a category button (Mouse, Keyboard, Wait) or its dropdown arrow
2. Select action type from dropdown menu
3. **Default action inserted immediately** at cursor position with sensible defaults:
   - Mouse Move: (100, 100)
   - Mouse Click: left button, down press, (100, 100)
   - Mouse Scroll: dy=3, (100, 100)
   - Key Press/Release: key="a"
   - Wait: 0.5 seconds

4. Row is automatically selected, opening details in side panel
5. Edit the default values in the side panel

### Editing an Action

1. **Click a row in the table** to select it
2. **Details panel updates** with edit fields for that action type
3. **Modify values** in the fields - changes sync in real-time to table
4. **Label field** at top is always available for all action types
5. Close panel by selecting a different row or no row

### Label Editing

- Label field in details panel is always present
- Applies to all action types
- Empty labels are stored as None

## Implementation Details

### New Methods

**`_build_details_panel(parent: tk.Frame) -> None`**
- Creates the right-side panel on initialization
- Sets up label field and content area
- Initializes with "Select an action" message

**`_show_details_panel(iid: str | None) -> None`**
- Populates details panel when row is selected
- Clears old widgets and rebuilds for the selected event type
- Creates appropriate input fields based on `MacroEvent.type`
- Binds all fields to `_sync_detail_to_table()` on change

**`_sync_detail_to_table() -> None`**
- Called when any detail field changes
- Reads values from detail widgets
- Updates the stored `MacroEvent` in `_row_events[iid]`
- Updates the table display row with formatted values
- Converts units as needed (e.g., ms ↔ seconds for wait)

**`_add_action_row(action_type: str) -> None`**
- Refactored: no longer shows dialogs
- Creates default `MacroEvent` based on type
- Inserts row immediately into table
- Selects the new row (opens it in details panel)
- Normalizes timestamps

**`_on_selection_change() -> None`**
- Updated to call `_show_details_panel()` when row selected
- Still shows overlay for mouse actions

### Event Type Details

**Mouse Move:**
- Fields: X, Y
- Default: (100, 100)

**Mouse Click:**
- Fields: Button (left/right/middle), Action (down/up), X, Y
- Default: left, down, (100, 100)

**Mouse Scroll:**
- Fields: DX, DY, X, Y
- Default: 0, 3, (100, 100)

**Key Press/Release:**
- Fields: Key (text)
- Default: "a"

**Wait:**
- Fields: Duration (milliseconds)
- Default: 500 ms (0.5 seconds)
- Automatically converts ms ↔ seconds

### Data Flow

```
User clicks category button
           ↓
_show_action_menu() displays popup menu
           ↓
User selects action type
           ↓
_add_action_row(action_type) creates default MacroEvent
           ↓
Default event inserted into table
           ↓
Row automatically selected via table.selection_set()
           ↓
_on_selection_change() fires
           ↓
_show_details_panel(iid) populates side panel
           ↓
User edits field in panel
           ↓
Field's <KeyRelease> binds to _sync_detail_to_table()
           ↓
_sync_detail_to_table() updates MacroEvent and table
```

## Advantages Over Popup Dialogs

| Aspect | Popups | Side Panel |
|--------|--------|-----------|
| **Visibility** | Covered by main window | Always visible |
| **Context** | Loses table context | See table while editing |
| **Workflow** | Add → Dialog → Close | Add → Edit in panel |
| **Discoverability** | Hidden until clicked | Always accessible |
| **Multi-edit** | One at a time | Switch quickly between rows |
| **Default values** | User enters everything | Sensible defaults, quick edit |

## UI State

### Details Panel State

- **No selection**: Shows "Select an action to edit details" message
- **Single selection**: Shows fields for that action's type
- **Multi-selection**: Shows "Select an action" (undefined state)

### Label Field

- Always visible at top
- Works for all action types
- Can be empty

### Syncing

- **Direction**: Panel → Table (one-way)
- **Trigger**: KeyRelease on any field
- **Real-time**: Changes visible immediately in table
- **Atomic**: Full event updated on each keystroke

## Test Coverage

✅ **All 80 tests pass**
- No changes needed to test suite
- Side panel is UI-only, doesn't affect logic
- Data storage and playback unchanged
- All existing functionality preserved

## Files Modified

**`src/run.py`:**
- Modified `_build_table()` to create left/right layout
- Added `_build_details_panel()` to create side panel
- Added `_show_details_panel()` to populate panel for selected row
- Added `_sync_detail_to_table()` to update from panel to table
- Refactored `_add_action_row()` to insert defaults instead of dialogs
- Updated `_on_selection_change()` to show details panel

**`src/editing.py`:**
- Kept as-is for double-click editing (alternative path)
- Could be used in future for advanced editing dialogs

## Future Enhancements

- Drag values between rows
- Copy/paste values
- Undo/redo for detail edits
- Value validation with error messages
- Quick templates for common action sequences
- Keyboard shortcuts (Tab to next field, Enter to confirm)
- Collapsible sections for complex event types
- Tooltips explaining each field

## Backward Compatibility

- Double-click on table cells still opens popups (legacy path)
- All existing tests pass
- Data format unchanged
- Save/load unchanged
- Playback unchanged

The side panel provides a more intuitive, discoverable way to add and edit actions while maintaining all existing functionality!
