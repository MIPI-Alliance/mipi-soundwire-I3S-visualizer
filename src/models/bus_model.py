"""Bus Model classes for representing SWI3S bus as sequential bit writes.

The bus model represents data on a serialized bus where bits are indexed
sequentially from 0 to (rows * columns - 1). Row and column positions
are derived from the bit index using the frame dimensions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple, TYPE_CHECKING
from enum import Enum
import json

from .enums import DirectionType, SlotType, DisplayField, PortMode

if TYPE_CHECKING:
    from src.utils.validators import ValidationResult
    from src.drawing.clash_detector import ClashInfo


class ClashType(Enum):
    """Type of clash detected on the bus."""
    NONE = 0
    SAME_DEVICE = 1       # Internal to device, not physical bus issue
    DIFFERENT_DEVICE = 2  # Physical bus collision - multiple devices driving


@dataclass
class BitInfo:
    """Information about a single bit written to the bus.

    Attributes:
        bit_index: Global sequential index (0 to N-1)
        slot: Type of slot (DATA, CDS, S0, S1, GUARD, TAIL, etc.)
        direction: SOURCE (write) or SINK (read)
        device: Device number (-1=manager, -2=universal, -3=viz, 0-11=peripheral)
        dp: Data port number (None for system slots)
        channel: Channel within data port
        sample: Global sample number
        bit: Bit position within the sample
        clash: Type of clash at this position (if any)
        display_fields: Optional display field flags for label generation
        port_mode: Port mode (0=Normal, 1=Reserved, 2=Test Ones, 3=Test Zeros)
        scrambler_enabled: Whether scrambler is enabled for this data port
    """
    bit_index: int
    slot: SlotType
    direction: DirectionType
    device: int
    dp: Optional[int] = None
    channel: int = 0
    sample: int = 0
    bit: int = 0
    clash: ClashType = ClashType.NONE
    display_fields: Optional[DisplayField] = None  # For viz label generation
    port_mode: int = 0  # 0=Normal, 2=Test Ones (c0t1), 3=Test Zeros (c0t0)
    scrambler_enabled: bool = False  # Scrambler enabled for this data port

    # Store num_columns for row/column derivation (set by BusModel)
    _num_columns: int = field(default=1, repr=False, compare=False)

    @property
    def row(self) -> int:
        """Derive row from bit index."""
        return self.bit_index // self._num_columns

    @property
    def column(self) -> int:
        """Derive column from bit index."""
        return self.bit_index % self._num_columns


@dataclass
class BusModel:
    """Sequential bus model - bits indexed 0 to N-1.

    The bus model represents a SoundWire I3S frame as sequential bit writes.
    Each bit position can have multiple writers (from different data ports),
    all of which are recorded in the bits list. Row and column positions
    are derived mathematically from the frame dimensions.

    Attributes:
        num_rows: Number of rows in the frame
        num_columns: Number of columns per row
        row_rate: Row rate in kHz (for time calculations)
        bits: List of all BitInfo objects (multiple entries at same position allowed)
        bus_clashes: List of bit indices with different-device clashes
        device_clashes: List of bit indices with same-device clashes
        read_overlaps: List of bit indices with multiple readers
    """
    num_rows: int
    num_columns: int
    row_rate: float = 3072.0  # kHz, default value
    bits: List[BitInfo] = field(default_factory=list)

    # Clash tracking
    bus_clashes: List[int] = field(default_factory=list)
    device_clashes: List[int] = field(default_factory=list)
    read_overlaps: List[int] = field(default_factory=list)

    # Detailed clash information (with device info)
    clash_details: List['ClashInfo'] = field(default_factory=list)

    # TxP/DRQ flow control mismatches
    txp_mismatches: List[int] = field(default_factory=list)     # TxP sources without sinks
    txp_orphan_sinks: List[int] = field(default_factory=list)   # TxP sinks without sources
    drq_mismatches: List[int] = field(default_factory=list)     # DRQ sources without sinks
    drq_orphan_sinks: List[int] = field(default_factory=list)   # DRQ sinks without sources

    # Scrambler mismatches (source and sink with different scrambler settings)
    scrambler_mismatches: List[Tuple[int, int, int]] = field(default_factory=list)  # (bit_index, source_dp, sink_dp)

    # Test mode mismatches (different port modes at same bit slot)
    # Format: (bit_index, (dp1, mode1), (dp2, mode2)) where mode differs
    test_mode_mismatches: List[Tuple[int, Tuple[int, int], Tuple[int, int]]] = field(default_factory=list)

    # Validation issues (warnings that don't block drawing)
    validation_issues: List[Tuple[str, 'ValidationResult']] = field(default_factory=list)

    # Interval overflow warnings (data port bits don't fit in configured interval)
    # Format: (dp_name, bits_needed, bits_available)
    interval_overflow_warnings: List[Tuple[str, int, int]] = field(default_factory=list)

    # Display truncation warnings (data would fit if more rows were displayed)
    # Format: (dp_name, interval_rows, displayed_rows)
    display_truncation_warnings: List[Tuple[str, int, int]] = field(default_factory=list)

    # Sample/bit mismatches between source and sink at same bit slot
    # Format: (bit_index, source_dp, source_sample, source_bit, sink_dp, sink_sample, sink_bit)
    sample_bit_mismatches: List[Tuple[int, int, int, int, int, int, int]] = field(default_factory=list)

    # Sink dataports with EnableHandover but no FCP bits (sink handovers don't make sense)
    # Format: (dp_name, dp_number)
    sink_handover_warnings: List[Tuple[str, int]] = field(default_factory=list)

    # Data ports with EnableDataPort=True but NumChannels=0 (nothing to draw)
    # Format: (dp_name, dp_number)
    enabled_no_channels_warnings: List[Tuple[str, int]] = field(default_factory=list)

    @property
    def total_bits(self) -> int:
        """Total number of bit positions in the frame."""
        return self.num_rows * self.num_columns

    def bit_index(self, row: int, column: int) -> int:
        """Convert (row, column) to sequential bit index."""
        return row * self.num_columns + column

    def position(self, bit_index: int) -> Tuple[int, int]:
        """Convert sequential index to (row, column)."""
        return (bit_index // self.num_columns, bit_index % self.num_columns)

    def add_bit(self, bit: BitInfo) -> None:
        """Add a bit to the bus model.

        Multiple bits at the same position are allowed (e.g., when multiple
        data ports write to the same bit slot).

        Args:
            bit: BitInfo object to add
        """
        # Set the num_columns for row/column derivation
        bit._num_columns = self.num_columns

        # Bounds check (debug assertion)
        if bit.bit_index < 0 or bit.bit_index >= self.total_bits:
            import logging
            logger = logging.getLogger('swi3s_visualizer.bus_model')
            logger.warning(f'add_bit: bit_index {bit.bit_index} out of bounds [0, {self.total_bits})')

        # Append to list (allow multiple bits at same position)
        self.bits.append(bit)

        # Track clashes
        if bit.clash == ClashType.DIFFERENT_DEVICE:
            if bit.bit_index not in self.bus_clashes:
                self.bus_clashes.append(bit.bit_index)
        elif bit.clash == ClashType.SAME_DEVICE:
            if bit.bit_index not in self.device_clashes:
                self.device_clashes.append(bit.bit_index)

    def get_bits_at(self, bit_index: int) -> List[BitInfo]:
        """Get all bits at the specified index.

        Args:
            bit_index: Global sequential index

        Returns:
            List of BitInfo objects at this position (may be empty)
        """
        return [b for b in self.bits if b.bit_index == bit_index]

    def get_bits_at_position(self, row: int, column: int) -> List[BitInfo]:
        """Get all bits at the specified row and column.

        Args:
            row: Row number
            column: Column number

        Returns:
            List of BitInfo objects at this position (may be empty)
        """
        return self.get_bits_at(self.bit_index(row, column))

    def get_bits_in_row(self, row: int) -> List[BitInfo]:
        """Get all bits in the specified row.

        Args:
            row: Row number

        Returns:
            List of BitInfo objects in the row, sorted by bit_index
        """
        start_idx = row * self.num_columns
        end_idx = start_idx + self.num_columns
        bits = [b for b in self.bits if start_idx <= b.bit_index < end_idx]
        return sorted(bits, key=lambda b: b.bit_index)

    def remove_bits_matching(self, bit_index: int, device: int, slot_type: SlotType) -> int:
        """Remove bits at a given position matching device and slot type.

        This is used when a higher-priority slot (data bit) suppresses a
        lower-priority slot (guard or tail) from the same device.

        Args:
            bit_index: Global sequential index
            device: Device number to match
            slot_type: Slot type to match (GUARD_0, GUARD_1, TAIL, etc.)

        Returns:
            Number of bits removed
        """
        original_count = len(self.bits)
        self.bits = [b for b in self.bits if not (
            b.bit_index == bit_index and
            b.device == device and
            b.slot == slot_type
        )]
        return original_count - len(self.bits)

    def add_read_overlap(self, bit_index: int) -> None:
        """Record a read overlap at the specified bit index.

        Args:
            bit_index: Index where multiple readers overlap
        """
        if bit_index not in self.read_overlaps:
            self.read_overlaps.append(bit_index)


class BusModelJSONEncoder(json.JSONEncoder):
    """JSON encoder for BusModel and related classes.

    Output format organizes by bit index as top level:
    {
        "num_rows": 1,
        "num_columns": 16,
        "row_rate": 3072,
        "bits": {
            "0": {
                "row": 0,
                "column": 0,
                "slots": [
                    { "slot_type": "CDS", "direction": "SOURCE", ... }
                ]
            },
            "1": {
                "row": 0,
                "column": 1,
                "slots": [
                    { "slot_type": "DATA", "direction": "SOURCE", "dp_num": 0, ... },
                    { "slot_type": "DATA", "direction": "SOURCE", "dp_num": 1, ... }
                ]
            }
        },
        "bus_clashes": [],
        "device_clashes": [],
        "read_overlaps": []
    }
    """

    def default(self, o):
        if isinstance(o, BusModel):
            # Group bits by bit_index
            bits_by_index: Dict[int, List[BitInfo]] = {}
            for bit in o.bits:
                if bit.bit_index not in bits_by_index:
                    bits_by_index[bit.bit_index] = []
                bits_by_index[bit.bit_index].append(bit)

            # Build the bits dictionary with row, column, slots structure
            bits_dict = {}
            for bit_index in sorted(bits_by_index.keys()):
                bit_list = bits_by_index[bit_index]
                row = bit_index // o.num_columns
                column = bit_index % o.num_columns
                bits_dict[str(bit_index)] = {
                    'row': row,
                    'column': column,
                    'slots': [self._encode_slot(b) for b in bit_list]
                }

            # Build warnings dict from various warning lists
            warnings = self._build_warnings_dict(o)

            return {
                'num_rows': o.num_rows,
                'num_columns': o.num_columns,
                'row_rate': o.row_rate,
                'bits': bits_dict,
                'bus_clashes': o.bus_clashes,
                'device_clashes': o.device_clashes,
                'read_overlaps': o.read_overlaps,
                'warnings': warnings,
            }
        elif isinstance(o, Enum):
            return o.name
        return super().default(o)

    def _encode_slot(self, bit: BitInfo) -> dict:
        """Encode a single slot entry (without bit_index, row, column)."""
        return {
            'slot': bit.slot.name,
            'direction': bit.direction.name,
            'device': bit.device,
            'dp': bit.dp,
            'sample': bit.sample,
            'channel': bit.channel,
            'bit': bit.bit,
            'clash': bit.clash.name,
        }

    def _build_warnings_dict(self, model: BusModel) -> Dict[str, List[int]]:
        """Build warnings dict from various warning lists in the model.

        Extracts DP indices from warning messages. Returns dict with
        warning types as keys and lists of affected DP indices as values.
        Empty categories are omitted.

        Args:
            model: BusModel containing warning lists

        Returns:
            Dict like {"truncation": [0, 1], "validation": [2]}
        """
        import re
        warnings: Dict[str, List[int]] = {}

        # Extract DP indices from interval_overflow_warnings
        # Format: (dp_name, bits_needed, bits_available) where dp_name is "DP0", "DP1", etc.
        if model.interval_overflow_warnings:
            truncation_dps: List[int] = []
            for dp_name, _, _ in model.interval_overflow_warnings:
                match = re.match(r'DP(\d+)', dp_name)
                if match:
                    dp_index = int(match.group(1))
                    if dp_index not in truncation_dps:
                        truncation_dps.append(dp_index)
            if truncation_dps:
                warnings['truncation'] = sorted(truncation_dps)

        # Extract DP indices from validation_issues
        # Format: (name, ValidationResult) where name is "DP0", "Interface", etc.
        if model.validation_issues:
            validation_dps: List[int] = []
            for name, _ in model.validation_issues:
                match = re.match(r'DP(\d+)', name)
                if match:
                    dp_index = int(match.group(1))
                    if dp_index not in validation_dps:
                        validation_dps.append(dp_index)
            if validation_dps:
                warnings['validation'] = sorted(validation_dps)

        return warnings
