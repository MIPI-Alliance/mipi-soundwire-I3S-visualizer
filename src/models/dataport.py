"""DataPort configuration and state management.

This module provides classes for SoundWire data port configuration,
runtime state tracking, and frame rendering algorithms.

Classes:
    DataPortState: Runtime state for frame rendering
    DataPortConfig: Configuration and register values
    DataPort: Main class combining config, state, and rendering algorithm

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
from typing import Optional, TYPE_CHECKING

from .bit_slot import BitSlotData, BitSlotState
from .enums import SlotType, DirectionType, FlowMode, TransportPhase
from .flow_control_port import FlowControlPort

if TYPE_CHECKING:
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


# =============================================================================
# Runtime State Class
# =============================================================================

class DataPortState:
    """Runtime state for a DataPort during frame rendering.

    Contains all mutable state that changes as the rendering algorithm
    processes each row and column. Fields are grouped by reset scope
    (frame / interval / row / transport); see the module docstring.
    """

    def __init__(self) -> None:
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


# =============================================================================
# Configuration Class
# =============================================================================

class DataPortConfig:
    """Configuration and register state for a DataPort.

    This is a pure data container with no validation. Validation is performed
    by context-specific validators (UI, batch processing, etc.).

    Contains all register values and static configuration attributes.
    These values are set via UI/CSV and don't change during frame rendering.
    """

    def __init__(self) -> None:
        """Initialize configuration with zero/default values.

        All values start at zero/False. Useful defaults come from the data model
        (loaded from CSV or set programmatically).
        """
        # All variables that contain "_REG" correspond to registers in the specification.
        # NOTE: DeviceNumber_REG is stored in Interface.dp_device_assignments, not here
        self._EnableCh_REG: int = 0  # 16-bit bitmask for enabled channels
        # Cached (enabled_channels, num_channels). None = dirty; one invalidation point.
        self._ch_cache: Optional[tuple[tuple[int, ...], int]] = None
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
        if self._EnableCh_REG != value:
            self._EnableCh_REG = value
            self._ch_cache = None

    def _compute_ch_cache(self) -> tuple[tuple[int, ...], int]:
        """Derive the enabled-channel tuple and count from EnableCh_REG."""
        enabled = tuple(i for i in range(16) if self._EnableCh_REG & (1 << i))
        self._ch_cache = (enabled, len(enabled))
        return self._ch_cache

    @property
    def _NumChannels(self) -> int:
        """Count of enabled channels (derived from EnableCh_REG)."""
        return (self._ch_cache or self._compute_ch_cache())[1]

    @property
    def _enabled_channels(self) -> tuple[int, ...]:
        """Tuple of enabled channel numbers (derived from EnableCh_REG)."""
        return (self._ch_cache or self._compute_ch_cache())[0]

    @property
    def _HorizontalEnd(self) -> int:
        """Last column of the horizontal window (HorizontalStart + HorizontalCount)."""
        return self.HorizontalStart_REG + self.HorizontalCount_REG

    def _channel(self, sequential_index: int) -> int:
        """Map sequential index to actual channel number from EnableCh_REG bitmask."""
        return self._enabled_channels[sequential_index]


# =============================================================================
# Main DataPort Class
# =============================================================================

class DataPort:
    """SoundWire Data Port configuration and state management.

    External interface:
        - config: DataPortConfig for configuration/register attributes
        - dp_index: Canonical position index (0-15)
        - fcp: Parallel FlowControlPort for DRQ/guards/tails
        - reset(): Reset state for a new frame
        - next_bit_slot(): Get slot for current position (auto-advances)
        - current_row_in_interval: DP's current row index within its interval

    State is internal and not accessible from outside.
    """

    def __init__(self, device: 'Device', dp_index: int) -> None:
        """Initialize a DataPort.

        Args:
            device: The parent Device containing this data port
            dp_index: Canonical position index (0-15) for this data port
        """
        # Parent device (interface accessed via self._device._interface).
        self._device = device

        # Canonical position index — used for flat list ordering and CSV mapping.
        self.dp_index = dp_index

        self.config = DataPortConfig()

        self._state = DataPortState()

        # Flow Control Port — parallel source for DRQ/guards/tails (public).
        self.fcp = FlowControlPort(self)

    @property
    def current_row_in_interval(self) -> int:
        """Current row index within this DP's interval.

        Snapshot before calling next_bit_slot (which may advance it) and pass
        to fcp.next_bit_slot.
        """
        return self._state.current_row_in_interval

    # =========================================================================
    # Counter Advancement (transport → bit)
    # =========================================================================

    def _advance_channel_group(self) -> None:
        """Advance to next channel group.

        In SRI mode with groups exhausted, prepare the next transport within
        the row. In Normal mode with groups exhausted, terminate the pattern.
        Otherwise, move to the next channel group.

        Each branch leaves `phase` in its final state (no post-hoc fixup):
        the current slot is treated as the first slot of any inter-group gap,
        so one spacing tick is consumed up-front.
        """
        pattern_complete = (self._state.channel_group_base + self._state.channel_group_size
                            >= self.config._NumChannels)

        if pattern_complete and not self.config.SubRowInterval_REG:
            self._state.phase = TransportPhase.PATTERN_DONE
            return

        if pattern_complete:
            # SRI mode: prepare for next transport within the row.
            # (Final PATTERN_DONE in SRI is driven by HorizontalEnd in _slot().)
            self.reset_transport()
        else:
            # Move to next channel group.
            self._state.channel_group_base += self._state.channel_group_size
            remaining_channels = self.config._NumChannels - self._state.channel_group_base
            if remaining_channels > self._state.channel_group_size:
                remaining_channels = self._state.channel_group_size
            self._state.channels_in_group_remaining = remaining_channels - 1

            # Reset per-group counters. Sample counter restarts from sample_group_base
            # because all channel groups within a transport share the same sample(s):
            #   SampleGrouping=0 → each CG gets sample_group_base (one sample per transport)
            #   SampleGrouping>0 → each CG processes sample_group_base..base+N
            self._state.samples_in_group_remaining = self.config.SampleGrouping_REG
            if (self.config.ChannelGrouping_REG > 0 and
                self.config.ChannelGrouping_REG < self.config._NumChannels):
                self._state.sample = self._state.sample_group_base
            self._state.bit = self.config.SampleSize_REG
            self._state.channel_index = self._state.channel_group_base
            self._state.txp_sent = False

        # Inter-group gap resolution. Both "next CG" and "SRI next transport"
        # land here. The current slot is the first slot of the gap.
        if self.config.Spacing_REG == 0:
            # No gap: Normal mode kills the row here; SRI ends this transport.
            self._state.phase = TransportPhase.ROW_DONE
        else:
            # Consume the current slot as the first spacing tick.
            self._state.spacing_slots_remaining = self.config.Spacing_REG - 1
            self._state.phase = (TransportPhase.SPACING if self.config.Spacing_REG > 1
                                 else TransportPhase.ACTIVE)

    def _advance_sample(self) -> None:
        """Advance to next sample, calling _advance_channel_group when sample group exhausted."""
        self._state.sample += 1
        self._state.samples_in_group_remaining -= 1

        if self._state.samples_in_group_remaining < 0:
            self._advance_channel_group()
        else:
            self._state.bit = self.config.SampleSize_REG
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
            self._state.bit = self.config.SampleSize_REG

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
        self._state.phase = TransportPhase.ACTIVE
        self._state.spacing_slots_remaining = 0

        self._state.sample_group_base = self._state.sample
        self._state.samples_in_group_remaining = self.config.SampleGrouping_REG

        self._state.channel_group_base = 0
        self._state.channel_index = 0
        if (self.config.ChannelGrouping_REG == 0
                or self.config.ChannelGrouping_REG > self.config._NumChannels):
            self._state.channel_group_size = self.config._NumChannels
        else:
            self._state.channel_group_size = self.config.ChannelGrouping_REG
        self._state.channels_in_group_remaining = self._state.channel_group_size - 1

        self._state.bit = self.config.SampleSize_REG
        self._state.txp_sent = False
        self.fcp.reset_for_interval()

    def _check_skipping_at_offset(self) -> bool:
        """Apply the skipping accumulator at an offset row.

        When the accumulator reaches SkippingDenominator, mark the interval as
        skipped (phase = PATTERN_DONE) and return True; otherwise return False.
        """
        if self.config.SkippingNumerator_REG == 0:
            return False
        self._state.skipping_accumulator += self.config.SkippingNumerator_REG
        if self._state.skipping_accumulator < self._device._interface.SkippingDenominator_REG:
            return False
        # Atomic transition — PATTERN_DONE encodes both "skip" and "row done".
        self._state.phase = TransportPhase.PATTERN_DONE
        self._state.skipping_accumulator -= self._device._interface.SkippingDenominator_REG
        return True

    def reset_row(self) -> None:
        """Row-scope reset: per-row state cleared before the next column sweep."""
        self._state.column = 0
        self._state.wide_replay = None
        self._state.post_data_queue.clear()
        self.fcp.reset_for_row()

    def _reset_interval_wrap(self) -> None:
        """Interval-scope reset on wrap (called from _advance_row only).

        Clears interval-scoped flags between intervals. Required when Offset_REG > 0,
        because reset_transport() doesn't run until row == Offset_REG; without this,
        stale drq_sent/phase from the previous interval would corrupt the
        new interval's first DRQ trigger and slot logic.
        """
        self._state.current_row_in_interval = 0
        self._state.phase = TransportPhase.IDLE
        self.fcp.reset_drq_sent()
        # Advance sample base for new interval (truncation recovery for normal mode).
        # SRI is excluded: each transport is self-contained per row and advances the
        # sample counter naturally via _advance_channel_group -> reset_transport, so
        # applying the expected-samples recompute would double-count the last group.
        if (not self.config.SubRowInterval_REG
                and self.config.SampleGrouping_REG > 0
                and self.config.ChannelGrouping_REG > 0
                and self.config.ChannelGrouping_REG < self.config._NumChannels):
            self._state.sample = (self._state.sample_group_base
                                  + self.config.SampleGrouping_REG + 1)

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

        # Wrap around at end of interval.
        if self._state.current_row_in_interval > self.config.Interval_REG:
            self._reset_interval_wrap()

        if self._state.current_row_in_interval == self.config.Offset_REG:
            if self._check_skipping_at_offset():
                return
            self.reset_transport()

    def _advance_column(self) -> None:
        """Advance position by one column, wrapping to the next row at the edge."""
        self._state.column += 1
        if self._state.column >= self._device._interface.num_columns:
            self._advance_row()

    # =========================================================================
    # Slot Emission
    # =========================================================================

    def _slot(self) -> BitSlotState:
        """Return the BitSlotState at current position based on internal state.

        Returns a fresh BitSlotState (DATA with no data) when no data is owned.
        """
        slot = BitSlotState(slot_type=SlotType.DATA)

        if self._state.phase in (TransportPhase.ROW_DONE, TransportPhase.PATTERN_DONE):
            return slot

        if self.config._NumChannels == 0:
            return slot

        if self._state.phase in (TransportPhase.ACTIVE, TransportPhase.SPACING):
            if self._state.column < self.config.HorizontalStart_REG:
                return slot

            if self._state.column > self.config._HorizontalEnd:
                # Transport window exhausted on this row. Clear spacing so stale gaps
                # don't leak into the next row's first emission window.
                if self._state.spacing_slots_remaining > 0:
                    self._state.spacing_slots_remaining = 0
                self._state.phase = TransportPhase.ROW_DONE
                return slot

            if self._state.phase == TransportPhase.SPACING:
                self._state.spacing_slots_remaining -= 1
                if self._state.spacing_slots_remaining <= 0:
                    self._state.phase = TransportPhase.ACTIVE
                return slot

            # phase == ACTIVE here — emit data (or TxPresent).
            direction = DirectionType.SINK if self.config.PortDirection_REG else DirectionType.SOURCE
            if (self.config.FlowMode_REG in (FlowMode.TX_CONTROLLED, FlowMode.ASYNC)
                    and not self._state.txp_sent
                    and self._state.bit == self.config.SampleSize_REG):
                slot = BitSlotState(
                    slot_type=SlotType.TX_PRESENT,
                    direction=direction,
                    data=BitSlotData(
                        sample=self._state.sample,
                        channel=self.config._channel(self._state.channel_index),
                        bit=0
                    )
                )
                self._state.txp_sent = True
            else:
                slot = BitSlotState(
                    slot_type=SlotType.DATA,
                    direction=direction,
                    data=BitSlotData(
                        sample=self._state.sample,
                        channel=self.config._channel(self._state.channel_index),
                        bit=self._state.bit
                    )
                )
                self._advance_bit()

        return slot

    def _prime_post_data_queue(self) -> None:
        """Prime the post-data queue after a source-port data slot.

        Order is fixed: guard (if enabled) first, then TailWidth tails. Sink
        ports emit no guards/tails.
        """
        self._state.post_data_queue.clear()
        if self.config.PortDirection_REG:
            return  # sink ports emit no guards/tails
        if self.config.GuardEnable_REG:
            self._state.post_data_queue.append(
                SlotType.GUARD_1 if self.config.GuardPolarity_REG else SlotType.GUARD_0
            )
        for _ in range(self.config.TailWidth_REG):
            self._state.post_data_queue.append(SlotType.TAIL)

    # =========================================================================
    # Public Interface
    # =========================================================================

    def reset(self) -> None:
        """Reset all runtime state for a fresh drawing pass.

        Call before starting a new frame.
        """
        self._state.reset_frame()
        self.fcp.reset()
        # Prime row 0 if it's already at Offset_REG.
        if self._state.current_row_in_interval == self.config.Offset_REG:
            if not self._check_skipping_at_offset():
                self.reset_transport()

    def next_bit_slot(self) -> BitSlotState:
        """Get the slot for the current column and auto-advance.

        The only external frame-rendering entry point. Handles wide bits, guards,
        tails, and TxP internally; FCP guards/tails emit via self.fcp separately.

        Returns:
            BitSlotState for current position (EMPTY if not active).
        """
        # Wide replay in progress: emit stored slot, decrement counter.
        if self._state.wide_replay is not None:
            replay = self._state.wide_replay
            replay.remaining -= 1
            if replay.remaining == 0:
                self._state.wide_replay = None
            self._advance_column()
            return replay.slot

        # Probe current position for a data slot.
        slot = self._slot()
        if slot.is_owned():
            # Clip wide-bit data at HorizontalEnd; guards/tails may extend past.
            if self.config.BitWidth_REG > 0:
                remaining = min(
                    self.config.BitWidth_REG,
                    max(0, self.config._HorizontalEnd - self._state.column)
                )
                if remaining > 0:
                    self._state.wide_replay = WideBitReplay(slot=slot, remaining=remaining)
            # Source ports seed post-data queue for guards/tails.
            self._prime_post_data_queue()
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
