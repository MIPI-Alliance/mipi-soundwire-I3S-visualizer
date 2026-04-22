# DataPort FSM Refactor Proposal

**Status:** Proposal only — no code changes applied.

**Preserves public API:** `DataPort.reset`, `DataPort.next_bit_slot`,
`DataPort.config`, `DataPort.dp_index`, `DataPort.current_row_in_interval`,
`DataPort.fcp` — all unchanged. `DataPortConfig` register fields unchanged.

**Targets** (from review in `dataport_fsm_review.md`):

1. Collapse `transport_state` + `row_transport_done` + `horizontal_count_done`
   into one `TransportPhase` enum.
2. Replace `(wide_bit_slots_remaining, stored_wide_bit)` with
   `Optional[WideBitReplay]`.
3. Replace the `next_bit_slot` if-chain with a `match` on an `EmissionPhase`
   driven by an explicit post-data emission queue.
4. Centralize reset into four scope-named methods (frame/interval/row/transport).
5. Convert `horizontal_count_done` from a latched bool into a `@property`.

---

## 1. Transport lifecycle — one enum for three fields

### Before

```python
# DataPortState
self.transport_state: TransportState = TransportState.IDLE  # IDLE/ACTIVE/DONE
self.row_transport_done: bool = False
self.horizontal_count_done: bool = False
```

Twelve `(state, row_done, hc_done)` tuples — 5 distinct reachable, 4
observationally redundant, 2 invariant-violating (skip path), 1 never produced.

### After

```python
class TransportPhase(Enum):
    IDLE         = 0   # pre-transport: between intervals, before Offset_REG row
    ACTIVE       = 1   # emitting data inside the transport window
    SPACING      = 2   # inter-CG / inter-transport gap: spacing_slots_remaining > 0
    ROW_DONE     = 3   # no more data on this row, interval still alive
    PATTERN_DONE = 4   # transport pattern complete for this interval

# DataPortState
self.phase: TransportPhase = TransportPhase.IDLE
# row_transport_done: REMOVED
# horizontal_count_done: REMOVED (see Target 5 — becomes a @property)
```

### Mapping table (old → new)

| `transport_state` | `row_done` | `hc_done` | New `phase`      | Notes |
|---|---|---|---|---|
| IDLE   | F | F | `IDLE`         | Canonical initial state. |
| IDLE   | F | T | `IDLE`         | `hc_done` was stale; collapsed away. |
| IDLE   | T | * | `IDLE`         | Never produced in practice. |
| ACTIVE | F | F | `ACTIVE`       | Canonical emitting state. |
| ACTIVE | F | T | `ROW_DONE`     | `hc_done` becomes a `@property`; phase advances when `_slot` notices it. |
| ACTIVE | T | F | `ROW_DONE` or `SPACING` | Distinguished by whether spacing_slots_remaining > 0 (SPACING) vs `Spacing_REG == 0` shortcut (ROW_DONE). |
| ACTIVE | T | T | `ROW_DONE`     | Redundancy collapsed. |
| DONE   | F | F | `PATTERN_DONE` | Old skip path (was I1-violating); now correct by construction. |
| DONE   | F | T | `PATTERN_DONE` | |
| DONE   | T | F | `PATTERN_DONE` | Canonical normal termination. |
| DONE   | T | T | `PATTERN_DONE` | Redundancy collapsed. |

### Invariants enforced by construction

- **I1** (`DONE ⇒ row_done`): eliminated as a concept — there is no separate
  `row_done` flag. The skip path sets `phase = PATTERN_DONE` directly.
- **"hc_done is position-derivable"**: enforced structurally via `@property`
  (Target 5). The SPACING and ROW_DONE phases carry the non-derivable part.
- **"row_done while IDLE is impossible"**: `IDLE`, `SPACING`, `ROW_DONE`,
  `PATTERN_DONE` are mutually exclusive states, not bit-flag combinations.

### Behavior preserved

- `_slot()` short-circuit condition `row_done or state==DONE` becomes
  `phase in (ROW_DONE, PATTERN_DONE)`.
- Per-row reset of `row_done` becomes a phase transition on `_advance_row()`:
  `ROW_DONE → IDLE or ACTIVE` depending on Offset / interval wrap.
- Per-interval `hc_done` reset becomes implicit (new row ⇒ column = 0
  ⇒ property returns False).

---

## 2. WideBitReplay dataclass — make illegal state unrepresentable

### Before

```python
# DataPortState
self.wide_bit_slots_remaining: int = 0
self.stored_wide_bit: Optional['BitSlotState'] = None

# DataPortAlgorithm.next_bit_slot
if self._state.wide_bit_slots_remaining > 0:
    self._state.wide_bit_slots_remaining -= 1
    if self._state.stored_wide_bit is None:
        raise RuntimeError("stored_wide_bit_slot uninitialized with ...")
    slot = BitSlotState(
        slot_type=self._state.stored_wide_bit.slot_type,
        direction=self._state.stored_wide_bit.direction,
        data=self._state.stored_wide_bit.data
    )
    ...
```

