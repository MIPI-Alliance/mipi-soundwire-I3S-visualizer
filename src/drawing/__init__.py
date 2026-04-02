"""Drawing utilities for SWI3S visualizer."""

from .clash_detector import (
    ClashDetector,
    ClashType,  # Backward compatibility alias for SlotClashCategory
    SlotClashCategory,
    ClashInfo,
    BitSlotOccupancy,
    DeviceClashType,
)
from .canvas_renderer import CanvasRenderer

__all__ = [
    'ClashDetector',
    'ClashType',  # Backward compatibility
    'SlotClashCategory',
    'ClashInfo',
    'BitSlotOccupancy',
    'DeviceClashType',
    'CanvasRenderer',
]
