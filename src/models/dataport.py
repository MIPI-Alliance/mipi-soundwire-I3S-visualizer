"""DataPort configuration and state management.

Classes:
    DataPortState:  Runtime state
    DataPortConfig: Configuration and register values
    DataPort:       Combines config, state, and algorithm

`transport_phase: TransportPhase` tracks the transport lifecycle:
    ACTIVE       Emitting inside the horizontal window
    SPACING      Inter-channel-group / SRI inter-transport gap
    ROW_DONE     Row's window exhausted; transport still alive across wrap
    PATTERN_DONE Transport complete or interval skipped

Normal vs SRI:
    - Normal: One transport per SSP interval; channel groups
      structure the burst/space pattern within the transport.
    - SRI:    Multiple transports per row.

Counter cascade:
    wide_bit → bit_in_channel → channel → sample → channel_group → transport completion
"""

from __future__ import annotations
from typing import Optional, TYPE_CHECKING
from .enums import FlowMode, TransportPhase

if TYPE_CHECKING:
    from .device import Device

class DataPortState:
    """Runtime state for a DataPort."""

    def __init__(self, config: DataPortConfig) -> None:
        self.initialize(config)

    def initialize(self, config: DataPortConfig) -> None:
        """Set state to the current config."""
        self.column: int = 0
        self.row_in_interval: int = 0
        self.transport_phase: TransportPhase = TransportPhase.PATTERN_DONE
        self.interval_skipped: bool = False
        self.skipping_accumulator: int = 0
        self.sample_in_group: int = 0
        self.samples_in_group_remaining: int = config.SampleGrouping_REG
        self.channel_index: int = 0
        self.spacing_slots_remaining: int = 0
        self.channel_group_base_channel: int = 0
        self.channels_in_group_remaining: int = 0
        self.bit_in_channel: int = config.SampleSize_REG
        self.wide_bit_remaining: int = config.BitWidth_REG
        # Post-data emission state for source ports: optional guard then
        # tail bits, drained by clock_tick after each owned data slot.
        self.post_data_guard_pending: bool = False
        self.post_data_tail_remaining: int = 0
        self.txp_pending: bool = config._emits_txp

class DataPortConfig:
    """Configuration and register state for a DataPort."""

    def __init__(self) -> None:
        self._EnableCh_REG: int = 0  # 16-bit bitmask for enabled channels
        # Cached (enabled_channels, num_channels). None = dirty.
        self._channel_cache: Optional[tuple[tuple[int, ...], int]] = None
        self.ChannelGrouping_REG: int = 0
        self.Spacing_REG: int = 0
        self.SampleSize_REG: int = 0
        self.SampleGrouping_REG: int = 0
        self.Interval_REG: int = 0
        self.SkippingNumerator_REG: int = 0
        self.Offset_REG: int = 0
        self.HorizontalStart_REG: int = 0
        self.HorizontalCount_REG: int = 0
        self.TailWidth_REG: int = 0
        self.BitWidth_REG: int = 0
        self.PortDirection_REG: bool = False
        self.GuardEnable_REG: bool = False
        self.GuardPolarity_REG: bool = False
        self.SubRowInterval_REG: bool = False
        self.FlowMode_REG: int = 0
        self.PortMode_REG: int = 0  # 0=Normal, 1=Reserved, 2=Test Ones, 3=Test Zeros
        self.ScramblerEn_REG: bool = False

    @property
    def EnableCh_REG(self) -> int:
        """16-bit bitmask for enabled channels."""
        return self._EnableCh_REG

    @EnableCh_REG.setter
    def EnableCh_REG(self, value: int) -> None:
        if self._EnableCh_REG != value:
            self._EnableCh_REG = value
            self._channel_cache = None

    def _compute_channel_cache(self) -> tuple[tuple[int, ...], int]:
        """Derive the enabled-channel tuple and count from EnableCh_REG."""
        enabled = tuple(i for i in range(16) if self._EnableCh_REG & (1 << i))
        self._channel_cache = (enabled, len(enabled))
        return self._channel_cache

    @property
    def _num_channels(self) -> int:
        """Count of enabled channels (derived from EnableCh_REG)."""
        return (self._channel_cache or self._compute_channel_cache())[1]

    @property
    def _enabled_channels(self) -> tuple[int, ...]:
        """Tuple of enabled channel numbers (derived from EnableCh_REG)."""
        return (self._channel_cache or self._compute_channel_cache())[0]

    @property
    def _horizontal_end(self) -> int:
        """Last column of the horizontal window (HorizontalStart + HorizontalCount)."""
        return self.HorizontalStart_REG + self.HorizontalCount_REG

    @property
    def _emits_txp(self) -> bool:
        """True iff FlowMode prepends a TX_PRESENT slot before each
        (channel, sample) pair's DATA bits (TX_CONTROLLED or ASYNC).
        """
        return self.FlowMode_REG in (FlowMode.TX_CONTROLLED, FlowMode.ASYNC)

    @property
    def _emits_drq(self) -> bool:
        """True iff FlowMode activates the FCP's DRQ path (RX_CONTROLLED or ASYNC)."""
        return self.FlowMode_REG in (FlowMode.RX_CONTROLLED, FlowMode.ASYNC)

    @property
    def _effective_channel_grouping(self) -> int:
        """Channels per group: ChannelGrouping_REG, clamped to num_channels
        when the register is 0 or oversized."""
        if self.ChannelGrouping_REG == 0 or self.ChannelGrouping_REG > self._num_channels:
            return self._num_channels
        return self.ChannelGrouping_REG

    def _channel(self, index: int) -> int:
        """Map sequential index to actual channel number from EnableCh_REG bitmask."""
        return self._enabled_channels[index]

