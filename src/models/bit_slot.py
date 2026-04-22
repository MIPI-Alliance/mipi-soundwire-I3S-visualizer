"""
Bit slot data structures for SWI3S visualizer.

This module provides type-safe data structures to replace string-based
state encoding for bit slots in the frame.

Classes:
    BitSlotData: Data for a normal bit slot
    BitSlotState: Complete state of a bit slot
"""

from dataclasses import dataclass
from typing import Optional
import re

from .enums import SlotType, DirectionType, DisplayField


# =============================================================================
# Pattern Constants
# =============================================================================

# Pre-compiled regex patterns for label parsing (performance optimization)
# These patterns are used by bit_slot.py and canvas_renderer.py
PATTERN_CB = re.compile(r"^C(\d+)B(-?\d+)$")      # CXbY format
PATTERN_SC = re.compile(r"^S(\d+)C(\d+)$")       # SXcY format
PATTERN_SB = re.compile(r"^S(\d+)B(-?\d+)$")     # SXbY format
PATTERN_SCB = re.compile(r"^S(\d+)C(\d+)B(-?\d+)$")  # SXcYbZ format
PATTERN_C = re.compile(r"^C(\d+)$")              # CX format (channel only, for merged bits)
PATTERN_S = re.compile(r"^S(\d+)$")              # SX format (sample only, for merged bits)
PATTERN_TXP = re.compile(r"^TxP(\d+)$")          # TxPn format (TxPresent bit with channel)

# Legacy aliases (underscore prefix) - deprecated, use new names
_PATTERN_CB = PATTERN_CB
_PATTERN_SC = PATTERN_SC
_PATTERN_SB = PATTERN_SB
_PATTERN_SCB = PATTERN_SCB
_PATTERN_C = PATTERN_C
_PATTERN_S = PATTERN_S


# =============================================================================
# BitSlotData Class
# =============================================================================

@dataclass(frozen=True)
class BitSlotData:
    """Data for a normal bit slot.

    Represents the sample, channel, and bit information
    for a data-carrying slot. Frozen so shared instances (e.g. from wide-bit
    replay in DataPort) cannot be corrupted by accidental mutation.
    """
    sample: int = 0  # Absolute sample counter across all channels
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
            # Default: channel + bit (original behavior)
            return f"C{self.channel}B{self.bit}"

        parts = []
        if DisplayField.SAMPLE in display_fields:
            parts.append(f"S{self.sample}")
        if DisplayField.CHANNEL in display_fields:
            parts.append(f"C{self.channel}")
        if DisplayField.BIT in display_fields:
            parts.append(f"B{self.bit}")

        return "".join(parts)  # Returns empty string if no fields selected

    @classmethod
    def from_label(cls, label: str) -> Optional['BitSlotData']:
        """Parse label back to BitSlotData.

        Args:
            label: String in format "CXBY", "SXCY", "SXBY", or "SXCYBZ"

        Returns:
            BitSlotData instance, or None if label doesn't match format
        """
        # Try all supported formats using pre-compiled patterns
        # Format: cXbY (original)
        match = _PATTERN_CB.fullmatch(label)
        if match:
            return cls(
                channel=int(match.group(1)),
                bit=int(match.group(2))
            )

        # Format: sXcY (sample + channel)
        match = _PATTERN_SC.fullmatch(label)
        if match:
            return cls(
                channel=int(match.group(2)),
                bit=0,
                sample=int(match.group(1))
            )

        # Format: sXbY (sample + bit)
        match = _PATTERN_SB.fullmatch(label)
        if match:
            return cls(
                channel=0,
                bit=int(match.group(2)),
                sample=int(match.group(1))
            )

        # Format: sXcYbZ (all three)
        match = _PATTERN_SCB.fullmatch(label)
        if match:
            return cls(
                channel=int(match.group(2)),
                bit=int(match.group(3)),
                sample=int(match.group(1))
            )

        # Format: cX (channel only, for merged bits)
        match = _PATTERN_C.fullmatch(label)
        if match:
            return cls(
                channel=int(match.group(1)),
                bit=0
            )

        # Format: sX (sample only, for merged bits)
        match = _PATTERN_S.fullmatch(label)
        if match:
            return cls(
                channel=0,
                bit=0,
                sample=int(match.group(1))
            )

        return None


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
    row: int = -1     # Position in frame (-1 = not set)
    column: int = -1  # Position in frame (-1 = not set)

    def is_data_slot(self) -> bool:
        """Check if this is a data-carrying slot.

        Returns:
            True if slot_type is DATA
        """
        return self.slot_type == SlotType.DATA

    def is_control_slot(self) -> bool:
        """Check if this is a control slot (CDS, S0, S1).

        Returns:
            True if slot_type is CDS, S0, or S1
        """
        return self.slot_type in (SlotType.CDS, SlotType.S0, SlotType.S1)

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


# =============================================================================
# Sentinel Values
# =============================================================================

# Sentinel value for empty positions (DataPort returns this when not active at current position)
EMPTY_SLOT = BitSlotState(
    slot_type=SlotType.EMPTY,
    direction=DirectionType.SOURCE,
    device_num=-1,
    dp_num=-1
)
