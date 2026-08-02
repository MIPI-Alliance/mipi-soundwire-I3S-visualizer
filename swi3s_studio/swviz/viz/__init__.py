"""Visualization layer for SWI3S visualizer.

This package contains visualizer-specific configuration that does not affect
the SoundWire specification model. These settings control how frames are
displayed, not what they contain.
"""

from .dataport_viz import DataPortVizConfig, VizConfig

__all__ = ['DataPortVizConfig', 'VizConfig']