class DataPort:
    """SWI3S Data Port — config + state + algorithm.

    Public surface:
        config               configuration / register attributes
        dp_index             canonical position index (0-15)
        row_in_interval      current row within this DP's interval
        initialize()         initialize before use
        clock_tick()         advance one UI; engine derives BitSlotState from state
    """

    def __init__(self, device: 'Device', dp_index: int) -> None:
        self._device = device  # interface reachable via self._device._interface
        self.dp_index = dp_index
        self.config = DataPortConfig()
        self.state = DataPortState(self.config)

    @property
    def row_in_interval(self) -> int:
        """Current row within this DP's interval."""
        return self.state.row_in_interval

    @property
    def interval_skipped(self) -> bool:
        """True when the current interval is skipped by the Payload Interval
        Skipping algorithm (no payload emitted)."""
        return self.state.interval_skipped

    def initialize(self) -> None:
        """Initialize before dataport use."""
        self.state.initialize(self.config)
        self._advance_interval()

    def clock_tick(self) -> None:
        """Advance the DataPort by one UI.

        Self-contained UI advance — engine consumes BitSlotState via the
        engine-side _derive_dp_bit_slot helper (see core/engine.py).
        """
        config = self.config
        state = self.state

        in_emit_path = not (
            config._num_channels == 0
            or state.interval_skipped
            or state.transport_phase in (TransportPhase.ROW_DONE, TransportPhase.PATTERN_DONE)
            or state.row_in_interval < config.Offset_REG
            or state.column < config.HorizontalStart_REG
        )

        if in_emit_path:
            if state.column > config._horizontal_end:
                # Window exhausted on this row. Clear spacing so stale gaps
                # don't leak into next row.
                state.spacing_slots_remaining = 0
                state.transport_phase = TransportPhase.ROW_DONE
                # Falls through to post-data drain below.
            elif state.transport_phase == TransportPhase.SPACING:
                state.spacing_slots_remaining -= 1
                if state.spacing_slots_remaining <= 0:
                    state.transport_phase = TransportPhase.ACTIVE
                # Falls through to post-data drain below.
            else:
                # Owned slot — DATA or TX_PRESENT. Advance cascade, prime
                # post-data, advance column.
                self._advance_wide_bit()
                self._prime_post_data()
                self._advance_column()
                return

        # Not owned (gated, window-exhausted, or in spacing) — drain one
        # post-data slot if any pending.
        if state.post_data_guard_pending:
            state.post_data_guard_pending = False
        elif state.post_data_tail_remaining > 0:
            state.post_data_tail_remaining -= 1
        self._advance_column()

    def _prime_post_data(self) -> None:
        """Prime post-data emission state after a source-port owned slot."""
        self.state.post_data_guard_pending = False
        self.state.post_data_tail_remaining = 0
        if self.config.PortDirection_REG:
            return
        if self.config.GuardEnable_REG:
            self.state.post_data_guard_pending = True
        self.state.post_data_tail_remaining = self.config.TailWidth_REG

    def _advance_column(self) -> None:
        """Advance column; wrap to the next row at the right edge."""
        self.state.column += 1
        if self.state.column >= self._device._interface.num_columns:
            self._advance_row()

    def _advance_row(self) -> None:
        """Advance the row counter and prepare per-row state."""
        self.state.column = 0
        # Post-data emission doesn't survive row wraps.
        self.state.post_data_guard_pending = False
        self.state.post_data_tail_remaining = 0

        # Row-cut resume: horizontal window exhausted the prior row before
        # the pattern completed. The pattern resumes on this fresh row, so
        # flip phase back to ACTIVE. Applies to both non-SRI multi-row
        # transports and SRI mid-pattern cuts.
        if self.state.transport_phase == TransportPhase.ROW_DONE:
            self.state.transport_phase = TransportPhase.ACTIVE

        self.state.row_in_interval += 1

        if self.state.row_in_interval > self.config.Interval_REG:
            self.state.row_in_interval = 0
            self._advance_interval()

    def _advance_interval(self) -> None:
        """Advance to the next interval: latch skipping status and reset transport."""
        self.state.interval_skipped = self._advance_skipping()
        self._reset_transport()

    def _advance_skipping(self) -> bool:
        """Advance the skipping accumulator at the start of an SSP interval.

        Returns True iff this interval should be skipped.
        """
        if self.config.SkippingNumerator_REG == 0:
            return False
        self.state.skipping_accumulator += self.config.SkippingNumerator_REG
        if self.state.skipping_accumulator < self._device._interface.SkippingDenominator_REG:
            return False
        self.state.skipping_accumulator -= self._device._interface.SkippingDenominator_REG
        return True

    def _reset_transport(self) -> None:
        """Re-init transport-scope state for a new transport pattern."""
        self.state.transport_phase = TransportPhase.ACTIVE
        self.state.spacing_slots_remaining = 0
        self.state.sample_in_group = 0
        self.state.samples_in_group_remaining = self.config.SampleGrouping_REG
        self.state.channel_group_base_channel = 0
        self.state.channel_index = 0
        self.state.channels_in_group_remaining = self.config._effective_channel_grouping - 1
        self.state.bit_in_channel = self.config.SampleSize_REG
        self.state.wide_bit_remaining = self.config.BitWidth_REG
        self.state.txp_pending = self.config._emits_txp

    def _advance_wide_bit(self) -> None:
        """Next wide-bit tick; cascades to _advance_bit_in_channel on exhaustion."""
        self.state.wide_bit_remaining -= 1
        if self.state.wide_bit_remaining < 0:
            self.state.wide_bit_remaining = self.config.BitWidth_REG
            self._advance_bit_in_channel()

    def _advance_bit_in_channel(self) -> None:
        """Next emission position within the current (channel, sample)."""
        if self.state.txp_pending:
            self.state.txp_pending = False
            return
        self.state.bit_in_channel -= 1
        if self.state.bit_in_channel < 0:
            self.state.bit_in_channel = self.config.SampleSize_REG
            self._advance_channel()

    def _advance_channel(self) -> None:
        """Next channel; cascades to _advance_sample on exhaustion."""
        self.state.channel_index += 1
        self.state.channels_in_group_remaining -= 1
        self.state.txp_pending = self.config._emits_txp
        if self.state.channels_in_group_remaining < 0:
            self.state.channel_index = self.state.channel_group_base_channel
            self.state.channels_in_group_remaining = self.config._effective_channel_grouping - 1
            self._advance_sample()

    def _advance_sample(self) -> None:
        """Next sample; cascades to _advance_channel_group on exhaustion."""
        self.state.sample_in_group += 1
        self.state.samples_in_group_remaining -= 1
        if self.state.samples_in_group_remaining < 0:
            self.state.sample_in_group = 0
            self.state.samples_in_group_remaining = self.config.SampleGrouping_REG
            self._advance_channel_group()

    def _advance_channel_group(self) -> None:
        """Advance to the next channel group (or to the next transport in SRI)."""
        channel_group_size = self.config._effective_channel_grouping
        transport_pattern_complete = (self.state.channel_group_base_channel + channel_group_size
                            >= self.config._num_channels)

        if transport_pattern_complete:
            if self.config.SubRowInterval_REG:
                # SRI mid-row transport rollover. _reset_transport() arms phase=ACTIVE
                # and counters; the inter-group gap block below may overwrite phase
                # to SPACING/ROW_DONE based on Spacing_REG.
                self._reset_transport()
            else:
                self.state.transport_phase = TransportPhase.PATTERN_DONE
                return
        else:
            # Next CG within same transport. The cascade that brought us here
            # left the inner counters (bit_in_channel, wide_bit_remaining, txp_pending,
            # sample_in_group, samples_in_group_remaining) at fresh-transport
            # values. Retarget the CG-scope counters at the new group: advance
            # channel_group_base_channel, point channel_index at the new base, and set
            # channels_in_group_remaining (trimmed if the last group is partial).
            self.state.channel_group_base_channel += channel_group_size
            remaining_channels = self.config._num_channels - self.state.channel_group_base_channel
            if remaining_channels > channel_group_size:
                remaining_channels = channel_group_size
            self.state.channels_in_group_remaining = remaining_channels - 1
            self.state.channel_index = self.state.channel_group_base_channel

        # Inter-group gap (SRI next-transport or next-CG path; non-SRI
        # transport-pattern-complete returned above).
        if self.config.SubRowInterval_REG or not transport_pattern_complete:
            if self.config.Spacing_REG == 0:
                # No gap: SRI ends the row here; the next row's
                # _advance_interval arms a fresh transport.
                self.state.transport_phase = TransportPhase.ROW_DONE
            else:
                self.state.spacing_slots_remaining = self.config.Spacing_REG - 1
                self.state.transport_phase = (TransportPhase.SPACING if self.config.Spacing_REG > 1
                                     else TransportPhase.ACTIVE)