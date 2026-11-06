"""
Bit slot data structures for SWI3S visualizer.

Type-safe data structures for the state of bit slots in the frame.

Classes:
    BitSlotData: Data for a normal bit slot
    BitSlotState: Complete state of a bit slot
"""

from dataclasses import dataclass
from typing import Optional

from .enums import DirectionType, DisplayField, SlotType

# =============================================================================
# BitSlotData Class
# =============================================================================

@dataclass(frozen=True)
class BitSlotData:
    """Data for a normal bit slot.

    Represents the sample, channel, and bit information
    for a data-carrying slot. Frozen so shared instances (e.g. from wide-bit
    replay in DataPort) cannot be corrupted by accidental mutation.

    The `sample_in_group` field holds the transport-scoped sample index
    (0..SampleGrouping_REG). The global/absolute sample counter is not
    tracked in DataPort state — it is reconstructed externally by the
    engine from its own per-DP transport count and SampleGrouping_REG.
    Callers that need a label with an absolute sample (e.g. frame_renderer)
    may pass the global value into this field at label-formatting time.
    """
    sample_in_group: int = 0  # Transport-scoped sample index (0..SG)
    channel: int = 0
    bit: int = 0

    def to_label(self, display_fields: Optional[DisplayField] = None) -> str:
        """Generate display label for the bit slot.

        Args:
            display_fields: Optional DisplayField flags indicating which fields to show.
                           If None, defaults to channel + bit (CXBY format).
                           If empty (no flags), returns empty string.

        Returns:
            String like "SXCY", "SXBY", "CXBY", or "" depending on selected fields
        """
        if display_fields is None:
            # Default: channel + bit
            return f"C{self.channel}B{self.bit}"

        parts = []
        if DisplayField.SAMPLE in display_fields:
            parts.append(f"S{self.sample_in_group}")
        if DisplayField.CHANNEL in display_fields:
            parts.append(f"C{self.channel}")
        if DisplayField.BIT in display_fields:
            parts.append(f"B{self.bit}")

        return "".join(parts)  # Returns empty string if no fields selected


# =============================================================================
# BitSlotState Class
# =============================================================================

@dataclass
class BitSlotState:
    """Complete state of a bit slot.

    Represents all information about a bit slot including its type,
    direction, ownership, and optional data content.
    """
    slot_type: SlotType
    direction: DirectionType = DirectionType.SOURCE
    device_num: int = 0
    dp_num: int = 0
    data: Optional[BitSlotData] = None
    fresh_transport: bool = False

    def is_data_slot(self) -> bool:
        """Check if this is a data-carrying slot.

        Returns:
            True if slot_type is DATA
        """
        return self.slot_type == SlotType.DATA

    def is_owned(self) -> bool:
        """Check if this slot is owned by a data port.

        Returns:
            True if slot is not a normal/empty type but owned, or has data
        """
        # EMPTY slots are never owned
        if self.slot_type == SlotType.EMPTY:
            return False
        # A slot is owned if it's not DATA type or if it has data
        # DATA slots without data are not owned
        return self.slot_type != SlotType.DATA or self.data is not None

    def get_label(self) -> str:
        """Get display label for this slot.

        Returns:
            Label string appropriate for the slot type
        """
        if self.is_data_slot() and self.data:
            return self.data.to_label()
        elif self.slot_type == SlotType.GUARD_0:
            return "G0"
        elif self.slot_type == SlotType.GUARD_1:
            return "G1"
        elif self.slot_type == SlotType.TAIL:
            return "tail"
        elif self.slot_type == SlotType.HANDOVER:
            return "TA"
        elif self.slot_type == SlotType.CDS:
            return "CDS"
        elif self.slot_type == SlotType.S0:
            return "S0"
        elif self.slot_type == SlotType.S1:
            return "S1"
        elif self.slot_type == SlotType.TX_PRESENT:
            # TxP bit - label includes channel number from data
            if self.data:
                return f"TxP{self.data.channel}"
            return "TxP"
        elif self.slot_type == SlotType.DRQ:
            # DRQ bit - Data Request for Rx Controlled or Async flow modes
            return "DRQ"
        elif self.slot_type == SlotType.EMPTY:
            return ""
        else:
            return "not owned"
