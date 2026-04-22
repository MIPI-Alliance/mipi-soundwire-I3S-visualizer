"""DataPort configuration and state management.

This module provides classes for SoundWire data port configuration,
runtime state tracking, and frame rendering algorithms.

Classes:
    DataPortState: Runtime state for frame rendering
    DataPortConfig: Configuration and register values
    DataPortAlgorithm: State machine for frame rendering
    DataPort: Main facade class combining config, state, and algorithm

NOTE: This module must remain UI-independent. No tkinter, widgets, dialogs,
or any UI framework imports are allowed. This module should be usable as a
library without any UI dependencies.

Transport Pattern State Machine
===============================

A "transport pattern" (per spec) is the geometric repeating pattern on the bus,
spanning at least one row. The DataPort tracks lifecycle with one enum and one flag:

    transport_state: TransportState — IDLE (pre-transport), ACTIVE (emitting), or DONE (complete)
    row_transport_done: True when no more data on current row

Normal Mode vs SRI Mode
-----------------------

Both modes produce the same [burst][space][burst][space]... pattern, but at
different granularities:

    Normal Mode (with channel grouping):
    - One transport per interval (multi-row)
    - Channel groups create the burst/space pattern
    - Spacing register = gap between channel groups
    - Termination: all channel groups processed

    SRI Mode (Sub Row Interval):
    - Multiple transports per row
    - Spacing register = SubRow spacing between transports
    - Termination: HorizontalEnd position reached

Both channel grouping and sample grouping can be used with SRI mode.

State Transitions
-----------------

Normal mode:

    [IDLE] ──(row == Offset)──> [ACTIVE] ──(all CGs done)──> [PATTERN DONE]
       ^                                                           |
       +───────────────────(new interval)──────────────────────────+

SRI mode:

    [ACTIVE] ──(all data sent)──> [PREPARE NEXT] ──> [ACTIVE] ──> ...
                                       |
                                  (HorizontalEnd?)
                                       |
                                      Yes
                                       |
                                       v
                                [PATTERN DONE]

Key Methods
-----------

    _advance_channel_group(): Advances to next channel group or handles completion
        - Normal: calls _end_transport_pattern()
        - SRI: inlined - resets for next transport with spacing

    _end_transport_pattern(): Unified termination - sets all done flags

Counter Hierarchy
-----------------

The algorithm advances counters in this hierarchy (outer to inner):

    Interval (rows) -> Channel Group -> Sample -> Channel -> Bit

When a counter exhausts, it triggers advancement of the next outer counter:

    bit exhausted      -> _advance_channel()
    channel exhausted  -> _advance_sample()
    sample exhausted   -> _advance_channel_group()
    channel_group done -> (transport completion handled inline)
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from .bit_slot import BitSlotData, BitSlotState
from .enums import SlotType, DirectionType, FlowMode, TransportState
from .flow_control_port import FlowControlPort

if TYPE_CHECKING:
    from .interface import Interface
    from .device import Device


# =============================================================================
# Runtime State Class
# =============================================================================

class DataPortState:
    """Runtime state for a DataPort during frame rendering.

    Contains all mutable state that changes as the rendering algorithm
    processes each row and column. Reset via reset() before each render pass.

    This class is used internally by DataPort and should not be instantiated directly.
    """

    def __init__(self) -> None:
        """Initialize runtime state with default values."""
        self.reset()

    def reset(self) -> None:
        """Reset all runtime state for a fresh drawing pass.

        This resets ALL state variables that can persist between draws,
        ensuring each drawing pass starts clean.
        """
        # Internal position tracking (for next_bit_slot() iteration)
        self.column: int = 0

        # Interval tracking
        self.current_row_in_interval: int = 0
        self.transport_state: TransportState = TransportState.IDLE
        self.row_transport_done: bool = False
        self.horizontal_count_done: bool = False

        # Guard and tail state
        self.tails_left: int = 0
        self.guard_left: bool = False

        # Sample tracking
        self.skipping_accumulator: int = 0
        self.sample: int = 0
        self.sample_group_base: int = 0  # Sample number at start of transport (for channel grouping)
        self.samples_in_group_remaining: int = 0

        # Channel tracking
        self.channel_index: int = 0
        self.spacing_slots_remaining: int = 0
        self.channel_group_base: int = 0
        self.channels_in_group_remaining: int = 0
        self.channel_group_size: int = 0

        # Bit tracking
        self.bit: int = 0
        self.txp_sent: bool = False

        # Wide bit tracking - for returning same slot across multiple columns
        self.wide_bit_slots_remaining: int = 0
        self.stored_wide_bit: Optional['BitSlotState'] = None


# =============================================================================
# Configuration Class
# =============================================================================

class DataPortConfig:
    """Configuration and register state for a DataPort.

    This is a pure data container with no validation. Validation is performed
    by context-specific validators (UI, batch processing, etc.).

    Contains all register values and static configuration attributes.
    These values are set via UI/CSV and don't change during frame rendering.

    This class is used internally by DataPort and should not be instantiated directly.
    """

    def __init__(self) -> None:
        """Initialize configuration with zero/default values.

        All values start at zero/False. Useful defaults come from the data model
        (loaded from CSV or set programmatically).
        """
        # All variables that contain "_REG" correspond to registers in the specification.
        # NOTE: DeviceNumber_REG is stored in Interface.dp_device_assignments, not here
        self._EnableCh_REG: int = 0  # 16-bit bitmask for enabled channels
        # Cached derived values (invalidated when EnableCh_REG changes)
        self._cached_enabled_channels: Optional[tuple] = None
        self._cached_num_channels: Optional[int] = None
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
        self.ScramblerEn_REG: bool = False  # Scrambler enabled

    @property
    def EnableCh_REG(self) -> int:
        """16-bit bitmask for enabled channels."""
        return self._EnableCh_REG

    @EnableCh_REG.setter
    def EnableCh_REG(self, value: int) -> None:
        """Set EnableCh_REG and invalidate cached values."""
        if self._EnableCh_REG != value:
            self._EnableCh_REG = value
            self._cached_enabled_channels = None
            self._cached_num_channels = None

    @property
    def _NumChannels(self) -> int:
        """Count of enabled channels (derived from EnableCh_REG).

        Uses cached value for performance - this is called many times per frame.
        """
        if self._cached_num_channels is None:
            self._cached_num_channels = self._EnableCh_REG.bit_count()
        return self._cached_num_channels

    @property
    def _enabled_channels(self) -> tuple:
        """Tuple of enabled channel numbers (derived from EnableCh_REG).

        Uses cached value for performance - this is called many times per frame
        in the hot rendering loop.
        """
        if self._cached_enabled_channels is None:
            self._cached_enabled_channels = tuple(
                i for i in range(16) if self._EnableCh_REG & (1 << i)
            )
        return self._cached_enabled_channels

    @property
    def _HorizontalEnd(self) -> int:
        """Last column of the horizontal window (HorizontalStart + HorizontalCount)."""
        return self.HorizontalStart_REG + self.HorizontalCount_REG

    def _channel(self, sequential_index: int) -> int:
        """Map sequential index to actual channel number from EnableCh_REG bitmask.

        Uses cached _enabled_channels for performance - this is called in the
        innermost rendering loop.
        """
        enabled = self._enabled_channels
        if sequential_index < len(enabled):
            return enabled[sequential_index]
        raise IndexError(f'_channel: index {sequential_index} exceeds {len(enabled)} enabled')


# =============================================================================
# Algorithm Class
# =============================================================================

class DataPortAlgorithm:
    """Algorithm methods for DataPort frame rendering.

    Contains the state machine logic for next_bit_slot(), new_row(), and related
    methods. Operates on DataPortConfig and DataPortState instances directly.

    This class is used internally by DataPort and should not be instantiated directly.
    """

    def __init__(self, dataport: 'DataPort') -> None:
        """Initialize with references to DataPort's config and state."""
        self._dataport = dataport
        self._state = dataport._state

    @property
    def _config(self) -> 'DataPortConfig':
        """Access current config through dataport reference."""
        return self._dataport.config

    @property
    def _interface(self) -> 'Interface':
        """Access interface through dataport's device reference."""
        return self._dataport._device._interface

    # =========================================================================
    # Counter Advancement (transport → bit)
    # =========================================================================

    def _end_transport_pattern(self) -> None:
        """Mark the transport pattern as complete - no more data until next interval/row.

        This is the unified termination point for both normal mode (when all channel
        groups are done) and SRI mode (when HorizontalEnd is reached).
        """
        self._state.transport_state = TransportState.DONE
        self._state.row_transport_done = True

    def _advance_channel_group(self) -> None:
        """Advance to next channel group; in SRI mode prepare next transport, otherwise terminate the pattern when groups exhausted."""
        if self._state.channel_group_base + self._state.channel_group_size >= self._config._NumChannels:
            # All channel groups complete - handle transport completion
            if not self._config.SubRowInterval_REG:
                # Normal mode: transport pattern complete
                self._end_transport_pattern()
            else:
                # SRI mode: prepare for next transport within the row.
                # Resets state for a new transport opportunity with SubRow spacing.
                # (actual pattern termination happens at HorizontalEnd via position check)
                self._start_interval()
                self._state.spacing_slots_remaining = self._config.Spacing_REG
        else:
            # Move to next channel group
            self._state.channel_group_base += self._state.channel_group_size
            remaining_channels = self._config._NumChannels - self._state.channel_group_base
            if remaining_channels > self._state.channel_group_size:
                remaining_channels = self._state.channel_group_size
            self._state.channels_in_group_remaining = remaining_channels - 1

            # Reset for new group - sample counter restarts from sample_group_base
            # because all channel groups within a transport share the same sample(s).
            # With SampleGrouping=0: each CG gets sample_group_base (one sample per transport)
            # With SampleGrouping>0: each CG processes samples from sample_group_base to sample_group_base+N
            self._state.samples_in_group_remaining = self._config.SampleGrouping_REG
            if (self._config.ChannelGrouping_REG > 0 and
                self._config.ChannelGrouping_REG < self._config._NumChannels):
                self._state.sample = self._state.sample_group_base
            self._state.bit = self._config.SampleSize_REG
            self._state.channel_index = self._state.channel_group_base
            self._state.spacing_slots_remaining = self._config.Spacing_REG
            self._state.txp_sent = False

        # Handle spacing after any channel group transition
        if self._config.Spacing_REG == 0:
            self._state.row_transport_done = True
        else:
            self._state.spacing_slots_remaining -= 1

    def _advance_sample(self) -> None:
        """Advance to next sample, calling _advance_channel_group when sample group exhausted."""
        self._state.sample += 1
        self._state.samples_in_group_remaining -= 1

        if self._state.samples_in_group_remaining < 0:
            # Sample group complete - advance channel group
            self._advance_channel_group()
        else:
            # More samples in this group - reset to first channel
            self._state.bit = self._config.SampleSize_REG
            self._state.channel_index = self._state.channel_group_base
            self._state.channels_in_group_remaining = self._state.channel_group_size - 1
            self._state.txp_sent = False

    def _advance_channel(self) -> None:
        """Advance to next channel, calling _advance_sample when channel group exhausted."""
        self._state.channel_index += 1
        self._state.txp_sent = False
        self._state.channels_in_group_remaining -= 1

        if self._state.channels_in_group_remaining < 0:
            self._advance_sample()
        else:
            # More channels in this group - reset bit counter
            self._state.bit = self._config.SampleSize_REG

    def _advance_bit(self) -> None:
        """Advance to next bit, calling _advance_channel when bit counter exhausted."""
        self._state.bit -= 1
        if self._state.bit < 0:
            self._advance_channel()

    # =========================================================================
    # Position Management
    # =========================================================================

    def _start_interval(self) -> None:
        """Initialize all state for a new interval."""
        # Lifecycle flags
        self._state.transport_state = TransportState.ACTIVE
        self._state.spacing_slots_remaining = 0

        # Sample tracking - save current sample as base for this interval
        # (for channel grouping, each group restarts from this base)
        self._state.sample_group_base = self._state.sample
        self._state.samples_in_group_remaining = self._config.SampleGrouping_REG

        # Channel tracking
        self._state.channel_group_base = 0
        self._state.channel_index = 0
        if self._config.ChannelGrouping_REG == 0 or self._config.ChannelGrouping_REG > self._config._NumChannels:
            self._state.channel_group_size = self._config._NumChannels
        else:
            self._state.channel_group_size = self._config.ChannelGrouping_REG
        self._state.channels_in_group_remaining = self._state.channel_group_size - 1

        # Bit tracking
        self._state.bit = self._config.SampleSize_REG
        self._state.txp_sent = False
        self._dataport.fcp.reset_for_interval()

    def _check_skipping_at_offset(self) -> bool:
        """Apply the skipping accumulator at an offset row.

        Increments the accumulator by SkippingNumerator_REG. If it reaches the
        denominator, marks the interval as skipped (transport_state = DONE),
        decrements the accumulator, and returns True so the caller can skip
        starting the interval.

        Returns:
            True if this interval should be skipped, False otherwise.
        """
        if self._config.SkippingNumerator_REG == 0:
            return False
        self._state.skipping_accumulator += self._config.SkippingNumerator_REG
        if self._state.skipping_accumulator < self._interface.SkippingDenominator_REG:
            return False
        self._state.transport_state = TransportState.DONE
        self._state.skipping_accumulator -= self._interface.SkippingDenominator_REG
        return True

    def _advance_row(self) -> None:
        """Wrap position to the next row and prepare per-row state."""
        self._state.column = 0
        self._state.guard_left = False
        self._state.tails_left = 0
        self._state.wide_bit_slots_remaining = 0
        self._state.stored_wide_bit = None
        self._dataport.fcp.reset_for_row()
        self._state.row_transport_done = False

        self._state.current_row_in_interval += 1

        # Wrap around at end of interval
        if self._state.current_row_in_interval > self._config.Interval_REG:
            self._state.current_row_in_interval = 0
            # Reset interval-scoped flags between intervals. Required when Offset_REG > 0,
            # because _start_interval() doesn't run until row == Offset_REG; without this,
            # stale drq_sent/transport_state from the previous interval would corrupt the
            # new interval's first DRQ trigger and slot logic.
            self._state.transport_state = TransportState.IDLE
            self._dataport.fcp.reset_drq_sent()
            # Advance sample base for new interval (truncation recovery for normal mode).
            # SRI is excluded: each transport is self-contained per row and advances the
            # sample counter naturally via _advance_channel_group -> _start_interval, so
            # applying the expected-samples recompute would double-count the last group.
            if (not self._config.SubRowInterval_REG and
                self._config.SampleGrouping_REG > 0 and
                self._config.ChannelGrouping_REG > 0 and
                self._config.ChannelGrouping_REG < self._config._NumChannels):
                expected_samples_per_interval = self._config.SampleGrouping_REG + 1
                self._state.sample = self._state.sample_group_base + expected_samples_per_interval

        if self._state.current_row_in_interval == self._config.Offset_REG:
            if self._check_skipping_at_offset():
                return
            self._start_interval()

    def _advance_column(self) -> None:
        """Advance position by one column, wrapping to the next row at the edge."""
        self._state.column += 1
        if self._state.column >= self._interface.num_columns:
            self._advance_row()

    def _slot(self) -> BitSlotState:
        """Return the BitSlotState at current position based on internal state.

        Returns a fresh BitSlotState (NORMAL with no data) when no data is owned.
        """
        slot: BitSlotState = BitSlotState(slot_type=SlotType.NORMAL)
        column = self._state.column

        if self._state.row_transport_done or self._state.transport_state == TransportState.DONE:
            return slot

        if self._config._NumChannels == 0:
            return slot

        if self._state.transport_state == TransportState.ACTIVE:
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

            if self._state.spacing_slots_remaining > 0:
                self._state.spacing_slots_remaining -= 1
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

    def reset_algorithm(self) -> None:
        """Reset algorithm-specific tracking state and prepare for row 0."""
        self._state.row_transport_done = False
        if self._state.current_row_in_interval == self._config.Offset_REG:
            if self._check_skipping_at_offset():
                return
            self._start_interval()

    def next_bit_slot(self) -> BitSlotState:
        """Get slot for current position and auto-advance.

        This is the ONLY external interface. Each call returns what the DataPort
        wants to write at the current column position, then advances to the next.
        Handles wide bits, guards, tails, and TxP all internally. DRQ/FCP guards
        and tails are emitted separately by the parallel FlowControlPort.

        Returns:
            BitSlotState for current position (EMPTY if not active).
        """
        # Handle wide bits for data slots (replay stored data across BitWidth columns)
        if self._state.wide_bit_slots_remaining > 0:
            self._state.wide_bit_slots_remaining -= 1
            if self._state.stored_wide_bit is None:
                raise RuntimeError("stored_wide_bit_slot uninitialized with wide_bit_slots_remaining > 0")
            slot = BitSlotState(
                slot_type=self._state.stored_wide_bit.slot_type,
                direction=self._state.stored_wide_bit.direction,
                data=self._state.stored_wide_bit.data
            )
            self._advance_column()
            return slot

        # Check if current position has data
        slot = self._slot()

        # If we got a data slot, return it and prepare guards/tails for later
        if slot.is_owned():
            # Handle wide bits for data slots
            # Clip wide-bit data at HorizontalEnd; guards/tails may extend past.
            if self._config.BitWidth_REG > 0:
                columns_remaining_in_window = self._config._HorizontalEnd - self._state.column
                self._state.wide_bit_slots_remaining = min(
                    self._config.BitWidth_REG,
                    max(0, columns_remaining_in_window)
                )
                self._state.stored_wide_bit = slot

            # Source ports get guards and tails after data (will emit when no more data)
            if not self._config.PortDirection_REG:
                self._state.tails_left = self._config.TailWidth_REG
                self._state.guard_left = self._config.GuardEnable_REG

            self._advance_column()
            return slot

        # NO DATA at this position - check for guards (emit only when no data)
        if self._state.guard_left:
            self._state.guard_left = False
            slot = BitSlotState(
                slot_type=SlotType.GUARD_1 if self._config.GuardPolarity_REG else SlotType.GUARD_0,
                direction=DirectionType.SOURCE
            )
            # Guards are always 1 bit wide (not affected by BitWidth)
            self._advance_column()
            return slot

        # Check for tails (after guard)
        if self._state.tails_left > 0:
            self._state.tails_left -= 1
            slot = BitSlotState(
                slot_type=SlotType.TAIL,
                direction=DirectionType.SOURCE
            )
            self._advance_column()
            return slot

        # No data, guard, or tail at this position — emit EMPTY
        self._advance_column()
        return BitSlotState(slot_type=SlotType.EMPTY)