Two fields, one logical state. RuntimeError guards an invariant the type
checker can't see.

### After

```python
@dataclass
class WideBitReplay:
    slot: BitSlotState
    remaining: int                  # >= 1 by invariant; 0 means "done" → None

# DataPortState
self.wide_replay: Optional[WideBitReplay] = None

# DataPortAlgorithm.next_bit_slot
if self._state.wide_replay is not None:
    replay = self._state.wide_replay
    slot = replay.slot                 # no copy — immutable by convention
    replay.remaining -= 1
    if replay.remaining == 0:
        self._state.wide_replay = None
    self._advance_column()
    return slot
```

### Invariants enforced by construction

- **I2** (`slots > 0 ⇔ stored is not None`): structural — one `Optional` field.
- **No stale slot leakage**: setting `wide_replay = None` is the only exit, so
  there's no way to have `remaining == 0` with a `slot` still attached.
- **RuntimeError at line 551 is deleted**: it can no longer occur.

### Construction site

```python
# Before — two assignments in DataPortAlgorithm._slot / next_bit_slot
if self._config.BitWidth_REG > 0:
    columns_remaining_in_window = self._config._HorizontalEnd - self._state.column
    self._state.wide_bit_slots_remaining = min(
        self._config.BitWidth_REG,
        max(0, columns_remaining_in_window)
    )
    self._state.stored_wide_bit = slot

# After — single construction; None when no replay needed
remaining = min(self._config.BitWidth_REG,
                max(0, self._config._HorizontalEnd - self._state.column))
self._state.wide_replay = WideBitReplay(slot=slot, remaining=remaining) if remaining > 0 else None
```

---

## 3. Emission FSM — explicit queue + `match`

### Before

```python
def next_bit_slot(self) -> BitSlotState:
    if self._state.wide_bit_slots_remaining > 0:
        ...
        return slot

    slot = self._slot()
    if slot.is_owned():
        ...
        if not self._config.PortDirection_REG:
            self._state.tails_left = self._config.TailWidth_REG
            self._state.guard_left = self._config.GuardEnable_REG
        self._advance_column()
        return slot

    if self._state.guard_left:
        self._state.guard_left = False
        slot = BitSlotState(slot_type=SlotType.GUARD_1 if ... else SlotType.GUARD_0, ...)
        self._advance_column()
        return slot

    if self._state.tails_left > 0:
        self._state.tails_left -= 1
        slot = BitSlotState(slot_type=SlotType.TAIL, ...)
        self._advance_column()
        return slot

    self._advance_column()
    return BitSlotState(slot_type=SlotType.EMPTY)
```

`guard_left: bool` + `tails_left: int` act as an ad-hoc 2-stage queue with
implicit order (guard before tails). Four `if` branches, all structurally
identical (emit + advance).

### After

```python
from collections import deque
from dataclasses import dataclass, field

@dataclass
class DataPortState:
    ...
    post_data_queue: deque[SlotType] = field(default_factory=deque)
    # tails_left: REMOVED
    # guard_left: REMOVED

def _seed_post_data_queue(self, is_source: bool) -> None:
    """Populate the queue for emissions that follow a data slot."""
    self._state.post_data_queue.clear()
    if not is_source:
        return
    if self._config.GuardEnable_REG:
        self._state.post_data_queue.append(
            SlotType.GUARD_1 if self._config.GuardPolarity_REG else SlotType.GUARD_0
        )
    for _ in range(self._config.TailWidth_REG):
        self._state.post_data_queue.append(SlotType.TAIL)

def next_bit_slot(self) -> BitSlotState:
    match self._emission_phase():
        case EmissionPhase.WIDE_REPLAY:
            return self._emit_wide_replay()
        case EmissionPhase.DATA_PROBE:
            slot = self._slot()
            if slot.is_owned():
                return self._emit_data(slot)
            # Fall through: no data → try queue
            if self._state.post_data_queue:
                return self._emit_queued()
            self._advance_column()
            return BitSlotState(slot_type=SlotType.EMPTY)

def _emission_phase(self) -> EmissionPhase:
    if self._state.wide_replay is not None:
        return EmissionPhase.WIDE_REPLAY
    return EmissionPhase.DATA_PROBE   # queue drain happens inside DATA_PROBE fall-through
```

### Rationale for deque + `match`

A pure `match` on an `EmissionPhase` enum (WIDE_REPLAY / DATA / GUARD / TAIL /
EMPTY) would require deciding the phase *before* probing `_slot()`, but `_slot`
is what determines whether we're in DATA. A hybrid is cleanest: `match` covers
the two pre-decidable cases (WIDE_REPLAY vs DATA_PROBE), and the queue handles
the post-data emission sequence.

### Invariants enforced by construction

- **"Emission order is fixed: guard before tails"**: structural — queue is
  FIFO, and `_seed_post_data_queue` appends guard before tails.
