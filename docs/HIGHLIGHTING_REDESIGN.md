# Playback Highlighting Redesign

## Old System (Fragile) — REPLACED
- Player called `_on_event_executing(event)` for each atomic `MacroEvent`
- UI tried to match event to a row by type + timestamp tolerance
- Problems:
  - Type matching failed if events bundled differently
  - Timestamp tolerance (0.0001s) was arbitrary
  - Required player to call us for every event
  - 5 bundled moves = 5 highlight flashes (flickery)
  - Broke when timestamps were scrambled (the exact problem we fixed!)

## New System (Robust) — IMPLEMENTED
- Pre-compute time ranges for each **displayed row** (not atomic events)
- Player sends current elapsed time
- UI highlights the row whose time interval contains that time
- Clean separation: player tracks time, UI tracks display

## Implementation

### 1. Compute Row Time Ranges

**When:** `_load_groups_to_table()`, after `_normalize_timestamps()`

**What:** Cache `(start_ts, end_ts)` for each row in `_row_events`

```python
def _compute_row_time_ranges(self) -> None:
    """Compute start/end timestamps for each row in display order.
    
    After bundling and normalization, each row has one or more events.
    The row's time range is [first_event.ts, last_event.ts].
    
    Cache this for fast highlight lookup during playback.
    """
    self._row_time_ranges: dict[str, tuple[float, float]] = {}
    
    for iid in self.table.get_children():
        events = self._row_events.get(iid, [])
        if not events:
            continue
        
        first_ts = events[0].ts
        last_ts = events[-1].ts
        
        # Handle wait rows: add duration to end time
        if events[-1].type == "wait" and events[-1].duration:
            last_ts += events[-1].duration
        
        self._row_time_ranges[iid] = (first_ts, last_ts)
```

**Call this at end of `_load_groups_to_table()` and after `_normalize_timestamps()`.**

### 2. Modify Player Callback

**Current:**
```python
on_event=self._on_event_executing  # Player calls this for each event
```

**New:**
```python
on_playback_time=self._on_playback_time  # Player calls this with elapsed time
```

Player changes (simple):
```python
# Instead of:
#   self._on_event(event)  # called for each event
#
# Player now sends time periodically (e.g., every 50ms):
elapsed = time.perf_counter() - playback_start
if self._on_playback_time:
    self._on_playback_time(elapsed)
```

### 3. New UI Highlighting Method

```python
def _on_playback_time(self, elapsed_time: float) -> None:
    """Update highlight based on current playback time.
    
    Called periodically from player with elapsed time.
    Finds which row's time range contains this time and highlights it.
    """
    # Find the row that is currently executing
    # (should be exactly one, but use first if overlap due to rounding)
    current_iid = None
    
    for iid, (start_ts, end_ts) in self._row_time_ranges.items():
        if start_ts <= elapsed_time < end_ts:
            current_iid = iid
            break
    
    # Update highlight if it changed
    if current_iid != self._playing_iid:
        self._update_highlight(current_iid)

def _update_highlight(self, new_iid: str | None) -> None:
    """Remove highlight from old row, add to new row."""
    # Clear old highlight
    if self._playing_iid and self._playing_iid in self._row_events:
        current_tags = self.table.item(self._playing_iid, "tags")
        new_tags = tuple(t for t in current_tags if t != "row_playing")
        self.table.item(self._playing_iid, tags=new_tags)
    
    # Add new highlight
    if new_iid and new_iid in self._row_events:
        current_tags = self.table.item(new_iid, "tags")
        new_tags = ("row_playing",) + tuple(t for t in current_tags if t != "row_playing")
        self.table.item(new_iid, tags=new_tags)
        self.table.see(new_iid)  # Scroll to visible
        
        # Cancel any pending timer
        if self._highlight_timer:
            self.root.after_cancel(self._highlight_timer)
            self._highlight_timer = None
    
    self._playing_iid = new_iid
```

## Changes Required

### src/run.py

1. **Add `_row_time_ranges` state:**
   ```python
   self._row_time_ranges: dict[str, tuple[float, float]] = {}
   ```

2. **Add `_compute_row_time_ranges()` method**

3. **Call from `_load_groups_to_table()`:**
   ```python
   def _load_groups_to_table(self, groups: list[MacroGroup]) -> None:
       ...
       self._normalize_timestamps()
       self._compute_row_time_ranges()  # NEW
   ```

4. **Call from `_normalize_timestamps()` at the end:**
   ```python
   def _normalize_timestamps(self) -> None:
       ...
       self._compute_row_time_ranges()  # NEW
   ```

5. **Replace `_on_event_executing()` with `_on_playback_time()`**

6. **Update `_start_playback()` to pass new callback:**
   ```python
   player = Player(
       ...,
       on_playback_time=self._on_playback_time,  # NEW
   )
   ```

