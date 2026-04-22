"""SWI3S Visualizer Data Models

This package contains the data model classes for representing
SoundWire I3S frames, interfaces, and data ports.
"""

from .enums import DirectionType, SlotType, DisplayField, FlowMode, TransportPhase
from .frame import Frame_model, Row_info, Col_info, Slot_info, SimpleJSONEncoder
from .interface import Interface
from .dataport import DataPort, DataPortConfig
from .flow_control_port import FlowControlPort, FlowControlPortConfig, FlowControlPortState
from .bit_slot import BitSlotData, BitSlotState
from .bus_model import BusModel, BitInfo, ClashType, BusModelJSONEncoder
from .device import Device, create_device_map, get_devices_in_priority_order
from .manager import Manager, SystemSlotLayout, SystemSlot

__all__ = [
    # Enums
    'DirectionType',
    'SlotType',
    'ClashType',
    'FlowMode',
    'TransportPhase',
    # Frame model (legacy)
    'Frame_model',
    'Row_info',
    'Col_info',
    'Slot_info',
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
    # Manager
    'Manager',
    'SystemSlotLayout',
    'SystemSlot',
]
