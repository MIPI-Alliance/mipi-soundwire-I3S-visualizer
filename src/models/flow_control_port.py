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

NOTE: This module must remain UI-independent - no tkinter, widgets, dialogs,
or UI framework imports. See CLAUDE.md for policy.
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
        """Set state to idle-start values.

        No config is needed: the FCP starts dormant (row_in_interval=0,
        drq_sent=False, no pending guards/tails/replay) and waits for its
        row/column counters to reach the DRQ trigger condition.
        """
        self.column: int = 0
        self.row_in_interval: int = 0
        self.drq_sent: bool = False
        self.wide_bit_remaining: int = 0
        self.stored_wide_bit_slot: Optional[BitSlotState] = None
        self.post_data_queue.clear()


class FlowControlPortConfig:
    """Configuration and register state for an FCP.

    Holds the six FCP-specific registers. Names drop the FCP_ prefix because
    they live in their own namespace (accessed via `dp.fcp.config.*`).
    """

    def __init__(self) -> None:
        self.HorizontalStart_REG: int = 0
        self.BitWidth_REG: int = 0
        self.TailWidth_REG: int = 0
        self.Offset_REG: int = 0
        self.GuardEnable_REG: bool = False
        self.GuardPolarity_REG: bool = False


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
        self._dataport = dataport  # parent DP: reads FlowMode_REG,
                                   # PortDirection_REG, Interval_REG
        self.config = FlowControlPortConfig()
        self._state = FlowControlPortState()

    def initialize(self) -> None:
        """Initialize before FCP use."""
        self._state.initialize()

    def fetch_bit_slot(self) -> BitSlotState:
        """Emit bit slot information at the current position and auto-advance."""
        slot = self._data_slot()

        if slot.is_owned():
            self._advance_column()
            return slot

        if self._state.post_data_queue:
            slot_type = self._state.post_data_queue.popleft()
            self._advance_column()
            return BitSlotState(slot_type=slot_type, direction=DirectionType.SOURCE)

        self._advance_column()
        return BitSlotState(slot_type=SlotType.EMPTY)

    def _data_slot(self) -> BitSlotState:
        """Return the slot FCP emits at this column.

        Priority order:
            1. Wide-bit replay (a prior DRQ with BitWidth_REG > 0)
            2. DRQ trigger at (Offset_REG row, HorizontalStart_REG column)
               in RX_CONTROLLED / ASYNC flow modes
            3. EMPTY

        Post-DRQ guards/tails drain from post_data_queue in fetch_bit_slot
        (mirrors DataPort's pattern).
        """
        # Wide-bit replay: return the stored slot directly. Safe to share
        # the object — the engine overwrites row/column/device_num/dp_num
        # on every fetch, and the bus-model adapters copy fields into
        # fresh BitInfo records instead of storing the slot reference.
        if self._state.stored_wide_bit_slot is not None:
            slot = self._state.stored_wide_bit_slot
            self._advance_wide_bit()
            return slot

        dp_config = self._dataport.config
        if (dp_config._emits_drq
                and not self._state.drq_sent
                and self._state.row_in_interval == self.config.Offset_REG
                and self._state.column == self.config.HorizontalStart_REG):
            # DRQ direction is opposite to the DP's data direction.
            # Sink DP sends DRQ (SOURCE); Source DP receives DRQ (SINK).
            slot = BitSlotState(
                slot_type=SlotType.DRQ,
                direction=DirectionType.SOURCE if dp_config.PortDirection_REG else DirectionType.SINK,
            )
            self._state.drq_sent = True

            # Unlike DP (which primes its post-data queue from fetch_bit_slot
            # after every owned slot), FCP primes here on fresh DRQ only —
            # wide-bit replays are continuations of the same DRQ and must NOT
            # re-prime.
            self._prime_post_data_queue()

            # Prime wide-bit replay for this DRQ, then advance once for this
            # emission. BitWidth_REG == 0 clears stored on the same call, so
            # no separate guard is needed.
            self._state.wide_bit_remaining = self.config.BitWidth_REG
            self._state.stored_wide_bit_slot = slot
            self._advance_wide_bit()
            return slot

        return BitSlotState(slot_type=SlotType.EMPTY)

    def _prime_post_data_queue(self) -> None:
        """Prime the post-DRQ queue (guard + tails) after a SOURCE DRQ.

        Mirror image of DataPort._prime_post_data_queue: DP primes when the
        port is Source (PortDirection_REG=False); FCP primes when the DRQ is
        Source, which happens when the parent DP is Sink (PortDirection_REG=True).
        """
        self._state.post_data_queue.clear()
        if not self._dataport.config.PortDirection_REG:
            return
        if self.config.GuardEnable_REG:
            self._state.post_data_queue.append(
                SlotType.GUARD_1 if self.config.GuardPolarity_REG else SlotType.GUARD_0
            )
        for _ in range(self.config.TailWidth_REG):
            self._state.post_data_queue.append(SlotType.TAIL)

    def _advance_column(self) -> None:
        """Advance column; wrap to the next row at the right edge."""
        self._state.column += 1
        if self._state.column >= self._dataport._device._interface.num_columns:
            self._advance_row()

    def _advance_row(self) -> None:
        """Advance the row counter and prepare per-row state."""
        self._state.column = 0
        self._state.post_data_queue.clear()
        # Wide-bit replay doesn't survive row wraps.
        self._state.wide_bit_remaining = 0
        self._state.stored_wide_bit_slot = None
        self._state.row_in_interval += 1
        if self._state.row_in_interval > self._dataport.config.Interval_REG:
            self._state.row_in_interval = 0
            self._advance_interval()

    def _advance_interval(self) -> None:
        """Advance to the next interval: clear the DRQ latch."""
        self._state.drq_sent = False

    def _advance_wide_bit(self) -> None:
        """Next wide-bit replay tick; clear stored slot on exhaustion.

        Terminal (unlike DP's _advance_wide_bit which cascades to _advance_bit) —
        FCP's wide-bit is a one-shot replay, not part of a counter cascade.
        """
        self._state.wide_bit_remaining -= 1
        if self._state.wide_bit_remaining < 0:
            self._state.stored_wide_bit_slot = None
