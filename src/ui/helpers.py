"""
UI helper functions for SWI3S Visualizer.

This module contains utility functions and constants used by UI components.
"""

import re
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from src.models import Interface
from src.config import UI_IS_EXCESS_1, DataPortRanges, InterfaceRanges


# =============================================================================
# UI Field Names (for label generation, NOT for CSV I/O)
# =============================================================================

# Interface parameter field names in UI display order
# Note: CDS_GuardPolarity_REG is NOT in this list - it's controlled via the CDS guard checkbox dialog
INTERFACE_FIELD_NAMES: List[str] = [
    'NumColumns_REG',
    'SkippingDenominator_REG',
    'PHY3Enabled',
    'S0Width',
    'S1TailWidth_REG',
    'EnforceS1Handover',
    'CDS_BitWidth_REG',
    'CDS_GuardEnabled_REG',
    'CDS_TailWidth_REG',
    'EnforceCDSHandover',
    'RowRate',
    'RowsToDraw',
]

# Data port parameter field names in UI display order
# Note: GuardPolarity_REG is NOT in this list - it's controlled via the guard checkbox dialog
DP_FIELD_NAMES: List[str] = [
    'DeviceNumber_REG',
    'NumChannels',
    'SampleSize_REG',
    'SampleGrouping_REG',
    'ChannelGrouping_REG',
    'Spacing_REG',
    'Interval_REG',
    'Offset_REG',
    'HorizontalStart_REG',
    'HorizontalCount_REG',
    'TailWidth_REG',
    'BitWidth_REG',
    'SkippingNumerator_REG',
    'PortDirection_REG',      # Source/Sink boolean
    'GuardEnable_REG',        # Guard Enabled boolean (click to select G0/G1/Off)
    'SubRowInterval_REG',     # SRI boolean
    'FlowMode_REG',           # Flow Control mode (click to select)
    'PortMode_REG',           # Port Mode (click to select Normal/Test)
    'ScramblerEn_REG',        # Scrambler Enabled boolean
    'EnforceHandover',        # Enforce Handover boolean (moved to bottom)
    'Enabled',                # Data Port Enabled boolean (moved to bottom)
    'SampleRate',             # Calculated display field
]


# =============================================================================
# Parameter Ranges (for UI validation and label generation)
# =============================================================================

# Frame display limits
MIN_ROWS_IN_FRAME = 1
MAX_ROWS_IN_FRAME = 10240

# Parameter ranges for interface parameters
# Maps CSV field name to (min, max) tuple; omit for boolean fields
# NumColumns_REG uses excess-1: register value N means N+1 columns
INTERFACE_PARAM_RANGES: Dict[str, tuple] = {
    'NumColumns_REG': (InterfaceRanges.MIN_COLUMNS_PER_ROW, InterfaceRanges.MAX_COLUMNS_PER_ROW),
    'RowRate': (InterfaceRanges.MIN_ROW_RATE, InterfaceRanges.MAX_ROW_RATE),
    'RowsToDraw': (MIN_ROWS_IN_FRAME, MAX_ROWS_IN_FRAME),
    'SkippingDenominator_REG': (InterfaceRanges.MIN_SKIPPING_DENOMINATOR, InterfaceRanges.MAX_SKIPPING_DENOMINATOR),
    # PHY3Enabled - boolean, no range
    'S0Width': (InterfaceRanges.MIN_S0_WIDTH, InterfaceRanges.MAX_S0_WIDTH),
    'S1TailWidth_REG': (InterfaceRanges.MIN_TAIL_WIDTH, InterfaceRanges.MAX_TAIL_WIDTH),
    # S1Handover - boolean, no range
    'CDS_BitWidth_REG': (InterfaceRanges.MIN_CDS_WIDTH, InterfaceRanges.MAX_CDS_WIDTH),
    # CDS_GuardEnabled_REG - boolean, no range
    'CDS_TailWidth_REG': (InterfaceRanges.MIN_CDS_TAIL_WIDTH, InterfaceRanges.MAX_CDS_TAIL_WIDTH),
    # CDSHandover - boolean, no range
}

# UI excess-1 adjustment for display ranges
_excess_1_adj = 0 if UI_IS_EXCESS_1 else 1

