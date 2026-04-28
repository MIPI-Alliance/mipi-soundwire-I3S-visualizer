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
    """Flow Control Port State."""

    def __init__(self) -> None:
        self.initialize()

    def initialize(self) -> None:
        """Set state to idle-start values."""
        self.column: int = 0
        self.row_in_interval: int = 0
        self.guard_pending: bool = False
        self.tail_remaining: int = 0
        self.initialize_transport()

    def initialize_transport(self) -> None:
        """Re-init transport-lifecycle state for a new transport pattern."""
        self.drq_sent: bool = False
        self.wide_bit_remaining: int = 0

class FlowControlPortConfig:
    """Configuration and register state for an Flow Control Port."""

    def __init__(self) -> None:
        self.FCP_HorizontalStart_REG: int = 0
        self.FCP_BitWidth_REG: int = 0
        self.FCP_TailWidth_REG: int = 0
        self.FCP_Offset_REG: int = 0
        self.FCP_GuardEnable_REG: bool = False
        self.FCP_GuardPolarity_REG: bool = False

class FlowControlPort:
    """SWI3S Flow Control Port — config + state + algorithm.
        initialize()         initialize before use
        clock_tick()         advance one UI; engine derives BitSlotState from state
    """

    def __init__(self, dataport: DataPort) -> None:
        self._dataport = dataport
        self.config = FlowControlPortConfig()
        self.state = FlowControlPortState()

    def initialize(self) -> None:
        """Initialize before FCP use."""
        self.state.initialize()
        self._start_interval()

    def clock_tick(self) -> None:
        """Advance the Flow Control Port by one UI."""
        state = self.state
        dp_config = self._dataport.config
        device = self._dataport._device
        drq_is_source = not dp_config._is_source

        if state.drq_sent and state.wide_bit_remaining >= 0:
            if drq_is_source:
                device.held_write_bit()
            elif state.wide_bit_remaining == 0:
                device.read_drq()
            self._advance_wide_bit()
            self._advance_column()
            return

        if (dp_config._drq_enabled
                and not self._dataport.state.interval_skipped
                and not state.drq_sent
                and state.row_in_interval == self.config.FCP_Offset_REG
                and state.column == self.config.FCP_HorizontalStart_REG):
            if drq_is_source:
                device.write_drq()
            elif self.config.FCP_BitWidth_REG == 0:
                device.read_drq()
            self._arm_drq_repeat()
            self._advance_column()
            return

        if state.guard_pending:
            if self.config.FCP_GuardPolarity_REG:
                device.write_guard1()
            else:
                device.write_guard0()
            state.guard_pending = False
        elif state.tail_remaining > 0:
            if state.tail_remaining == self.config.FCP_TailWidth_REG:
                device.write_tail()
            else:
                device.held_write_bit()
            state.tail_remaining -= 1
        self._advance_column()

    def _start_interval(self) -> None:
        """Start the next interval."""
        self.state.initialize_transport()

    def _advance_column(self) -> None:
        """Next column; cascades to _advance_row."""
        self.state.column += 1
        if self.state.column >= self._dataport._device.num_columns:
            self._advance_row()

    def _advance_row(self) -> None:
        """Next row; cascades to _start_interval."""
        self.state.column = 0
        self.state.guard_pending = False
        self.state.tail_remaining = 0
        self.state.wide_bit_remaining = -1
        self.state.row_in_interval += 1
        if self.state.row_in_interval > self._dataport.config.Interval_REG:
            self.state.row_in_interval = 0
            self._start_interval()

    def _advance_wide_bit(self) -> None:
        """Next wide-bit UI; cascades to _advance_bit_in_channel."""
        self.state.wide_bit_remaining -= 1

    def _arm_drq_repeat(self) -> None:
        """Latch fresh DRQ."""
        self.state.drq_sent = True
        self._arm_guard_tail()
        self.state.wide_bit_remaining = self.config.FCP_BitWidth_REG
        self._advance_wide_bit()

    def _arm_guard_tail(self) -> None:
        """Arm guard/tail slots."""
        self.state.guard_pending = False
        self.state.tail_remaining = 0
        if self._dataport.config._is_source:
            return
        if self.config.FCP_GuardEnable_REG:
            self.state.guard_pending = True
        self.state.tail_remaining = self.config.FCP_TailWidth_REG