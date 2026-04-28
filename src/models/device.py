"""Device class for SoundWire I3S bus device abstraction.

This module provides the Device class that encapsulates device behavior
and manages data ports belonging to each device on the bus.

NOTE: This module must remain UI-independent. No tkinter, widgets, dialogs,
or any UI framework imports are allowed. This module should be usable as a
library without any UI dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Union

from src.config.constants import SpecialDevices
from .bit_slot import BitSlotState, BitSlotData
from .enums import SlotType, DirectionType
from .dataport import DataPort
from .flow_control_port import FlowControlPort

if TYPE_CHECKING:
    from .dataport import DataPortConfig
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
        self._active_port: Optional[Union[DataPort, FlowControlPort]] = None
        self._current_slot: Optional[BitSlotState] = None
        self._last_slot_per_port: dict = {}

    @property
    def data_ports(self) -> List['DataPort']:
        """Return list of data ports owned by this device."""
        return self._data_ports

    @property
    def num_columns(self) -> int:
        """Frame column count (shared bus geometry)."""
        return self._interface.num_columns

    @property
    def SkippingDenominator_REG(self) -> int:
        """Skipping denominator (shared bus register)."""
        return self._interface.SkippingDenominator_REG

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

    # ------------------------------------------------------------------
    # Bus I/O. The device drives the bus on behalf of its DataPorts:
    # writes bits when a DP is sourcing, reads bits when a DP is sinking.
    # DataPort.clock_tick() decides when each operation occurs (per the
    # cascade and wide-bit timing); the device performs the actual bit
    # write/read. Default implementations are no-ops — the visualizer
    # renders bus metadata via the engine's _derive_*_bit_slot helpers
    # without needing real bit values. Hardware-realistic simulators or
    # test harnesses subclass to drive a real bus.
    #
    # The methods take no arguments. Channel / sample / bit position
    # information for the visualizer is read from dp.state by the engine;
    # device subclasses that need to identify the calling DP do so via
    # their own mechanism (per-DP bound hooks, context, etc.).
    # ------------------------------------------------------------------

    def _channel_from_index(self, config: 'DataPortConfig', index: int) -> int:
        count = -1
        for i in range(16):
            if config.EnableCh_REG & (1 << i):
                count += 1
                if count == index:
                    return i
        return 0

    def _build_dp_slot(self, slot_type, direction):
        dp = self._active_port
        assert isinstance(dp, DataPort)
        if slot_type in (SlotType.DATA, SlotType.TX_PRESENT):
            # Source fires on first UI (wide_bit_remaining == BitWidth_REG);
            # sink fires on last UI (wide_bit_remaining == 0).
            expected_wbr = dp.config.BitWidth_REG if direction == DirectionType.SOURCE else 0
            fresh = (
                dp.state.channel_group_base_channel == 0
                and dp.state.channel_index == 0
                and dp.state.sample_in_group == 0
                and dp.state.bit_in_channel == dp.config.SampleSize_REG
                and dp.state.samples_in_group_remaining == dp.config.SampleGrouping_REG
                and dp.state.wide_bit_remaining == expected_wbr
                and dp.state.txp_pending == dp.config._txp_enabled
            )
            if slot_type == SlotType.TX_PRESENT:
                data = BitSlotData(
                    sample_in_group=dp.state.sample_in_group,
                    channel=self._channel_from_index(dp.config, dp.state.channel_index),
                    bit=0,
                )
            else:
                data = BitSlotData(
                    sample_in_group=dp.state.sample_in_group,
                    channel=self._channel_from_index(dp.config, dp.state.channel_index),
                    bit=dp.state.bit_in_channel,
                )
            return BitSlotState(
                slot_type=slot_type,
                direction=direction,
                data=data,
                fresh_transport=fresh,
            )
        return BitSlotState(slot_type=slot_type, direction=direction)

    def _build_fcp_slot(self, slot_type, direction):
        return BitSlotState(slot_type=slot_type, direction=direction)

    def _record_dp(self, slot_type, direction):
        slot = self._build_dp_slot(slot_type, direction)
        self._current_slot = slot
        held_slot = BitSlotState(
            slot_type=slot.slot_type,
            direction=slot.direction,
            device_num=slot.device_num,
            dp_num=slot.dp_num,
            data=slot.data,
            fresh_transport=False,
        )
        self._last_slot_per_port[id(self._active_port)] = held_slot

    def _record_fcp(self, slot_type, direction):
        slot = self._build_fcp_slot(slot_type, direction)
        self._current_slot = slot
        self._last_slot_per_port[id(self._active_port)] = slot

    def _record_held(self):
        prev = self._last_slot_per_port.get(id(self._active_port))
        if prev is not None:
            self._current_slot = prev

    def _drq_direction(self) -> DirectionType:
        # DRQ direction is opposite to the parent DP's data direction.
        # FCP._dataport.config.PortDirection_REG: True = SINK DP -> DRQ SOURCE.
        fcp = self._active_port
        assert isinstance(fcp, FlowControlPort)
        dp_config = fcp._dataport.config
        return DirectionType.SOURCE if dp_config.PortDirection_REG else DirectionType.SINK

    def write_data_bit_from_fifo(self) -> None:
        """Pull a fresh DATA bit from the audio fifo and write it to the bus.

        Called at UI 0 of a wide-bit period for a Source DP."""
        self._record_dp(SlotType.DATA, DirectionType.SOURCE)

    def held_write_bit(self) -> None:
        """Hold the previously written bit on the bus for one more UI.

        Called for wide-bit repeat UIs (DATA, TX_PRESENT, or TAIL)."""
        self._record_held()

    def read_data_bit_to_fifo(self) -> None:
        """Read a DATA bit from the bus and push it to the audio fifo.

        Called at the last UI of a wide-bit period for a Sink DP."""
        self._record_dp(SlotType.DATA, DirectionType.SINK)

    def write_txp(self) -> None:
        """Write a fresh TX_PRESENT bit to the bus.

        Called at UI 0 of a wide-bit TX_PRESENT period for a Source DP."""
        self._record_dp(SlotType.TX_PRESENT, DirectionType.SOURCE)

    def read_txp(self) -> None:
        """Read a TX_PRESENT bit from the bus.

        Called at the last UI of a wide-bit TX_PRESENT period for a Sink DP."""
        self._record_dp(SlotType.TX_PRESENT, DirectionType.SINK)

    def write_drq(self) -> None:
        """Write a fresh DRQ bit to the bus.

        Called at UI 0 of a wide-bit DRQ period when the FCP's DRQ direction
        is SOURCE (i.e., parent DP is Sink). Subsequent wide-bit UIs are
        held_write_bit."""
        self._record_fcp(SlotType.DRQ, self._drq_direction())

    def read_drq(self) -> None:
        """Read a DRQ bit from the bus.

        Called at the last UI of a wide-bit DRQ period when the FCP's DRQ
        direction is SINK (i.e., parent DP is Source)."""
        self._record_fcp(SlotType.DRQ, self._drq_direction())

    def write_guard0(self) -> None:
        """Write a guard 0 bit to the bus (single UI, post-data emission)."""
        if isinstance(self._active_port, FlowControlPort):
            self._record_fcp(SlotType.GUARD_0, DirectionType.SOURCE)
        else:
            self._record_dp(SlotType.GUARD_0, DirectionType.SOURCE)

    def write_guard1(self) -> None:
        """Write a guard 1 bit to the bus (single UI, post-data emission)."""
        if isinstance(self._active_port, FlowControlPort):
            self._record_fcp(SlotType.GUARD_1, DirectionType.SOURCE)
        else:
            self._record_dp(SlotType.GUARD_1, DirectionType.SOURCE)

    def write_tail(self) -> None:
        """Write a fresh TAIL bit to the bus.

        Called at UI 0 of a TailWidth_REG-wide post-data period.
        Subsequent tail UIs are held_write_bit."""
        if isinstance(self._active_port, FlowControlPort):
            self._record_fcp(SlotType.TAIL, DirectionType.SOURCE)
        else:
            self._record_dp(SlotType.TAIL, DirectionType.SOURCE)

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
