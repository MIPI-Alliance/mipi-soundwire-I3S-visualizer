"""
Copyright (c) 2023 MIPI Alliance and other contributors. All Rights Reserved.

Constants and configuration values for SoundWire I3S Visualizer.
"""

from typing import Tuple


class CSVFields:
    """CSV file field names - centralized to prevent typos.

    Naming convention: Constant names are UPPER_SNAKE_CASE versions of their CSV field values.
    This makes it easy to identify which CSV field a constant represents.
    """
    # File metadata
    SAVE_FILE_USING_EXCESS_ONE = 'Save file using excess one'

    # Interface parameters (must match INTERFACE_CSV_FIELD_NAMES order)
    NUM_COLUMNS_REG = 'NumColumns_REG'
    ROW_RATE = 'RowRate'
    ROWS_TO_DRAW = 'RowsToDraw'
    SKIPPING_DENOMINATOR_REG = 'SkippingDenominator_REG'
    PHY3_ENABLED = 'PHY3Enabled'
    S0_WIDTH = 'S0Width'
    S1_TAIL_WIDTH_REG = 'S1TailWidth_REG'
    ENFORCE_S1_HANDOVER = 'EnforceS1Handover'
    CDS_BIT_WIDTH_REG = 'CDS_BitWidth_REG'
    CDS_GUARD_ENABLED_REG = 'CDS_GuardEnabled_REG'
    CDS_GUARD_POLARITY_REG = 'CDS_GuardPolarity_REG'
    CDS_TAIL_WIDTH_REG = 'CDS_TailWidth_REG'
    ENFORCE_CDS_HANDOVER = 'EnforceCDSHandover'

    # Data port parameters (DP_ prefix distinguishes from interface fields)
    DP_NAME = 'Name'
    DP_DEVICE_NUMBER_REG = 'DeviceNumber_REG'
    DP_NUM_CHANNELS = 'NumChannels'
    DP_CHANNEL_GROUPING_REG = 'ChannelGrouping_REG'
    DP_SPACING_REG = 'Spacing_REG'
    DP_SAMPLE_SIZE_REG = 'SampleSize_REG'
    DP_SAMPLE_GROUPING_REG = 'SampleGrouping_REG'
    DP_INTERVAL_REG = 'Interval_REG'
    DP_SKIPPING_NUMERATOR_REG = 'SkippingNumerator_REG'
    DP_OFFSET_REG = 'Offset_REG'
    DP_HORIZONTAL_START_REG = 'HorizontalStart_REG'
    DP_HORIZONTAL_COUNT_REG = 'HorizontalCount_REG'
    DP_TAIL_WIDTH_REG = 'TailWidth_REG'
    DP_BIT_WIDTH_REG = 'BitWidth_REG'
    DP_PORT_DIRECTION_REG = 'PortDirection_REG'
    DP_ENFORCE_HANDOVER = 'EnforceHandover'
    DP_GUARD_ENABLE_REG = 'GuardEnable_REG'
    DP_GUARD_POLARITY_REG = 'GuardPolarity_REG'
    DP_ENABLED = 'Enabled'
    DP_MANAGER_DATAPORT = 'ManagerDataport'
    DP_SUB_ROW_INTERVAL_REG = 'SubRowInterval_REG'
    DP_ENABLE_CH_REG = 'EnableCh_REG'
    DP_DISPLAY_FIELDS = 'DisplayFields'  # Display field selection: "sc", "sb", "cb" (default)
    DP_FLOW_MODE_REG = 'FlowMode_REG'  # Flow mode: 0=Normal, 1=Tx, 2=Rx, 3=Async
    DP_PORT_MODE_REG = 'PortMode_REG'  # Port mode: 0=Normal, 1=Reserved, 2=Test Ones, 3=Test Zeros
    DP_SCRAMBLER_EN_REG = 'ScramblerEn_REG'  # Scrambler enabled (True/False)

    # Flow Control Port (FCP) parameters for DRQ bits (Rx Controlled/Async modes)
    DP_FCP_HORIZONTAL_START_REG = 'FCP_HorizontalStart_REG'
    DP_FCP_BIT_WIDTH_REG = 'FCP_BitWidth_REG'
    DP_FCP_TAIL_WIDTH_REG = 'FCP_TailWidth_REG'
    DP_FCP_OFFSET_REG = 'FCP_Offset_REG'
    DP_FCP_GUARD_ENABLE_REG = 'FCP_GuardEnable_REG'
    DP_FCP_GUARD_POLARITY_REG = 'FCP_GuardPolarity_REG'