# =============================================================================
# Main DataPort Class
# =============================================================================

class DataPort:
    """SoundWire Data Port configuration and state management.

    External interface:
        - config: DataPortConfig for configuration/register attributes
        - dp_index: Canonical position index (0-15)
        - reset(): Reset state for a new frame
        - next_bit_slot(): Get slot for current position (auto-advances)

    State is internal and not accessible from outside.
    """

    def __init__(self, device: 'Device', dp_index: int) -> None:
        """Initialize a DataPort.

        Args:
            device: The parent Device containing this data port
            dp_index: Canonical position index (0-15) for this data port
        """
        # Store reference to parent device (access interface via device._interface)
        self._device = device

        # Canonical position index - used for flat list ordering and CSV mapping
        self.dp_index = dp_index

        # Configuration is managed by DataPortConfig class
        self.config = DataPortConfig()

        # Runtime state is managed by DataPortState class (internal)
        self._state = DataPortState()

        # Flow Control Port - parallel source for DRQ/guards/tails (public)
        self.fcp = FlowControlPort(self)

        # Algorithm methods are managed by DataPortAlgorithm class (internal)
        self._algorithm = DataPortAlgorithm(self)

    @property
    def current_row_in_interval(self) -> int:
        """DP's current row index within its interval.

        Exposed so the engine can snapshot this value before calling
        `next_bit_slot()` (which may advance it) and pass it to
        `self.fcp.next_bit_slot()`.
        """
        return self._state.current_row_in_interval

    def reset(self) -> None:
        """Reset all runtime state for a fresh drawing pass.

        Call before starting a new frame.
        """
        self._state.reset()
        self.fcp.reset()
        self._algorithm.reset_algorithm()

    def next_bit_slot(self) -> BitSlotState:
        """Get slot for current position and auto-advance.

        This is the ONLY external interface for frame rendering. Each call returns
        what the DataPort wants to write at the current column position, then
        advances to the next column. Handles wide bits, guards, tails, DRQ, and
        TxP all internally.

        Returns:
            BitSlotState for current position (EMPTY if not active)
        """
        return self._algorithm.next_bit_slot()


