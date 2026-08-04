"""Drawing utilities for SWI3S visualizer.

Vendored into SWI3S Studio WITHOUT `canvas_renderer` (the tkinter rendering layer
Studio doesn't use) so the engine imports with no tkinter/customtkinter dependency.
"""

from .clash_detector import (
    BitSlotOccupancy,
    ClashDetector,
    ClashInfo,
    ClashType,  # Backward compatibility alias for SlotClashCategory
    DeviceClashType,
    SlotClashCategory,
)

__all__ = [
    'ClashDetector',
    'ClashType',  # Backward compatibility
    'SlotClashCategory',
    'ClashInfo',
    'BitSlotOccupancy',
    'DeviceClashType',
]
