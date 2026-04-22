"""Interface configuration class for SWI3S visualizer"""

from __future__ import annotations

from typing import Dict, List, TYPE_CHECKING

# Import math at module level for math.lcm
import math

from src.utils.descriptors import ValidatedInt, ValidatedBool, ValidatedFloat

# Import DataPort only for type checking to avoid circular import
if TYPE_CHECKING:
    from .dataport import DataPort
    from .device import Device
    from .flow_control_port import FlowControlPort


class Interface:
    """SoundWire I3S Interface configuration

    Represents the complete interface configuration including timing,
    frame structure, and data port definitions.
    """

    # Frame parameters (class constants)
    # NumColumns_REG is excess-1 encoded: register value N means N+1 actual columns
    MIN_COLUMNS_PER_ROW = 1   # Minimum register value (2 actual columns)
    MAX_COLUMNS_PER_ROW = 31  # Maximum register value (32 actual columns)
    MIN_COLUMNS = MIN_COLUMNS_PER_ROW
    MAX_COLUMNS = MAX_COLUMNS_PER_ROW
    MIN_ROWS = 1
    MAX_ROWS = 10240
    MIN_ROW_RATE = 1.0
    MAX_ROW_RATE = 48000.0
    MIN_SKIPPING_DENOMINATOR = 1
    MAX_SKIPPING_DENOMINATOR = 4096
    MIN_S0_WIDTH = 1
    MAX_S0_WIDTH = 8
    MIN_S1_WIDTH = 1
    MAX_S1_WIDTH = 8
    MIN_CDS_WIDTH = 0
    MAX_CDS_WIDTH = 7
    MIN_CDS_TAIL_WIDTH = 0
    MAX_CDS_TAIL_WIDTH = 3
    MIN_TAIL_WIDTH = 0
    MAX_TAIL_WIDTH = 2

    NUM_DATA_PORTS = 12

    # Property descriptors for validated attributes
    # Integer properties with range validation
    NumColumns_REG: int = ValidatedInt('NumColumns_REG', MIN_COLUMNS, MAX_COLUMNS, 'NumColumns_REG')  # type: ignore[assignment]
    s0_width: int = ValidatedInt('s0_width', MIN_S0_WIDTH, MAX_S0_WIDTH, 'S0 width')  # type: ignore[assignment]
    s1_width: int = ValidatedInt('s1_width', MIN_S1_WIDTH, MAX_S1_WIDTH, 'S1 width')  # type: ignore[assignment]
    CDS_BitWidth_REG: int = ValidatedInt('CDS_BitWidth_REG', MIN_CDS_WIDTH, MAX_CDS_WIDTH, 'CDS width')  # type: ignore[assignment]
    CDS_TailWidth_REG: int = ValidatedInt('CDS_TailWidth_REG', MIN_CDS_TAIL_WIDTH, MAX_CDS_TAIL_WIDTH, 'CDS tail width')  # type: ignore[assignment]
    tail_width: int = ValidatedInt('tail_width', MIN_TAIL_WIDTH, MAX_TAIL_WIDTH, 'Tail width')  # type: ignore[assignment]
    SkippingDenominator_REG: int = ValidatedInt('SkippingDenominator_REG', MIN_SKIPPING_DENOMINATOR, MAX_SKIPPING_DENOMINATOR, 'Skipping denominator')  # type: ignore[assignment]
    row_rate: float = ValidatedFloat('row_rate', MIN_ROW_RATE, MAX_ROW_RATE, 'Row rate')  # type: ignore[assignment]

    # Boolean properties
    phy3_enabled: bool = ValidatedBool('phy3_enabled', 'PHY3 enabled')  # type: ignore[assignment]
    cds_handover_enabled: bool = ValidatedBool('cds_handover_enabled', 'Draw CDS Handover')  # type: ignore[assignment]
    s1_handover_enabled: bool = ValidatedBool('s1_handover_enabled', 'Draw S1 Handover')  # type: ignore[assignment]
    CDS_GuardEnabled_REG: bool = ValidatedBool('CDS_GuardEnabled_REG', 'CDS guard enabled')  # type: ignore[assignment]
    CDS_GuardPolarity_REG: bool = ValidatedBool('CDS_GuardPolarity_REG', 'CDS guard polarity')  # type: ignore[assignment]

    def __init__(self) -> None:
        # Import here to avoid circular dependency at module level
        from .dataport import DataPort
        from .device import Device
        from .flow_control_port import FlowControlPort

        self.NumColumns_REG: int = 15  # Register value 15 = 16 actual columns
        self.phy3_enabled: bool = False
        self.s0_width: int = Interface.MIN_S0_WIDTH
        self.s1_width: int = Interface.MIN_S1_WIDTH
        self.cds_handover_enabled: bool = True
        self.s1_handover_enabled: bool = True
        self.CDS_GuardEnabled_REG: bool = False
        self.CDS_GuardPolarity_REG: bool = False
        self.CDS_BitWidth_REG: int = Interface.MIN_CDS_WIDTH
        self.CDS_TailWidth_REG: int = Interface.MIN_CDS_TAIL_WIDTH
        self.tail_width: int = Interface.MIN_TAIL_WIDTH
        self.SkippingDenominator_REG: int = 1
        self.row_rate: float = 3072.0
        self.description: str = ''  # User description for CSV storage

        # Primary storage: devices dict containing DataPorts
        # Device 0 is the default device, created with all DataPorts initially
        self.devices: Dict[int, Device] = {}
        default_device = Device(0, self)
        self.devices[0] = default_device

        # Create all DataPorts and add to default device
        for i in range(self.NUM_DATA_PORTS):
            dp = DataPort(default_device, i)
            default_device.add_data_port(dp)

        # Parallel per-DP Flow Control Ports, indexed by dp_index.
        # Owned by Interface (not DataPort) so the DataPort hardware model
        # stays pure — FCP lifecycle is driven independently by the engine.
        # Each FCP keeps a config back-reference to its DataPort for
        # hardware-accurate reads of FlowMode_REG / PortDirection_REG.
        self.flow_control_ports: List['FlowControlPort'] = [
            FlowControlPort(self._get_dp_by_index(i))
            for i in range(self.NUM_DATA_PORTS)
        ]

        # Device assignments for each data port (legacy, for backward compat)
        # Values: device number (0-11), or SpecialDevices.MANAGER (-1) for manager
        # Index corresponds to dp_index
        self.dp_device_assignments: List[int] = [0] * self.NUM_DATA_PORTS

    @property
    def data_ports(self) -> List['DataPort']:
        """Return flat list of all DataPorts sorted by dp_index.

        This property provides backward compatibility with code that
        expects interface.data_ports to be a list.
        """
        all_dps: List['DataPort'] = []
        for device in self.devices.values():
            all_dps.extend(device.data_ports)
        return sorted(all_dps, key=lambda dp: dp.dp_index)

    @property
    def num_columns(self) -> int:
        """Return the actual number of columns (NumColumns_REG + 1)."""
        return self.NumColumns_REG + 1

    def get_fcp(self, dp_index: int) -> 'FlowControlPort':
        """Get the FlowControlPort parallel to the DataPort at dp_index.

        The FCP is owned by the Interface (not the DataPort) so the DataPort
        hardware model stays a pure bit-emission source. The engine drives
        the FCP lifecycle explicitly at the same points the DataPort
        transitions (frame reset, new transport, row boundary, interval wrap).

        Args:
            dp_index: Canonical data port index (0-11)

        Returns:
            The FlowControlPort parallel to data_ports[dp_index].
        """
        return self.flow_control_ports[dp_index]

    def get_dp_device(self, dp_index: int) -> int:
        """Get device assignment for a data port.

        Args:
            dp_index: Data port index (0-11)

        Returns:
            Device number (0-11) or SpecialDevices.MANAGER (-1)
        """
        return self.dp_device_assignments[dp_index]

    def _get_dp_by_index(self, dp_index: int) -> 'DataPort':
        """Get a DataPort by its canonical index.

        Args:
            dp_index: Data port index (0-11)

        Returns:
            The DataPort with the given dp_index

        Raises:
            ValueError: If no DataPort with the given index exists
        """
        for device in self.devices.values():
            for dp in device.data_ports:
                if dp.dp_index == dp_index:
                    return dp
        raise ValueError(f"No DataPort found with dp_index {dp_index}")

    def set_dp_device(self, dp_index: int, device_num: int) -> None:
        """Set device assignment for a data port.

        Moves the DataPort to the specified device, creating the device
        if it doesn't exist.

        Args:
            dp_index: Data port index (0-11)
            device_num: Device number (0-11) or SpecialDevices.MANAGER (-1)
        """
        # Early return if no change
        if self.dp_device_assignments[dp_index] == device_num:
            return

        from .device import Device

        # Update legacy tracking list
        self.dp_device_assignments[dp_index] = device_num

        # Get the DataPort
        dp = self._get_dp_by_index(dp_index)

        # Find and remove from current device
        for device in self.devices.values():
            if dp in device.data_ports:
                device.remove_data_port(dp)
                break

        # Create target device if needed
        if device_num not in self.devices:
            self.devices[device_num] = Device(device_num, self)

        # Add to target device
        self.devices[device_num].add_data_port(dp)

    def is_dp_in_manager(self, dp_index: int) -> bool:
        """Check if a data port is assigned to the manager.

        Args:
            dp_index: Data port index (0-11)

        Returns:
            True if data port is in manager
        """
        from src.config.constants import SpecialDevices
        return self.dp_device_assignments[dp_index] == SpecialDevices.MANAGER

    @property
    def interval_lcm(self) -> int:
        """Calculate LCM of all enabled data port pattern repetition periods.

        The System SSP Interval is the LCM of all data port pattern periods -
        the number of rows after which all patterns align and repeat.

        For each data port:
        - Base interval = Interval_REG + 1
        - If skipping enabled: pattern_period = base × denom
          (the skipping accumulator cycles through denom values)
        - Otherwise: pattern_period = base

        Example: Interval=7 (8 rows), SkippingNumerator=1, Denominator=4
        → pattern_period = 8 × 4 = 32 rows
        (3 active intervals then 1 skipped, repeating every 32 rows)
        """
        interval_list: List[int] = []
        for data_port in self.data_ports:
            # Include data ports that have channels configured (NumChannels > 0)
            # Uses cached _num_channels property for performance
            if data_port.config._num_channels > 0:
                base_interval = data_port.config.Interval_REG + 1
                skipping_num = data_port.config.SkippingNumerator_REG

                if skipping_num > 0 and skipping_num < self.SkippingDenominator_REG:
                    # Pattern repeats when skipping accumulator cycles back
                    # This happens every (base_interval * denom) rows
                    denom = self.SkippingDenominator_REG
                    pattern_period = base_interval * denom
                    interval_list.append(pattern_period)
                else:
                    interval_list.append(base_interval)

        if not interval_list:
            return 1  # Identity for LCM - same as DP with Interval=0

        # Use math.lcm (Python 3.9+) for efficient LCM calculation
        result: int = interval_list[0]
        for val in interval_list[1:]:
            result = math.lcm(result, val)

        return result

    @property
    def CDS_HorizontalStart_REG(self) -> int:
        """Calculate the starting column for CDS.

        In PHY3 mode:
            - S1 is at column 0
            - S1 tails follow S1 (columns 1 to tail_width)
            - S1 handover (if enabled) follows tails
            - CDS starts after S1 handover

        In non-PHY3 mode:
            - CDS starts at column 0

        Returns:
            The column index where CDS starts
        """
        if self.phy3_enabled:
            s1_handover_width = 1 if self.s1_handover_enabled else 0
            # S1 at 0, then tails, then handover, then CDS
            return 1 + self.tail_width + s1_handover_width
        else:
            # CDS starts at column 0 in non-PHY3 mode
            return 0
