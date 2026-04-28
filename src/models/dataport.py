"""DataPort configuration and state management.

Classes:
    DataPortState:  Runtime state
    DataPortConfig: Configuration and register values
    DataPort:       Combines config, state, and algorithm

`transport_phase: TransportPhase` tracks the transport lifecycle:
    PENDING      Awaiting first read/write in the interval
    ACTIVE       Inside the transport window
    SPACING      Channel group spacing / SRI inter-transport spacing
    ROW_DONE     Row's window done; transport still alive across row wrap
    PATTERN_DONE Transport complete or interval skipped

Normal vs SRI:
    - Normal: One transport per SSP interval; channel groups
      structure the burst/space pattern within the transport.
    - SRI:    Multiple transports per row.

Counter cascade:
    wide_bit → bit_in_channel → channel → sample → channel_group → transport completion
"""

from __future__ import annotations
from typing import TYPE_CHECKING
from .enums import FlowMode, TransportPhase

if TYPE_CHECKING:
    from .device import Device

class DataPortState:
    """DataPort State."""

    def __init__(self, config: DataPortConfig) -> None:
        self.initialize(config)

    def initialize(self, config: DataPortConfig) -> None:
        """Initialize state."""
        self.column: int = 0
        self.row_in_interval: int = 0
        self.interval_skipped: bool = False
        self.skipping_accumulator: int = 0
        self.guard_pending: bool = False
        self.tail_remaining: int = 0
        self.initialize_transport(config)

    def initialize_transport(self, config: DataPortConfig) -> None:
        """Initialize transport state for a new transport pattern."""
        self.transport_phase: TransportPhase = TransportPhase.PENDING
        self.spacing_slots_remaining: int = 0
        self.sample_in_group: int = 0
        self.samples_in_group_remaining: int = config.SampleGrouping_REG
        self.channel_group_base_channel: int = 0
        self.channel_index: int = 0
        self.channels_in_group_remaining: int = config._effective_channel_grouping - 1
        self.bit_in_channel: int = config.SampleSize_REG
        self.wide_bit_remaining: int = config.BitWidth_REG
        self.txp_pending: bool = config._txp_enabled

class DataPortConfig:
    """Configuration and register state for a DataPort."""

    EnableCh_REG: int
    ChannelGrouping_REG: int
    Spacing_REG: int
    SampleSize_REG: int
    SampleGrouping_REG: int
    Interval_REG: int
    SkippingNumerator_REG: int
    Offset_REG: int
    HorizontalStart_REG: int
    HorizontalCount_REG: int
    TailWidth_REG: int
    BitWidth_REG: int
    PortDirection_REG: bool
    GuardEnable_REG: bool
    GuardPolarity_REG: bool
    SubRowInterval_REG: bool
    FlowMode_REG: int
    PortMode_REG: int
    ScramblerEn_REG: bool

    @property
    def _num_channels(self) -> int:
        """Count of enabled channels."""
        return bin(self.EnableCh_REG).count('1')

    @property
    def _horizontal_end(self) -> int:
        """Last column of the horizontal window."""
        return self.HorizontalStart_REG + self.HorizontalCount_REG

    @property
    def _is_source(self) -> bool:
        """True if this is a source DP"""
        return not self.PortDirection_REG

    @property
    def _txp_enabled(self) -> bool:
        """True if FlowMode activates TX_PRESENT bit."""
        return self.FlowMode_REG in (FlowMode.TX_CONTROLLED, FlowMode.ASYNC)

    @property
    def _drq_enabled(self) -> bool:
        """True if FlowMode activates the FCP's DRQ path (RX_CONTROLLED or ASYNC)."""
        return self.FlowMode_REG in (FlowMode.RX_CONTROLLED, FlowMode.ASYNC)

    @property
    def _effective_channel_grouping(self) -> int:
        """Channels per group."""
        if self.ChannelGrouping_REG == 0 or self.ChannelGrouping_REG > self._num_channels:
            return self._num_channels
        return self.ChannelGrouping_REG

