"""Flow Control Port (FCP) - independent bus source for DRQ + guards + tails.

The FCP is a parallel state machine to the DataPort's data path. Both are
iterated by the engine; when both emit at the same column, the bus model's
existing SAME_DEVICE clash detector surfaces it as a configuration error.

No arbitration or priority logic lives in the core loop. Clash handling is
delegated to the clash detector.

Classes:
    FlowControlPortConfig: FCP register values (HorizontalStart, BitWidth, etc.)
    FlowControlPortState: Runtime state for the DRQ/guard/tail state machine
    FlowControlPort: Emits one slot per column; EMPTY when not claiming

NOTE: This module must remain UI-independent - no tkinter, widgets, dialogs,
or UI framework imports. See CLAUDE.md for policy.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from .bit_slot import BitSlotState
from .enums import SlotType, DirectionType, FlowMode

if TYPE_CHECKING:
    from .dataport import DataPort


# =============================================================================
# Configuration Class
# =============================================================================

class FlowControlPortConfig:
    """Configuration and register values for an FCP.

    Holds the six FCP-specific registers. Names drop the FCP_ prefix because
    they now live in their own namespace (accessed via `dp.fcp.config.*`).
    """

    def __init__(self) -> None:
        self.HorizontalStart_REG: int = 0
        self.BitWidth_REG: int = 0
        self.TailWidth_REG: int = 0
        self.Offset_REG: int = 0
        self.GuardEnable_REG: bool = False
        self.GuardPolarity_REG: bool = False


# =============================================================================
# Runtime State Class
# =============================================================================

class FlowControlPortState:
    """Runtime state for the FCP state machine.

    Owns its own wide-bit replay buffer (not shared with the DataPort).
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Reset all runtime state for a fresh drawing pass."""
        self.drq_sent: bool = False
        self.tails_left: int = 0
        self.guard_left: bool = False
        self.wide_bit_slots_remaining: int = 0
        self.stored_wide_bit_slot: Optional[BitSlotState] = None


# =============================================================================
# Main FlowControlPort Class
# =============================================================================

class FlowControlPort:
    """Flow Control Port - emits DRQ + tails + guards.

    Operates as a parallel state machine to the DataPort's data path. Both
    are iterated by the engine; if they emit at the same column, the bus
    model's SAME_DEVICE clash detector surfaces it as a config error.

    Active only in RX_CONTROLLED or ASYNC flow modes. Reads FlowMode_REG and
    PortDirection_REG from the parent DataPort (DRQ direction is inverted
    relative to the DP's data direction).
    """

    def __init__(self, dataport: 'DataPort') -> None:
        """Initialize the FCP with a reference to its parent DataPort.

        Args:
            dataport: Parent DataPort (used to read FlowMode_REG and
                PortDirection_REG from its config).
        """
        self._dataport = dataport
        self.config = FlowControlPortConfig()
        self._state = FlowControlPortState()

    def reset(self) -> None:
        """Reset all runtime state for a fresh drawing pass."""
        self._state.reset()

    def reset_for_interval(self) -> None:
        """Clear interval-scoped flags. Called by DP when starting a new interval."""
        self._state.drq_sent = False
        self._state.tails_left = 0
        self._state.guard_left = False

    def reset_drq_sent(self) -> None:
        """Clear drq_sent flag. Called by DP at interval wrap when _reset_transport() doesn't run."""
        self._state.drq_sent = False

    def reset_for_row(self) -> None:
        """Clear wide-bit replay state. Called by DP at row boundary."""
        self._state.wide_bit_slots_remaining = 0
        self._state.stored_wide_bit_slot = None

    def fetch_bit_slot(self, column: int, row_in_interval: int) -> BitSlotState:
        """Return the slot FCP emits at this column.

        Handles (in priority order):
            1. Wide-bit replay (from a prior DRQ with BitWidth_REG > 0)
            2. Pending guard (after a DRQ with GuardEnable_REG)
            3. Pending tails (after a DRQ with TailWidth_REG)
            4. DRQ trigger at (Offset_REG, HorizontalStart_REG) in RX_CONTROLLED/ASYNC

        Args:
            column: Current column in the row.
            row_in_interval: DP's row_in_interval (snapshotted before
                the DP's data path runs, so FCP sees the current row's value
                rather than the advanced-past value).

        Returns:
            BitSlotState for the column. Returns EMPTY when FCP does not
            claim this column.
        """
        dp_config = self._dataport.config

        # Wide-bit replay (same slot across multiple columns)
        if self._state.wide_bit_slots_remaining > 0:
            self._state.wide_bit_slots_remaining -= 1
            if self._state.stored_wide_bit_slot is None:
                raise RuntimeError("stored_wide_bit_slot uninitialized with wide_bit_slots_remaining > 0")
            return BitSlotState(
                slot_type=self._state.stored_wide_bit_slot.slot_type,
                direction=self._state.stored_wide_bit_slot.direction,
                data=self._state.stored_wide_bit_slot.data,
            )

        # Pending guard from a prior DRQ
        if self._state.guard_left:
            self._state.guard_left = False
            return BitSlotState(
                slot_type=SlotType.GUARD_1 if self.config.GuardPolarity_REG else SlotType.GUARD_0,
                direction=DirectionType.SOURCE,
            )

        # Pending tails from a prior DRQ
        if self._state.tails_left > 0:
            self._state.tails_left -= 1
            return BitSlotState(
                slot_type=SlotType.TAIL,
                direction=DirectionType.SOURCE,
            )

        # DRQ trigger - only in RX_CONTROLLED or ASYNC flow modes
        fcp_active_row = (row_in_interval == self.config.Offset_REG)
        if (dp_config.FlowMode_REG in (FlowMode.RX_CONTROLLED, FlowMode.ASYNC) and
                fcp_active_row and
                not self._state.drq_sent and
                column == self.config.HorizontalStart_REG):
            # DRQ direction is opposite to the DP's data direction.
            # Sink DP sends DRQ (SOURCE); Source DP receives DRQ (SINK).
            slot = BitSlotState(
                slot_type=SlotType.DRQ,
                direction=DirectionType.SOURCE if dp_config.PortDirection_REG else DirectionType.SINK,
            )
            self._state.drq_sent = True

            # Guards and tails only follow SOURCE DRQs (when this DP sends DRQ)
            if slot.direction == DirectionType.SOURCE:
                self._state.guard_left = self.config.GuardEnable_REG
                self._state.tails_left = self.config.TailWidth_REG

            # Wide-bit replay setup for DRQ
            if self.config.BitWidth_REG > 0:
                self._state.wide_bit_slots_remaining = self.config.BitWidth_REG
                self._state.stored_wide_bit_slot = slot

            return slot

        # FCP does not claim this column
        return BitSlotState(slot_type=SlotType.EMPTY)