- **"Sink ports never emit guards/tails"**: `is_source` check is the single
  gate; no scattered `if not PortDirection_REG` checks.
- **"Guards are always 1 column wide"**: queue stores `SlotType` tokens
  consumed 1-per-call; no interaction with `BitWidth_REG`.
- **"Post-data emissions re-seeded on every data slot"**: `clear()` before
  append, so the most recent data slot's config wins (preserves current
  behavior documented in Section 2d of the review).

---

## 4. Centralized reset — four scopes, one method each

### Before

Five reset sites touching overlapping subsets (see Section 3, S6 of the
review):

| Method | What it resets |
|---|---|
| `DataPortState.reset` | Everything (20 fields) |
| `DataPort.reset` | State + fcp + algorithm |
| `DataPortAlgorithm.reset_algorithm` | `row_transport_done` + conditional `_start_interval` |
| `DataPortAlgorithm._start_interval` | Interval-scoped (10 fields), but NOT column/row/guards/wide/skipping |
| `DataPortAlgorithm._advance_row` | Row-scoped (column/guards/wide) + conditional interval wrap |

### After — scope definitions

| Scope | Lifetime | Fields owned |
|---|---|---|
| **Frame** | Whole rendering pass (`DataPort.reset`) | `skipping_accumulator`, `sample`, `current_row_in_interval`, `phase = IDLE` |
| **Interval** | One Interval_REG period | `phase` (→ IDLE on wrap), `sample` recompute at wrap, `current_row_in_interval = 0` |
| **Row** | One column sweep (`_advance_row`) | `column`, `post_data_queue`, `wide_replay`, fcp row reset |
| **Transport** | One transport pattern within an interval (`_start_transport`) | `sample_group_base`, `samples_in_group_remaining`, `channel_group_base`, `channel_group_size`, `channel_index`, `channels_in_group_remaining`, `bit`, `txp_sent`, `spacing_slots_remaining`, fcp interval reset |

### After — one method per scope

```python
# DataPortState — only the frame-scope reset lives here
def reset_frame(self) -> None:
    """Frame-scope reset: all fields to initial values."""
    self.column = 0
    self.current_row_in_interval = 0
    self.phase = TransportPhase.IDLE
    self.skipping_accumulator = 0
    self.sample = 0
    self.sample_group_base = 0
    self.samples_in_group_remaining = 0
    self.channel_index = 0
    self.spacing_slots_remaining = 0
    self.channel_group_base = 0
    self.channels_in_group_remaining = 0
    self.channel_group_size = 0
    self.bit = 0
    self.txp_sent = False
    self.wide_replay = None
    self.post_data_queue.clear()

# DataPortAlgorithm — scope-named helpers
def reset_row(self) -> None:
    """Row-scope: per-row state cleared before next column sweep."""
    self._state.column = 0
    self._state.wide_replay = None
    self._state.post_data_queue.clear()
    self._dataport.fcp.reset_for_row()

def reset_interval(self) -> None:
    """Interval-scope: called on interval wrap (not on transport start)."""
    self._state.current_row_in_interval = 0
    self._state.phase = TransportPhase.IDLE
    self._dataport.fcp.reset_drq_sent()
    # Sample truncation recompute (Normal mode, channel+sample grouping)
    if (not self._config.SubRowInterval_REG and
        self._config.SampleGrouping_REG > 0 and
        self._config.ChannelGrouping_REG > 0 and
        self._config.ChannelGrouping_REG < self._config._NumChannels):
        self._state.sample = self._state.sample_group_base + self._config.SampleGrouping_REG + 1

def reset_transport(self) -> None:
    """Transport-scope: called at Offset row (Normal) or per-transport (SRI).

    Replaces the body of the old _start_interval. Does NOT touch column,
    row counter, wide replay, or post-data queue (those are row-scoped).
    """
    self._state.phase = TransportPhase.ACTIVE
    self._state.sample_group_base = self._state.sample
    self._state.samples_in_group_remaining = self._config.SampleGrouping_REG
    self._state.channel_group_base = 0
    self._state.channel_index = 0
    if self._config.ChannelGrouping_REG == 0 or self._config.ChannelGrouping_REG > self._config._NumChannels:
        self._state.channel_group_size = self._config._NumChannels
    else:
        self._state.channel_group_size = self._config.ChannelGrouping_REG
    self._state.channels_in_group_remaining = self._state.channel_group_size - 1
    self._state.bit = self._config.SampleSize_REG
    self._state.txp_sent = False
    self._state.spacing_slots_remaining = 0
    self._dataport.fcp.reset_for_interval()

# DataPort.reset — unchanged signature, delegates cleanly
def reset(self) -> None:
    self._state.reset_frame()
    self.fcp.reset()
    # "Prime the pump" for row 0 (replaces reset_algorithm's conditional start)
    if self._state.current_row_in_interval == self.config.Offset_REG:
        if not self._algorithm._check_skipping_at_offset():
            self._algorithm.reset_transport()
```

### `reset_algorithm` — deleted

