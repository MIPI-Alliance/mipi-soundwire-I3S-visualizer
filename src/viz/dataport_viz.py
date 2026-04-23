"""Visualization metadata for data ports and interface.

These fields are visualizer-specific and have no effect on hardware behavior.
They control how the visualizer displays the frame, not what the frame contains.

Classes:
    DataPortVizConfig: Per-data-port visualization settings
    VizConfig: Top-level visualization configuration
"""

from dataclasses import dataclass, field
from typing import List

from src.models.enums import DisplayField

# Number of data ports per interface (matches Interface.NUM_DATA_PORTS)
# Defined here to avoid circular import with src.models.interface
NUM_DATA_PORTS = 12


@dataclass
class DataPortVizConfig:
    """Per-data-port visualization configuration.

    These settings affect how the data port is displayed in the visualizer
    but have no effect on the actual SoundWire frame content.

    Attributes:
        name: Display name for the data port (default: "DP0", "DP1", etc.)
        enabled: Whether to draw this data port in the visualization
        enable_handover: Whether to draw handover indicators for this data port
        display_fields: Which label fields to show (sample, channel, bit)
    """
    name: str = ""
    enabled: bool = False
    enable_handover: bool = True
    display_fields: DisplayField = field(default_factory=lambda: DisplayField(0))


@dataclass
class VizConfig:
    """Top-level visualization configuration.

    Contains settings that affect how the entire frame is visualized,
    including per-data-port visualization settings.

    Attributes:
        rows_to_draw: Number of rows to display in the visualization
        data_ports: List of per-data-port visualization configurations
    """
    rows_to_draw: int = 64
    data_ports: List[DataPortVizConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Initialize data port viz configs if not provided."""
        if not self.data_ports:
            self.data_ports = [
                DataPortVizConfig(name=f"DP{i}")
                for i in range(NUM_DATA_PORTS)
            ]