class DataPort:
    """SWI3S Data Port — config + state + algorithm.  
        initialize()         initialize before use
        clock_tick()         advance one UI; engine derives BitSlotState from state
    """

    state: DataPortState  # created in initialize()

    def __init__(self, device: 'Device', dp_index: int) -> None:
        self._device = device
        self.dp_index = dp_index
        self.config = DataPortConfig()

    def initialize(self) -> None:
        """Initialize before dataport use."""
        self.state = DataPortState(self.config)
        self._start_interval()

    def clock_tick(self) -> None:
        """Advance the DataPort by one UI.

        Three paths:
          1. In transport window and inside the horizontal window on an owned slot —
             write or read the bus, advance the cascade, arm guard/tail, advance column, return.
          2. In transport window but window-exhausted or in SPACING — update phase /
             spacing counter, then fall through to (3).
          3. Not owned — pop guard and tail armed by a prior owned
             source slot, then advance column.
        """
        in_transport_window = (
            self.config._num_channels > 0
            and not self.state.interval_skipped
            and self.state.transport_phase not in (TransportPhase.ROW_DONE, TransportPhase.PATTERN_DONE)
            and self.state.row_in_interval >= self.config.Offset_REG
            and self.state.column >= self.config.HorizontalStart_REG
        )

        if in_transport_window:
            if self.state.column > self.config._horizontal_end:
                self.state.transport_phase = TransportPhase.ROW_DONE
            elif self.state.transport_phase == TransportPhase.SPACING:
                self.state.spacing_slots_remaining -= 1
                if self.state.spacing_slots_remaining == 0:
                    self.state.transport_phase = TransportPhase.ACTIVE
            else:
                self.state.transport_phase = TransportPhase.ACTIVE
                if self.config._is_source:
                    if self.state.wide_bit_remaining == self.config.BitWidth_REG:
                        if self.state.txp_pending:
                            self._device.write_txp()
                        else:
                            self._device.write_data_bit_from_fifo()
                    else:
                        self._device.held_write_bit()
                else:
                    if self.state.wide_bit_remaining == 0:
                        if self.state.txp_pending:
                            self._device.read_txp()
                        else:
                            self._device.read_data_bit_to_fifo()

                self._advance_wide_bit()
                self._arm_guard_tail()
                self._advance_column()
                return

        self._pop_guard_tail()
        self._advance_column()
    
    def _start_interval(self) -> None:
        """Start the next interval."""
        self.state.interval_skipped = self._advance_skipping_accumulator()
        self.state.initialize_transport(self.config)

    def _advance_skipping_accumulator(self) -> bool:
        """Advance skipping accumulator. Returns True iff interval should be skipped."""
        if self.config.SkippingNumerator_REG == 0:
            return False
        self.state.skipping_accumulator += self.config.SkippingNumerator_REG
        if self.state.skipping_accumulator < self._device.SkippingDenominator_REG:
            return False
        self.state.skipping_accumulator -= self._device.SkippingDenominator_REG
        return True

    def _advance_column(self) -> None:
        """Next column; cascades to _advance_row."""
        self.state.column += 1
        if self.state.column >= self._device.num_columns:
            self._advance_row()

    def _advance_row(self) -> None:
        """Next row; cascades to _start_interval."""
        self.state.column = 0
        self.state.guard_pending = False
        self.state.tail_remaining = 0

        if self.state.transport_phase == TransportPhase.ROW_DONE:
            self.state.transport_phase = TransportPhase.ACTIVE

        self.state.row_in_interval += 1

        if self.state.row_in_interval > self.config.Interval_REG:
            self.state.row_in_interval = 0
            self._start_interval()

    def _advance_wide_bit(self) -> None:
        """Next wide-bit UI; cascades to _advance_bit_in_channel."""
        self.state.wide_bit_remaining -= 1
        if self.state.wide_bit_remaining < 0:
            self.state.wide_bit_remaining = self.config.BitWidth_REG
            self._advance_bit_in_channel()

    def _advance_bit_in_channel(self) -> None:
        """Next bit; cascades to _advance_channel."""
        if self.state.txp_pending:
            self.state.txp_pending = False
            return
        self.state.bit_in_channel -= 1
        if self.state.bit_in_channel < 0:
            self.state.bit_in_channel = self.config.SampleSize_REG
            self._advance_channel()

    def _advance_channel(self) -> None:
        """Next channel; cascades to _advance_sample."""
        self.state.channel_index += 1
        self.state.channels_in_group_remaining -= 1
        self.state.txp_pending = self.config._txp_enabled
        if self.state.channels_in_group_remaining < 0:
            self.state.channel_index = self.state.channel_group_base_channel
            self.state.channels_in_group_remaining = self.config._effective_channel_grouping - 1
            self._advance_sample()

    def _advance_sample(self) -> None:
        """Next sample; cascades to _advance_channel_group."""
        self.state.sample_in_group += 1
        self.state.samples_in_group_remaining -= 1
        if self.state.samples_in_group_remaining < 0:
            self.state.sample_in_group = 0
            self.state.samples_in_group_remaining = self.config.SampleGrouping_REG
            self._advance_channel_group()

    def _advance_channel_group(self) -> None:
        """Next channel group (or next transport in SRI)."""
        transport_pattern_complete = (self.state.channel_group_base_channel + self.config._effective_channel_grouping
                            >= self.config._num_channels)

        if transport_pattern_complete:
            if self.config.SubRowInterval_REG:
                self.state.initialize_transport(self.config)
            else:
                self.state.transport_phase = TransportPhase.PATTERN_DONE
                return
        else:
            self.state.channel_group_base_channel += self.config._effective_channel_grouping
            remaining_channels = self.config._num_channels - self.state.channel_group_base_channel
            if remaining_channels > self.config._effective_channel_grouping:
                remaining_channels = self.config._effective_channel_grouping
            self.state.channels_in_group_remaining = remaining_channels - 1
            self.state.channel_index = self.state.channel_group_base_channel

        if (not transport_pattern_complete) or self.config.SubRowInterval_REG:
            if self.config.Spacing_REG != 0:
                self.state.spacing_slots_remaining = self.config.Spacing_REG - 1
                if self.config.Spacing_REG > 1:
                    self.state.transport_phase = TransportPhase.SPACING
                else:
                    self.state.transport_phase = TransportPhase.ACTIVE
            else:
                self.state.transport_phase = TransportPhase.ROW_DONE

    def _pop_guard_tail(self) -> None:
        """Pop guard/tail slots."""
        if self.state.guard_pending:
            if self.config.GuardPolarity_REG:
                self._device.write_guard1()
            else:
                self._device.write_guard0()
            self.state.guard_pending = False
        elif self.state.tail_remaining > 0:
            if self.state.tail_remaining == self.config.TailWidth_REG:
                self._device.write_tail()
            else:
                self._device.held_write_bit()
            self.state.tail_remaining -= 1

    def _arm_guard_tail(self) -> None:
        """Arm guard/tail slots."""
        self.state.guard_pending = False
        self.state.tail_remaining = 0
        if not self.config._is_source:
            return
        if self.config.GuardEnable_REG:
            self.state.guard_pending = True
        self.state.tail_remaining = self.config.TailWidth_REG