"""Flow Control Port (FCP) - independent bus source for DRQ + guards + tails.

Classes:
    FlowControlPortState:  Runtime state
    FlowControlPortConfig: Configuration and register values
    FlowControlPort:       Combines config, state, and algorithm

The FCP is a parallel state machine to the DataPort's data path. Both are
iterated by the engine; when both emit at the same column, the bus model's
existing SAME_DEVICE clash detector surfaces it as a configuration error.
No arbitration or priority logic lives in the core loop.

Active only in RX_CONTROLLED or ASYNC flow modes. Reads FlowMode_REG,
PortDirection_REG, and Interval_REG from the parent DataPort (DRQ direction
is inverted relative to the DP's data direction).
"""

from __future__ import annotations
from collections import deque
from typing import Optional, TYPE_CHECKING
from .bit_slot import BitSlotState
from .enums import SlotType, DirectionType

if TYPE_CHECKING:
    from .dataport import DataPort

class FlowControlPortState:
    """Runtime state for an FCP."""

    def __init__(self) -> None:
        # post_data_queue is reused across initializations so external holders
        # keep a stable reference; initialize() clears it in place.
        self.post_data_queue: deque[SlotType] = deque()
        self.initialize()

    def initialize(self) -> None:
        """Set state to idle-start values."""
        self.column: int = 0
        self.row_in_interval: int = 0
        self.drq_sent: bool = False
        self.wide_bit_remaining: int = 0
        self.stored_wide_bit_slot: Optional[BitSlotState] = None
        # Post-data emission state. Mirrors post_data_queue contents so the
        # engine can derive slot type without peeking at the queue.
        self.post_data_guard_pending: bool = False
        self.post_data_tail_remaining: int = 0
        self.post_data_queue.clear()

class FlowControlPortConfig:
    """Configuration and register state for an FCP."""

    def __init__(self) -> None:
        self.FCP_HorizontalStart_REG: int = 0
        self.FCP_BitWidth_REG: int = 0
        self.FCP_TailWidth_REG: int = 0
        self.FCP_Offset_REG: int = 0
        self.FCP_GuardEnable_REG: bool = False
        self.FCP_GuardPolarity_REG: bool = False

