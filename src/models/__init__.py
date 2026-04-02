"""SWI3S Visualizer Data Models

This package contains the data model classes for representing
SoundWire I3S frames, interfaces, and data ports.
"""

from .enums import DirectionType, SlotType, DisplayField, FlowMode
from .frame import Frame_model, Row_info, Col_info, Slot_info, SimpleJSONEncoder
from .interface import Interface
from .dataport import DataPort, DataPortConfig
from .bit_slot import BitSlotData, BitSlotState, NOT_OWNED_SLOT
from .bus_model import BusModel, BitInfo, ClashType, BusModelJSONEncoder
from .device import Device, create_device_map, get_devices_in_priority_order
from .manager import Manager, SystemSlotLayout, SystemSlot

__all__ = [
    # Enums
    'DirectionType',
    'SlotType',
    'ClashType',
    'FlowMode',
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
    # Bit slot state
    'BitSlotData',
    'BitSlotState',
    'NOT_OWNED_SLOT',
    # Device
    'Device',
    'create_device_map',
    'get_devices_in_priority_order',
    # Manager
    'Manager',
    'SystemSlotLayout',
    'SystemSlot',
]
