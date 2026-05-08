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
        return self._num_channels if self.ChannelGrouping_REG == 0 else self.ChannelGrouping_REG

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
        cfg, s = self.config, self.state
        in_transport_window = (
            cfg._num_channels > 0
            and not s.interval_skipped
            and s.transport_phase not in (TransportPhase.ROW_DONE, TransportPhase.PATTERN_DONE)
            and s.row_in_interval >= cfg.Offset_REG
            and s.column >= cfg.HorizontalStart_REG
        )

        if in_transport_window:
            if s.column > cfg._horizontal_end:
                s.transport_phase = TransportPhase.ROW_DONE
            elif s.transport_phase == TransportPhase.SPACING:
                s.spacing_slots_remaining -= 1
                if s.spacing_slots_remaining == 0:
                    s.transport_phase = TransportPhase.ACTIVE
            else:
                s.transport_phase = TransportPhase.ACTIVE
                if cfg._is_source:
                    if s.wide_bit_remaining == cfg.BitWidth_REG:
                        if s.txp_pending:
                            self._device.write_txp()
                        else:
                            self._device.write_data_bit_from_fifo()
                    else:
                        self._device.held_write_bit()
                else:
                    if s.wide_bit_remaining == 0:
                        if s.txp_pending:
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
        cfg, s = self.config, self.state
        if cfg.SkippingNumerator_REG == 0:
            return False
        s.skipping_accumulator += cfg.SkippingNumerator_REG
        if s.skipping_accumulator < self._device.SkippingDenominator_REG:
            return False
        s.skipping_accumulator -= self._device.SkippingDenominator_REG
        return True

    def _advance_column(self) -> None:
        """Next column; cascades to _advance_row."""
        self.state.column += 1
        if self.state.column >= self._device.num_columns:
            self._advance_row()

    def _advance_row(self) -> None:
        """Next row; cascades to _start_interval."""
        s = self.state
        s.column = 0
        s.guard_pending = False
        s.tail_remaining = 0

        if s.transport_phase == TransportPhase.ROW_DONE:
            s.transport_phase = TransportPhase.ACTIVE

        s.row_in_interval += 1

        if s.row_in_interval > self.config.Interval_REG:
            s.row_in_interval = 0
            self._start_interval()

    def _advance_wide_bit(self) -> None:
        """Next wide-bit UI; cascades to _advance_bit_in_channel."""
        s = self.state
        if s.wide_bit_remaining == 0:
            s.wide_bit_remaining = self.config.BitWidth_REG
            self._advance_bit_in_channel()
        else:
            s.wide_bit_remaining -= 1

    def _advance_bit_in_channel(self) -> None:
        """Next bit; cascades to _advance_channel."""
        s = self.state
        if s.txp_pending:
            s.txp_pending = False
            return
        if s.bit_in_channel == 0:
            s.bit_in_channel = self.config.SampleSize_REG
            self._advance_channel()
        else:
            s.bit_in_channel -= 1

    def _advance_channel(self) -> None:
        """Next channel; cascades to _advance_sample."""
        cfg, s = self.config, self.state
        s.txp_pending = cfg._txp_enabled
        if s.channels_in_group_remaining == 0:
            s.channel_index = s.channel_group_base_channel
            s.channels_in_group_remaining = cfg._effective_channel_grouping - 1
            self._advance_sample()
        else:
            s.channel_index += 1
            s.channels_in_group_remaining -= 1

    def _advance_sample(self) -> None:
        """Next sample; cascades to _advance_channel_group."""
        cfg, s = self.config, self.state
        if s.samples_in_group_remaining == 0:
            s.sample_in_group = 0
            s.samples_in_group_remaining = cfg.SampleGrouping_REG
            self._advance_channel_group()
        else:
            s.sample_in_group += 1
            s.samples_in_group_remaining -= 1

    def _advance_channel_group(self) -> None:
        """Next channel group (or next transport in SRI)."""
        cfg, s = self.config, self.state
        transport_pattern_complete = (s.channel_group_base_channel + cfg._effective_channel_grouping >= cfg._num_channels)

        if transport_pattern_complete:
            if cfg.SubRowInterval_REG:
                s.initialize_transport(cfg)
            else:
                s.transport_phase = TransportPhase.PATTERN_DONE
                return
        else:
            s.channel_group_base_channel += cfg._effective_channel_grouping
            remaining_channels = cfg._num_channels - s.channel_group_base_channel
            if remaining_channels > cfg._effective_channel_grouping:
                remaining_channels = cfg._effective_channel_grouping
            s.channels_in_group_remaining = remaining_channels - 1
            s.channel_index = s.channel_group_base_channel

        if cfg.Spacing_REG != 0:
            s.spacing_slots_remaining = cfg.Spacing_REG - 1
            if cfg.Spacing_REG > 1:
                s.transport_phase = TransportPhase.SPACING
            else:
                s.transport_phase = TransportPhase.ACTIVE
        else:
            s.transport_phase = TransportPhase.ROW_DONE

    def _pop_guard_tail(self) -> None:
        """Pop guard/tail slots."""
        cfg, s = self.config, self.state
        if s.guard_pending:
            if cfg.GuardPolarity_REG:
                self._device.write_guard1()
            else:
                self._device.write_guard0()
            s.guard_pending = False
        elif s.tail_remaining > 0:
            if s.tail_remaining == cfg.TailWidth_REG:
                self._device.write_tail()
            else:
                self._device.held_write_bit()
            s.tail_remaining -= 1

    def _arm_guard_tail(self) -> None:
        """Arm guard/tail slots."""
        cfg, s = self.config, self.state
        s.guard_pending = False
        s.tail_remaining = 0
        if not cfg._is_source:
            return
        if cfg.GuardEnable_REG:
            s.guard_pending = True
        s.tail_remaining = cfg.TailWidth_REG