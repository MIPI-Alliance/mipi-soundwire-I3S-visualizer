"""SWI3S Visualizer Data Models

This package contains the data model classes for representing
SoundWire I3S frames, interfaces, and data ports.
"""

from .bit_slot import BitSlotData, BitSlotState
from .bus_model import BitInfo, BusModel, BusModelJSONEncoder, ClashType
from .dataport import DataPort, DataPortConfig
from .device import Device, create_device_map, get_devices_in_priority_order
from .enums import DirectionType, DisplayField, FlowMode, SlotType, TransportPhase
from .flow_control_port import FlowControlPort, FlowControlPortConfig, FlowControlPortState
from .frame import ColInfo, FrameModel, RowInfo, SimpleJSONEncoder, SlotInfo
from .interface import Interface

__all__ = [
    # Enums
    'DirectionType',
    'SlotType',
    'ClashType',
    'FlowMode',
    'TransportPhase',
    # Frame model (legacy)
    'FrameModel',
    'RowInfo',
    'ColInfo',
    'SlotInfo',
    'SimpleJSONEncoder',
    # Bus model (new)
    'BusModel',
    'BitInfo',
    'BusModelJSONEncoder',
    # Interface
    'Interface',
    # DataPort
    'DataPort',
    'DataPortConfig',
    'DisplayField',
    # FlowControlPort
    'FlowControlPort',
    'FlowControlPortConfig',
    'FlowControlPortState',
    # Bit slot state
    'BitSlotData',
    'BitSlotState',
    # Device
    'Device',
    'create_device_map',
    'get_devices_in_priority_order',
]
