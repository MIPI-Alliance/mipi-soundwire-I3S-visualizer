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
from typing import Optional, TYPE_CHECKING
from .bit_slot import BitSlotState
from .enums import SlotType, DirectionType

if TYPE_CHECKING:
    from .dataport import DataPort

class FlowControlPortState:
    """Runtime state for an FCP."""

    def __init__(self) -> None:
        self.initialize()

    def initialize(self) -> None:
        """Set state to idle-start values."""
        self.column: int = 0
        self.row_in_interval: int = 0
        self.drq_sent: bool = False
        self.wide_bit_remaining: int = 0
        self.stored_wide_bit_slot: Optional[BitSlotState] = None
        # Post-data emission state: optional guard then tail bits, drained
        # by clock_tick after a fresh DRQ.
        self.guard_pending: bool = False
        self.tail_remaining: int = 0

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
        clock_tick()         advance one UI; engine derives BitSlotState from state
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

    def clock_tick(self) -> None:
        """Advance the FCP by one UI.

        Self-contained UI advance — engine consumes BitSlotState via the
        engine-side _derive_fcp_bit_slot helper (see core/engine.py).
        """
        state = self.state

        # Wide-bit replay: a prior DRQ holds the bus for BitWidth_REG more UIs.
        if state.stored_wide_bit_slot is not None:
            self._advance_wide_bit()
            self._advance_column()
            return

        dp_config = self._dataport.config
        if (dp_config._drq_enabled
                and not self._dataport.interval_skipped
                and not state.drq_sent
                and state.row_in_interval == self.config.FCP_Offset_REG
                and state.column == self.config.FCP_HorizontalStart_REG):
            # Fresh DRQ. Build a slot to stash for replay (engine will see
            # this stored slot on subsequent replay UIs via the derive
            # helper). DRQ direction is opposite to the DP's data direction:
            # Sink DP sends DRQ (SOURCE); Source DP receives DRQ (SINK).
            slot = BitSlotState(
                slot_type=SlotType.DRQ,
                direction=DirectionType.SINK if dp_config._is_source else DirectionType.SOURCE,
            )
            self._arm_drq_replay(slot)
            self._advance_column()
            return

        # Post-DRQ drain (Source-DRQ only — fields stay at defaults otherwise).
        if state.guard_pending:
            state.guard_pending = False
        elif state.tail_remaining > 0:
            state.tail_remaining -= 1
        self._advance_column()

    def _arm_drq_replay(self, slot: BitSlotState) -> None:
        """Latch fresh DRQ: mark sent, prime post-data state, stash slot
        for wide-bit replay, advance one tick for this emission."""
        self.state.drq_sent = True
        self._prime_post_data()
        self.state.wide_bit_remaining = self.config.FCP_BitWidth_REG
        self.state.stored_wide_bit_slot = slot
        self._advance_wide_bit()

    def _prime_post_data(self) -> None:
        """Prime post-DRQ emission state (guard + tails) after a SOURCE DRQ.
        DRQ is SOURCE iff DP is SINK (DRQ direction is opposite to data direction)."""
        self.state.guard_pending = False
        self.state.tail_remaining = 0
        if self._dataport.config._is_source:
            return
        if self.config.FCP_GuardEnable_REG:
            self.state.guard_pending = True
        self.state.tail_remaining = self.config.FCP_TailWidth_REG

    def _advance_column(self) -> None:
        """Advance column; wrap to the next row at the right edge."""
        self.state.column += 1
        if self.state.column >= self._dataport._device.num_columns:
            self._advance_row()

    def _advance_row(self) -> None:
        """Advance the row counter and prepare per-row state."""
        self.state.column = 0
        # Post-data emission doesn't survive row wraps.
        self.state.guard_pending = False
        self.state.tail_remaining = 0
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

        Terminal (unlike DP's _advance_wide_bit which cascades to _advance_bit_in_channel) —
        FCP's wide-bit is a one-shot replay, not part of a counter cascade.
        """
        self.state.wide_bit_remaining -= 1
        if self.state.wide_bit_remaining < 0:
            self.state.stored_wide_bit_slot = None
