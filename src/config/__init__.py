"""
Copyright (c) 2023 MIPI Alliance and other contributors. All Rights Reserved.

Configuration and constants for SoundWire I3S Visualizer.
"""

from .constants import (
    CSVFields,
    SpecialDevices,
    SlotTypeStrings,
    CanvasColors,
    Colors,
    FeatureFlags,
    InterfaceRanges,
    DataPortRanges,
    DebugFlags,
    # Convenience exports for feature flags (used by UI)
    SRI_USES_SAMPLE_COUNT,
    FRACTION_IS_DITHERED_TRANSPORT_INTERVAL,
    UI_IS_EXCESS_1,
    # Convenience exports for debug flags (used by drawing)
    Debug_Drawing,
    Debug_FileIO,
    Debug_Clash,
)

__all__ = [
    'CSVFields',
    'SpecialDevices',
    'SlotTypeStrings',
    'CanvasColors',
    'Colors',
    'FeatureFlags',
    'InterfaceRanges',
    'DataPortRanges',
    'DebugFlags',
]
