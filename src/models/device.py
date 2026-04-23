"""Device class for SoundWire I3S bus device abstraction.

This module provides the Device class that encapsulates device behavior
and manages data ports belonging to each device on the bus.

NOTE: This module must remain UI-independent. No tkinter, widgets, dialogs,
or any UI framework imports are allowed. This module should be usable as a
library without any UI dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from src.config.constants import SpecialDevices

if TYPE_CHECKING:
    from .dataport import DataPort
    from .interface import Interface


class Device:
    """Represents a device on the SoundWire I3S bus.

    Encapsulates device behavior and holds data ports for each device.
    Provides priority ordering for processing and distinguishes between
    special devices (Manager, Universal, Visualizer) and peripherals.

    Attributes:
        device_num: Device number (-1=Manager, -2=Universal, -3=Viz, 0-11=peripheral)
        _interface: Reference to parent Interface
        _data_ports: List of data ports owned by this device
    """

    def __init__(self, device_num: int, interface: 'Interface') -> None:
        """Initialize a Device.

        Args:
            device_num: Device number (-1=Manager, -2=Universal, -3=Viz, 0-11=peripheral)
            interface: Parent Interface containing shared registers
        """
        self.device_num = device_num
        self._interface = interface
        self._data_ports: List['DataPort'] = []

    @property
    def data_ports(self) -> List['DataPort']:
        """Return list of data ports owned by this device."""
        return self._data_ports

    @property
    def num_columns(self) -> int:
        """Return number of columns from interface (for backward compat)."""
        return self._interface.num_columns

    @property
    def is_manager(self) -> bool:
        """True if this is the Manager device."""
        return self.device_num == SpecialDevices.MANAGER

    @property
    def is_universal(self) -> bool:
        """True if this is the Universal device (CDS)."""
        return self.device_num == SpecialDevices.UNIVERSAL

    @property
    def is_visualizer(self) -> bool:
        """True if this is the Visualizer pseudo-device."""
        return self.device_num == SpecialDevices.VISUALIZER

    @property
    def is_peripheral(self) -> bool:
        """True if this is a peripheral device (0-11)."""
        return (SpecialDevices.MIN_REGULAR <= self.device_num
                <= SpecialDevices.MAX_REGULAR)

    @property
    def priority(self) -> int:
        """Processing priority for this device.

        Lower values are processed first:
        - Manager (-1): priority 0 (first)
        - Peripherals (0-11): priority 1-12
        - Universal (-2): high priority (processed via CDS system slots)
        - Visualizer (-3): highest priority (post-processing only)

        Returns:
            Priority value for ordering
        """
        if self.is_manager:
            return 0
        elif self.is_peripheral:
            return self.device_num + 1
        elif self.is_universal:
            return 100  # CDS is handled via system slots, not device iteration
        else:
            return 200  # Visualizer is post-processing only

    def add_data_port(self, dp: 'DataPort') -> None:
        """Add a data port to this device.

        Args:
            dp: Data port to add
        """
        if dp not in self._data_ports:
            self._data_ports.append(dp)

    def remove_data_port(self, dp: 'DataPort') -> None:
        """Remove a data port from this device.

        Args:
            dp: Data port to remove
        """
        if dp in self._data_ports:
            self._data_ports.remove(dp)

    def fifo_pull(self, dp_index: int, ch_index: int) -> Optional[int]:
        """Audio source delivers the next data bit for (DP, channel).

        Conceptual hook for the data-layer fifo. The visualizer renders
        metadata only (slot kind, position, channel/sample/bit indices) and
        does not need real bit values, so the default implementation is a
        no-op returning None. Hardware-realistic simulators or test harnesses
        may subclass to wire a real audio fifo.
        """
        return None

    def fifo_push(self, dp_index: int, ch_index: int, bit: int) -> None:
        """Audio sink receives a data bit for (DP, channel).

        Conceptual hook for the data-layer fifo. See fifo_pull for context.
        """
        pass

    def __repr__(self) -> str:
        """String representation for debugging."""
        if self.is_manager:
            name = "Manager"
        elif self.is_universal:
            name = "Universal"
        elif self.is_visualizer:
            name = "Visualizer"
        else:
            name = f"Peripheral-{self.device_num}"
        return f"Device({name}, ports={len(self.data_ports)})"


def create_device_map(data_ports: List['DataPort'], interface: 'Interface') -> dict[int, Device]:
    """Create a map of device number to Device objects from data ports.

    Groups data ports by their device number and creates Device objects
    for each unique device.

    Args:
        data_ports: List of data ports to group
        interface: Interface containing device assignments and shared registers

    Returns:
        Dictionary mapping device number to Device object
    """
    device_map: dict[int, Device] = {}

    for dp_index, dp in enumerate(data_ports):
        # Determine device number from interface's device assignments
        device_num = interface.get_dp_device(dp_index)

        # Create device if not exists
        if device_num not in device_map:
            device_map[device_num] = Device(device_num, interface)

        # Add data port to device
        device_map[device_num].add_data_port(dp)

    return device_map


def get_devices_in_priority_order(device_map: dict[int, Device]) -> List[Device]:
    """Get devices sorted by processing priority.

    Args:
        device_map: Map of device number to Device

    Returns:
        List of devices sorted by priority (lowest first)
    """
    return sorted(device_map.values(), key=lambda d: d.priority)
