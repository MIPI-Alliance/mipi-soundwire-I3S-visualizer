"""DataPort configuration and state management.

Classes:
    DataPortState:  Runtime state
    DataPortConfig: Configuration and register values
    DataPort:       Combines config, state, and algorithm

`phase: TransportPhase` tracks the transport lifecycle:
    ACTIVE       Emitting inside the horizontal window
    SPACING      Inter-channel-group / SRI inter-transport gap
    ROW_DONE     Row's window exhausted; transport still alive across wrap
    PATTERN_DONE Transport complete or interval skipped

Normal vs SRI:
    - Normal: One transport per SSP interval; channel groups
      structure the burst/space pattern within the transport.
    - SRI:    Multiple transports per row.

Counter cascade:
    wide_bit → bit → channel → sample → channel_group → transport completion
"""

from __future__ import annotations
from collections import deque
from typing import Optional, TYPE_CHECKING
from .bit_slot import BitSlotData, BitSlotState
from .enums import SlotType, DirectionType, FlowMode, TransportPhase

if TYPE_CHECKING:
    from .device import Device

class DataPortState:
    """Runtime state for a DataPort."""

    def __init__(self, config: DataPortConfig) -> None:
        # post_data_queue is reused across initializations so external holders
        # keep a stable reference; initialize() clears it in place.
        self.post_data_queue: deque[SlotType] = deque()
        self.initialize(config)

    def initialize(self, config: DataPortConfig) -> None:
        """Set state to the current config."""
        self.column: int = 0
        self.row_in_interval: int = 0
        self.phase: TransportPhase = TransportPhase.PATTERN_DONE
        self.interval_skipped: bool = False
        self.skipping_accumulator: int = 0
        self.sample_in_group: int = 0
        self.samples_in_group_remaining: int = config.SampleGrouping_REG
        self.channel_index: int = 0
        self.spacing_slots_remaining: int = 0
        self.channel_group_base: int = 0
        self.channels_in_group_remaining: int = 0
        self.bit: int = config.SampleSize_REG
        self.wide_bit_remaining: int = config.BitWidth_REG
        self.txp_pending: bool = config._emits_txp  # True → next emission is TX_PRESENT
        self.post_data_queue.clear()

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
        fetch_bit_slot()     emit slot at current position (auto-advances to next UI)
    """

    def __init__(self, device: 'Device', dp_index: int) -> None:
        self._device = device  # interface reachable via self._device._interface
        self.dp_index = dp_index
        self.config = DataPortConfig()
        self._state = DataPortState(self.config)

    @property
    def row_in_interval(self) -> int:
        """Current row within this DP's interval."""
        return self._state.row_in_interval

    def initialize(self) -> None:
        """Initialize before dataport use."""
        self._state.initialize(self.config)
        self._advance_interval()

    def fetch_bit_slot(self) -> BitSlotState:
        """Emit bit slot information at the current position and auto-advance."""
        if (self.config._num_channels == 0
                or self._state.interval_skipped
                or self._state.phase in (TransportPhase.ROW_DONE, TransportPhase.PATTERN_DONE)
                or self._state.row_in_interval < self.config.Offset_REG
                or self._state.column < self.config.HorizontalStart_REG):
            slot = BitSlotState(slot_type=SlotType.EMPTY)
        else:
            slot = self._data_slot()

        if slot.is_owned():
            self._prime_post_data_queue()
            self._advance_column()
            return slot

        if self._state.post_data_queue:
            slot_type = self._state.post_data_queue.popleft()
            self._advance_column()
            return BitSlotState(slot_type=slot_type, direction=DirectionType.SOURCE)

        self._advance_column()
        return BitSlotState(slot_type=SlotType.EMPTY)

    def _data_slot(self) -> BitSlotState:
        """Return the data slot at the current position, inside the active window."""
        if self._state.column > self.config._horizontal_end:
            # Transport window exhausted on this row. Clear spacing so
            # stale gaps don't leak into next row.
            self._state.spacing_slots_remaining = 0
            self._state.phase = TransportPhase.ROW_DONE
            return BitSlotState(slot_type=SlotType.EMPTY)

        if self._state.phase == TransportPhase.SPACING:
            self._state.spacing_slots_remaining -= 1
            if self._state.spacing_slots_remaining <= 0:
                self._state.phase = TransportPhase.ACTIVE
            return BitSlotState(slot_type=SlotType.EMPTY)

        direction = DirectionType.SINK if self.config.PortDirection_REG else DirectionType.SOURCE
        if self._state.txp_pending:
            slot = BitSlotState(
                slot_type=SlotType.TX_PRESENT,
                direction=direction,
                data=BitSlotData(
                    sample_in_group=self._state.sample_in_group,
                    channel=self.config._channel(self._state.channel_index),
                    bit=0
                ),
            )
        else:
            slot = BitSlotState(
                slot_type=SlotType.DATA,
                direction=direction,
                data=BitSlotData(
                    sample_in_group=self._state.sample_in_group,
                    channel=self.config._channel(self._state.channel_index),
                    bit=self._state.bit
                ),
            )

        self._advance_wide_bit()
        return slot

    def _prime_post_data_queue(self) -> None:
        """Prime the post-data queue after a source-port data slot."""
        self._state.post_data_queue.clear()
        if self.config.PortDirection_REG:
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
        if self._state.column >= self._device._interface.num_columns:
            self._advance_row()

    def _advance_row(self) -> None:
        """Advance the row counter and prepare per-row state."""
        self._state.column = 0
        self._state.post_data_queue.clear()

        # Row-cut resume: horizontal window exhausted the prior row before
        # the pattern completed. The pattern resumes on this fresh row, so
        # flip phase back to ACTIVE. Applies to both non-SRI multi-row
        # transports and SRI mid-pattern cuts.
        if self._state.phase == TransportPhase.ROW_DONE:
            self._state.phase = TransportPhase.ACTIVE

        self._state.row_in_interval += 1

        if self._state.row_in_interval > self.config.Interval_REG:
            self._state.row_in_interval = 0
            self._advance_interval()

    def _advance_interval(self) -> None:
        """Advance to the next interval: latch skipping status and reset transport."""
        self._state.interval_skipped = self._advance_skipping()
        self._reset_transport()

    def _advance_skipping(self) -> bool:
        """Advance the skipping accumulator at the start of an SSP interval.

        Returns True iff this interval should be skipped.
        """
        if self.config.SkippingNumerator_REG == 0:
            return False
        self._state.skipping_accumulator += self.config.SkippingNumerator_REG
        if self._state.skipping_accumulator < self._device._interface.SkippingDenominator_REG:
            return False
        self._state.skipping_accumulator -= self._device._interface.SkippingDenominator_REG
        return True

    def _reset_transport(self) -> None:
        """Re-init transport-scope state for a new transport pattern."""
        self._state.phase = TransportPhase.ACTIVE
        self._state.spacing_slots_remaining = 0
        self._state.sample_in_group = 0
        self._state.samples_in_group_remaining = self.config.SampleGrouping_REG
        self._state.channel_group_base = 0
        self._state.channel_index = 0
        self._state.channels_in_group_remaining = self.config._effective_channel_grouping - 1
        self._state.bit = self.config.SampleSize_REG
        self._state.wide_bit_remaining = self.config.BitWidth_REG
        self._state.txp_pending = self.config._emits_txp

    def _advance_wide_bit(self) -> None:
        """Next wide-bit tick; cascades to _advance_bit on exhaustion."""
        self._state.wide_bit_remaining -= 1
        if self._state.wide_bit_remaining < 0:
            self._state.wide_bit_remaining = self.config.BitWidth_REG
            self._advance_bit()

    def _advance_bit(self) -> None:
        """Next emission position within the current (channel, sample)."""
        if self._state.txp_pending:
            self._state.txp_pending = False
            return
        self._state.bit -= 1
        if self._state.bit < 0:
            self._state.bit = self.config.SampleSize_REG
            self._advance_channel()

    def _advance_channel(self) -> None:
        """Next channel; cascades to _advance_sample on exhaustion."""
        self._state.channel_index += 1
        self._state.channels_in_group_remaining -= 1
        self._state.txp_pending = self.config._emits_txp
        if self._state.channels_in_group_remaining < 0:
            self._state.channel_index = self._state.channel_group_base
            self._state.channels_in_group_remaining = self.config._effective_channel_grouping - 1
            self._advance_sample()

    def _advance_sample(self) -> None:
        """Next sample; cascades to _advance_channel_group on exhaustion."""
        self._state.sample_in_group += 1
        self._state.samples_in_group_remaining -= 1
        if self._state.samples_in_group_remaining < 0:
            self._state.sample_in_group = 0
            self._state.samples_in_group_remaining = self.config.SampleGrouping_REG
            self._advance_channel_group()

    def _advance_channel_group(self) -> None:
        """Advance to the next channel group (or to the next transport in SRI)."""
        cg_size = self.config._effective_channel_grouping
        pattern_complete = (self._state.channel_group_base + cg_size
                            >= self.config._num_channels)

        if pattern_complete:
            if self.config.SubRowInterval_REG:
                # SRI mid-row transport rollover.
                self._reset_transport()
            else:
                self._state.phase = TransportPhase.PATTERN_DONE
                return
        else:
            # Next CG within same transport. The cascade that brought us here
            # left the inner counters (bit, wide_bit_remaining, txp_pending,
            # sample_in_group, samples_in_group_remaining) at fresh-transport
            # values. Retarget the CG-scope counters at the new group: advance
            # channel_group_base, point channel_index at the new base, and set
            # channels_in_group_remaining (trimmed if the last group is partial).
            self._state.channel_group_base += cg_size
            remaining_channels = self.config._num_channels - self._state.channel_group_base
            if remaining_channels > cg_size:
                remaining_channels = cg_size
            self._state.channels_in_group_remaining = remaining_channels - 1
            self._state.channel_index = self._state.channel_group_base

        # Inter-group gap (SRI next-transport or next-CG path; non-SRI
        # pattern-complete returned above).
        if self.config.SubRowInterval_REG or not pattern_complete:
            if self.config.Spacing_REG == 0:
                # No gap: SRI ends the row here; the next row's
                # _advance_interval arms a fresh transport.
                self._state.phase = TransportPhase.ROW_DONE
            else:
                self._state.spacing_slots_remaining = self.config.Spacing_REG - 1
                self._state.phase = (TransportPhase.SPACING if self.config.Spacing_REG > 1
                                     else TransportPhase.ACTIVE)