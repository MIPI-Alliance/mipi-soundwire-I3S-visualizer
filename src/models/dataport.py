"""DataPort configuration and state management.

Classes:
    DataPortState: Runtime state for frame rendering
    DataPortConfig: Configuration and register values
    DataPort: Combines config, state, and rendering algorithm

Must remain UI-independent (no tkinter / widget / dialog imports) — see
CLAUDE.md. Usable headless as a library.

`phase: TransportPhase` tracks the transport lifecycle:
    ACTIVE       emitting inside the horizontal window
    SPACING      inter-channel-group / inter-transport gap
    ROW_DONE     row's window exhausted; transport still alive across wrap
    PATTERN_DONE transport complete or interval skipped; emission gated

Normal vs SRI:
    - Normal: one multi-row transport per SSP interval; channel groups
      structure the burst/space pattern within the transport.
    - SRI:    multiple transports per row; HorizontalEnd bounds emission.

Reset scopes (outer → inner):
    Reset     DataPort.reset                   hardware reset
    Interval  row-counter rollover             skipping check; arm transport
    Row       inlined in _advance_row          clear per-row containers
    Transport _reset_transport                 re-init transport-scope state

Counter cascade (exhaustion propagates outward):
    bit → channel → sample → channel_group → transport completion
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from .bit_slot import BitSlotData, BitSlotState
from .enums import SlotType, DirectionType, FlowMode, TransportPhase

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

    Mutable state updated as the rendering algorithm walks rows and columns.
    Fields are grouped by reset scope; see the module docstring.
    """

    def __init__(self) -> None:
        # Persistent containers created once; cleared (not reassigned) on reset.
        self.post_data_queue: deque[SlotType] = deque()
        self.wide_replay: Optional[WideBitReplay] = None
        self.reset()

    def reset(self) -> None:
        """Hardware reset — full re-init of all runtime state.

        Leaves `phase = PATTERN_DONE` (gated / safe-off); callers must
        follow with `DataPort._start_interval()` to arm the first transport.
        `DataPort.reset()` chains both.
        """
        # Position
        self.column: int = 0

        # Interval-scope
        self.row_in_interval: int = 0
        self.phase: TransportPhase = TransportPhase.PATTERN_DONE

        # Cycles with the effective SSP interval; zeroed at hardware reset
        # so every render starts deterministically.
        self.skipping_accumulator: int = 0

        # Transport-scope (set by _reset_transport at every interval start
        # and at each SRI mid-row transport rollover).
        self.sample_in_group: int = 0
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

    Pure data container; validation lives in context-specific validators
    (UI, batch, etc.). Fields ending `_REG` mirror hardware registers.
    """

    def __init__(self) -> None:
        # DeviceNumber_REG lives on Interface.dp_device_assignments, not here.
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

    def _channel(self, index: int) -> int:
        """Map sequential index to actual channel number from EnableCh_REG bitmask."""
        return self._enabled_channels[index]


# =============================================================================
# Main DataPort Class
# =============================================================================

class DataPort:
    """SoundWire Data Port — config + state + rendering algorithm.

    Public surface:
        config               configuration / register attributes
        dp_index             canonical position index (0-15)
        row_in_interval      current row within this DP's interval
        reset()              hardware reset before a new rendering pass
        next_bit_slot()      emit slot at current position (auto-advances)

    The companion FlowControlPort lives on the parent Interface
    (`interface.flow_control_ports[dp_index]`) and is driven by the engine —
    DataPort holds no FCP back-reference (see CLAUDE.md hardware-model policy).
    """

    def __init__(self, device: 'Device', dp_index: int) -> None:
        self._device = device  # interface reachable via self._device._interface
        self.dp_index = dp_index
        self.config = DataPortConfig()
        self._state = DataPortState()

    @property
    def row_in_interval(self) -> int:
        """Current row within this DP's interval.

        Snapshot before next_bit_slot (which may advance it) and pass to the
        companion FCP's next_bit_slot.
        """
        return self._state.row_in_interval

    # =========================================================================
    # Counter Advancement (transport → bit)
    # =========================================================================

    def _advance_channel_group(self) -> None:
        """Advance to the next channel group (or to the next transport in SRI).

        Leaves `phase` in its final state; the current slot is the first
        tick of any inter-group/inter-transport gap, so one spacing tick
        is consumed up-front.
        """
        pattern_complete = (self._state.channel_group_base + self._state.channel_group_size
                            >= self.config._num_channels)

        if pattern_complete:
            if self.config.SubRowInterval_REG:
                # SRI mid-row transport rollover. Final PATTERN_DONE in SRI
                # is driven by the HorizontalEnd guard in _probe_slot.
                self._reset_transport()
            else:
                # Normal mode: transport done — wait for row-counter rollover.
                self._state.phase = TransportPhase.PATTERN_DONE
                return
        else:
            self._state.channel_group_base += self._state.channel_group_size
            remaining_channels = self.config._num_channels - self._state.channel_group_base
            if remaining_channels > self._state.channel_group_size:
                remaining_channels = self._state.channel_group_size
            self._state.channels_in_group_remaining = remaining_channels - 1

            # Sample counter restarts at 0 because all channel groups within
            # one transport share the same sample(s):
            #   SampleGrouping=0 → each CG processes sample_in_group 0 only
            #   SampleGrouping>0 → each CG processes sample_in_group 0..SG
            self._state.samples_in_group_remaining = self.config.SampleGrouping_REG
            if (self.config.ChannelGrouping_REG > 0 and
                self.config.ChannelGrouping_REG < self.config._num_channels):
                self._state.sample_in_group = 0
            self._state.bit = self.config.SampleSize_REG
            self._state.channel_index = self._state.channel_group_base
            self._state.txp_sent = False

        # Inter-group gap. Both the SRI-next-transport and next-CG paths land
        # here; the current slot is consumed as the first tick of the gap.
        if self.config.Spacing_REG == 0:
            # No gap: Normal mode already returned PATTERN_DONE above; SRI
            # ends the row and the next row's _start_interval arms a fresh
            # transport.
            self._state.phase = TransportPhase.ROW_DONE
        else:
            self._state.spacing_slots_remaining = self.config.Spacing_REG - 1
            self._state.phase = (TransportPhase.SPACING if self.config.Spacing_REG > 1
                                 else TransportPhase.ACTIVE)

    def _advance_sample(self) -> None:
        """Next sample; cascades to _advance_channel_group on exhaustion."""
        self._state.sample_in_group += 1
        self._state.samples_in_group_remaining -= 1

        if self._state.samples_in_group_remaining < 0:
            self._advance_channel_group()
        else:
            self._state.bit = self.config.SampleSize_REG
            self._state.channel_index = self._state.channel_group_base
            self._state.channels_in_group_remaining = self._state.channel_group_size - 1
            self._state.txp_sent = False

    def _advance_channel(self) -> None:
        """Next channel; cascades to _advance_sample on exhaustion."""
        self._state.channel_index += 1
        self._state.txp_sent = False
        self._state.channels_in_group_remaining -= 1

        if self._state.channels_in_group_remaining < 0:
            self._advance_sample()
        else:
            self._state.bit = self.config.SampleSize_REG

    def _advance_bit(self) -> None:
        """Next bit; cascades to _advance_channel on exhaustion."""
        self._state.bit -= 1
        if self._state.bit < 0:
            self._advance_channel()

    # =========================================================================
    # Scope-Named Resets
    # =========================================================================

    def _reset_transport(self) -> None:
        """Re-init transport-scope state for a new transport pattern.

        Called from _start_interval on every row-counter rollover and, SRI
        only, inline from _advance_channel_group on mid-row pattern
        completion. Does NOT touch column, row counter, wide replay, or
        post-data queue (those are row-scoped).
        """
        self._state.phase = TransportPhase.ACTIVE
        self._state.spacing_slots_remaining = 0

        self._state.sample_in_group = 0
        self._state.samples_in_group_remaining = self.config.SampleGrouping_REG

        self._state.channel_group_base = 0
        self._state.channel_index = 0
        if (self.config.ChannelGrouping_REG == 0
                or self.config.ChannelGrouping_REG > self.config._num_channels):
            self._state.channel_group_size = self.config._num_channels
        else:
            self._state.channel_group_size = self.config.ChannelGrouping_REG
        self._state.channels_in_group_remaining = self._state.channel_group_size - 1

        self._state.bit = self.config.SampleSize_REG
        self._state.txp_sent = False

    def _apply_skipping(self) -> bool:
        """Advance the skipping accumulator at the start of an SSP interval.

        When the accumulator reaches SkippingDenominator, mark the interval
        skipped (phase = PATTERN_DONE) and return True; otherwise False.
        """
        if self.config.SkippingNumerator_REG == 0:
            return False
        self._state.skipping_accumulator += self.config.SkippingNumerator_REG
        if self._state.skipping_accumulator < self._device._interface.SkippingDenominator_REG:
            return False
        # PATTERN_DONE encodes both "skip" and "row done" in one state.
        self._state.phase = TransportPhase.PATTERN_DONE
        self._state.skipping_accumulator -= self._device._interface.SkippingDenominator_REG
        return True

    def _start_interval(self) -> None:
        """Hardware behaviour at row-counter rollover: check skipping; if
        not skipped, arm a fresh transport. Emission is row-gated by
        Offset_REG in _probe_slot.
        """
        if self._apply_skipping():
            return
        self._reset_transport()

    # =========================================================================
    # Position Management
    # =========================================================================

    def _advance_row(self) -> None:
        """Advance the row counter and prepare per-row state."""
        # Row-scope reset: clear per-row containers.
        self._state.column = 0
        self._state.wide_replay = None
        self._state.post_data_queue.clear()

        # SRI row-cut: ROW_DONE was set by the HorizontalEnd guard while a
        # transport was still alive. Fresh row resumes emission. Interval
        # rollover (below) overrides this if it fires.
        if self._state.phase == TransportPhase.ROW_DONE:
            self._state.phase = TransportPhase.ACTIVE

        self._state.row_in_interval += 1

        # Row-counter rollover = SSP interval boundary. Fire interval-start
        # so state is identical at every interval (including post-reset).
        if self._state.row_in_interval > self.config.Interval_REG:
            self._state.row_in_interval = 0
            self._start_interval()

    def _advance_column(self) -> None:
        """Advance column; wrap to the next row at the right edge."""
        self._state.column += 1
        if self._state.column >= self._device._interface.num_columns:
            self._advance_row()

    # =========================================================================
    # Slot Emission
    # =========================================================================

    def _probe_slot(self) -> BitSlotState:
        """Return the BitSlotState at current position.

        EMPTY when outside the transport window, in a spacing gap, or post
        PATTERN_DONE. DATA or TX_PRESENT (with BitSlotData payload) when
        emitting.
        """

        if self._state.phase in (TransportPhase.ROW_DONE, TransportPhase.PATTERN_DONE):
            return BitSlotState(slot_type=SlotType.EMPTY)

        if self.config._num_channels == 0:
            return BitSlotState(slot_type=SlotType.EMPTY)

        # Pre-Offset row-gate: transport is armed but emission is gated
        # until the row counter reaches Offset_REG.
        if self._state.row_in_interval < self.config.Offset_REG:
            return BitSlotState(slot_type=SlotType.EMPTY)

        if self._state.phase in (TransportPhase.ACTIVE, TransportPhase.SPACING):
            if self._state.column < self.config.HorizontalStart_REG:
                return BitSlotState(slot_type=SlotType.EMPTY)

            if self._state.column > self.config._horizontal_end:
                # Transport window exhausted on this row. Clear spacing so
                # stale gaps don't leak into next row's first emission.
                if self._state.spacing_slots_remaining > 0:
                    self._state.spacing_slots_remaining = 0
                self._state.phase = TransportPhase.ROW_DONE
                return BitSlotState(slot_type=SlotType.EMPTY)

            if self._state.phase == TransportPhase.SPACING:
                self._state.spacing_slots_remaining -= 1
                if self._state.spacing_slots_remaining <= 0:
                    self._state.phase = TransportPhase.ACTIVE
                return BitSlotState(slot_type=SlotType.EMPTY)

            # phase == ACTIVE — emit data (or TxPresent).
            direction = DirectionType.SINK if self.config.PortDirection_REG else DirectionType.SOURCE
            if (self.config.FlowMode_REG in (FlowMode.TX_CONTROLLED, FlowMode.ASYNC)
                    and not self._state.txp_sent
                    and self._state.bit == self.config.SampleSize_REG):
                self._state.txp_sent = True
                return BitSlotState(
                    slot_type=SlotType.TX_PRESENT,
                    direction=direction,
                    data=BitSlotData(
                        sample_in_group=self._state.sample_in_group,
                        channel=self.config._channel(self._state.channel_index),
                        bit=0
                    ),
                )
            slot = BitSlotState(
                slot_type=SlotType.DATA,
                direction=direction,
                data=BitSlotData(
                    sample_in_group=self._state.sample_in_group,
                    channel=self.config._channel(self._state.channel_index),
                    bit=self._state.bit
                ),
            )
            self._advance_bit()
            return slot

        return BitSlotState(slot_type=SlotType.EMPTY)

    def _prime_post_data_queue(self) -> None:
        """Seed the post-data queue after a source-port data slot.

        Order: guard (if enabled) then TailWidth tails. Sink ports emit
        no guards/tails.
        """
        self._state.post_data_queue.clear()
        if self.config.PortDirection_REG:
            return
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
        """Hardware reset before a new rendering pass.

        Chains DataPortState.reset() with the interval-start sequence so
        the initial interval begins in the same state as every subsequent
        one (following a row-counter rollover). The companion FlowControlPort
        must be reset separately by the engine.
        """
        self._state.reset()
        self._start_interval()

    def next_bit_slot(self) -> BitSlotState:
        """Emit the slot at the current position and auto-advance.

        The sole external frame-rendering entry point. Handles wide bits,
        guards, tails, and TxP internally; the companion FCP emits
        separately (driven by the engine).
        """
        if self._state.wide_replay is not None:
            replay = self._state.wide_replay
            replay.remaining -= 1
            if replay.remaining == 0:
                self._state.wide_replay = None
            self._advance_column()
            return replay.slot

        slot = self._probe_slot()
        if slot.is_owned():
            # Clip wide-bit replay at HorizontalEnd; guards/tails may extend past.
            if self.config.BitWidth_REG > 0:
                remaining = min(
                    self.config.BitWidth_REG,
                    max(0, self.config._horizontal_end - self._state.column)
                )
                if remaining > 0:
                    self._state.wide_replay = WideBitReplay(slot=slot, remaining=remaining)
            self._prime_post_data_queue()
            self._advance_column()
            return slot

        if self._state.post_data_queue:
            slot_type = self._state.post_data_queue.popleft()
            self._advance_column()
            return BitSlotState(slot_type=slot_type, direction=DirectionType.SOURCE)

        self._advance_column()
        return BitSlotState(slot_type=SlotType.EMPTY)
