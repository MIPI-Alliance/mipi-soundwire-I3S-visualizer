"""Manager class for SoundWire I3S bus manager abstraction.

This module provides the Manager class that encapsulates the logic for
calculating system slot positions (S0, S1, CDS) and managing the frame
structure for SoundWire I3S visualization.

NOTE: This module must remain UI-independent. No tkinter, widgets, dialogs,
or any UI framework imports are allowed. This module should be usable as a
library without any UI dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Tuple, Optional, NamedTuple

from src.models.enums import SlotType

if TYPE_CHECKING:
    from src.models.interface import Interface


class SystemSlot(NamedTuple):
    """A system slot position and type."""
    column: int
    slot_type: SlotType
    device: int  # -1 for Manager, -2 for Universal


@dataclass
class SystemSlotLayout:
    """Layout of system slots in a frame.

    Calculated once from interface configuration and cached.
    """
    # S0 columns (at end of frame for PHY3)
    s0_columns: List[int] = field(default_factory=list)

    # S1 column (at start for PHY3)
    s1_column: Optional[int] = None

    # S1 handover
    s1_handover_column: Optional[int] = None

    # S1 tail columns
    s1_tail_columns: List[int] = field(default_factory=list)

    # CDS columns
    cds_columns: List[int] = field(default_factory=list)

    # CDS guard column
    cds_guard_column: Optional[int] = None
    cds_guard_polarity: bool = False  # False=G0, True=G1

    # CDS tail columns
    cds_tail_columns: List[int] = field(default_factory=list)

    # CDS handover columns
    cds_handover_columns: List[int] = field(default_factory=list)

    # Data port area
    data_port_start_column: int = 0
    data_port_end_column: int = 0

    # Minimum columns required for system slots
    min_columns_required: int = 0

    # PHY3 enabled
    phy3_enabled: bool = False

    @property
    def cds_start_column(self) -> int:
        """First CDS column."""
        return self.cds_columns[0] if self.cds_columns else 0

    @property
    def cds_end_column(self) -> int:
        """Last CDS column."""
        return self.cds_columns[-1] if self.cds_columns else 0

    def get_all_system_columns(self) -> set[int]:
        """Get set of all columns used by system slots."""
        columns = set()
        columns.update(self.s0_columns)
        if self.s1_column is not None:
            columns.add(self.s1_column)
        if self.s1_handover_column is not None:
            columns.add(self.s1_handover_column)
        columns.update(self.s1_tail_columns)
        columns.update(self.cds_columns)
        if self.cds_guard_column is not None:
            columns.add(self.cds_guard_column)
        columns.update(self.cds_tail_columns)
        columns.update(self.cds_handover_columns)
        return columns

    def is_system_column(self, column: int) -> bool:
        """Check if a column is used by system slots."""
        return column in self.get_all_system_columns()


class Manager:
    """Manages system slot layout and frame structure for SoundWire I3S.

    The Manager class encapsulates the complex logic for calculating
    S0, S1, and CDS column positions based on PHY3 settings and
    interface configuration.

    This class provides a clean interface for:
    - Querying system slot positions
    - Determining the data port area within a frame
    - Validating frame configuration

    Attributes:
        interface: The interface configuration
        layout: Calculated system slot layout
    """

    def __init__(self, interface: 'Interface'):
        """Initialize Manager with interface configuration.

        Args:
            interface: Interface configuration with PHY3/CDS settings
        """
        self.interface = interface
        self.layout = self._calculate_layout()

    def _calculate_layout(self) -> SystemSlotLayout:
        """Calculate system slot layout from interface configuration.

        Returns:
            SystemSlotLayout with all slot positions
        """
        layout = SystemSlotLayout()
        layout.phy3_enabled = self.interface.phy3_enabled

        if self.interface.phy3_enabled:
            layout = self._calculate_phy3_layout()
        else:
            layout = self._calculate_non_phy3_layout()

        return layout

    def _calculate_phy3_layout(self) -> SystemSlotLayout:
        """Calculate layout for PHY3 mode.

        In PHY3 mode:
        - S0 columns are at the end of the frame
        - S1 is at column 0
        - S1 tails follow S1
        - S1 handover follows S1 tails
        - CDS follows S1 handover
        - CDS guard/tail/handover follow CDS
        - Data ports use remaining columns
        """
        layout = SystemSlotLayout()
        layout.phy3_enabled = True
        num_columns = self.interface.num_columns

        # Calculate minimum columns needed
        layout.min_columns_required = (
            self.interface.s0_width +
            (self.interface.CDS_BitWidth_REG + 1) +
            1 +  # S1
            int(self.interface.CDS_GuardEnabled_REG) +
            int(self.interface.cds_handover_enabled) +
            int(self.interface.s1_handover_enabled) +
            self.interface.tail_width +
            self.interface.CDS_TailWidth_REG
        )

        if num_columns <= layout.min_columns_required:
            # Not enough columns - return empty layout
            return layout

        # S0 columns at end of frame
        for col_offset in range(self.interface.s0_width):
            layout.s0_columns.append(num_columns - col_offset - 1)

        # S1 at column 0
        layout.s1_column = 0

        # S1 tails after S1
        for col_offset in range(self.interface.tail_width):
            layout.s1_tail_columns.append(1 + col_offset)

        # S1 handover after S1 tails
        s1_handover_width = 1 if self.interface.s1_handover_enabled else 0
        if self.interface.s1_handover_enabled:
            layout.s1_handover_column = 1 + self.interface.tail_width

        # CDS after S1 handover
        cds_start = 1 + s1_handover_width + self.interface.tail_width
        for col_offset in range(self.interface.CDS_BitWidth_REG + 1):
            layout.cds_columns.append(cds_start + col_offset)

        # CDS guard after CDS
        if self.interface.CDS_GuardEnabled_REG:
            layout.cds_guard_column = cds_start + (self.interface.CDS_BitWidth_REG + 1)
            layout.cds_guard_polarity = self.interface.CDS_GuardPolarity_REG

        # CDS tails after CDS (and guard if enabled)
        cds_tail_start = (cds_start +
                         (self.interface.CDS_BitWidth_REG + 1) +
                         int(self.interface.CDS_GuardEnabled_REG))
        for col_offset in range(self.interface.CDS_TailWidth_REG):
            layout.cds_tail_columns.append(cds_tail_start + col_offset)

        # CDS handover after CDS tails
        if self.interface.cds_handover_enabled:
            layout.cds_handover_columns.append(
                cds_start +
                (self.interface.CDS_BitWidth_REG + 1) +
                int(self.interface.CDS_GuardEnabled_REG) +
                self.interface.CDS_TailWidth_REG
            )

        # Data port area
        layout.data_port_start_column = (
            cds_start +
            (self.interface.CDS_BitWidth_REG + 1) +
            int(self.interface.CDS_GuardEnabled_REG) +
            self.interface.CDS_TailWidth_REG +
            int(self.interface.cds_handover_enabled)
        )
        layout.data_port_end_column = num_columns - self.interface.s0_width - 1

        return layout

    def _calculate_non_phy3_layout(self) -> SystemSlotLayout:
        """Calculate layout for non-PHY3 mode.

        In non-PHY3 mode:
        - CDS starts at column 0
        - CDS guard/tail/handover follow CDS
        - No S0/S1 columns
        - Data ports use remaining columns
        """
        layout = SystemSlotLayout()
        layout.phy3_enabled = False
        num_columns = self.interface.num_columns

        # CDS at start of frame
        for col_offset in range(self.interface.CDS_BitWidth_REG + 1):
            layout.cds_columns.append(col_offset)

        # CDS guard after CDS
        if self.interface.CDS_GuardEnabled_REG:
            layout.cds_guard_column = self.interface.CDS_BitWidth_REG + 1
            layout.cds_guard_polarity = self.interface.CDS_GuardPolarity_REG

        # CDS tails after CDS guard
        cds_tail_start = (
            (self.interface.CDS_BitWidth_REG + 1) +
            int(self.interface.CDS_GuardEnabled_REG)
        )
        for col_offset in range(self.interface.CDS_TailWidth_REG):
            layout.cds_tail_columns.append(cds_tail_start + col_offset)

        # CDS handovers at both ends (non-PHY3)
        if self.interface.cds_handover_enabled:
            # After CDS tails
            layout.cds_handover_columns.append(
                (self.interface.CDS_BitWidth_REG + 1) +
                int(self.interface.CDS_GuardEnabled_REG) +
                self.interface.CDS_TailWidth_REG
            )
            # At end of frame
            layout.cds_handover_columns.append(num_columns - 1)

        # Data port area
        layout.data_port_start_column = (
            (self.interface.CDS_BitWidth_REG + 1) +
            int(self.interface.CDS_GuardEnabled_REG) +
            self.interface.CDS_TailWidth_REG +
            int(self.interface.cds_handover_enabled)
        )
        layout.data_port_end_column = (
            num_columns - 1 - int(self.interface.cds_handover_enabled)
        )

        # Calculate minimum columns (for validation)
        layout.min_columns_required = (
            (self.interface.CDS_BitWidth_REG + 1) +
            int(self.interface.CDS_GuardEnabled_REG) +
            self.interface.CDS_TailWidth_REG +
            2 * int(self.interface.cds_handover_enabled)
        )

        return layout

    def get_system_slots(self) -> List[SystemSlot]:
        """Get list of all system slots in column order.

        Returns:
            List of SystemSlot tuples (column, slot_type, device)
        """
        from src.config.constants import SpecialDevices

        slots = []
        layout = self.layout

        # S0 columns
        for col in layout.s0_columns:
            slots.append(SystemSlot(col, SlotType.S0, SpecialDevices.MANAGER))

        # S1 column
        if layout.s1_column is not None:
            slots.append(SystemSlot(layout.s1_column, SlotType.S1, SpecialDevices.MANAGER))

        # S1 handover
        if layout.s1_handover_column is not None:
            slots.append(SystemSlot(
                layout.s1_handover_column, SlotType.HANDOVER, SpecialDevices.MANAGER
            ))

        # S1 tails
        for col in layout.s1_tail_columns:
            slots.append(SystemSlot(col, SlotType.TAIL, SpecialDevices.MANAGER))

        # CDS columns
        for col in layout.cds_columns:
            slots.append(SystemSlot(col, SlotType.CDS, SpecialDevices.UNIVERSAL))

        # CDS guard
        if layout.cds_guard_column is not None:
            slot_type = SlotType.GUARD_1 if layout.cds_guard_polarity else SlotType.GUARD_0
            slots.append(SystemSlot(
                layout.cds_guard_column, slot_type, SpecialDevices.UNIVERSAL
            ))

        # CDS tails
        for col in layout.cds_tail_columns:
            slots.append(SystemSlot(col, SlotType.TAIL, SpecialDevices.UNIVERSAL))

        # CDS handovers
        for col in layout.cds_handover_columns:
            slots.append(SystemSlot(col, SlotType.HANDOVER, SpecialDevices.UNIVERSAL))

        return sorted(slots, key=lambda s: s.column)

    def has_sufficient_columns(self) -> bool:
        """Check if frame has enough columns for system slots.

        Returns:
            True if there are enough columns
        """
        return self.interface.num_columns > self.layout.min_columns_required

    def get_data_port_column_range(self) -> Tuple[int, int]:
        """Get the column range available for data ports.

        Returns:
            Tuple of (start_column, end_column) inclusive
        """
        return (self.layout.data_port_start_column, self.layout.data_port_end_column)

    def __repr__(self) -> str:
        """String representation for debugging."""
        return (f"Manager(phy3={self.layout.phy3_enabled}, "
                f"cds={self.layout.cds_columns}, "
                f"dp_range=({self.layout.data_port_start_column}, "
                f"{self.layout.data_port_end_column}))")
