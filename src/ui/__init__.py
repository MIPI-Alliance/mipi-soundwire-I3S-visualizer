"""
UI components for SWI3S Visualizer.

This module provides dialogs, widgets, and helper functions for the user interface.
"""

from src.ui.widgets.tooltip import SimpleToolTip
from src.ui.dialogs import (
    ChannelSelectorDialog,
    GuardSelectorDialog,
    DisplayOptionsDialog,
    FlowModeSelectorDialog,
)
from src.ui.helpers import (
    friendly_name,
    validate_entry,
    validate_entry_values,
    safe_int,
    INTERFACE_TOOLTIPS,
    DP_TOOLTIPS,
    INTERFACE_FIELD_NAMES,
    DP_FIELD_NAMES,
    INTERFACE_PARAM_RANGES,
    DP_PARAM_RANGES,
    DP_CUSTOM_LABELS,
    DP_FIELD_MAPPINGS,
    MIN_ROWS_IN_FRAME,
    MAX_ROWS_IN_FRAME,
    get_interface_labels,
    get_dp_labels,
)
from src.ui.constants import (
    ENTRY_WIDTH,
    PIXELS_PER_CHAR,
    ENTRY_PADX,
    ENTRY_PADY,
    NUM_DP_ENTRY_ROWS,
    ROW_SIZE,
    COLUMN_SIZE,
    HEADER_COLUMN_SIZE,
    TEXT_SIZE,
    SCROLLBAR_WIDTH,
    FRAME_Y_OFFSET,
    AUX_CANVAS_FUDGE,
    CHECKBOX_BORDER_WIDTH,
    CHECKBOX_WIDTH,
    CHECKBOX_HEIGHT,
    CHECKBOX_TOP_PADDING,
    CHECKBOX_RELX,
    CHECKBOX_RELY,
    APP_FONT,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_DATA_FRAME_HEIGHT,
    DEFAULT_CANVAS_HEIGHT,
    WINDOW_WIDTH_MULTIPLIER,
)
from src.ui.theme import (
    DARK_GRAY,
    LIGHT_GRAY,
    color_to_hex,
    get_theme_colors,
    get_disabled_colors,
)
from src.ui.app_ui import UIManager, UIConfig
from src.ui.frame_renderer import FrameRenderer, RenderConfig
from src.ui.parameter_panel import ParameterPanel

__all__ = [
    # UI Manager
    'UIManager',
    'UIConfig',
    # Frame Renderer
    'FrameRenderer',
    'RenderConfig',
    # Parameter Panel
    'ParameterPanel',
    # Widgets
    'SimpleToolTip',
    # Dialogs
    'ChannelSelectorDialog',
    'GuardSelectorDialog',
    'DisplayOptionsDialog',
    'FlowModeSelectorDialog',
    # Helper functions
    'friendly_name',
    'validate_entry',
    'validate_entry_values',
    'safe_int',
    'get_interface_labels',
    'get_dp_labels',
    # Tooltip dictionaries
    'INTERFACE_TOOLTIPS',
    'DP_TOOLTIPS',
    # Field names and ranges
    'INTERFACE_FIELD_NAMES',
    'DP_FIELD_NAMES',
    'INTERFACE_PARAM_RANGES',
    'DP_PARAM_RANGES',
    'DP_CUSTOM_LABELS',
    'DP_FIELD_MAPPINGS',
    'MIN_ROWS_IN_FRAME',
    'MAX_ROWS_IN_FRAME',
    # UI layout constants
    'ENTRY_WIDTH',
    'PIXELS_PER_CHAR',
    'ENTRY_PADX',
    'ENTRY_PADY',
    'NUM_DP_ENTRY_ROWS',
    'ROW_SIZE',
    'COLUMN_SIZE',
    'HEADER_COLUMN_SIZE',
    'TEXT_SIZE',
    'SCROLLBAR_WIDTH',
    'FRAME_Y_OFFSET',
    'AUX_CANVAS_FUDGE',
    'CHECKBOX_BORDER_WIDTH',
    'CHECKBOX_WIDTH',
    'CHECKBOX_HEIGHT',
    'CHECKBOX_TOP_PADDING',
    'CHECKBOX_RELX',
    'CHECKBOX_RELY',
    'APP_FONT',
    'DEFAULT_WINDOW_HEIGHT',
    'DEFAULT_DATA_FRAME_HEIGHT',
    'DEFAULT_CANVAS_HEIGHT',
    'WINDOW_WIDTH_MULTIPLIER',
    # Theme utilities
    'DARK_GRAY',
    'LIGHT_GRAY',
    'color_to_hex',
    'get_theme_colors',
    'get_disabled_colors',
]
