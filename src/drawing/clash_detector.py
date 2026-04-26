"""
Clash detection for SoundWire bit slots.

This module provides clash detection logic for identifying bus conflicts
when multiple devices attempt to drive or read the same bit slot.

Classes:
    SlotClashCategory: Categories of bus clashes (write/read/txp)
    DeviceClashType: Type of device clash (same-device vs different-device)
    BitSlotOccupancy: Tracks occupancy state for a single bit slot
    ClashInfo: Detailed information about a specific clash
    ClashDetector: Main detector class for tracking and detecting clashes
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Set, Tuple, Optional

from src.utils.logging_config import get_logger

# Import ClashType from bus_model for device clash tracking
from src.models.bus_model import ClashType as DeviceClashType


# =============================================================================
# Enums and Data Classes
# =============================================================================

class SlotClashCategory(Enum):
    """Categories of bus clashes that can occur."""
    WRITE_CLASH = "write"      # Multiple sources driving same slot
    READ_CLASH = "read"         # Multiple sinks reading same slot
    TXP_MISMATCH = "txp"        # TxP source without matching sink
    NO_CLASH = "none"


class SlotOccupancyType(Enum):
    """Types of slot occupancy for clash detection.

    Using an enum instead of strings for faster comparison and type safety.
    """
    WRITE = "write"
    READ = "read"
    GUARD = "guard"
    TAIL = "tail"
    HANDOVER = "handover"
    TXP_SOURCE = "txp_source"
    TXP_SINK = "txp_sink"
    DRQ_SOURCE = "drq_source"
    DRQ_SINK = "drq_sink"


# Backward compatibility alias
ClashType = SlotClashCategory


@dataclass
class BitSlotOccupancy:
    """Tracks occupancy for a single bit slot."""
    device: int
    slot_type: SlotOccupancyType  # Using enum for performance
    canvas_ids: List[int] = field(default_factory=list)  # For handover arrows


@dataclass
class ClashInfo:
    """Detailed information about a specific clash.

    Tracks both the category of clash (write/read) and whether it's
    between the same device or different devices.
    """
    bit_index: int                          # Global bit slot index
    category: SlotClashCategory             # Write clash, read clash, etc.
    device_clash_type: DeviceClashType      # SAME_DEVICE or DIFFERENT_DEVICE
    device_a: int                           # First device
    device_b: int                           # Second device (clashing)
    slot_type_a: str                        # What device_a was doing
    slot_type_b: str                        # What device_b tried to do
    _row: int = field(default=0, repr=False)      # Row number (set by ClashDetector)
    _column: int = field(default=0, repr=False)   # Column number (set by ClashDetector)

    @property
    def row(self) -> int:
        """Row number (set by ClashDetector)."""
        return self._row

    @property
    def column(self) -> int:
        """Column number (set by ClashDetector)."""
        return self._column


# =============================================================================
# Main Clash Detector Class
# =============================================================================

class ClashDetector:
    """Detects and tracks bus clashes across bit slots.

    Replaces 16 parallel arrays with clean dictionary-based tracking.
    """

    def __init__(self, NumColumns_REG: int):
        """Initialize clash detector.

        Args:
            NumColumns_REG: Number of columns in each row of the frame
        """
        self.NumColumns_REG = NumColumns_REG
        self.logger = get_logger('clash_detector')

        # Main tracking dictionary: bit_slot -> list of occupancies
        self.occupancies: Dict[int, List[BitSlotOccupancy]] = {}

        # Track clashed slots by type
        self.write_clashes: Set[int] = set()
        self.read_clashes: Set[int] = set()

        # Enhanced clash tracking: same-device vs different-device
        self.same_device_clashes: Set[int] = set()       # Internal clashes (not physical)
        self.different_device_clashes: Set[int] = set()  # Physical bus collisions
        self.clash_details: List[ClashInfo] = []         # Detailed clash information

        # Track TxP source and sink slots for validation
        self.txp_sources: Set[int] = set()  # Slots with TxP source bits
        self.txp_sinks: Set[int] = set()    # Slots with TxP sink bits
        self.txp_mismatches: Set[int] = set()  # TxP sources without matching sinks
        self.txp_orphan_sinks: Set[int] = set()  # TxP sinks without matching sources

        # Track DRQ source and sink slots for validation
        self.drq_sources: Set[int] = set()  # Slots with DRQ source bits
        self.drq_sinks: Set[int] = set()    # Slots with DRQ sink bits
        self.drq_mismatches: Set[int] = set()  # DRQ sources without matching sinks
        self.drq_orphan_sinks: Set[int] = set()  # DRQ sinks without matching sources

        # Track whether validation has been performed
        self._txp_validated: bool = False
        self._drq_validated: bool = False

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    def _get_bit_slot(self, row: int, column: int) -> int:
        """Convert row/column to bit slot index.

        Args:
            row: Row number
            column: Column number

        Returns:
            Bit slot index
        """
        return column + row * self.NumColumns_REG

    def _has_occupancy(self, bit_slot: int, slot_type: SlotOccupancyType,
                       device: Optional[int] = None) -> bool:
        """Check if a bit slot has a specific occupancy.

        Args:
            bit_slot: Bit slot index
            slot_type: Type of occupancy to check for (enum)
            device: Optional device number (if None, checks any device)

        Returns:
            True if occupancy exists
        """
        if bit_slot not in self.occupancies:
            return False

        for occ in self.occupancies[bit_slot]:
            if occ.slot_type == slot_type:
                if device is None or occ.device == device:
                    return True
        return False

    def _get_device_for_type(self, bit_slot: int, slot_type: SlotOccupancyType) -> Optional[int]:
        """Get the device number for a specific occupancy type.

        Args:
            bit_slot: Bit slot index
            slot_type: Type of occupancy (enum)

        Returns:
            Device number, or None if not found
        """
        if bit_slot not in self.occupancies:
            return None

        for occ in self.occupancies[bit_slot]:
            if occ.slot_type == slot_type:
                return occ.device
        return None

    def _add_occupancy(self, bit_slot: int, device: int,
                       slot_type: SlotOccupancyType, canvas_ids: Optional[List[int]] = None) -> None:
        """Add an occupancy to a bit slot.

        Args:
            bit_slot: Bit slot index
            device: Device number
            slot_type: Type of occupancy (enum)
            canvas_ids: Optional canvas IDs for rendering
        """
        if bit_slot not in self.occupancies:
            self.occupancies[bit_slot] = []

        occ = BitSlotOccupancy(
            device=device,
            slot_type=slot_type,
            canvas_ids=canvas_ids or []
        )
        self.occupancies[bit_slot].append(occ)

    def record_clash(self, bit_slot: int, category: SlotClashCategory,
                      device_a: int, device_b: int,
                      slot_type_a: str, slot_type_b: str) -> DeviceClashType:
        """Record a clash with device type distinction.

        Args:
            bit_slot: Bit slot index
            category: Category of clash (write/read)
            device_a: First device (existing)
            device_b: Second device (attempting action)
            slot_type_a: What device_a was doing
            slot_type_b: What device_b tried to do

        Returns:
            DeviceClashType indicating same-device or different-device
        """
        # Determine if same-device or different-device clash
        if device_a == device_b:
            device_clash_type = DeviceClashType.SAME_DEVICE
            # Only track write clashes as physical conflicts
            # Read overlaps are informational only
            if category == SlotClashCategory.WRITE_CLASH:
                self.same_device_clashes.add(bit_slot)
        else:
            device_clash_type = DeviceClashType.DIFFERENT_DEVICE
            # Only track write clashes as physical bus collisions
            # Read overlaps are not physical conflicts (multiple readers OK)
            if category == SlotClashCategory.WRITE_CLASH:
                self.different_device_clashes.add(bit_slot)

        # Create detailed clash info
        clash_info = ClashInfo(
            bit_index=bit_slot,
            category=category,
            device_clash_type=device_clash_type,
            device_a=device_a,
            device_b=device_b,
            slot_type_a=slot_type_a,
            slot_type_b=slot_type_b
        )
        # Store row/column for convenience
        clash_info._row = bit_slot // self.NumColumns_REG
        clash_info._column = bit_slot % self.NumColumns_REG

        self.clash_details.append(clash_info)

        return device_clash_type

    # -------------------------------------------------------------------------
    # Write Clash Detection
    # -------------------------------------------------------------------------

    def _remove_occupancy(self, bit_slot: int, slot_type: SlotOccupancyType,
                         device: int) -> Optional[List[int]]:
        """Remove an occupancy from a bit slot.

        Args:
            bit_slot: Bit slot index
            slot_type: Type of occupancy to remove (enum)
            device: Device number

        Returns:
            Canvas IDs that were removed, or None
        """
        if bit_slot not in self.occupancies:
            return None

        for i, occ in enumerate(self.occupancies[bit_slot]):
            if occ.slot_type == slot_type and occ.device == device:
                removed = self.occupancies[bit_slot].pop(i)
                return removed.canvas_ids
        return None

    def check_write_clash(self, row: int, column: int, device: int) -> Tuple[bool, bool, Optional[str]]:
        """Check for write clash at the given position.

        Args:
            row: Row number
            column: Column number
            device: Device number attempting to write

        Returns:
            Tuple of (has_clash, should_remove_handover, suppress_same_device_slot)
            - has_clash: True if there's a write clash
            - should_remove_handover: True if handover from same device should be removed
            - suppress_same_device_slot: 'guard' or 'tail' if same-device slot should be
              removed (data bit has priority), None otherwise
        """
        bit_slot = self._get_bit_slot(row, column)
        has_clash = False
        should_remove_handover = False
        suppress_same_device_slot: Optional[str] = None

        # Check if slot is already driven - same-level writes always clash (even same device)
        if self._has_occupancy(bit_slot, SlotOccupancyType.WRITE):
            write_device = self._get_device_for_type(bit_slot, SlotOccupancyType.WRITE)
            has_clash = True
            self.write_clashes.add(bit_slot)
            # Record with device type distinction
            if write_device is not None:
                self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                  write_device, device, 'write', 'write')
            self.logger.warning(f'Bus clash detected (write)', extra={
                'row': row,
                'column': column,
                'device': device,
                'existing_device': write_device,
                'same_device': write_device == device
            })

        # Check handover - same device removes it, different device clashes
        if self._has_occupancy(bit_slot, SlotOccupancyType.HANDOVER):
            handover_device = self._get_device_for_type(bit_slot, SlotOccupancyType.HANDOVER)
            if handover_device is not None and handover_device != device:
                has_clash = True
                self.write_clashes.add(bit_slot)
                # Record with device type distinction
                self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                  handover_device, device, 'handover', 'write')
                self.logger.warning(f'Bus clash detected (write vs handover)', extra={
                    'row': row,
                    'column': column,
                    'device': device,
                    'handover_device': handover_device
                })
            elif handover_device == device:
                # Same device - remove handover (but still check tail/guard below)
                should_remove_handover = True

        # Check guard - same device suppresses guard, different device clashes
        if self._has_occupancy(bit_slot, SlotOccupancyType.GUARD):
            guard_device = self._get_device_for_type(bit_slot, SlotOccupancyType.GUARD)
            if guard_device == device:
                # Same device - data bit has priority, suppress guard
                suppress_same_device_slot = 'guard'
                self._remove_occupancy(bit_slot, SlotOccupancyType.GUARD, device)
            elif guard_device is not None:
                has_clash = True
                self.write_clashes.add(bit_slot)
                # Record with device type distinction
                self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                  guard_device, device, 'guard', 'write')

        # Check tail - same device suppresses tail, different device clashes
        if self._has_occupancy(bit_slot, SlotOccupancyType.TAIL):
            tail_device = self._get_device_for_type(bit_slot, SlotOccupancyType.TAIL)
            if tail_device == device:
                # Same device - data bit has priority, suppress tail
                suppress_same_device_slot = 'tail'
                self._remove_occupancy(bit_slot, SlotOccupancyType.TAIL, device)
            elif tail_device is not None:
                has_clash = True
                self.write_clashes.add(bit_slot)
                # Record with device type distinction
                self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                  tail_device, device, 'tail', 'write')

        # Note: We don't add the write occupancy here - caller uses add_write()

        return has_clash, should_remove_handover, suppress_same_device_slot

    def remove_handover(self, row: int, column: int, device: int) -> Optional[List[int]]:
        """Remove a handover occupancy.

        Args:
            row: Row number
            column: Column number
            device: Device number

        Returns:
            Canvas IDs that were removed, or None
        """
        bit_slot = self._get_bit_slot(row, column)
        return self._remove_occupancy(bit_slot, SlotOccupancyType.HANDOVER, device)

    def add_write(self, row: int, column: int, device: int) -> None:
        """Add a write occupancy after removing handover.

        Args:
            row: Row number
            column: Column number
            device: Device number
        """
        bit_slot = self._get_bit_slot(row, column)
        self._add_occupancy(bit_slot, device, SlotOccupancyType.WRITE)

    # -------------------------------------------------------------------------
    # Guard Clash Detection
    # -------------------------------------------------------------------------

    def check_guard_clash(self, row: int, column: int, device: int) -> Tuple[bool, int, bool]:
        """Check for guard clash at the given position.

        Args:
            row: Row number
            column: Column number
            device: Device number

        Returns:
            Tuple of (has_clash, should_draw, suppress_same_device_tail)
            - has_clash: True if there's a physical or logical clash
            - should_draw: 1 if guard should be drawn, 0 if it should be suppressed
            - suppress_same_device_tail: True if a same-device tail should be removed

        The caller's draw condition is typically: `should_draw or not has_clash`
        - (False, 1): No clash, draw guard
        - (False, 0): No clash but suppress guard (data bit has priority)
        - (True, 0): Clash detected, don't draw
        - (True, 1): Would mean clash but draw anyway (not used)
        """
        bit_slot = self._get_bit_slot(row, column)
        suppress_same_device_tail = False

        # Check handover from same/different device
        if self._has_occupancy(bit_slot, SlotOccupancyType.HANDOVER):
            handover_device = self._get_device_for_type(bit_slot, SlotOccupancyType.HANDOVER)
            if handover_device == device:
                # Same device - handover will be removed by caller, guard takes its place
                self._add_occupancy(bit_slot, device, SlotOccupancyType.GUARD)
                return False, 1, False  # No clash, draw guard
            elif handover_device is not None:
                # Different device - clash
                self.write_clashes.add(bit_slot)
                self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                  handover_device, device, 'handover', 'guard')
                return True, 0, False

        # Check write clash with different device
        if self._has_occupancy(bit_slot, SlotOccupancyType.WRITE):
            write_device = self._get_device_for_type(bit_slot, SlotOccupancyType.WRITE)
            if write_device == device:
                # Same device - data bit has priority, don't draw guard
                return False, 0, False
            elif write_device is not None:
                self.write_clashes.add(bit_slot)
                self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                  write_device, device, 'write', 'guard')
                return True, 0, False

        # Check guard clash - same-level guards always clash (even same device)
        if self._has_occupancy(bit_slot, SlotOccupancyType.GUARD):
            guard_device = self._get_device_for_type(bit_slot, SlotOccupancyType.GUARD)
            self.write_clashes.add(bit_slot)
            if guard_device is not None:
                self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                  guard_device, device, 'guard', 'guard')
            return True, 0, False

        # Check tail - guard takes priority within same device
        if self._has_occupancy(bit_slot, SlotOccupancyType.TAIL):
            tail_device = self._get_device_for_type(bit_slot, SlotOccupancyType.TAIL)
            if tail_device == device:
                # Same device - guard replaces tail, remove tail occupancy
                self._remove_occupancy(bit_slot, SlotOccupancyType.TAIL, device)
                self._add_occupancy(bit_slot, device, SlotOccupancyType.GUARD)
                suppress_same_device_tail = True
                return False, 1, suppress_same_device_tail
            elif tail_device is not None:
                self.write_clashes.add(bit_slot)
                self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                  tail_device, device, 'tail', 'guard')
                return True, 0, False

        # No clash - add guard
        self._add_occupancy(bit_slot, device, SlotOccupancyType.GUARD)
        return False, 1, False

    # -------------------------------------------------------------------------
    # Tail Clash Detection
    # -------------------------------------------------------------------------

    def check_tail_clash(self, row: int, column: int, device: int) -> Tuple[bool, int]:
        """Check for tail clash at the given position.

        Args:
            row: Row number
            column: Column number
            device: Device number

        Returns:
            Tuple of (has_clash, should_draw)
            - has_clash: True if there's a physical or logical clash
            - should_draw: 1 if tail should be drawn, 0 if it should be suppressed

        The caller's draw condition is typically: `if should_draw:`
        - (False, 1): No clash, draw tail
        - (False, 0): No clash but suppress tail (higher priority slot exists)
        - (True, 0): Clash detected, don't draw
        - (True, 1): Would mean clash but draw anyway (not used)
        """
        bit_slot = self._get_bit_slot(row, column)

        # Check handover from same/different device
        if self._has_occupancy(bit_slot, SlotOccupancyType.HANDOVER):
            handover_device = self._get_device_for_type(bit_slot, SlotOccupancyType.HANDOVER)
            if handover_device == device:
                # Same device - handover will be removed by caller, tail takes its place
                self._add_occupancy(bit_slot, device, SlotOccupancyType.TAIL)
                return False, 1  # No clash, draw tail
            elif handover_device is not None:
                # Different device - clash
                self.write_clashes.add(bit_slot)
                self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                  handover_device, device, 'handover', 'tail')
                return True, 0

        # Check write clash
        if self._has_occupancy(bit_slot, SlotOccupancyType.WRITE):
            write_device = self._get_device_for_type(bit_slot, SlotOccupancyType.WRITE)
            if write_device == device:
                # Same device - data bit has priority, don't draw tail
                return False, 0
            elif write_device is not None:
                self.write_clashes.add(bit_slot)
                self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                  write_device, device, 'write', 'tail')
                return True, 0

        # Check tail clash - same-level tails always clash (even same device)
        if self._has_occupancy(bit_slot, SlotOccupancyType.TAIL):
            tail_device = self._get_device_for_type(bit_slot, SlotOccupancyType.TAIL)
            self.write_clashes.add(bit_slot)
            if tail_device is not None:
                self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                  tail_device, device, 'tail', 'tail')
            return True, 0

        # Check guard - guard has priority, don't replace
        if self._has_occupancy(bit_slot, SlotOccupancyType.GUARD):
            guard_device = self._get_device_for_type(bit_slot, SlotOccupancyType.GUARD)
            if guard_device == device:
                # Same device - guard has priority, don't draw tail
                return False, 0
            elif guard_device is not None:
                self.write_clashes.add(bit_slot)
                self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                  guard_device, device, 'guard', 'tail')
                return True, 0

        # No clash - add tail
        self._add_occupancy(bit_slot, device, SlotOccupancyType.TAIL)
        return False, 1

    # -------------------------------------------------------------------------
    # Read Clash Detection
    # -------------------------------------------------------------------------

    def check_read_clash(self, row: int, column: int, device: int) -> bool:
        """Check for read clash at the given position.

        Read overlaps are flagged regardless of device - even same-device reads
        from different data ports are visual warnings since they indicate
        multiple data ports reading the same slot.

        Args:
            row: Row number
            column: Column number
            device: Device number

        Returns:
            True if there's a read clash
        """
        bit_slot = self._get_bit_slot(row, column)

        # Check if already being read by ANY data port (same or different device)
        # Reads are passive so this is just a visual warning, not a bus conflict
        if self._has_occupancy(bit_slot, SlotOccupancyType.READ):
            read_device = self._get_device_for_type(bit_slot, SlotOccupancyType.READ)
            self.read_clashes.add(bit_slot)
            # Record with device type distinction (for informational purposes)
            if read_device is not None:
                self.record_clash(bit_slot, SlotClashCategory.READ_CLASH,
                                  read_device, device, 'read', 'read')
            self.logger.warning(f'Read overlap detected', extra={
                'row': row,
                'column': column,
                'device': device,
                'existing_device': read_device,
                'same_device': read_device == device
            })
            # Still add this read so we can detect triple+ overlaps
            self._add_occupancy(bit_slot, device, SlotOccupancyType.READ)
            return True

        # Check if handover from a different device
        if self._has_occupancy(bit_slot, SlotOccupancyType.HANDOVER):
            handover_device = self._get_device_for_type(bit_slot, SlotOccupancyType.HANDOVER)
            if handover_device is not None and handover_device != device:
                self.read_clashes.add(bit_slot)
                # Record with device type distinction
                self.record_clash(bit_slot, SlotClashCategory.READ_CLASH,
                                  handover_device, device, 'handover', 'read')
                self.logger.warning(f'Read overlap detected (vs handover)', extra={
                    'row': row,
                    'column': column,
                    'device': device,
                    'existing_device': handover_device
                })
                self._add_occupancy(bit_slot, device, SlotOccupancyType.READ)
                return True

        self._add_occupancy(bit_slot, device, SlotOccupancyType.READ)
        return False

    # -------------------------------------------------------------------------
    # Handover Management
    # -------------------------------------------------------------------------

    def add_handover(self, row: int, column: int, device: int,
                     canvas_ids: List[int]) -> Tuple[bool, bool, bool]:
        """Add a handover occupancy.

        Args:
            row: Row number
            column: Column number
            device: Device number
            canvas_ids: Canvas IDs for the handover arrows

        Returns:
            Tuple of (has_read_clash, has_write_clash, should_suppress)
            - should_suppress: True if handover should be deleted (real slot exists)
        """
        bit_slot = self._get_bit_slot(row, column)
        has_read_clash = False
        has_write_clash = False
        should_suppress = False

        # Check read clash (all read overlaps are flagged, regardless of device)
        if self._has_occupancy(bit_slot, SlotOccupancyType.READ):
            read_device = self._get_device_for_type(bit_slot, SlotOccupancyType.READ)
            self.read_clashes.add(bit_slot)
            self.logger.warning(f'Read overlap detected (handover)', extra={
                'row': row,
                'column': column,
                'device': device,
                'existing_device': read_device
            })
            has_read_clash = True

        # Check write - if same device, suppress handover (real slot takes precedence)
        if self._has_occupancy(bit_slot, SlotOccupancyType.WRITE):
            write_device = self._get_device_for_type(bit_slot, SlotOccupancyType.WRITE)
            if write_device != device:
                self.write_clashes.add(bit_slot)
                # Record with device type distinction
                if write_device is not None:
                    self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                      write_device, device, 'write', 'handover')
                self.logger.warning(f'Bus clash detected (handover)', extra={
                    'row': row,
                    'column': column,
                    'device': device,
                    'existing_device': write_device
                })
                has_write_clash = True
            else:
                # Same device - suppress handover, real slot takes precedence
                should_suppress = True

        # Check tail - different device tail causes clash, same device suppresses
        if self._has_occupancy(bit_slot, SlotOccupancyType.TAIL):
            tail_device = self._get_device_for_type(bit_slot, SlotOccupancyType.TAIL)
            if tail_device != device:
                self.write_clashes.add(bit_slot)
                # Record with device type distinction
                if tail_device is not None:
                    self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                      tail_device, device, 'tail', 'handover')
                self.logger.warning(f'Bus clash detected (handover vs tail)', extra={
                    'row': row,
                    'column': column,
                    'device': device,
                    'tail_device': tail_device
                })
                has_write_clash = True
            should_suppress = True

        # Check guard - different device guard causes clash, same device suppresses
        if self._has_occupancy(bit_slot, SlotOccupancyType.GUARD):
            guard_device = self._get_device_for_type(bit_slot, SlotOccupancyType.GUARD)
            if guard_device != device:
                self.write_clashes.add(bit_slot)
                # Record with device type distinction
                if guard_device is not None:
                    self.record_clash(bit_slot, SlotClashCategory.WRITE_CLASH,
                                      guard_device, device, 'guard', 'handover')
                self.logger.warning(f'Bus clash detected (handover vs guard)', extra={
                    'row': row,
                    'column': column,
                    'device': device,
                    'guard_device': guard_device
                })
                has_write_clash = True
            should_suppress = True

        # Add handover regardless of clash (for tracking)
        self._add_occupancy(bit_slot, device, SlotOccupancyType.HANDOVER, canvas_ids)
        return has_read_clash, has_write_clash, should_suppress

    def get_handover_canvas_ids(self, row: int, column: int) -> Optional[List[int]]:
        """Get canvas IDs for a handover at the given position.

        Args:
            row: Row number
            column: Column number

        Returns:
            List of canvas IDs, or None if no handover exists
        """
        bit_slot = self._get_bit_slot(row, column)
        if bit_slot not in self.occupancies:
            return None

        for occ in self.occupancies[bit_slot]:
            if occ.slot_type == SlotOccupancyType.HANDOVER:
                return occ.canvas_ids
        return None

    def get_write_clashes(self) -> List[int]:
        """Get list of bit slots with write clashes.

        Returns:
            List of bit slot indices with write clashes
        """
        return sorted(list(self.write_clashes))

    def get_read_clashes(self) -> List[int]:
        """Get list of bit slots with read clashes.

        Returns:
            List of bit slot indices with read clashes
        """
        return sorted(list(self.read_clashes))

    def get_same_device_clashes(self) -> List[int]:
        """Get list of bit slots with same-device (internal) clashes.

        Same-device clashes are internal to the device and don't represent
        physical bus collisions.

        Returns:
            List of bit slot indices with same-device clashes
        """
        return sorted(list(self.same_device_clashes))

    def get_different_device_clashes(self) -> List[int]:
        """Get list of bit slots with different-device (physical) clashes.

        Different-device clashes represent actual physical bus collisions
        where multiple devices are driving the bus simultaneously.

        Returns:
            List of bit slot indices with different-device clashes
        """
        return sorted(list(self.different_device_clashes))

    def get_clash_details(self) -> List[ClashInfo]:
        """Get detailed information about all clashes.

        Returns:
            List of ClashInfo objects with full clash details
        """
        return self.clash_details.copy()

    # -------------------------------------------------------------------------
    # Flow Control (TxP/DRQ) Validation Helpers
    # -------------------------------------------------------------------------

    def _validate_flow_control_pairs(self, sources: Set[int], sinks: Set[int],
                                     mismatch_set: Set[int], label: str,
                                     find_orphans: bool = False) -> List[Tuple[int, int]]:
        """Generic validation for flow control (TxP/DRQ) source/sink pairs.

        Args:
            sources: Set of bit slots with sources
            sinks: Set of bit slots with sinks
            mismatch_set: Set to store mismatches (modified in place)
            label: Label for logging ('TxP' or 'DRQ')
            find_orphans: If True, find sinks without sources; else sources without sinks

        Returns:
            List of (row, column) tuples for unmatched slots
        """
        if find_orphans:
            unmatched = sinks - sources
            msg = f'{label} sink without matching source'
        else:
            unmatched = sources - sinks
            msg = f'{label} source without matching sink'

        mismatch_set.clear()
        mismatch_set.update(unmatched)

        results = []
        for bit_slot in sorted(unmatched):
            row = bit_slot // self.NumColumns_REG
            column = bit_slot % self.NumColumns_REG
            results.append((row, column))
            self.logger.warning(msg, extra={
                'row': row,
                'column': column,
                'bit_slot': bit_slot
            })
        return results

    # -------------------------------------------------------------------------
    # TxP Source/Sink Tracking
    # -------------------------------------------------------------------------

    def add_txp_source(self, row: int, column: int, device: int) -> None:
        """Add a TxP source bit at the given position.

        Args:
            row: Row number
            column: Column number
            device: Device number
        """
        bit_slot = self._get_bit_slot(row, column)
        self.txp_sources.add(bit_slot)
        self._add_occupancy(bit_slot, device, SlotOccupancyType.TXP_SOURCE)

    def add_txp_sink(self, row: int, column: int, device: int) -> None:
        """Add a TxP sink bit at the given position.

        Args:
            row: Row number
            column: Column number
            device: Device number
        """
        bit_slot = self._get_bit_slot(row, column)
        self.txp_sinks.add(bit_slot)
        self._add_occupancy(bit_slot, device, SlotOccupancyType.TXP_SINK)

    def validate_txp_pairs(self) -> List[Tuple[int, int]]:
        """Validate that all TxP source bits have matching sink bits.

        Should be called after all data ports are drawn.

        Returns:
            List of (row, column) tuples for TxP sources without matching sinks
        """
        self._txp_validated = True
        return self._validate_flow_control_pairs(
            self.txp_sources, self.txp_sinks, self.txp_mismatches, 'TxP'
        )

    def validate_txp_sinks(self) -> List[Tuple[int, int]]:
        """Validate that all TxP sink bits have matching source bits.

        Should be called after all data ports are drawn.

        Returns:
            List of (row, column) tuples for TxP sinks without matching sources
        """
        return self._validate_flow_control_pairs(
            self.txp_sources, self.txp_sinks, self.txp_orphan_sinks, 'TxP',
            find_orphans=True
        )

    def get_txp_mismatches(self) -> List[int]:
        """Get list of bit slots with TxP source but no matching sink.

        Returns:
            List of bit slot indices with TxP mismatches
        """
        return sorted(list(self.txp_mismatches))

    def get_txp_orphan_sinks(self) -> List[int]:
        """Get list of bit slots with TxP sink but no matching source.

        Returns:
            List of bit slot indices with orphan TxP sinks
        """
        return sorted(list(self.txp_orphan_sinks))

    # -------------------------------------------------------------------------
    # DRQ Source/Sink Tracking
    # -------------------------------------------------------------------------

    def add_drq_source(self, row: int, column: int, device: int) -> None:
        """Add a DRQ source bit at the given position.

        Args:
            row: Row number
            column: Column number
            device: Device number
        """
        bit_slot = self._get_bit_slot(row, column)
        self.drq_sources.add(bit_slot)
        self._add_occupancy(bit_slot, device, SlotOccupancyType.DRQ_SOURCE)

    def add_drq_sink(self, row: int, column: int, device: int) -> None:
        """Add a DRQ sink bit at the given position.

        Args:
            row: Row number
            column: Column number
            device: Device number
        """
        bit_slot = self._get_bit_slot(row, column)
        self.drq_sinks.add(bit_slot)
        self._add_occupancy(bit_slot, device, SlotOccupancyType.DRQ_SINK)

    def validate_drq_pairs(self) -> List[Tuple[int, int]]:
        """Validate that all DRQ source bits have matching sink bits.

        Should be called after all data ports are drawn.

        Returns:
            List of (row, column) tuples for DRQ sources without matching sinks
        """
        self._drq_validated = True
        return self._validate_flow_control_pairs(
            self.drq_sources, self.drq_sinks, self.drq_mismatches, 'DRQ'
        )

    def validate_drq_sinks(self) -> List[Tuple[int, int]]:
        """Validate that all DRQ sink bits have matching source bits.

        Should be called after all data ports are drawn.

        Returns:
            List of (row, column) tuples for DRQ sinks without matching sources
        """
        return self._validate_flow_control_pairs(
            self.drq_sources, self.drq_sinks, self.drq_orphan_sinks, 'DRQ',
            find_orphans=True
        )

    def get_drq_mismatches(self) -> List[int]:
        """Get list of bit slots with DRQ source but no matching sink.

        Returns:
            List of bit slot indices with DRQ mismatches
        """
        return sorted(list(self.drq_mismatches))

    def get_drq_orphan_sinks(self) -> List[int]:
        """Get list of bit slots with DRQ sink but no matching source.

        Returns:
            List of bit slot indices with orphan DRQ sinks
        """
        return sorted(list(self.drq_orphan_sinks))

    # -------------------------------------------------------------------------
    # Summary and Utility Methods
    # -------------------------------------------------------------------------

    def get_summary(self) -> Dict[str, int]:
        """Get summary statistics of clashes.

        Auto-runs validation if not already performed.

        Returns:
            Dictionary with clash counts
        """
        # Auto-run validation if not done
        if not self._txp_validated:
            self.validate_txp_pairs()
            self.validate_txp_sinks()
        if not self._drq_validated:
            self.validate_drq_pairs()
            self.validate_drq_sinks()

        return {
            'write_clashes': len(self.write_clashes),
            'read_clashes': len(self.read_clashes),
            'same_device_clashes': len(self.same_device_clashes),
            'different_device_clashes': len(self.different_device_clashes),
            'total_clash_events': len(self.clash_details),
            'txp_mismatches': len(self.txp_mismatches),
            'txp_orphan_sinks': len(self.txp_orphan_sinks),
            'txp_sources': len(self.txp_sources),
            'txp_sinks': len(self.txp_sinks),
            'drq_mismatches': len(self.drq_mismatches),
            'drq_orphan_sinks': len(self.drq_orphan_sinks),
            'drq_sources': len(self.drq_sources),
            'drq_sinks': len(self.drq_sinks),
            'total_occupancies': sum(len(occs) for occs in self.occupancies.values())
        }

    def clear(self) -> None:
        """Clear all tracking data."""
        self.occupancies.clear()
        self.write_clashes.clear()
        self.read_clashes.clear()
        self.same_device_clashes.clear()
        self.different_device_clashes.clear()
        self.clash_details.clear()
        self.txp_sources.clear()
        self.txp_sinks.clear()
        self.txp_mismatches.clear()
        self.txp_orphan_sinks.clear()
        self.drq_sources.clear()
        self.drq_sinks.clear()
        self.drq_mismatches.clear()
        self.drq_orphan_sinks.clear()
        # Reset validation flags
        self._txp_validated = False
        self._drq_validated = False
