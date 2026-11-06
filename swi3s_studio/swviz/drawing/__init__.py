"""Drawing utilities for the SWI3S visualizer: slot-clash detection.

The package imports no GUI toolkit, so the engine loads headless.
"""

from .clash_detector import (
    BitSlotOccupancy,
    ClashDetector,
    ClashInfo,
    DeviceClashType,
    SlotClashCategory,
)

__all__ = [
    'ClashDetector',
    'SlotClashCategory',
    'ClashInfo',
    'BitSlotOccupancy',
    'DeviceClashType',
]