class FlowControlPort:
    """SWI3S Flow Control Port — config + state + algorithm.

    Emits DRQ + guards + tails as an independent peer of the DataPort. Both
    are iterated by the engine; collisions are surfaced by the bus model's
    SAME_DEVICE clash detector (no arbitration here).

    Public surface:
        config               configuration / register attributes
        initialize()         initialize before use
        fetch_bit_slot()     emit slot at current position (auto-advances)
    """

    def __init__(self, dataport: DataPort) -> None:
        # parent DP: reads FlowMode_REG, PortDirection_REG, Interval_REG, interval_skipped
        self._dataport = dataport
        self.config = FlowControlPortConfig()
        self.state = FlowControlPortState()

    def initialize(self) -> None:
        """Initialize before FCP use."""
        self.state.initialize()
        self._advance_interval()

    # ------------------------------------------------------------------
    # New action API (Phase 5 — surface only; implementations in Phase 6).
    # clock_tick() is the per-UI entry point. Engine consumes via
    # _derive_fcp_bit_slot() + clock_tick() (see engine.py).
    # ------------------------------------------------------------------

    def clock_tick(self) -> None:
        """Advance the FCP by one UI.

        Phase 5: thin wrapper over fetch_bit_slot() so the new entry-point
        name is available without behavior change. Phase 6 replaces this
        with cascade-driven action calls.
        """
        self.fetch_bit_slot()

    def write_drq(self) -> None:
        """Source-side FCP (parent DP is Sink): emit DRQ at UI 0 of a
        wide-bit period. Implementation in Phase 6."""
        raise NotImplementedError("Wired in Phase 6")

    def write_held_bit(self) -> None:
        """Re-emit the held bit (DRQ or TAIL) for subsequent UIs of a
        wide-bit period. Implementation in Phase 6."""
        raise NotImplementedError("Wired in Phase 6")

    def read_drq(self) -> None:
        """Sink-side FCP (parent DP is Source): sample DRQ at the last UI
        of a wide-bit period. Implementation in Phase 6."""
        raise NotImplementedError("Wired in Phase 6")

    def write_guard0(self) -> None:
        """Emit guard 0 (single UI, post-DRQ emission).
        Implementation in Phase 6."""
        raise NotImplementedError("Wired in Phase 6")

    def write_guard1(self) -> None:
        """Emit guard 1 (single UI, post-DRQ emission).
        Implementation in Phase 6."""
        raise NotImplementedError("Wired in Phase 6")

    def write_tail(self) -> None:
        """Emit tail at UI 0 of FCP_TailWidth_REG-wide period
        (post-DRQ emission). Implementation in Phase 6."""
        raise NotImplementedError("Wired in Phase 6")

    # ------------------------------------------------------------------
    # Legacy entry point — engine uses derive helper + clock_tick.
    # ------------------------------------------------------------------

    def fetch_bit_slot(self) -> BitSlotState:
        """Emit bit slot information at the current position and auto-advance."""
        slot = self._data_slot()

        if slot.is_owned():
            self._advance_column()
            return slot

        if self.state.post_data_queue:
            slot_type = self.state.post_data_queue.popleft()
            # Mirror queue mutation in derived state fields.
            if slot_type == SlotType.TAIL:
                self.state.post_data_tail_remaining -= 1
            else:
                # GUARD_0 or GUARD_1
                self.state.post_data_guard_pending = False
            self._advance_column()
            return BitSlotState(slot_type=slot_type, direction=DirectionType.SOURCE)

        self._advance_column()
        return BitSlotState(slot_type=SlotType.EMPTY)

    def _data_slot(self) -> BitSlotState:
        """Return the slot FCP emits at this column."""
        # Wide-bit replay: return the stored slot directly. Safe to share
        # the object — the engine overwrites row/column/device_num/dp_num
        # on every fetch, and the bus-model adapters copy fields into
        # fresh BitInfo records instead of storing the slot reference.
        if self.state.stored_wide_bit_slot is not None:
            slot = self.state.stored_wide_bit_slot
            self._advance_wide_bit()
            return slot

        dp_config = self._dataport.config
        if (dp_config._emits_drq
                and not self._dataport.interval_skipped
                and not self.state.drq_sent
                and self.state.row_in_interval == self.config.FCP_Offset_REG
                and self.state.column == self.config.FCP_HorizontalStart_REG):
            # DRQ direction is opposite to the DP's data direction.
            # Sink DP sends DRQ (SOURCE); Source DP receives DRQ (SINK).
            slot = BitSlotState(
                slot_type=SlotType.DRQ,
                direction=DirectionType.SOURCE if dp_config.PortDirection_REG else DirectionType.SINK,
            )
            self._arm_drq_replay(slot)
            return slot

        return BitSlotState(slot_type=SlotType.EMPTY)

    def _arm_drq_replay(self, slot: BitSlotState) -> None:
        """Latch fresh DRQ: mark sent, prime post-data queue, stash slot
        for wide-bit replay, advance one tick for this emission."""
        self.state.drq_sent = True
        self._prime_post_data_queue()
        self.state.wide_bit_remaining = self.config.FCP_BitWidth_REG
        self.state.stored_wide_bit_slot = slot
        self._advance_wide_bit()

    def _prime_post_data_queue(self) -> None:
        """Prime the post-DRQ queue (guard + tails) after a SOURCE DRQ."""
        self.state.post_data_queue.clear()
        self.state.post_data_guard_pending = False
        self.state.post_data_tail_remaining = 0
        if not self._dataport.config.PortDirection_REG:
            return
        if self.config.FCP_GuardEnable_REG:
            self.state.post_data_queue.append(
                SlotType.GUARD_1 if self.config.FCP_GuardPolarity_REG else SlotType.GUARD_0
            )
            self.state.post_data_guard_pending = True
        for _ in range(self.config.FCP_TailWidth_REG):
            self.state.post_data_queue.append(SlotType.TAIL)
        self.state.post_data_tail_remaining = self.config.FCP_TailWidth_REG

    def _advance_column(self) -> None:
        """Advance column; wrap to the next row at the right edge."""
        self.state.column += 1
        if self.state.column >= self._dataport._device._interface.num_columns:
            self._advance_row()

    def _advance_row(self) -> None:
        """Advance the row counter and prepare per-row state."""
        self.state.column = 0
        self.state.post_data_queue.clear()
        # Post-data emission doesn't survive row wraps.
        self.state.post_data_guard_pending = False
        self.state.post_data_tail_remaining = 0
        # Wide-bit replay doesn't survive row wraps.
        self.state.wide_bit_remaining = 0
        self.state.stored_wide_bit_slot = None
        self.state.row_in_interval += 1
        if self.state.row_in_interval > self._dataport.config.Interval_REG:
            self.state.row_in_interval = 0
            self._advance_interval()

    def _advance_interval(self) -> None:
        """Advance to the next interval: re-init transport-scope state."""
        self._reset_transport()

    def _reset_transport(self) -> None:
        """Re-init transport-scope state for a new transport pattern."""
        self.state.drq_sent = False
        self.state.wide_bit_remaining = 0
        self.state.stored_wide_bit_slot = None

    def _advance_wide_bit(self) -> None:
        """Next wide-bit replay tick; clear stored slot on exhaustion.

        Terminal (unlike DP's _advance_wide_bit which cascades to _advance_bit) —
        FCP's wide-bit is a one-shot replay, not part of a counter cascade.
        """
        self.state.wide_bit_remaining -= 1
        if self.state.wide_bit_remaining < 0:
            self.state.stored_wide_bit_slot = None