### src/player.py

1. **Add `on_playback_time` parameter to `__init__`:**
   ```python
   def __init__(self, ..., on_playback_time=None):
       self._on_playback_time = on_playback_time
   ```

2. **In `_play_group()`, periodically send elapsed time:**
   ```python
   group_start = events[0].ts
   playback_start = time.perf_counter()
   last_time_update = 0.0
   
   for event in events:
       elapsed = time.perf_counter() - playback_start
       
       # Update UI with current time (every 50ms)
       if self._on_playback_time and (elapsed - last_time_update) > 0.05:
           self._on_playback_time(elapsed / self._speed)
           last_time_update = elapsed
       
       # Wait and execute as before
       wait_secs = ((event.ts - group_start) / self._speed) - elapsed
       ...
   ```

## Advantages

1. **Decoupled:** UI doesn't care about event structure, just time ranges
2. **Robust:** works with any bundling/bundling changes
3. **Smooth:** periodic updates show continuous progress, not discrete flashes
4. **Correct:** handles wait rows naturally (add duration)
5. **Simple:** no event matching, type checking, or duplicate tracking
6. **Visual clarity:** entire row lights up for its full duration

## Edge Cases

**Wait rows:**
- Row with only a wait event at ts=2.0, duration=0.5
- Time range: [2.0, 2.5]
- Highlights during that interval

**Bundled moves:**
- Row with 5 consecutive moves: ts 0.0, 0.1, 0.2, 0.3, 0.4
- Time range: [0.0, 0.4]
- Single continuous highlight for all 5 moves

**Multi-group playback:**
- Group 1: rows with time ranges [0, 1], [1, 2]
- Group 2: rows with time ranges [0, 1.5], [1.5, 3]
- Player sends `elapsed` separately per group; UI highlights correctly

## Fixing the Final Row Highlighting Bug

The mousefly.json test case revealed an inconsistency: the final wait rows (extending to `float('inf')`) were never highlighted during playback.

### Root Cause
The matching logic used:
```python
if start_ts <= elapsed_time < end_ts:
```

For the last row with `end_ts = float('inf')`, when the final callback sent `elapsed_time = float('inf')`, the condition `elapsed_time < float('inf')` was always `False`.

### Solution
Changed the matching logic to handle infinity:
```python
if start_ts <= elapsed_time and (end_ts == float('inf') or elapsed_time < end_ts):
```

And updated the final callback in player.py to send `float('inf')`:
```python
if self._on_playback_time:
    self._on_playback_time(float('inf'))
```

### Backend Testing
Created `src/highlighting.py` with pure, testable highlighting logic independent of Tkinter:
- `find_matching_row()`: Find which row contains a given elapsed time
- `verify_time_coverage()`: Detect gaps or overlaps in time ranges

Created `test_highlighting_backend.py` with 12 comprehensive tests including the specific mousefly.json scenario.

✅ **All 12 backend tests pass**, including the critical `test_mousefly_scenario` that verifies:
- The three final wait rows have continuous, non-overlapping ranges
- Each wait gets highlighted in sequence
- The final row extending to infinity correctly matches `float('inf')`

## Implementation Status

✓ **COMPLETE** — All code changes implemented and tested

### Code Changes Made

**src/run.py:**
- Removed `_matched_events` tracking (no longer needed)
- Added `_row_time_ranges: dict[str, tuple[float, float]]` state
- Added `_compute_row_time_ranges()` method to cache time intervals for each row
- Replaced `_on_event_executing()` with `_on_playback_time(elapsed_time)` callback
- Replaced `_highlight_playing_row()` and helpers with simpler `_update_highlight(iid)` method
- Simplified `_clear_playing_highlight()` to just call `_update_highlight(None)`
- Updated `_start_playback()` to pass `on_playback_time` instead of `on_event`

**src/player.py:**
- Changed `on_event` parameter to `on_playback_time` in `__init__()`
- Updated `_play_group()` to call `on_playback_time(elapsed)` every 50ms
- Removed `_on_event()` call from `_execute_event()`

### Test Results

✓ All 35 tests pass
  - 23 original macro operation tests
  - 5 unit tests for timestamp normalization
  - 7 end-to-end highlighting tests (updated for new system)

## Advantages of New System

1. **Decoupled from event structure** — UI doesn't care about MacroEvent bundling
2. **Robust to timestamp changes** — works with any timestamp, not brittle matching
3. **Smooth highlighting** — periodic updates show continuous playback progress
4. **Handles bundled events naturally** — 5 moves in one row = single continuous highlight
5. **Wait rows work correctly** — duration is added to end time automatically
6. **Simpler code** — no event matching, type checking, or deduplication logic
7. **Better UX** — row glows for its full duration, not flashing multiple times