Its two responsibilities (reset `row_transport_done`; prime row 0) are folded
into the new structure:

- Clearing `row_transport_done` is obsolete (field removed).
- Priming row 0 moves into `DataPort.reset` above, using the phase machinery
  directly — one less indirection.

### Invariants enforced by construction

- **"Each field has exactly one scope"**: no more wondering whether
  `_start_interval` should reset `column`. The scope table is the contract.
- **"`skipping_accumulator` persists across intervals"**: explicit — it's
  frame-scoped, only touched in `reset_frame` and `_check_skipping_at_offset`.
- **"`horizontal_count_done` naturally resets per row"**: derived from
  `column`, which is row-scoped. No field to forget.
- **I1** (DONE without row_done via skip path): eliminated — skip sets
  `phase = PATTERN_DONE` atomically, and there is no separate `row_done`.

---

## 5. `horizontal_count_done` → `@property`

### Before

```python
# DataPortState
self.horizontal_count_done: bool = False

# DataPortAlgorithm._slot
if column == self._config.HorizontalStart_REG:
    self._state.horizontal_count_done = False
if column < self._config.HorizontalStart_REG:
    return slot
if self._state.horizontal_count_done or self._state.transport_state == TransportState.DONE:
    return slot
if column > self._config._HorizontalEnd:
    self._state.horizontal_count_done = True
    self._state.spacing_slots_remaining = 0
    return slot
```

Latched bool that's purely a function of `column` and `_HorizontalEnd`,
plus a side-effect (`spacing_slots_remaining = 0`) on first crossing.

### After

```python
# DataPortAlgorithm
@property
def _past_horizontal_end(self) -> bool:
    """True when current column is past the transport window's right edge."""
    return self._state.column > self._config._HorizontalEnd

# DataPortAlgorithm._slot
if column < self._config.HorizontalStart_REG:
    return slot
if self._past_horizontal_end:
    # Transport window exhausted on this row. Clear spacing so stale gaps
    # don't leak into the next row's first emission window.
    if self._state.spacing_slots_remaining > 0:
        self._state.spacing_slots_remaining = 0
    # Phase transition: ACTIVE/SPACING → ROW_DONE for this row.
    # (PATTERN_DONE stays PATTERN_DONE; IDLE never reaches _slot.)
    if self._state.phase in (TransportPhase.ACTIVE, TransportPhase.SPACING):
        self._state.phase = TransportPhase.ROW_DONE
    return slot
```

### Invariants enforced by construction

- **"`horizontal_count_done` is position-derivable"**: it no longer exists as
  a field. `_past_horizontal_end` is pure.
- **"Reset at `column == HorizontalStart_REG`"**: obsolete — `column = 0` at
  `_advance_row` makes the property False automatically on every new row.
- **"No stale `hc_done` leaking across intervals"**: impossible by
  construction; the former Section 2a row #2 (IDLE, F, T) is unreachable.

### Side-effect preservation

The `spacing_slots_remaining = 0` side-effect is preserved but conditional,
so it's idempotent across multiple `_slot()` calls past `_HorizontalEnd`
(previously it ran on every call after the first; now it's a no-op after the
first). This is a behavior-neutral tightening.

---

## Unified diff

