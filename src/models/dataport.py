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
spanning at least one row. The DataPort tracks lifecycle with a single enum:

    phase: TransportPhase — one of:
        IDLE         Pre-transport: between intervals, before Offset row
        ACTIVE       Emitting data inside the transport window
        SPACING      Inter-CG / inter-transport gap (spacing counter > 0)
        ROW_DONE     No more data on this row; interval still alive
        PATTERN_DONE Transport pattern complete for this interval

The five phases are mutually exclusive; illegal combinations (e.g. IDLE with
row_done set) are unrepresentable.

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

    [IDLE] ──(row == Offset)──> [ACTIVE] ──(CGs done)──> [PATTERN_DONE]
       ^                           | ^                          |
       |                           v |                          |
       |                         [SPACING] (gap between CGs)    |
       |                                                        |
       +──────────────(new interval wrap)───────────────────────+

SRI mode:

    [ACTIVE] ──(CG done)──> [SPACING]/[ROW_DONE] ──> [ACTIVE] ──> ...
                                 |
                            (HorizontalEnd?)
                                 |
                                Yes
                                 |
                                 v
                          [PATTERN_DONE]

Reset Scopes
------------

Each field has exactly one reset scope:

    Frame     — whole rendering pass (DataPort.reset / DataPortState.reset_frame)
    Interval  — one Interval_REG period (_reset_interval_wrap on wrap)
    Row       — one column sweep (reset_row before each row)
    Transport — one transport pattern within an interval (reset_transport)

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

from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, TYPE_CHECKING

from .bit_slot import BitSlotData, BitSlotState
from .enums import SlotType, DirectionType, FlowMode, TransportPhase
from .flow_control_port import FlowControlPort

if TYPE_CHECKING:
    from .interface import Interface
    from .device import Device


# =============================================================================
# Supporting Types
# =============================================================================

@dataclass
class WideBitReplay:
    """A data slot being replayed across multiple columns (BitWidth_REG > 0).

    Invariant: remaining >= 1. When the replay is exhausted, the containing
    Optional is set to None rather than leaving remaining == 0 with a slot
    still attached.
    """
    slot: BitSlotState
    remaining: int


class EmissionPhase(Enum):
    """Drives the match in next_bit_slot()."""
    WIDE_REPLAY = auto()   # replay stored slot across BitWidth columns
    DATA_PROBE  = auto()   # probe _slot(); fall back to queue; fall back to EMPTY


# =============================================================================
# Runtime State Class
# =============================================================================