# Parameter ranges for data port parameters
# Maps field name to (min, max) tuple; omit for boolean/special fields
DP_PARAM_RANGES: Dict[str, tuple] = {
    'DeviceNumber_REG': (DataPortRanges.MIN_DEVICE_NUMBER, DataPortRanges.MAX_DEVICE_NUMBER),
    'NumChannels': (DataPortRanges.MIN_CHANNELS, DataPortRanges.MAX_CHANNELS),
    'SampleSize_REG': (DataPortRanges.MIN_SAMPLE_SIZE + _excess_1_adj, DataPortRanges.MAX_SAMPLE_SIZE + _excess_1_adj),
    'SampleGrouping_REG': (DataPortRanges.MIN_SAMPLE_GROUPING + _excess_1_adj, DataPortRanges.MAX_SAMPLE_GROUPING + _excess_1_adj),
    'ChannelGrouping_REG': (DataPortRanges.MIN_CHANNEL_GROUPING, DataPortRanges.MAX_CHANNEL_GROUPING),
    'Spacing_REG': (DataPortRanges.MIN_CHANNEL_GROUP_SPACING, DataPortRanges.MAX_CHANNEL_GROUP_SPACING),
    'Interval_REG': (DataPortRanges.MIN_INTERVAL + _excess_1_adj, DataPortRanges.MAX_INTERVAL + _excess_1_adj),
    'Offset_REG': (DataPortRanges.MIN_OFFSET, DataPortRanges.MAX_OFFSET),
    'HorizontalStart_REG': (DataPortRanges.MIN_H_START, DataPortRanges.MAX_H_START),
    'HorizontalCount_REG': (DataPortRanges.MIN_H_COUNT, DataPortRanges.MAX_H_COUNT),
    'TailWidth_REG': (DataPortRanges.MIN_TAIL_WIDTH, DataPortRanges.MAX_TAIL_WIDTH),
    'BitWidth_REG': (DataPortRanges.MIN_BIT_WIDTH, DataPortRanges.MAX_BIT_WIDTH),
    'SkippingNumerator_REG': (DataPortRanges.MIN_SKIPPING_NUMERATOR, DataPortRanges.MAX_SKIPPING_NUMERATOR),
    # PortDirection_REG - boolean (Source/Sink), no range
    # Handover - boolean, no range
    # GuardEnable_REG - boolean, no range
    # SubRowInterval_REG - boolean, no range
    # InManager - boolean, no range
    # Enabled - boolean, no range
    # FlowMode_REG - controlled via dialog, no range shown
    # SampleRate - calculated display, no range
}

# Custom display labels for special fields (overrides friendly_name)
DP_CUSTOM_LABELS: Dict[str, str] = {
    'DeviceNumber_REG': 'Device (0-11,Manager)',
    'PortDirection_REG': 'Source [checked] / Sink',
    'SampleRate': 'Sample Rate [kHz]',
    'Enabled': 'Draw Data Port',
    'FlowMode_REG': 'Flow Control',
    'PortMode_REG': 'Port Test Mode',
    'ScramblerEn_REG': 'Scrambler Enable',
}

# Custom display labels for interface fields (overrides friendly_name)
INTERFACE_CUSTOM_LABELS: Dict[str, str] = {
    'RowRate': 'Row Rate [kHz] (1-48000)',
}


def get_interface_labels() -> List[str]:
    """Generate UI labels for interface parameters from field names and ranges."""
    labels = []
    for name in INTERFACE_FIELD_NAMES:
        # Check if there's a custom label first
        if name in INTERFACE_CUSTOM_LABELS:
            labels.append(INTERFACE_CUSTOM_LABELS[name])
        else:
            labels.append(friendly_name(name, INTERFACE_PARAM_RANGES.get(name)))
    return labels


def get_dp_labels() -> List[str]:
    """Generate UI labels for data port parameters from field names and ranges."""
    labels = []
    for field_name in DP_FIELD_NAMES:
        if field_name in DP_CUSTOM_LABELS:
            labels.append(DP_CUSTOM_LABELS[field_name])
        else:
            labels.append(friendly_name(field_name, DP_PARAM_RANGES.get(field_name)))
    return labels


# =============================================================================
# Tooltip Text Dictionaries
# =============================================================================

# Tooltip text for interface parameters
INTERFACE_TOOLTIPS: Dict[str, str] = {
    'NumColumns_REG': 'Number of bit slots per row',
    'RowRate': 'Frame row rate in kHz',
    'RowsToDraw': 'Number of rows to display',
    'SkippingDenominator_REG': 'Skipping denominator for fractional sample intervals',
    'PHY3Enabled': 'Enable PHY3 with S0/S1 enabled',
    'S0Width': 'Width of S0 ',
    'S1TailWidth_REG': 'S1 tail width',
    'EnforceS1Handover': 'Enforce S1 handovers',
    'CDS_BitWidth_REG': 'Control Data Stream width',
    'CDS_GuardEnabled_REG': 'Enable CDS guard bit',
    'CDS_GuardPolarity_REG': 'CDS guard polarity',
    'CDS_TailWidth_REG': 'CDS tail width',
    'EnforceCDSHandover': 'Enforce CDS handover',
}

