"""Flow Control Port (FCP) configuration and state management.

Classes:
    FlowControlPortState:  Runtime state
    FlowControlPortConfig: Configuration and register values
    FlowControlPort:       Combines config, state, and algorithm
"""

from __future__ import annotations
from typing import TYPE_CHECKING

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
        """Advance the FCP by one UI."""
        state = self.state
        dp_config = self._dataport.config
        device = self._dataport._device
        drq_is_source = not dp_config._is_source   # DP Sink → DRQ Source

        if state.drq_sent and state.wide_bit_remaining >= 0:
            if drq_is_source:
                device.write_held_bit()
            elif state.wide_bit_remaining == 0:    # last UI of wide bit
                device.read_drq()
            self._advance_wide_bit()
            self._advance_column()
            return

        if (dp_config._drq_enabled
                and not self._dataport.interval_skipped
                and not state.drq_sent
                and state.row_in_interval == self.config.FCP_Offset_REG
                and state.column == self.config.FCP_HorizontalStart_REG):
            if drq_is_source:
                device.write_drq()                 
            elif self.config.FCP_BitWidth_REG == 0:
                device.read_drq()
            self._arm_drq_replay()
            self._advance_column()
            return

        if state.guard_pending:
            state.guard_pending = False
        elif state.tail_remaining > 0:
            state.tail_remaining -= 1
        self._advance_column()

    def _arm_drq_replay(self) -> None:
        """Latch fresh DRQ: mark sent, arm guards/tails, set wide-bit counter, advance."""
        self.state.drq_sent = True
        self._arm_guards_tails()
        self.state.wide_bit_remaining = self.config.FCP_BitWidth_REG
        self._advance_wide_bit()

    def _arm_guards_tails(self) -> None:
        """Arm post-DRQ state (guard + tails) after a SOURCE DRQ."""
        self.state.guard_pending = False
        self.state.tail_remaining = 0
        if self._dataport.config._is_source:
            return
        if self.config.FCP_GuardEnable_REG:
            self.state.guard_pending = True
        self.state.tail_remaining = self.config.FCP_TailWidth_REG

    def _advance_column(self) -> None:
        """Advance column."""
        self.state.column += 1
        if self.state.column >= self._dataport._device.num_columns:
            self._advance_row()

    def _advance_row(self) -> None:
        """Advance the row counter."""
        self.state.column = 0
        self.state.guard_pending = False
        self.state.tail_remaining = 0
        # Force-exit any in-progress DRQ replay: wide-bit replay doesn't survive row wraps.
        self.state.wide_bit_remaining = -1
        self.state.row_in_interval += 1
        if self.state.row_in_interval > self._dataport.config.Interval_REG:
            self.state.row_in_interval = 0
            self._advance_interval()

    def _advance_interval(self) -> None:
        """Advance to the next interval"""
        self._reset_transport()

    def _reset_transport(self) -> None:
        """Re-init for a new transport pattern."""
        self.state.drq_sent = False
        self.state.wide_bit_remaining = 0

    def _advance_wide_bit(self) -> None:
        """Next wide-bit replay tick. Terminal — FCP's wide-bit is a one-shot replay,
        not part of a counter cascade (unlike DP's _advance_wide_bit).
        When wide_bit_remaining drops below 0, the replay sentinel
        (drq_sent and wide_bit_remaining >= 0) naturally fails on next tick."""
        self.state.wide_bit_remaining -= 1