class SpecialDevices:
    """Special device numbers for JSON export and clash detection"""
    MANAGER = -1      # S0, S1, their handovers, manager data ports
    UNIVERSAL = -2    # CDS and CDS handover (any device can write)
    VISUALIZER = -3   # Visualizer-only elements (handovers) - not physical bus traffic
    MIN_REGULAR = 0   # Regular device numbers: 0-11
    MAX_REGULAR = 11


class SlotTypeStrings:
    """String representations of slot types for internal use"""
    NOT_OWNED = 'not owned'
    TAIL = 'tail'
    GUARD_0 = 'G0'
    GUARD_1 = 'G1'
    HANDOVER = 'TA'
    CDS = 'CDS'
    S0 = 'S0'
    S1 = 'S1'
    TX_PRESENT = 'TxP'  # TxPresent bit prefix (label will be TxPn where n is channel)
    DRQ = 'DRQ'  # Data Request bit for Rx Controlled or Async flow modes


class CanvasColors:
    """Canvas drawing colors with semantic meaning - match error_panel COLORS"""
    # Clash/error colors - match error panel colors for consistency
    BUS_CLASH = '#D32F2F'        # Red - physical bus collision (multiple writers)
    DEVICE_CLASH = '#FBC02D'     # Yellow - same-device internal conflict
    READ_OVERLAP = '#1976D2'     # Blue - read overlap (multiple readers)

    # Note: Other colors come from theme and are accessed via:
    # self.DARK_GRAY - border/grid lines
    # self.LIGHT_GRAY - lighter borders
    # self.PREFERRED_GRAY - backgrounds/tails (theme-aware)
    # self.current_text_color - text (theme-aware)
    # DP_COLORS - data port colors (see below)


class Colors:
    """Color palette for data ports and UI elements"""
    # Data port colors (12 colors for 12 data ports)
    # Thanks to Eddie for the colours
    DP_COLORS: Tuple[str, ...] = (
        '#FF80BF',  # DP0 - Pink
        '#FFA080',  # DP1 - Coral
        '#FFFF80',  # DP2 - Yellow
        '#A0FF80',  # DP3 - Light Green
        '#80FFFF',  # DP4 - Cyan
        '#8080FF',  # DP5 - Blue
        '#BF80FF',  # DP6 - Purple
        '#FFBFFF',  # DP7 - Light Pink
        '#FFBFBF',  # DP8 - Light Coral
        '#FFFFBF',  # DP9 - Light Yellow
        '#BFFFBF',  # DP10 - Pale Green
        '#BFFFFF',  # DP11 - Pale Cyan
    )


class FeatureFlags:
    """Feature flags for optional behavior"""
    SRI_USES_SAMPLE_COUNT = True
    FRACTION_IS_DITHERED_TRANSPORT_INTERVAL = False
    UI_IS_EXCESS_1 = True


class InterfaceRanges:
    """Validation ranges for Interface parameters.

    These ranges are used by UI validation and validators, NOT by the model itself.
    The model (Interface) is a pure data container.
    """
    MIN_COLUMNS_PER_ROW = 1   # Minimum register value (2 actual columns)
    MAX_COLUMNS_PER_ROW = 31  # Maximum register value (32 actual columns)
    MIN_COLUMNS = MIN_COLUMNS_PER_ROW
    MAX_COLUMNS = MAX_COLUMNS_PER_ROW
    MIN_ROWS = 1
    MAX_ROWS = 10240
    MIN_ROW_RATE = 1.0
    MAX_ROW_RATE = 48000.0
    MIN_SKIPPING_DENOMINATOR = 1
    MAX_SKIPPING_DENOMINATOR = 4096
    MIN_S0_WIDTH = 1
    MAX_S0_WIDTH = 8
    MIN_S1_WIDTH = 1
    MAX_S1_WIDTH = 8
    MIN_CDS_S0_HANDOVER_WIDTH = 0
    MAX_CDS_S0_HANDOVER_WIDTH = 8
    MIN_CDS_WIDTH = 0
    MAX_CDS_WIDTH = 7
    MIN_CDS_TAIL_WIDTH = 0
    MAX_CDS_TAIL_WIDTH = 3
    MIN_TAIL_WIDTH = 0
    MAX_TAIL_WIDTH = 2