```diff
--- a/src/models/enums.py
+++ b/src/models/enums.py
@@ -32,15 +32,20 @@ class PortMode(IntEnum):
     TEST_ZEROS = 3    # Test Mode - all zeros (c0t0)


-class TransportState(Enum):
-    """Lifecycle of a DataPort's transport pattern within an interval.
-
-    Encodes a 3-state lifecycle that previously required two booleans
-    (transport_started, transport_done). The (started=True, done=True)
-    combination was impossible by construction; the enum makes that explicit.
+class TransportPhase(Enum):
+    """Lifecycle phase for a DataPort's transport pattern.
+
+    Collapses the former (transport_state, row_transport_done,
+    horizontal_count_done) triple into one exhaustive enum. Each phase is
+    mutually exclusive; illegal combinations are unrepresentable.
     """
-    IDLE   = 0  # Pre-transport: post-interval-wrap, before Offset_REG row reached
-    ACTIVE = 1  # Currently emitting data within the transport pattern
-    DONE   = 2  # Transport complete for this interval (skipped or ended naturally)
+    IDLE         = 0  # Pre-transport: between intervals, before Offset row
+    ACTIVE       = 1  # Emitting data inside the transport window
+    SPACING      = 2  # Inter-CG / inter-transport gap (spacing counter > 0)
+    ROW_DONE     = 3  # No more data on this row; interval still alive
+    PATTERN_DONE = 4  # Transport pattern complete for this interval


 class SlotType(Enum):
--- a/src/models/dataport.py
+++ b/src/models/dataport.py
@@ -86,12 +86,13 @@

 from __future__ import annotations

-from typing import Optional, TYPE_CHECKING
+from collections import deque
+from dataclasses import dataclass, field
+from enum import Enum, auto
+from typing import Optional, TYPE_CHECKING

 from .bit_slot import BitSlotData, BitSlotState
-from .enums import SlotType, DirectionType, FlowMode, TransportState
+from .enums import SlotType, DirectionType, FlowMode, TransportPhase
 from .flow_control_port import FlowControlPort

 if TYPE_CHECKING:
     from .interface import Interface
     from .device import Device


+# =============================================================================
+# Supporting types
+# =============================================================================
+
+@dataclass
+class WideBitReplay:
+    """A data slot being replayed across multiple columns (BitWidth_REG > 0).
+
+    Invariant: remaining >= 1. When the replay is exhausted, the containing
+    Optional is set to None rather than leaving remaining == 0.
+    """
+    slot: BitSlotState
+    remaining: int
+
+
+class EmissionPhase(Enum):
+    """Drives the match in next_bit_slot()."""
+    WIDE_REPLAY = auto()
+    DATA_PROBE  = auto()   # probe _slot(); fall back to queue; fall back to EMPTY
+
+
 # =============================================================================
 # Runtime State Class
 # =============================================================================

 class DataPortState:
-    """Runtime state for a DataPort during frame rendering. ..."""
+    """Runtime state for a DataPort during frame rendering.
+
+    Fields are grouped by scope (frame / interval / row / transport); see
+    docs/dataport_fsm_refactor.md for the scope table.
+    """

     def __init__(self) -> None:
-        self.reset()
+        self.post_data_queue: deque[SlotType] = deque()
+        self.wide_replay: Optional[WideBitReplay] = None
+        self.reset_frame()

-    def reset(self) -> None:
-        """Reset all runtime state for a fresh drawing pass. ..."""
-        # Internal position tracking (for next_bit_slot() iteration)
-        self.column: int = 0
-
-        # Interval tracking
-        self.current_row_in_interval: int = 0
-        self.transport_state: TransportState = TransportState.IDLE
-        self.row_transport_done: bool = False
-        self.horizontal_count_done: bool = False
-
-        # Guard and tail state
-        self.tails_left: int = 0
-        self.guard_left: bool = False
-
-        # Sample tracking
-        self.skipping_accumulator: int = 0
-        self.sample: int = 0
-        self.sample_group_base: int = 0
-        self.samples_in_group_remaining: int = 0
-
-        # Channel tracking
-        self.channel_index: int = 0
-        self.spacing_slots_remaining: int = 0
-        self.channel_group_base: int = 0
-        self.channels_in_group_remaining: int = 0
-        self.channel_group_size: int = 0
-
-        # Bit tracking
-        self.bit: int = 0
-        self.txp_sent: bool = False
-
-        # Wide bit tracking - for returning same slot across multiple columns
-        self.wide_bit_slots_remaining: int = 0
-        self.stored_wide_bit: Optional['BitSlotState'] = None
+    def reset_frame(self) -> None:
+        """Frame-scope reset — full re-init for a new rendering pass."""
+        # Position (row-scope fields, but set to initial here too)
+        self.column: int = 0
+        # Interval-scope
+        self.current_row_in_interval: int = 0
+        self.phase: TransportPhase = TransportPhase.IDLE
+        # Frame-scope (persists across intervals)
+        self.skipping_accumulator: int = 0
+        self.sample: int = 0
+        # Transport-scope (reset by reset_transport on each Offset row)
+        self.sample_group_base: int = 0
+        self.samples_in_group_remaining: int = 0
+        self.channel_index: int = 0
+        self.spacing_slots_remaining: int = 0
+        self.channel_group_base: int = 0
+        self.channels_in_group_remaining: int = 0
+        self.channel_group_size: int = 0
+        self.bit: int = 0
+        self.txp_sent: bool = False
+        # Row-scope (reset_row clears these too)
+        self.wide_replay = None
+        self.post_data_queue.clear()
+
+    # Alias kept for the external DataPort.reset() → state.reset() call site.
+    # Internal callers should use reset_frame() directly.
+    reset = reset_frame

 # =============================================================================
 # Algorithm Class
 # =============================================================================

 class DataPortAlgorithm:
     ...

+    # -------------------------------------------------------------------------
+    # Position predicates (pure, no state mutation)
+    # -------------------------------------------------------------------------
+
+    @property
+    def _past_horizontal_end(self) -> bool:
+        """True when current column is past the transport window's right edge."""
+        return self._state.column > self._config._HorizontalEnd
+
     # =========================================================================
     # Counter Advancement (transport → bit)
     # =========================================================================

     def _end_transport_pattern(self) -> None:
-        """Mark the transport pattern as complete ..."""
-        self._state.transport_state = TransportState.DONE
-        self._state.row_transport_done = True
+        """Mark the transport pattern as complete — single atomic transition."""
+        self._state.phase = TransportPhase.PATTERN_DONE

     def _advance_channel_group(self) -> None:
         if self._state.channel_group_base + self._state.channel_group_size >= self._config._NumChannels:
             if not self._config.SubRowInterval_REG:
                 self._end_transport_pattern()
             else:
-                self._start_interval()
+                self.reset_transport()
                 self._state.spacing_slots_remaining = self._config.Spacing_REG
+                if self._config.Spacing_REG > 0:
+                    self._state.phase = TransportPhase.SPACING
         else:
             self._state.channel_group_base += self._state.channel_group_size
             remaining_channels = self._config._NumChannels - self._state.channel_group_base
             if remaining_channels > self._state.channel_group_size:
                 remaining_channels = self._state.channel_group_size
             self._state.channels_in_group_remaining = remaining_channels - 1
             self._state.samples_in_group_remaining = self._config.SampleGrouping_REG
             if (self._config.ChannelGrouping_REG > 0 and
                 self._config.ChannelGrouping_REG < self._config._NumChannels):
                 self._state.sample = self._state.sample_group_base
             self._state.bit = self._config.SampleSize_REG
             self._state.channel_index = self._state.channel_group_base
             self._state.spacing_slots_remaining = self._config.Spacing_REG
             self._state.txp_sent = False
+            if self._config.Spacing_REG > 0:
+                self._state.phase = TransportPhase.SPACING

-        if self._config.Spacing_REG == 0:
-            self._state.row_transport_done = True
-        else:
-            self._state.spacing_slots_remaining -= 1
+        # Spacing=0 shortcut: in Normal mode this used to set row_transport_done
+        # (killing the row); preserve that behavior as ROW_DONE. In SRI mode it
+        # means "no gap between transports" — also ROW_DONE (single transport).
+        if self._config.Spacing_REG == 0 and self._state.phase != TransportPhase.PATTERN_DONE:
+            self._state.phase = TransportPhase.ROW_DONE
+        elif self._state.phase == TransportPhase.SPACING:
+            self._state.spacing_slots_remaining -= 1
+            if self._state.spacing_slots_remaining <= 0:
+                self._state.phase = TransportPhase.ACTIVE

     # =========================================================================
     # Position Management
     # =========================================================================

-    def _start_interval(self) -> None:
+    def reset_transport(self) -> None:
         """Initialize state for a new transport pattern (Normal: per interval;
         SRI: per transport within a row)."""
-        self._state.transport_state = TransportState.ACTIVE
+        self._state.phase = TransportPhase.ACTIVE
         self._state.spacing_slots_remaining = 0
         self._state.sample_group_base = self._state.sample
         self._state.samples_in_group_remaining = self._config.SampleGrouping_REG
         self._state.channel_group_base = 0
         self._state.channel_index = 0
         if self._config.ChannelGrouping_REG == 0 or self._config.ChannelGrouping_REG > self._config._NumChannels:
             self._state.channel_group_size = self._config._NumChannels
         else:
             self._state.channel_group_size = self._config.ChannelGrouping_REG
         self._state.channels_in_group_remaining = self._state.channel_group_size - 1
         self._state.bit = self._config.SampleSize_REG
         self._state.txp_sent = False
         self._dataport.fcp.reset_for_interval()

     def _check_skipping_at_offset(self) -> bool:
         if self._config.SkippingNumerator_REG == 0:
             return False
         self._state.skipping_accumulator += self._config.SkippingNumerator_REG
         if self._state.skipping_accumulator < self._interface.SkippingDenominator_REG:
             return False
-        self._state.transport_state = TransportState.DONE
+        # Atomic transition — the old code set DONE without row_done, violating I1.
+        # PATTERN_DONE encodes both facts in one assignment.
+        self._state.phase = TransportPhase.PATTERN_DONE
         self._state.skipping_accumulator -= self._interface.SkippingDenominator_REG
         return True

-    def _advance_row(self) -> None:
+    def reset_row(self) -> None:
+        """Row-scope reset."""
         self._state.column = 0
-        self._state.guard_left = False
-        self._state.tails_left = 0
-        self._state.wide_bit_slots_remaining = 0
-        self._state.stored_wide_bit = None
+        self._state.wide_replay = None
+        self._state.post_data_queue.clear()
         self._dataport.fcp.reset_for_row()
-        self._state.row_transport_done = False
+
+    def _advance_row(self) -> None:
+        """Wrap position to the next row and prepare per-row state."""
+        self.reset_row()
+        # Entering a new row: ROW_DONE → IDLE-or-ACTIVE (resolved below).
+        if self._state.phase == TransportPhase.ROW_DONE:
+            self._state.phase = TransportPhase.IDLE

         self._state.current_row_in_interval += 1

         if self._state.current_row_in_interval > self._config.Interval_REG:
-            self._state.current_row_in_interval = 0
-            self._state.transport_state = TransportState.IDLE
-            self._dataport.fcp.reset_drq_sent()
-            if (not self._config.SubRowInterval_REG and
-                self._config.SampleGrouping_REG > 0 and
-                self._config.ChannelGrouping_REG > 0 and
-                self._config.ChannelGrouping_REG < self._config._NumChannels):
-                expected_samples_per_interval = self._config.SampleGrouping_REG + 1
-                self._state.sample = self._state.sample_group_base + expected_samples_per_interval
+            self._reset_interval_wrap()

         if self._state.current_row_in_interval == self._config.Offset_REG:
             if self._check_skipping_at_offset():
                 return
-            self._start_interval()
+            self.reset_transport()
+
+    def _reset_interval_wrap(self) -> None:
+        """Interval-scope reset on wrap (called from _advance_row only)."""
+        self._state.current_row_in_interval = 0
+        self._state.phase = TransportPhase.IDLE
+        self._dataport.fcp.reset_drq_sent()
+        if (not self._config.SubRowInterval_REG and
+            self._config.SampleGrouping_REG > 0 and
+            self._config.ChannelGrouping_REG > 0 and
+            self._config.ChannelGrouping_REG < self._config._NumChannels):
+            self._state.sample = self._state.sample_group_base + self._config.SampleGrouping_REG + 1

     def _advance_column(self) -> None:
         self._state.column += 1
         if self._state.column >= self._interface.num_columns:
             self._advance_row()

     def _slot(self) -> BitSlotState:
         slot: BitSlotState = BitSlotState(slot_type=SlotType.NORMAL)
         column = self._state.column

-        if self._state.row_transport_done or self._state.transport_state == TransportState.DONE:
+        if self._state.phase in (TransportPhase.ROW_DONE, TransportPhase.PATTERN_DONE):
             return slot

         if self._config._NumChannels == 0:
             return slot

-        if self._state.transport_state == TransportState.ACTIVE:
-            if column == self._config.HorizontalStart_REG:
-                self._state.horizontal_count_done = False
+        if self._state.phase in (TransportPhase.ACTIVE, TransportPhase.SPACING):
             if column < self._config.HorizontalStart_REG:
                 return slot
-            if self._state.horizontal_count_done or self._state.transport_state == TransportState.DONE:
-                return slot
-
-            if column > self._config._HorizontalEnd:
-                self._state.horizontal_count_done = True
-                self._state.spacing_slots_remaining = 0
+            if self._past_horizontal_end:
+                if self._state.spacing_slots_remaining > 0:
+                    self._state.spacing_slots_remaining = 0
+                self._state.phase = TransportPhase.ROW_DONE
                 return slot

-            if self._state.spacing_slots_remaining > 0:
+            if self._state.phase == TransportPhase.SPACING:
                 self._state.spacing_slots_remaining -= 1
+                if self._state.spacing_slots_remaining <= 0:
+                    self._state.phase = TransportPhase.ACTIVE
                 return slot

             direction = DirectionType.SINK if self._config.PortDirection_REG else DirectionType.SOURCE
             if (self._config.FlowMode_REG in (FlowMode.TX_CONTROLLED, FlowMode.ASYNC) and
                 not self._state.txp_sent and
                 self._state.bit == self._config.SampleSize_REG):
                 slot = BitSlotState(
                     slot_type=SlotType.TX_PRESENT,
                     direction=direction,
                     data=BitSlotData(
                         sample=self._state.sample,
                         channel=self._config._channel(self._state.channel_index),
                         bit=0
                     )
                 )
                 self._state.txp_sent = True
             else:
                 slot = BitSlotState(
                     slot_type=SlotType.NORMAL,
                     direction=direction,
                     data=BitSlotData(
                         sample=self._state.sample,
                         channel=self._config._channel(self._state.channel_index),
                         bit=self._state.bit
                     )
                 )
                 self._advance_bit()

         return slot

     # =========================================================================
     # Public Interface
     # =========================================================================

-    def reset_algorithm(self) -> None:
-        """Reset algorithm-specific tracking state and prepare for row 0."""
-        self._state.row_transport_done = False
-        if self._state.current_row_in_interval == self._config.Offset_REG:
-            if self._check_skipping_at_offset():
-                return
-            self._start_interval()
-
-    def next_bit_slot(self) -> BitSlotState:
+    def _seed_post_data_queue(self) -> None:
+        """Populate post-data queue after a source-port data slot."""
+        self._state.post_data_queue.clear()
+        if self._config.PortDirection_REG:
+            return  # sink ports emit no guards/tails
+        if self._config.GuardEnable_REG:
+            self._state.post_data_queue.append(
+                SlotType.GUARD_1 if self._config.GuardPolarity_REG else SlotType.GUARD_0
+            )
+        for _ in range(self._config.TailWidth_REG):
+            self._state.post_data_queue.append(SlotType.TAIL)
+
+    def _emission_phase(self) -> EmissionPhase:
+        if self._state.wide_replay is not None:
+            return EmissionPhase.WIDE_REPLAY
+        return EmissionPhase.DATA_PROBE
+
+    def next_bit_slot(self) -> BitSlotState:
         """Get slot for current position and auto-advance. ..."""
-        if self._state.wide_bit_slots_remaining > 0:
-            self._state.wide_bit_slots_remaining -= 1
-            if self._state.stored_wide_bit is None:
-                raise RuntimeError("stored_wide_bit_slot uninitialized with wide_bit_slots_remaining > 0")
-            slot = BitSlotState(
-                slot_type=self._state.stored_wide_bit.slot_type,
-                direction=self._state.stored_wide_bit.direction,
-                data=self._state.stored_wide_bit.data
-            )
-            self._advance_column()
-            return slot
-
-        slot = self._slot()
-        if slot.is_owned():
-            if self._config.BitWidth_REG > 0:
-                columns_remaining_in_window = self._config._HorizontalEnd - self._state.column
-                self._state.wide_bit_slots_remaining = min(
-                    self._config.BitWidth_REG,
-                    max(0, columns_remaining_in_window)
-                )
-                self._state.stored_wide_bit = slot
-            if not self._config.PortDirection_REG:
-                self._state.tails_left = self._config.TailWidth_REG
-                self._state.guard_left = self._config.GuardEnable_REG
-            self._advance_column()
-            return slot
-
-        if self._state.guard_left:
-            self._state.guard_left = False
-            slot = BitSlotState(
-                slot_type=SlotType.GUARD_1 if self._config.GuardPolarity_REG else SlotType.GUARD_0,
-                direction=DirectionType.SOURCE
-            )
-            self._advance_column()
-            return slot
-
-        if self._state.tails_left > 0:
-            self._state.tails_left -= 1
-            slot = BitSlotState(slot_type=SlotType.TAIL, direction=DirectionType.SOURCE)
-            self._advance_column()
-            return slot
-
-        self._advance_column()
-        return BitSlotState(slot_type=SlotType.EMPTY)
+        match self._emission_phase():
+            case EmissionPhase.WIDE_REPLAY:
+                replay = self._state.wide_replay
+                assert replay is not None  # invariant by construction
+                slot = replay.slot
+                replay.remaining -= 1
+                if replay.remaining == 0:
+                    self._state.wide_replay = None
+                self._advance_column()
+                return slot
+
+            case EmissionPhase.DATA_PROBE:
+                slot = self._slot()
+                if slot.is_owned():
+                    if self._config.BitWidth_REG > 0:
+                        remaining = min(
+                            self._config.BitWidth_REG,
+                            max(0, self._config._HorizontalEnd - self._state.column)
+                        )
+                        if remaining > 0:
+                            self._state.wide_replay = WideBitReplay(slot=slot, remaining=remaining)
+                    self._seed_post_data_queue()
+                    self._advance_column()
+                    return slot
+
+                if self._state.post_data_queue:
+                    slot_type = self._state.post_data_queue.popleft()
+                    self._advance_column()
+                    return BitSlotState(slot_type=slot_type, direction=DirectionType.SOURCE)
+
+                self._advance_column()
+                return BitSlotState(slot_type=SlotType.EMPTY)


 # =============================================================================
 # Main DataPort Class
 # =============================================================================

 class DataPort:
     ...

     def reset(self) -> None:
-        self._state.reset()
+        self._state.reset_frame()
         self.fcp.reset()
-        self._algorithm.reset_algorithm()
+        # Prime row 0 if it's already at Offset_REG (replaces reset_algorithm).
+        if self._state.current_row_in_interval == self.config.Offset_REG:
+            if not self._algorithm._check_skipping_at_offset():
+                self._algorithm.reset_transport()
```

