# Category-Based Action UI

## Overview

The "Add Item" toolbar has been reorganized with **category buttons** that display **dropdown menus** for specific action types, replacing the single "Action" button with a more intuitive, discoverable interface.

## UI Layout

### Record Tab "Add Item" Group

```
┌──────────────────────────────────────────────────────────────────┐
│ Add Item                                                         │
│ ┌────────┐ ┌─────────┐ ┌──────┐                                 │
│ │ Mouse  │ │Keyboard │ │ Wait │    Debug   Comment             │
│ │   ▼    │ │    ▼    │ │  ▼   │                                 │
│ └────────┘ └─────────┘ └──────┘                                 │
└──────────────────────────────────────────────────────────────────┘
```

Each category has:
- **Main button**: Click to show dropdown menu
- **Dropdown arrow (▼)**: Small button below to trigger dropdown (alternative click target)

## Categories and Actions

### 1. Mouse Category

Click "Mouse" or its ▼ button to show:

```
┌──────────────────┐
│ Move             │
│ Click Down       │
│ Click Up         │
│ Scroll           │
└──────────────────┘
```

**Move**: Create mouse movement event
- Editor: X coordinate, Y coordinate

**Click Down**: Press mouse button
- Editor: Button (left/right/middle), X coordinate, Y coordinate

**Click Up**: Release mouse button
- Editor: Button (left/right/middle), X coordinate, Y coordinate

**Scroll**: Scroll wheel event
- Editor: Horizontal (dx), Vertical (dy), X coordinate, Y coordinate

### 2. Keyboard Category

Click "Keyboard" or its ▼ button to show:

```
┌──────────────────┐
│ Key Press        │
│ Key Release      │
└──────────────────┘
```

**Key Press**: Press a key
- Editor: Key name (e.g., "a", "Return", "Key.ctrl")

**Key Release**: Release a key
- Editor: Key name

### 3. Wait Category

Click "Wait" or its ▼ button to show:

```
┌──────────────────┐
│ Wait/Pause       │
└──────────────────┘
```

**Wait/Pause**: Pause playback for a duration
- Editor: Duration (seconds or milliseconds)

### 4. Other Actions

Separate buttons (not in dropdown menus):

- **Debug**: Insert a debug row (existing feature)
- **Comment**: Insert a comment row (existing feature)

## Implementation Details

### UI Components

**Category Button Frame:**
```python
mouse_frame = tk.Frame(add_group)
mouse_frame.pack(side=tk.LEFT, padx=(0, 2))

# Main button
tk.Button(mouse_frame, text="Mouse", width=7, relief=tk.RAISED,
         command=lambda: self._show_action_menu("mouse", mouse_frame)).pack()

# Dropdown arrow button
tk.Button(mouse_frame, text="▼", width=2, font=("Arial", 8),
         command=lambda: self._show_action_menu("mouse", mouse_frame)).pack(fill=tk.X)
```

Each category (Mouse, Keyboard, Wait) has its own frame with two buttons stacked vertically:
1. Category name button (main)
2. Dropdown arrow button (▼)

### Methods

**`_show_action_menu(category: str, parent_widget: tk.Widget) -> None`**
- Creates a popup menu for the given category
- Positions menu below the category button
- Menu items trigger `_add_action_row(action_type)` when clicked

**`_add_action_row(action_type: str) -> None`**
- Takes an action type string (e.g., "mouse_move", "key_press")
- Creates editor dialog using `create_new_event()`
- Inserts new action into table with proper timestamps

### Dropdown Menu Behavior

- **Position**: Menu appears below the category button
- **Trigger**: Clicking either the main button or dropdown arrow shows the menu
- **Selection**: Clicking any action type closes the menu and starts the editor
- **Cancellation**: Clicking elsewhere closes the menu without action

## User Workflow

### Adding a Mouse Move Action

1. **Click "Mouse" button** or **▼** arrow below it
2. **Dropdown menu appears** showing: Move, Click Down, Click Up, Scroll
3. **Click "Move"**
4. **Editor dialog opens** (New Mouse Move)
   - X coordinate field
   - Y coordinate field
5. **Fill in coordinates** (e.g., 100, 150)
6. **Click OK**
7. **New row added** to macro with:
   - Type: "mouse_move"
   - Value: "(100, 150)"
   - Timestamp: Automatically assigned after previous event

### Adding a Key Press Action

1. **Click "Keyboard" button** or **▼** arrow below it
2. **Dropdown menu appears** showing: Key Press, Key Release
3. **Click "Key Press"**
4. **Editor dialog opens** (New Key Event)
   - Key field (text input)
5. **Type key name** (e.g., "a", "Return", "Key.ctrl")
6. **Click OK**
7. **New row added** to macro with:
   - Type: "key_press"
   - Value: "a"
   - Timestamp: Automatically assigned

## Benefits Over Previous Design

| Aspect | Before | After |
|--------|--------|-------|
| **Discovery** | Single "Action" button (unclear options) | Category buttons make types obvious |
| **Categorization** | Dialog list mixing all types | Organized by functional category |
| **Visual Layout** | One button takes space | Compact, organized layout |
| **Accessibility** | One click path | Multiple click targets per category |
| **Clarity** | "Action" is generic | "Mouse", "Keyboard", "Wait" are specific |

## Code Changes

**`src/run.py` modifications:**

1. **Replaced single "Action" button** with three category button frames
2. **Added `_show_action_menu()` method** to display category-specific popup menus
3. **Updated `_add_action_row()` method** to accept action type string parameter

**`src/editing.py` - No changes needed**
- Existing `create_new_event()` already supports all action types
- Works seamlessly with new category-based UI

## Future Enhancements

- **Keyboard shortcuts**: Alt+M for Mouse, Alt+K for Keyboard, Alt+W for Wait
- **Recent actions**: Show most recently used actions at top of dropdown
- **Favorites**: Allow pinning frequently used actions
- **Custom categories**: Let users create custom action groupings
- **Icons**: Add small icons to category buttons for visual distinction

## Testing

All existing tests pass with the new UI:
- 80 total tests
- Categories and dropdown logic covered by integration
- New methods are thin wrappers around existing, tested functions

Run tests:
```bash
python -m unittest discover -s . -p "test_*.py" -v
```