class DataPortRanges:
    """Validation ranges for DataPort parameters.

    These ranges are used by UI validation and validators, NOT by the model itself.
    The model (DataPortConfig) is a pure data container.
    """
    MIN_DEVICE_NUMBER = 0
    MAX_DEVICE_NUMBER = 11
    MIN_OFFSET = 0
    MIN_INTERVAL = 0
    MAX_INTERVAL = 4095
    MAX_OFFSET = MAX_INTERVAL
    MIN_CHANNELS = 0
    MAX_CHANNELS = 16  # Natural count: 0-16 channels enabled
    MIN_SAMPLE_SIZE = 0
    MAX_SAMPLE_SIZE = 31
    MIN_SAMPLE_GROUPING = 0
    MAX_SAMPLE_GROUPING = 7
    MIN_CHANNEL_GROUPING = 0
    MAX_CHANNEL_GROUPING = 15  # ChannelGrouping_REG is 4-bit (0-15)
    MIN_CHANNEL_GROUP_SPACING = 0
    MAX_CHANNEL_GROUP_SPACING = MAX_CHANNELS
    MIN_SKIPPING_NUMERATOR = 0
    MAX_SKIPPING_NUMERATOR = InterfaceRanges.MAX_SKIPPING_DENOMINATOR - 1
    MIN_SKIPPING_DENOMINATOR = 0
    MAX_SKIPPING_DENOMINATOR = InterfaceRanges.MAX_SKIPPING_DENOMINATOR - 1
    MIN_H_START = 0
    MAX_H_START = InterfaceRanges.MAX_COLUMNS_PER_ROW
    MIN_H_COUNT = 0
    MAX_H_COUNT = InterfaceRanges.MAX_COLUMNS_PER_ROW
    MIN_TAIL_WIDTH = 0
    MAX_TAIL_WIDTH = 2
    MIN_BIT_WIDTH = 0
    MAX_BIT_WIDTH = 2
    MIN_ENABLE_CH = 0x0000
    MAX_ENABLE_CH = 0xFFFF
    MIN_FLOW_MODE = 0
    MAX_FLOW_MODE = 3
    MIN_PORT_MODE = 0
    MAX_PORT_MODE = 3
    MIN_FRAME_COLUMNS = 2
    MAX_FRAME_COLUMNS = InterfaceRanges.MAX_COLUMNS_PER_ROW + 1  # Actual column count

    # Flow Control Port (FCP) parameter ranges for DRQ bits
    MIN_FCP_H_START = 0
    MAX_FCP_H_START = InterfaceRanges.MAX_COLUMNS_PER_ROW
    MIN_FCP_BIT_WIDTH = 0
    MAX_FCP_BIT_WIDTH = MAX_BIT_WIDTH
    MIN_FCP_TAIL_WIDTH = 0
    MAX_FCP_TAIL_WIDTH = MAX_TAIL_WIDTH
    MIN_FCP_OFFSET = 0
    MAX_FCP_OFFSET = MAX_INTERVAL
    MIN_FCP_GUARD_POLARITY = 0
    MAX_FCP_GUARD_POLARITY = 1


class DebugFlags:
    """Debug flags for development"""
    DRAWING = False
    FILE_IO = False
    CLASH = False


# Convenience exports for feature flags
SRI_USES_SAMPLE_COUNT = FeatureFlags.SRI_USES_SAMPLE_COUNT
FRACTION_IS_DITHERED_TRANSPORT_INTERVAL = FeatureFlags.FRACTION_IS_DITHERED_TRANSPORT_INTERVAL
UI_IS_EXCESS_1 = FeatureFlags.UI_IS_EXCESS_1

# Convenience exports for debug flags
Debug_Drawing = DebugFlags.DRAWING
Debug_FileIO = DebugFlags.FILE_IO
Debug_Clash = DebugFlags.CLASH