class DataPortState:
    """Runtime state for a DataPort during frame rendering.

    Contains all mutable state that changes as the rendering algorithm
    processes each row and column. Fields are grouped by reset scope
    (frame / interval / row / transport); see the module docstring.

    This class is used internally by DataPort and should not be instantiated directly.
    """

    def __init__(self) -> None:
        """Initialize runtime state with default values."""
        # Persistent containers created once; cleared (not reassigned) on reset.
        self.post_data_queue: deque[SlotType] = deque()
        self.wide_replay: Optional[WideBitReplay] = None
        self.reset_frame()

    def reset_frame(self) -> None:
        """Frame-scope reset — full re-init for a new rendering pass."""
        # Position
        self.column: int = 0

        # Interval-scope
        self.current_row_in_interval: int = 0
        self.phase: TransportPhase = TransportPhase.IDLE

        # Frame-scope (persists across intervals)
        self.skipping_accumulator: int = 0
        self.sample: int = 0

        # Transport-scope (reset by reset_transport on each Offset row)
        self.sample_group_base: int = 0
        self.samples_in_group_remaining: int = 0
        self.channel_index: int = 0
        self.spacing_slots_remaining: int = 0
        self.channel_group_base: int = 0
        self.channels_in_group_remaining: int = 0
        self.channel_group_size: int = 0
        self.bit: int = 0
        self.txp_sent: bool = False

        # Row-scope
        self.wide_replay = None
        self.post_data_queue.clear()

    # Backward-compat alias for external callers.
    reset = reset_frame


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

    Contains the state machine logic for next_bit_slot(), _advance_row(), and
    related methods. Operates on DataPortConfig and DataPortState instances
    directly.

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

    # -------------------------------------------------------------------------
    # Position predicates (pure, no state mutation)
    # -------------------------------------------------------------------------

    @property
    def _past_horizontal_end(self) -> bool:
        """True when the current column is past the transport window's right edge."""
        return self._state.column > self._config._HorizontalEnd

    # =========================================================================
    # Counter Advancement (transport → bit)
    # =========================================================================

    def _end_transport_pattern(self) -> None:
        """Mark the transport pattern as complete — single atomic transition.

        Unified termination for both normal mode (all channel groups done) and
        SRI mode (HorizontalEnd reached). Previously set two fields; now a
        single phase assignment.
        """
        self._state.phase = TransportPhase.PATTERN_DONE

    def _advance_channel_group(self) -> None:
        """Advance to next channel group; in SRI mode prepare next transport, otherwise terminate the pattern when groups exhausted."""
        if self._state.channel_group_base + self._state.channel_group_size >= self._config._NumChannels:
            # All channel groups complete - handle transport completion
            if not self._config.SubRowInterval_REG:
                # Normal mode: transport pattern complete
                self._end_transport_pattern()
            else:
                # SRI mode: prepare for next transport within the row.
                # (actual pattern termination happens at HorizontalEnd via position check)
                self.reset_transport()
                self._state.spacing_slots_remaining = self._config.Spacing_REG
                if self._config.Spacing_REG > 0:
                    self._state.phase = TransportPhase.SPACING
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
            if self._config.Spacing_REG > 0:
                self._state.phase = TransportPhase.SPACING

        # Spacing=0 shortcut: in Normal mode this used to set row_transport_done
        # (killing the row); preserve that behavior as ROW_DONE. In SRI mode it
        # means "no gap between transports" — also ROW_DONE (single transport).
        if self._config.Spacing_REG == 0 and self._state.phase != TransportPhase.PATTERN_DONE:
            self._state.phase = TransportPhase.ROW_DONE
        elif self._state.phase == TransportPhase.SPACING:
            self._state.spacing_slots_remaining -= 1
            if self._state.spacing_slots_remaining <= 0:
                self._state.phase = TransportPhase.ACTIVE

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
    # Scope-Named Resets
    # =========================================================================

    def reset_transport(self) -> None:
        """Transport-scope reset: initialize state for a new transport pattern.

        Called at the Offset row (Normal mode: once per interval) or on each
        new transport within a row (SRI mode). Does NOT touch column, row
        counter, wide replay, or post-data queue (those are row-scoped).
        """
        # Lifecycle flag
        self._state.phase = TransportPhase.ACTIVE
        self._state.spacing_slots_remaining = 0

        # Sample tracking - save current sample as base for this transport
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
        denominator, marks the interval as skipped (phase = PATTERN_DONE),
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
        # Atomic transition — old code set DONE without row_done, violating I1.
        # PATTERN_DONE encodes both facts in one assignment.
        self._state.phase = TransportPhase.PATTERN_DONE
        self._state.skipping_accumulator -= self._interface.SkippingDenominator_REG
        return True

    def reset_row(self) -> None:
        """Row-scope reset: per-row state cleared before the next column sweep."""
        self._state.column = 0
        self._state.wide_replay = None
        self._state.post_data_queue.clear()
        self._dataport.fcp.reset_for_row()

    def _reset_interval_wrap(self) -> None:
        """Interval-scope reset on wrap (called from _advance_row only).

        Clears interval-scoped flags between intervals. Required when Offset_REG > 0,
        because reset_transport() doesn't run until row == Offset_REG; without this,
        stale drq_sent/phase from the previous interval would corrupt the
        new interval's first DRQ trigger and slot logic.
        """
        self._state.current_row_in_interval = 0
        self._state.phase = TransportPhase.IDLE
        self._dataport.fcp.reset_drq_sent()
        # Advance sample base for new interval (truncation recovery for normal mode).
        # SRI is excluded: each transport is self-contained per row and advances the
        # sample counter naturally via _advance_channel_group -> reset_transport, so
        # applying the expected-samples recompute would double-count the last group.
        if (not self._config.SubRowInterval_REG and
            self._config.SampleGrouping_REG > 0 and
            self._config.ChannelGrouping_REG > 0 and
            self._config.ChannelGrouping_REG < self._config._NumChannels):
            self._state.sample = self._state.sample_group_base + self._config.SampleGrouping_REG + 1

    # =========================================================================
    # Position Management
    # =========================================================================

    def _advance_row(self) -> None:
        """Wrap position to the next row and prepare per-row state."""
        self.reset_row()
        # Entering a new row: ROW_DONE means the horizontal window closed on the
        # prior row but the transport pattern is still alive (counters unspent).
        # The fresh row's window resumes emission, so flip back to ACTIVE.
        # Interval/pre-Offset state is resolved below by _reset_interval_wrap.
        if self._state.phase == TransportPhase.ROW_DONE:
            self._state.phase = TransportPhase.ACTIVE

        self._state.current_row_in_interval += 1

        # Wrap around at end of interval
        if self._state.current_row_in_interval > self._config.Interval_REG:
            self._reset_interval_wrap()

        if self._state.current_row_in_interval == self._config.Offset_REG:
            if self._check_skipping_at_offset():
                return
            self.reset_transport()

    def _advance_column(self) -> None:
        """Advance position by one column, wrapping to the next row at the edge."""
        self._state.column += 1
        if self._state.column >= self._interface.num_columns:
            self._advance_row()

    # =========================================================================
    # Slot Emission
    # =========================================================================

    def _slot(self) -> BitSlotState:
        """Return the BitSlotState at current position based on internal state.

        Returns a fresh BitSlotState (NORMAL with no data) when no data is owned.
        """
        slot: BitSlotState = BitSlotState(slot_type=SlotType.NORMAL)
        column = self._state.column

        if self._state.phase in (TransportPhase.ROW_DONE, TransportPhase.PATTERN_DONE):
            return slot

        if self._config._NumChannels == 0:
            return slot

        if self._state.phase in (TransportPhase.ACTIVE, TransportPhase.SPACING):
            if column < self._config.HorizontalStart_REG:
                return slot

            if self._past_horizontal_end:
                # Transport window exhausted on this row. Clear spacing so stale gaps
                # don't leak into the next row's first emission window.
                if self._state.spacing_slots_remaining > 0:
                    self._state.spacing_slots_remaining = 0
                # Phase transition: ACTIVE/SPACING → ROW_DONE for this row.
                self._state.phase = TransportPhase.ROW_DONE
                return slot

            if self._state.phase == TransportPhase.SPACING:
                self._state.spacing_slots_remaining -= 1
                if self._state.spacing_slots_remaining <= 0:
                    self._state.phase = TransportPhase.ACTIVE
                return slot

            # phase == ACTIVE here — emit data (or TxPresent)
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

    def _seed_post_data_queue(self) -> None:
        """Populate the post-data queue after a source-port data slot.

        Order is fixed: guard (if enabled) first, then TailWidth tails. Sink
        ports emit no guards/tails.
        """
        self._state.post_data_queue.clear()
        if self._config.PortDirection_REG:
            return  # sink ports emit no guards/tails
        if self._config.GuardEnable_REG:
            self._state.post_data_queue.append(
                SlotType.GUARD_1 if self._config.GuardPolarity_REG else SlotType.GUARD_0
            )
        for _ in range(self._config.TailWidth_REG):
            self._state.post_data_queue.append(SlotType.TAIL)

    def _emission_phase(self) -> EmissionPhase:
        """Pre-decide the emission branch for next_bit_slot()."""
        if self._state.wide_replay is not None:
            return EmissionPhase.WIDE_REPLAY
        return EmissionPhase.DATA_PROBE

    def next_bit_slot(self) -> BitSlotState:
        """Get slot for current position and auto-advance.

        This is the ONLY external interface. Each call returns what the DataPort
        wants to write at the current column position, then advances to the next.
        Handles wide bits, guards, tails, and TxP all internally. DRQ/FCP guards
        and tails are emitted separately by the parallel FlowControlPort.

        Returns:
            BitSlotState for current position (EMPTY if not active).
        """
        match self._emission_phase():
            case EmissionPhase.WIDE_REPLAY:
                # Replay stored data across BitWidth columns.
                replay = self._state.wide_replay
                assert replay is not None  # invariant by construction
                slot = replay.slot
                replay.remaining -= 1
                if replay.remaining == 0:
                    self._state.wide_replay = None
                self._advance_column()
                return slot

            case EmissionPhase.DATA_PROBE:
                slot = self._slot()
                if slot.is_owned():
                    # Clip wide-bit data at HorizontalEnd; guards/tails may extend past.
                    if self._config.BitWidth_REG > 0:
                        remaining = min(
                            self._config.BitWidth_REG,
                            max(0, self._config._HorizontalEnd - self._state.column)
                        )
                        if remaining > 0:
                            self._state.wide_replay = WideBitReplay(slot=slot, remaining=remaining)
                    # Source ports seed post-data queue for guards/tails.
                    self._seed_post_data_queue()
                    self._advance_column()
                    return slot

                # No data — drain the post-data queue (guard then tails).
                if self._state.post_data_queue:
                    slot_type = self._state.post_data_queue.popleft()
                    self._advance_column()
                    return BitSlotState(slot_type=slot_type, direction=DirectionType.SOURCE)

                # No data, no queued emissions — EMPTY.
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
        self._state.reset_frame()
        self.fcp.reset()
        # Prime row 0 if it's already at Offset_REG (replaces the old
        # reset_algorithm() indirection).
        if self._state.current_row_in_interval == self.config.Offset_REG:
            if not self._algorithm._check_skipping_at_offset():
                self._algorithm.reset_transport()

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
