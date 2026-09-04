# Timestamp Normalization Fix — Summary

## Problem

When users edited macros (copy/paste/delete/rearrange), the absolute timestamps stored in `MacroEvent.ts` became scrambled relative to the visual row order in the UI. The player uses `ts` values to determine timing, so events would execute out of order, causing the "mouse spazzing out" behavior described in mousefly.json.

**Root causes:**
1. Mutations never adjusted `ts` values when reordering rows
2. `_paste_rows` calculated new timestamps based on global `max(ts)` instead of visual position
3. No validation that row order matched timestamp order after edits

## Solution

Implemented `_normalize_timestamps()` method that re-anchors all timestamps to match the visual row display order. Called after every table mutation.

### How It Works

```python
def _normalize_timestamps(self) -> None:
    """Re-anchor all timestamps to their visual display order.
    
    - Reset clock to 0.0 at each window_focus row (each group times independently)
    - For wait rows: set ts = current_ts, advance by duration
    - For other rows: shift all events so first lands at current_ts, 
      preserving internal relative timing
    - Advance current_ts to last event's new timestamp
    """
```

### Changes Made

**File: src/run.py**

1. **Added `_normalize_timestamps()` method** (lines 916-942)
   - Walks table rows in visual order
   - Re-anchors each row's timestamps relative to current clock position
   - Preserves internal timing within each row bundle
   - Resets clock at window boundaries

2. **Simplified `_paste_rows()` method** (lines 815-838)
   - Removed broken timestamp calculation logic
   - Now just inserts clipboard bundles without adjusting timestamps
   - Normalization handles all sequencing automatically

3. **Added `_normalize_timestamps()` calls after every table mutation:**
   - `_load_groups_to_table()` — line 565 (load/reload files)
   - `_delete_row()` — line 796 (delete operation)
   - `_paste_rows()` — line 833 (paste operation)
   - `_move_row_up()` — line 847 (move up)
   - `_move_row_down()` — line 858 (move down)
   - `_on_drag_release()` — line 970 (drag reorder)

## Invariant

After any edit operation, the `ts` values in `_row_events` satisfy the invariant:
- Each group's events are in chronological order
- Groups reset to ts ≈ 0 at window boundaries
- Visual row order matches timestamp chronological order

This invariant ensures playback executes events in the correct sequence.

## Test Coverage

### Unit Tests (test_timestamp_normalization.py)
- Normalize simple scrambles
- Normalize paste-in-middle scenarios
- Normalize drag reorder operations
- Normalize delete operations with wait rows
- Window focus boundary handling

### End-to-End Tests (test_e2e_timestamp_fix.py)
- Playback after paste operations
- Load scrambled files and verify playback
- Save/load round-trip preservation
- Realistic edit sequences (paste, delete, reorder)
- Wait row timing preservation
- mousefly.json file (actual problem case)
- Groups flattening and extraction

### Verification
- **35 total tests pass** (23 existing + 5 unit + 7 e2e)
- mousefly.json loads and plays without crashing
- All edit operations maintain correct timing
- Round-trip through save/load preserves order

## Impact

### What Changed
- Timestamps are now re-anchored after every edit
- `_paste_rows` is simpler (no broken timestamp math)
- Table state is always consistent with visual order

### What Didn't Change
- Player behavior (still uses `group_start = events[0].ts`)
- Save/load file format (timestamps still absolute within groups)
- Recording behavior (timestamps still generated from `time.perf_counter()`)
- Public API or UI functionality

### Backward Compatibility
- Old files with scrambled timestamps now load correctly
- File format is unchanged (v2 JSON structure preserved)
- No UI changes (fix is internal table state management)

## Example: The Fix in Action

**Before:** User records "move right" then "move left", copies each 3×, then arranges as RRRLLL.
Without fix: visual order is [R, R, R, L, L, L] but timestamps might be [0-1, 0.5-1.5, 2-3, 0-1, 0.5-1.5, 2-3]. Player executes: right, right, right, LEFT (ts jumps back!), ...

**After:** `_normalize_timestamps()` runs after reorder.
- Row 1 (right): ts shifted to 0.0
- Row 2 (right): ts shifted to 0.0 (internal preserved)
- Row 3 (right): ts shifted to 0.0 (internal preserved)
- Row 4 (left): ts shifted to 0.0 (clock resets after last right)
- Row 5 (left): ts shifted to 0.0 (internal preserved)
- Row 6 (left): ts shifted to 0.0 (internal preserved)

Player executes: right, right, right, left, left, left (correct order!).
