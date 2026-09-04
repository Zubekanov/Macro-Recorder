# Point-to-Point Mouse Move Feature

## Overview

Added a new **"Move Point-to-Point"** action type that allows smooth mouse movements from a starting position to an ending position over a specified duration. Each coordinate field includes a **targeting button (⊙)** to click on the screen and capture exact coordinates.

## Features

### 1. Point-to-Point Movement
- Specify a **starting position** (From X, From Y)
- Specify an **ending position** (To X, To Y)
- Set the **duration** to move between them in milliseconds
- Creates smooth, timed mouse movement during playback

### 2. Coordinate Targeting
Each coordinate field has a targeting button (⊙) that allows:
1. Click the **⊙** button next to a coordinate field
2. A targeting window opens
3. Click **anywhere on the screen** to capture that position
4. The coordinate is automatically captured and updated
5. The field updates and syncs to the table

## How to Use

### Adding a Point-to-Point Move

1. Click the **Mouse** category button (or its ▼ dropdown)
2. Select **"Move Point-to-Point"** from the menu
3. A default action is created with:
   - From: (100, 100)
   - To: (200, 200)
   - Duration: 500 ms

### Editing the Movement

Select the row to open the details panel:

```
Label: [_______]
─────────────────
From X: [100] [⊙]
From Y: [100] [⊙]
To X:   [200] [⊙]
To Y:   [200] [⊙]
Duration (ms): [500]
```

**Manual Entry:**
- Type coordinates directly into the X/Y fields
- Update duration in milliseconds
- Changes sync immediately to table

**Targeting Mode:**
1. Click the **⊙** button next to the field you want to set
2. Targeting window opens: "Click anywhere on the screen to capture coordinates"
3. Click on the screen at the desired location
4. Coordinate is captured and the field updates
5. Targeting window closes automatically
6. Table display updates with the new values

## Playback Behavior

During playback, the point-to-point move action will:
1. Start at the **From** coordinates
2. Move the mouse smoothly to the **To** coordinates
3. Complete the movement in the specified **Duration**
4. Use linear interpolation between the two points

**Example:**
- From: (100, 100)
- To: (500, 300)
- Duration: 1000 ms (1 second)
- Result: Smooth movement from top-left to bottom-right over 1 second

## Implementation Details

### Action Type: `mouse_move_timed`

Stored as a `MacroEvent` with:
```python
MacroEvent(
    type="mouse_move_timed",
    ts=timestamp,
    x=start_x,          # From X
    y=start_y,          # From Y
    dx=delta_x,         # (To X - From X)
    dy=delta_y,         # (To Y - From Y)
    duration=seconds    # Duration in seconds (converted from ms)
)
```

### Display Format

In the table, displays as:
```
(100, 100) → (500, 300) in 1000ms
```

### Details Panel Fields

- **From X / From Y**: Starting coordinates
- **To X / To Y**: Ending coordinates (calculated as x + dx, y + dy)
- **Duration (ms)**: Time to move between points in milliseconds

### Targeting Implementation

The targeting feature:
1. Creates a modal targeting window
2. Binds a global click listener to the root window
3. Captures the mouse pointer's screen coordinates on click
4. Updates the appropriate field (x1, y1, x2, y2)
5. Syncs changes back to the table
6. Closes the targeting window after capture

## Table Display

In the table's Value column:
```
move_point_to_point    (100, 100) → (500, 300) in 1000ms
```

Clicking the row updates the details panel with all fields ready for editing.

## User Experience

**Workflow:**
1. Click "Move Point-to-Point" in Mouse menu
2. Default action added with sensible coordinates
3. Use ⊙ buttons to quickly click on the screen and capture positions
4. Or manually type coordinates
5. Adjust duration as needed
6. Changes appear live in the table

**Advantages:**
- **Visual targeting**: Click on the exact screen position you want
- **Smooth movement**: Specify timing for natural-looking mouse motion
- **Easy adjustment**: Change start/end points without re-targeting
- **Real-time feedback**: See formatted movement description in table

## Technical Notes

### Coordinate System
- Uses absolute screen coordinates
- Captured via `winfo_pointerx()` and `winfo_pointery()`
- Works across multiple monitors

### Timing
- Duration specified in **milliseconds**
- Converted to seconds internally for storage
- Displayed as milliseconds in UI for user convenience

### Playback Integration
The player will need to handle interpolation:
- Calculate intermediate positions based on elapsed time
- Use linear interpolation between start and end points
- Move mouse smoothly from (x, y) to (x+dx, y+dy) over duration

## Example Scenarios

### Drag Operation
- From: (100, 100) - Click location
- To: (300, 200) - Drag destination
- Duration: 500 ms
- Result: Mouse moves from click point to drag location

### Writing Text
- Move to text field: (200, 150)
- Move between letters: Previous position → Next position
- Duration: 50-100 ms per letter
- Result: Realistic typing motion with mouse movements

### Menu Navigation
- From: Current position
- To: Menu item position
- Duration: 200 ms
- Result: Smooth navigation between menu items

## Future Enhancements

- **Bezier curves**: Non-linear paths between points
- **Acceleration**: Easing functions for natural motion
- **Way points**: Multi-point paths with multiple segments
- **Visual preview**: Show path overlay on screen
- **Recording**: Capture smooth movement from recorded session