# Tooltip text for data port parameters
DP_TOOLTIPS: Dict[str, str] = {
    'DeviceNumber_REG': 'Device Numbber (0-11), or Manager',
    'NumChannels': 'Number of enabled channels',
    'SampleSize_REG': 'Bits per sample (excess-1 encoded)',
    'SampleGrouping_REG': 'Samples grouped per transport pattern (excess-1 encoded)',
    'ChannelGrouping_REG': 'Channels grouped together before spacing',
    'Spacing_REG': 'Bit slots between SRI, or channel, groups.',
    'Interval_REG': 'Rows in a transport pattern frame (excess-1 encoded)',
    'Offset_REG': 'Row offset for first transport opportunity',
    'HorizontalStart_REG': 'Starting column',
    'HorizontalCount_REG': 'Columns owned per row (excess-1 encoded)',
    'TailWidth_REG': 'Tail width (excess-1 encoded)',
    'BitWidth_REG': 'Bit Width (excess-1 encoded)',
    'SkippingNumerator_REG': 'Skipping numerator for fractional intervals',
    'PortDirection_REG': 'Checked=Sink, Unchecked=Source',
    'EnforceHandover': 'Draw handover indicator',
    'GuardEnable_REG': 'Enable guard bit after data',
    'GuardPolarity_REG': 'Guard polarity',
    'SubRowInterval_REG': 'Multiple transports per row',
    'FlowMode_REG': 'Flow control mode',
    'PortMode_REG': 'Port test mode',
    'ScramblerEn_REG': 'Enable data scrambling',
    'Enabled': 'Draw this data port',
    'SampleRate': 'Calculated sample rate',
}


# =============================================================================
# Helper Functions
# =============================================================================

def friendly_name(variable_name: str, range_info: Optional[Tuple] = None) -> str:
    """Convert a variable/register name to friendly UI text.

    Rules:
    - Drop '_REG' suffix
    - Replace remaining underscores with spaces
    - Add space between PascalCase words (but not for acronyms like CDS, S0, S1)
    - Optionally append range in format ' (min-max)'

    Args:
        variable_name: The variable/register name to convert
        range_info: Optional tuple of (min, max) to append as range

    Examples:
        friendly_name('NumColumns_REG') -> 'Num Columns'
        friendly_name('NumColumns_REG', (2, 32)) -> 'Num Columns (2-32)'
        friendly_name('PHY3Enabled') -> 'PHY3 Enabled'
    """
    name = variable_name.replace('_REG', '')
    name = name.replace('_', ' ')
    # Add space before capitals that follow a lowercase letter
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    # Add space after digits before capitals
    name = re.sub(r'([0-9])([A-Z])', r'\1 \2', name)
    # Add space before a capital followed by lowercase, when preceded by capitals
    # This handles CDSHandover -> CDS Handover
    name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', name)
    name = name.strip()

    # Append range if provided
    if range_info is not None:
        min_val, max_val = range_info
        name += f' ({min_val}-{max_val})'

    return name


def validate_entry(action: str, value_if_allowed: str, low: Union[int, str], high: Union[int, str]) -> bool:
    """Validate that entry widget text is within numeric range.

    Used as a Tkinter validatecommand callback. Supports both integer and float values.

    Args:
        action: '1' for insert, '0' for delete
        value_if_allowed: The text that would result if allowed
        low: Minimum allowed value
        high: Maximum allowed value

    Returns:
        True if the value is allowed, False otherwise
    """
    if action == '1':  # Entry action
        try:
            # Try float first to support decimals (int will also work as float)
            value = float(value_if_allowed)
            if float(low) <= value <= float(high):
                return True
            else:
                return False
        except ValueError:
            # Allow partial decimal entry like "123." or "123.4" while typing
            # Check if it's a valid partial float format
            if value_if_allowed and all(c.isdigit() or c == '.' for c in value_if_allowed):
                # Check for multiple decimal points
                if value_if_allowed.count('.') <= 1:
                    # Allow it if we're still typing
                    return True
            return False
    else:
        return True


def validate_entry_values(action: str, value_if_allowed: str, values: Any) -> bool:
    """Validate that entry widget text is in allowed values list.

    Used as a Tkinter validatecommand callback.

    Args:
        action: '1' for insert, '0' for delete
        value_if_allowed: The text that would result if allowed
        values: Collection of allowed values

    Returns:
        True if the value is allowed, False otherwise
    """
    if action == '1':  # Entry action
        try:
            if value_if_allowed in values:
                return True
            else:
                return False
        except ValueError:
            return False
    else:
        return True