---

## Summary — invariants enforced by construction after refactor

| # | Invariant | Before | After |
|---|---|---|---|
| I1 | `DONE ⇒ row_done` | Violated by skip path | Concept eliminated; skip sets PATTERN_DONE atomically |
| I2 | `wide_slots > 0 ⇔ stored != None` | Runtime `raise` | Single `Optional[WideBitReplay]` — structural |
| - | "`hc_done` is position-derivable" | Latched bool | `@property` on column |
| - | "IDLE + row_done is impossible" | Not prevented | Mutually exclusive enum values |
| - | "Emission order: guard before tails" | Implicit in if-chain | Explicit deque FIFO |
| - | "Sink ports emit no guards/tails" | Scattered `not PortDirection_REG` checks | Single gate in `_seed_post_data_queue` |
| - | "Each field has exactly one reset scope" | Overlapping reset sites | Scope table + one method per scope |

## Out of scope for this refactor

- The latent Normal-mode `Spacing_REG == 0` behavior (row_done set for non-final CGs) — preserved as-is via ROW_DONE mapping. Worth a follow-up issue.
- Test CSV updates — no schema changes, but any test that directly inspects `DataPortState.row_transport_done` or `transport_state` will need touching. (Search: `grep -rn "row_transport_done\|transport_state\|horizontal_count_done\|stored_wide_bit\|wide_bit_slots_remaining\|guard_left\|tails_left" test/` before applying.)
- `FlowControlPort` interactions — untouched; `fcp.reset_for_row / reset_for_interval / reset_drq_sent / reset` still called at the same program points.
