"""Default register values for DataPort and Flow Control Port configs.

The hardware-model classes in `src/models/dataport.py` and
`src/models/flow_control_port.py` declare their `_REG` fields as
class-level annotations without instance defaults, so an unpopulated
config raises `AttributeError` on any register read. These helpers
populate every register with a zero/False default when a caller wants
a usable blank config (GUI startup, test fixtures, library use).

The CSV loader does NOT call these — missing required CSV fields still
hard-fail.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.models.dataport import DataPortConfig
    from src.models.flow_control_port import FlowControlPortConfig
    from src.models.interface import Interface


def reset_dp_config(cfg: DataPortConfig) -> None:
    """Populate every DataPort register with a zero/False default."""
    cfg.EnableCh_REG = 0
    cfg.ChannelGrouping_REG = 0
    cfg.Spacing_REG = 0
    cfg.SampleSize_REG = 0
    cfg.SampleGrouping_REG = 0
    cfg.Interval_REG = 0
    cfg.SkippingNumerator_REG = 0
    cfg.Offset_REG = 0
    cfg.HorizontalStart_REG = 0
    cfg.HorizontalCount_REG = 0
    cfg.TailWidth_REG = 0
    cfg.BitWidth_REG = 0
    cfg.PortDirection_REG = False
    cfg.GuardEnable_REG = False
    cfg.GuardPolarity_REG = False
    cfg.SubRowInterval_REG = False
    cfg.FlowMode_REG = 0
    cfg.PortMode_REG = 0
    cfg.ScramblerEn_REG = False


def reset_fcp_config(cfg: FlowControlPortConfig) -> None:
    """Populate every Flow Control Port register with a zero/False default."""
    cfg.FCP_HorizontalStart_REG = 0
    cfg.FCP_BitWidth_REG = 0
    cfg.FCP_TailWidth_REG = 0
    cfg.FCP_Offset_REG = 0
    cfg.FCP_GuardEnable_REG = False
    cfg.FCP_GuardPolarity_REG = False


def reset_port_configs(interface: Interface) -> None:
    """Reset every DataPort and Flow Control Port config on the interface."""
    for dp in interface.data_ports:
        reset_dp_config(dp.config)
    for fcp in interface.flow_control_ports:
        reset_fcp_config(fcp.config)