def safe_int(str_in: str) -> int:
    """Convert string to int, returning 0 for empty strings.

    Extends int() to handle empty strings from entry widgets gracefully.

    Args:
        str_in: String to convert

    Returns:
        Integer value, or 0 if string is empty

    Raises:
        TypeError: If str_in is not a string
    """
    if not isinstance(str_in, str):
        raise TypeError('Expected str for str_in')
    if str_in == '':
        return 0
    else:
        return int(str_in)


def safe_float(str_in: str, default: float = 0.0) -> float:
    """Convert string to float, returning default for empty strings.

    Extends float() to handle empty strings from entry widgets gracefully.

    Args:
        str_in: String to convert
        default: Default value to return for empty strings (default: 0.0)

    Returns:
        Float value, or default if string is empty

    Raises:
        TypeError: If str_in is not a string
    """
    if not isinstance(str_in, str):
        raise TypeError('Expected str for str_in')
    if str_in == '':
        return default
    else:
        return float(str_in)


# =============================================================================
# Data Port Field Mappings for UI <-> Model sync
# =============================================================================

# Type alias for field mapping tuples
# Format: (attribute_name, row_offset, ui_to_model_transform, model_to_ui_transform)
FieldMapping = Tuple[str, int, Callable[[str], int], Callable[[int], int]]

# Field mappings for data port entry boxes
# Maps attribute name to row offset and bidirectional transformation functions
# Transforms handle excess-1 encoding where needed
# NOTE: NumChannels is handled separately via dp_num_channels_entries (clickable)
# Row offsets are adjusted because NumChannels (row 1) is not in dp_entry_boxes
DP_FIELD_MAPPINGS: List[FieldMapping] = [
    # NumChannels removed - handled via channel selector dialog
    ('SampleSize_REG', 1,  # was row 2, now row 1 in dp_entry_boxes
     lambda v: safe_int(v) - (0 if UI_IS_EXCESS_1 else 1),
     lambda v: v + (0 if UI_IS_EXCESS_1 else 1)),
    ('SampleGrouping_REG', 2,  # was row 3, now row 2
     lambda v: safe_int(v) - (0 if UI_IS_EXCESS_1 else 1),
     lambda v: v + (0 if UI_IS_EXCESS_1 else 1)),
    ('ChannelGrouping_REG', 3,  # was row 4, now row 3
     lambda v: safe_int(v),
     lambda v: v),
    ('Spacing_REG', 4,  # was row 5, now row 4
     lambda v: safe_int(v),
     lambda v: v),
    ('Interval_REG', 5,  # was row 6, now row 5
     lambda v: safe_int(v) - (0 if UI_IS_EXCESS_1 else 1),
     lambda v: v + (0 if UI_IS_EXCESS_1 else 1)),
    ('Offset_REG', 6,  # was row 7, now row 6
     lambda v: safe_int(v),
     lambda v: v),
    ('HorizontalStart_REG', 7,  # was row 8, now row 7
     lambda v: safe_int(v),
     lambda v: v),
    ('HorizontalCount_REG', 8,  # was row 9, now row 8
     lambda v: safe_int(v),
     lambda v: v),
    ('TailWidth_REG', 9,  # was row 10, now row 9
     lambda v: safe_int(v),
     lambda v: v),
    ('BitWidth_REG', 10,  # was row 11, now row 10
     lambda v: safe_int(v),
     lambda v: v),
    ('SkippingNumerator_REG', 11,  # was row 12, now row 11
     lambda v: safe_int(v),
     lambda v: v),
]


# =============================================================================
# Dialog Utilities
# =============================================================================

# Button state colors for dialog selection buttons
BUTTON_COLOR_SELECTED = "#4CAF50"  # Green
BUTTON_COLOR_UNSELECTED = "#808080"  # Gray


def center_dialog_on_parent(dialog: Any, parent: Any) -> None:
    """Center a dialog window on its parent window.

    Args:
        dialog: The dialog window to center (must support update_idletasks, winfo_*, geometry)
        parent: The parent window (must support winfo_*)
    """
    dialog.update_idletasks()
    parent_x = parent.winfo_rootx()
    parent_y = parent.winfo_rooty()
    parent_w = parent.winfo_width()
    parent_h = parent.winfo_height()
    dialog_w = dialog.winfo_width()
    dialog_h = dialog.winfo_height()
    x = parent_x + (parent_w - dialog_w) // 2
    y = parent_y + (parent_h - dialog_h) // 2
    dialog.geometry(f"+{x}+{y}")
