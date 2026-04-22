"""Enumeration types for the SWI3S visualizer frame model"""

from enum import Enum, IntEnum, Flag, auto


class DirectionType(Enum):
    """Direction of data flow for a slot"""
    SOURCE = 0  # Slot is driven (write)
    SINK   = 1  # Slot is read


class FlowMode(IntEnum):
    """Flow control mode for data ports.

    Determines how data flow is controlled between source and sink.
    """
    NORMAL = 0        # Normal operation - no flow control bits
    TX_CONTROLLED = 1 # Tx Controlled - TxPresent bits indicate data validity
    RX_CONTROLLED = 2 # Rx Controlled - DRQ bits for data request
    ASYNC = 3         # Asynchronous - both TxPresent and DRQ bits


class PortMode(IntEnum):
    """Port mode for data ports.

    Determines the operational mode of the data port.
    """
    NORMAL = 0        # Normal operation - data bits
    RESERVED = 1      # Reserved - not used
    TEST_ONES = 2     # Test Mode - all ones (c0t1)
    TEST_ZEROS = 3    # Test Mode - all zeros (c0t0)


class TransportPhase(Enum):
    """Lifecycle phase for a DataPort's transport pattern.

    Collapses the former (transport_state, row_transport_done,
    horizontal_count_done) triple into one exhaustive enum. Each phase is
    mutually exclusive; illegal combinations are unrepresentable.
    """
    IDLE         = 0  # Pre-transport: between intervals, before Offset row
    ACTIVE       = 1  # Emitting data inside the transport window
    SPACING      = 2  # Inter-CG / inter-transport gap (spacing counter > 0)
    ROW_DONE     = 3  # No more data on this row; interval still alive
    PATTERN_DONE = 4  # Transport pattern complete for this interval


class SlotType(Enum):
    """Type of slot in the frame"""
    EMPTY    = -1 # Position not owned by any DataPort (returned by next_bit_slot when inactive)
    NORMAL   = 0  # Regular data bit
    GUARD_0  = 1  # Guard bit with polarity 0 (G0)
    TAIL     = 2  # Tail bit (to prevent clash)
    HANDOVER = 3  # Handover/turnaround bit
    CDS      = 4  # Control Data Stream
    S0       = 5  # S0 synchronization
    S1       = 6  # S1 synchronization
    GUARD_1  = 7  # Guard bit with polarity 1 (G1)
    CLASH    = 8  # Bus clash - multiple devices driving same slot
    TX_PRESENT = 9  # TxPresent bit for Tx Controlled or Async flow modes
    DRQ      = 10  # Data Request bit for Rx Controlled or Async flow modes


class DisplayField(Flag):
    """Flags for which fields to display in bit slot labels."""
    SAMPLE = auto()   # 's' - show sample number
    CHANNEL = auto()  # 'c' - show channel number
    BIT = auto()      # 'b' - show bit number
