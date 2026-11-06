"""
Copyright (c) 2023 MIPI Alliance and other contributors. All Rights Reserved.

CSV file handler for loading and saving SoundWire I3S configurations.
"""

import csv
import io
import logging
import math
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple

from ..config.constants import CSVFields, InterfaceRanges
from ..models.enums import DisplayField
from ..viz import DataPortVizConfig, VizConfig
from .json_handler import _write_text_whole

# =============================================================================
# Field Type Definitions
# =============================================================================

class FieldType(Enum):
    """Types of CSV field values."""
    INT = auto()       # Integer value
    FLOAT = auto()     # Float value
    BOOL = auto()      # Boolean value (true/false, yes/no or 1/0: _TRUE_CELLS, _FALSE_CELLS)
    STRING = auto()    # String value
    BINARY_INT = auto() # Integer in binary format (0b prefix)
    DISPLAY_FIELDS = auto()  # DisplayField flags (s=sample, c=channel, b=bit)


# =============================================================================
# CSV Field Constants (aliases for readability)
# =============================================================================

# Data port field names
DATA_PORT_NAME = CSVFields.DP_NAME
DATA_PORT_NUMBER = CSVFields.DP_DATAPORT_NUMBER
DATA_PORT_DEVICE_NUMBER = CSVFields.DP_DEVICE_NUMBER_REG
DATA_PORT_ENABLE_CH = CSVFields.DP_ENABLE_CH_REG
DATA_PORT_SAMPLE_SIZE = CSVFields.DP_SAMPLE_SIZE_REG
DATA_PORT_SAMPLE_GROUPING = CSVFields.DP_SAMPLE_GROUPING_REG
DATA_PORT_CHANNEL_GROUPING = CSVFields.DP_CHANNEL_GROUPING_REG
DATA_PORT_SPACING = CSVFields.DP_SPACING_REG
DATA_PORT_INTERVAL = CSVFields.DP_INTERVAL_REG
DATA_PORT_OFFSET = CSVFields.DP_OFFSET_REG
DATA_PORT_HORIZONTAL_START = CSVFields.DP_HORIZONTAL_START_REG
DATA_PORT_HORIZONTAL_COUNT = CSVFields.DP_HORIZONTAL_COUNT_REG
DATA_PORT_TAIL_WIDTH = CSVFields.DP_TAIL_WIDTH_REG
DATA_PORT_BIT_WIDTH = CSVFields.DP_BIT_WIDTH_REG
DATA_PORT_SKIPPING_NUMERATOR = CSVFields.DP_SKIPPING_NUMERATOR_REG
DATA_PORT_IS_SOURCE = CSVFields.DP_PORT_DIRECTION_REG
DATA_PORT_DRAW_HANDOVER = CSVFields.DP_ENFORCE_HANDOVER
DATA_PORT_GUARD_ENABLED = CSVFields.DP_GUARD_ENABLE_REG
DATA_PORT_GUARD_POLARITY = CSVFields.DP_GUARD_POLARITY_REG
DATA_PORT_SRI = CSVFields.DP_SUB_ROW_INTERVAL_REG
DATA_PORT_IN_MANAGER = CSVFields.DP_MANAGER_DATAPORT
DATA_PORT_ENABLED = CSVFields.DP_ENABLED
DATA_PORT_DISPLAY_FIELDS = CSVFields.DP_DISPLAY_FIELDS
DATA_PORT_FLOW_MODE = CSVFields.DP_FLOW_MODE_REG
DATA_PORT_PORT_MODE = CSVFields.DP_PORT_MODE_REG
DATA_PORT_SCRAMBLER_EN = CSVFields.DP_SCRAMBLER_EN_REG

# Flow Control Port (FCP) fields for DRQ bits
DATA_PORT_FCP_H_START = CSVFields.DP_FCP_HORIZONTAL_START_REG
DATA_PORT_FCP_BIT_WIDTH = CSVFields.DP_FCP_BIT_WIDTH_REG
DATA_PORT_FCP_TAIL_WIDTH = CSVFields.DP_FCP_TAIL_WIDTH_REG
DATA_PORT_FCP_OFFSET = CSVFields.DP_FCP_OFFSET_REG
DATA_PORT_FCP_GUARD_ENABLE = CSVFields.DP_FCP_GUARD_ENABLE_REG
DATA_PORT_FCP_GUARD_POLARITY = CSVFields.DP_FCP_GUARD_POLARITY_REG

# Per-source CDS guard/tail rows (13 columns: index 0 = Manager, 1..12 = Device
# 0..11). Not in CSVFields — these are Studio-authored rows (model/bus_config.py)
# and are handled specially below (13-wide, not the 12-wide DATAPORT_FIELD_MAP).
CDS_GUARD_ENABLE_PER_SOURCE = 'CDS_GuardEnabledPerSource'
CDS_GUARD_POLARITY_PER_SOURCE = 'CDS_GuardPolarityPerSource'
CDS_TAIL_PER_SOURCE = 'CDS_TailWidthPerSource'
# Drive type and end-drive-early are per source for the same reason: each register sits in
# a device's own CDS block. Drive type: 1 Normal, 0 Special (a CDS 1 left high-Z for the
# Manager's bus keeper). End drive early: 0 drive to the end of the UI,
# 1 stop before the end of the last UI.
CDS_DRIVE_TYPE_PER_SOURCE = 'CDS_DriveTypePerSource'
CDS_EDE_PER_SOURCE = 'CDS_EndDriveEarlyPerSource'
# The scalar spelling of the drive type, read only (never written) — see the load path.
_CDS_DRIVE_TYPE_SCALAR = 'CDS_DriveType'


def _reserved_tail_text(sources: List[str]) -> str:
    """The warning for CDS_TailWidth 3 on `sources` (Manager, Device n), listing every
    source joined by "; " and ending with the grid's 3-UI tail. The caller prefixes the
    line and field. `bus_config.reserved_cds_tail_note` is the BusConfig and native
    loader's own wording of the same fact."""
    width = InterfaceRanges.RESERVED_CDS_TAIL_WIDTH
    return (f"CDS_TailWidth {width} is Reserved (0..2 UI are defined) for "
            f"{'; '.join(f'{s} = {width}' for s in sources)}; the value is kept as written "
            f"and the grid draws a {width}-UI tail.")


# The plain 13-wide integer rows: csv_key -> (Interface attribute, default per source,
# lowest and highest legal cell). A cell outside its range keeps its low bits, as the
# register field does, and is reported in a warning.
# Add a new per-source CDS field as an entry here; the load and save loops read this
# table. Mirrors bus_config._CDS_PER_SOURCE_INT_ROWS, which is the same list for the
# other engine.
#
# CDS_DriveType defaults to 1, against its register reset, so a file with no row reads
# as Normal; CDS_EndDriveEarly defaults to 0, its register reset.
_CDS_INT_PER_SOURCE = {
    CDS_TAIL_PER_SOURCE: ('CDS_Tail_PerSource', 0,
                          InterfaceRanges.MIN_CDS_TAIL_WIDTH, InterfaceRanges.MAX_CDS_TAIL_WIDTH),
    CDS_DRIVE_TYPE_PER_SOURCE: ('CDS_DriveType_PerSource', 1, 0, 1),
    CDS_EDE_PER_SOURCE: ('CDS_EndDriveEarly_PerSource', 0, 0, 1),
}
# Scalar CDS guard/tail registers: READ from v2 files (to derive the per-source rows)
# and derived FROM the per-source rows on every load for the engine layout, but never
# WRITTEN (the per-source rows are authoritative).
_CDS_SCALAR_FIELDS = {'CDS_GuardEnabled_REG', 'CDS_GuardPolarity_REG', 'CDS_TailWidth_REG'}
CDS_NUM_SOURCES = 13  # Manager + Device 0..11


# =============================================================================
# Cell Reading Rules
# =============================================================================

# A whole-number cell: decimal, 0x or 0b, ASCII digits only. These are the forms
# BusConfig and the native loader read as a whole number.
_WHOLE_NUMBER = (
    (re.compile(r'0[bB][01]+'), 2, 2),
    (re.compile(r'0[xX][0-9a-fA-F]+'), 16, 2),
    (re.compile(r'[+-]?[0-9]+'), 10, 0),
)
_FLOAT_CELL = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
_TRUE_CELLS = ('true', '1', 'yes')
_FALSE_CELLS = ('false', '0', 'no')
# The most characters of a file's cell that an error or a warning quotes.
_QUOTED_CHARS = 60
# Unrecognised field names that one warning lists before it counts the rest.
_NAMES_LISTED = 3


def _clip(text: str) -> str:
    """`text` as an error or warning shows a file's text: whole when it has at most
    _QUOTED_CHARS characters, else the first _QUOTED_CHARS - 1 and an ellipsis."""
    return text if len(text) <= _QUOTED_CHARS else text[:_QUOTED_CHARS - 1] + '\u2026'


def _quote(text: str) -> str:
    """`text` clipped, in single quotes."""
    return f"'{_clip(text)}'"


# =============================================================================
# Load Result Data Class
# =============================================================================

@dataclass
class CSVLoadResult:
    """Result of loading a CSV file."""
    success: bool
    rows_in_frame: int = 0
    unrecognized_fields: List[Tuple[int, str]] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    error_message: Optional[str] = None
    # AppVersion string as written into the file, or None if the field wasn't present.
    # Advisory only: nothing compares it against MIN_COMPATIBLE_CSV_VERSION. Wire in a
    # parse/compare here if a format break needs to reject or convert older files.
    file_app_version: Optional[str] = None
    # The VizConfig the load filled: the one passed in, else the one created for the load.
    viz_config: Optional[VizConfig] = None
    # Per-row import warnings, each one complete message: a repeated field row, a
    # DisplayFields character outside 's', 'c', 'b', a per-source CDS value, or a legacy
    # scalar CDS_TailWidth_REG or CDS_DriveType, that does not fit its register field (its
    # low bits are carried), a CDS tail width of 3 (Reserved), and a negative legacy scalar
    # CDS_TailWidth_REG or CDS_DriveType (ignored: the CDS sources keep their defaults).
    # A per-port register field is kept as written; the native loader reports one that
    # does not fit.
    warnings: List[str] = field(default_factory=list)

    # Fields a writer may leave out, whose absence is not reported by `notes`.
    OPTIONAL_FIELDS = frozenset({'Interface: Description'})

    def notes(self, *, missing: bool = True) -> List[str]:
        """The load's import warnings as messages: the fields that were not recognised
        and ignored, and (with `missing`) the fields absent from the file that took their
        defaults, OPTIONAL_FIELDS excepted. The per-row warnings come first, one message
        each, then one message for the unrecognised fields and one for the missing. The
        unrecognised message lists the first `_NAMES_LISTED` names, each cut to
        `_QUOTED_CHARS` characters, and counts the rest."""
        out: List[str] = list(self.warnings)
        if self.unrecognized_fields:
            n = len(self.unrecognized_fields)
            names = ", ".join(
                f"{_clip(name)} (line {line})"
                for line, name in self.unrecognized_fields[:_NAMES_LISTED])
            if n > _NAMES_LISTED:
                names += f" and {n - _NAMES_LISTED} more"
            out.append(f"{n} field{'s are' if n != 1 else ' is'} not recognised and "
                       f"{'were' if n != 1 else 'was'} ignored: {names}.")
        gone = [f for f in self.missing_fields if f not in self.OPTIONAL_FIELDS]
        if missing and gone:
            n = len(gone)
            out.append(f"{n} field{'s are' if n != 1 else ' is'} missing and "
                       f"{'take their defaults' if n != 1 else 'takes its default'}: "
                       f"{', '.join(gone)}.")
        return out


# =============================================================================
# CSV Handler Class
# =============================================================================

class CSVHandler:
    """Handles CSV file I/O for configuration data.

    This class provides methods to save and load configuration data to/from CSV files.
    """

    # Interface parameter field names for CSV save/load
    # Format: (csv_field_name, attribute_name, field_type)
    INTERFACE_FIELD_MAP = [
        ('NumColumns_REG', 'NumColumns_REG', FieldType.INT),
        ('SkippingDenominator_REG', 'SkippingDenominator_REG', FieldType.INT),
        ('PHY3Enabled', 'phy3_enabled', FieldType.BOOL),
        ('S0Width', 's0_width', FieldType.INT),
        ('S1TailWidth_REG', 'tail_width', FieldType.INT),
        ('EnforceS1Handover', 's1_handover_enabled', FieldType.BOOL),
        ('CDS_BitWidth_REG', 'CDS_BitWidth_REG', FieldType.INT),
        ('CDS_GuardEnabled_REG', 'CDS_GuardEnabled_REG', FieldType.BOOL),
        ('CDS_GuardPolarity_REG', 'CDS_GuardPolarity_REG', FieldType.BOOL),
        ('CDS_TailWidth_REG', 'CDS_TailWidth_REG', FieldType.INT),
        ('EnforceCDSHandover', 'cds_handover_enabled', FieldType.BOOL),
        ('RowRate', 'row_rate', FieldType.FLOAT),
        ('Description', 'description', FieldType.STRING),  # User description text
    ]

    # Interface-level visualization fields (go to VizConfig, not Interface)
    # Format: (csv_field_name, viz_attribute_name, field_type)
    INTERFACE_VIZ_FIELD_MAP = [
        ('RowsToDraw', 'rows_to_draw', FieldType.INT),
    ]

    # Mapping of CSV field names to data port attributes
    # Format: (csv_field_name, attribute_name, field_type, uses_excess_one)
    # NOTE: DeviceNumber_REG and InManager are handled specially (stored in interface)
    # NOTE: Visualization fields (Name, EnableHandover, EnableDataPort, display_fields)
    #       are in DATAPORT_VIZ_FIELD_MAP below
    DATAPORT_FIELD_MAP = [
        (DATA_PORT_ENABLE_CH, 'EnableCh_REG', FieldType.BINARY_INT, False),
        (DATA_PORT_SAMPLE_SIZE, 'SampleSize_REG', FieldType.INT, True),
        (DATA_PORT_SAMPLE_GROUPING, 'SampleGrouping_REG', FieldType.INT, True),
        (DATA_PORT_CHANNEL_GROUPING, 'ChannelGrouping_REG', FieldType.INT, False),
        (DATA_PORT_SPACING, 'Spacing_REG', FieldType.INT, False),
        (DATA_PORT_INTERVAL, 'Interval_REG', FieldType.INT, True),
        (DATA_PORT_OFFSET, 'Offset_REG', FieldType.INT, False),
        (DATA_PORT_HORIZONTAL_START, 'HorizontalStart_REG', FieldType.INT, False),
        (DATA_PORT_HORIZONTAL_COUNT, 'HorizontalCount_REG', FieldType.INT, False),
        (DATA_PORT_TAIL_WIDTH, 'TailWidth_REG', FieldType.INT, False),
        (DATA_PORT_BIT_WIDTH, 'BitWidth_REG', FieldType.INT, False),
        (DATA_PORT_SKIPPING_NUMERATOR, 'SkippingNumerator_REG', FieldType.INT, False),
        (DATA_PORT_IS_SOURCE, 'PortDirection_REG', FieldType.BOOL, False),
        (DATA_PORT_GUARD_ENABLED, 'GuardEnable_REG', FieldType.BOOL, False),
        (DATA_PORT_GUARD_POLARITY, 'GuardPolarity_REG', FieldType.BOOL, False),
        (DATA_PORT_SRI, 'SubRowInterval_REG', FieldType.BOOL, False),
        (DATA_PORT_FLOW_MODE, 'FlowMode_REG', FieldType.INT, False),
        (DATA_PORT_PORT_MODE, 'PortMode_REG', FieldType.INT, False),
        (DATA_PORT_SCRAMBLER_EN, 'ScramblerEn_REG', FieldType.BOOL, False),
        # Flow Control Port (FCP) parameters for DRQ bits
        # These route to interface.flow_control_ports[dp_index].config.* (not data_port.config.*) via _FCP_CSV_FIELDS below.
        (DATA_PORT_FCP_H_START, 'FCP_HorizontalStart_REG', FieldType.INT, False),
        (DATA_PORT_FCP_BIT_WIDTH, 'FCP_BitWidth_REG', FieldType.INT, False),
        (DATA_PORT_FCP_TAIL_WIDTH, 'FCP_TailWidth_REG', FieldType.INT, False),
        (DATA_PORT_FCP_OFFSET, 'FCP_Offset_REG', FieldType.INT, False),
        (DATA_PORT_FCP_GUARD_ENABLE, 'FCP_GuardEnable_REG', FieldType.BOOL, False),
        (DATA_PORT_FCP_GUARD_POLARITY, 'FCP_GuardPolarity_REG', FieldType.BOOL, False),
    ]

    # CSV fields whose attr lives on interface.flow_control_ports[dp_index].config instead of data_port.config.
    _FCP_CSV_FIELDS = frozenset({
        DATA_PORT_FCP_H_START,
        DATA_PORT_FCP_BIT_WIDTH,
        DATA_PORT_FCP_TAIL_WIDTH,
        DATA_PORT_FCP_OFFSET,
        DATA_PORT_FCP_GUARD_ENABLE,
        DATA_PORT_FCP_GUARD_POLARITY,
    })

    # Data port visualization fields (go to VizConfig.data_ports, not DataPortConfig)
    # Format: (csv_field_name, viz_attribute_name, field_type)
    DATAPORT_VIZ_FIELD_MAP = [
        (DATA_PORT_NAME, 'name', FieldType.STRING),
        (DATA_PORT_NUMBER, 'dp_number', FieldType.INT),
        (DATA_PORT_DRAW_HANDOVER, 'enable_handover', FieldType.BOOL),
        (DATA_PORT_ENABLED, 'enabled', FieldType.BOOL),
        (DATA_PORT_DISPLAY_FIELDS, 'display_fields', FieldType.DISPLAY_FIELDS),
    ]

    # Special device assignment fields (stored in interface, not dataport)
    DEVICE_ASSIGNMENT_FIELDS = {
        DATA_PORT_DEVICE_NUMBER: FieldType.INT,
        DATA_PORT_IN_MANAGER: FieldType.BOOL,
    }

    # Build lookup dictionaries for faster field matching during load
    _interface_field_dict = {csv_field: (attr, ftype) for csv_field, attr, ftype in INTERFACE_FIELD_MAP}
    _interface_viz_field_dict = {csv_field: (attr, ftype) for csv_field, attr, ftype in INTERFACE_VIZ_FIELD_MAP}
    _dataport_field_dict = {csv_field: (attr, ftype, excess) for csv_field, attr, ftype, excess in DATAPORT_FIELD_MAP}
    _dataport_viz_field_dict = {csv_field: (attr, ftype) for csv_field, attr, ftype in DATAPORT_VIZ_FIELD_MAP}

    @staticmethod
    def _known_field_names() -> frozenset:
        """Every field name a load recognises, per-source CDS rows included."""
        return frozenset(
            {*CSVHandler._interface_field_dict, *CSVHandler._interface_viz_field_dict,
             *CSVHandler._dataport_field_dict, *CSVHandler._dataport_viz_field_dict,
             *CSVHandler.DEVICE_ASSIGNMENT_FIELDS,
             CDS_GUARD_ENABLE_PER_SOURCE, CDS_GUARD_POLARITY_PER_SOURCE,
             *_CDS_INT_PER_SOURCE, _CDS_DRIVE_TYPE_SCALAR})

    # =============================================================================
    # Value Parsing Methods
    # =============================================================================

    @staticmethod
    def parse_int_value(value: str) -> int:
        """Parse an integer value in decimal, binary (0b), or hex (0x) format, as BusConfig
        and the native loader read a whole number: ASCII digits only, so an underscore or
        another script's digit is not part of a number.

        Args:
            value: String value to parse (e.g., "123", "0b1111011", or "0xFFFF")

        Returns:
            Integer value

        Raises:
            ValueError: If value is empty or is not a whole number; the message quotes at
                most `_QUOTED_CHARS` characters of it
        """
        value = value.strip()
        if not value:
            raise ValueError("Cannot parse integer from empty string")
        for pattern, base, skip in _WHOLE_NUMBER:
            if pattern.fullmatch(value):
                try:
                    return int(value[skip:], base)
                except ValueError:          # a decimal run past Python's digit limit
                    break
        raise ValueError(f"Cannot parse integer from {_quote(value)}")

    @staticmethod
    def parse_float_value(value: str) -> float:
        """Parse a float value from string, as BusConfig and the native loader read a number:
        ASCII digits, one point, an exponent only when digits follow it, and a finite result,
        so an underscore, another script's digit, 'nan' or 'inf' is not a number.

        Args:
            value: String value to parse (e.g., "123.45" or "3072")

        Returns:
            Float value

        Raises:
            ValueError: If value is empty or is not a finite number; the message quotes at
                most `_QUOTED_CHARS` characters of it
        """
        value = value.strip()
        if not value:
            raise ValueError("Cannot parse float from empty string")
        if _FLOAT_CELL.fullmatch(value):
            result = float(value)
            if math.isfinite(result):
                return result
        raise ValueError(f"Cannot parse float from {_quote(value)}")

    @staticmethod
    def parse_bool_value(value: str) -> bool:
        """Parse a boolean value from string.

        Accepts, in any case: true/false, yes/no, 1/0 (the spellings BusConfig and the
        native loader accept). Any other text raises ValueError quoting at most
        `_QUOTED_CHARS` characters of it.

        Args:
            value: String value to parse

        Returns:
            Boolean value
        """
        text = value.strip().lower()
        if text in _TRUE_CELLS:
            return True
        if text in _FALSE_CELLS:
            return False
        raise ValueError(f"Cannot parse boolean from {_quote(value.strip())}")

    @staticmethod
    def display_field_strays(value: str) -> str:
        """The characters of a DisplayFields value that are not 's', 'c' or 'b'
        (case-insensitive, surrounding whitespace ignored), in order and without repeats."""
        out = ''
        for ch in value.strip():
            if ch.lower() not in 'scb' and ch not in out:
                out += ch
        return out

    @staticmethod
    def parse_display_fields(value: str) -> DisplayField:
        """Parse DisplayField flags from string.

        Accepts: 's', 'c', 'b', 'sc', 'scb', etc. Any other character is ignored;
        `display_field_strays` names them.

        Args:
            value: String with display field characters (s=sample, c=channel, b=bit)

        Returns:
            DisplayField flags
        """
        result = DisplayField(0)
        value = value.strip().lower()
        if 's' in value:
            result |= DisplayField.SAMPLE
        if 'c' in value:
            result |= DisplayField.CHANNEL
        if 'b' in value:
            result |= DisplayField.BIT
        return result

    @staticmethod
    def display_fields_to_str(flags: DisplayField) -> str:
        """Convert DisplayField flags to string.

        Args:
            flags: DisplayField flags

        Returns:
            String with display field characters (s=sample, c=channel, b=bit)
        """
        result = ''
        if DisplayField.SAMPLE in flags:
            result += 's'
        if DisplayField.CHANNEL in flags:
            result += 'c'
        if DisplayField.BIT in flags:
            result += 'b'
        return result

    @staticmethod
    def parse_value(value: str, field_type: FieldType) -> Any:
        """Parse a CSV value according to its field type.

        Args:
            value: Raw string value from CSV
            field_type: Type of the field

        Returns:
            Parsed value of appropriate type
        """
        if field_type == FieldType.STRING:
            return value

        if field_type == FieldType.BOOL:
            return CSVHandler.parse_bool_value(value)

        if field_type == FieldType.BINARY_INT:
            return CSVHandler.parse_int_value(value)

        if field_type == FieldType.INT:
            return CSVHandler.parse_int_value(value)

        if field_type == FieldType.FLOAT:
            return CSVHandler.parse_float_value(value)

        if field_type == FieldType.DISPLAY_FIELDS:
            return CSVHandler.parse_display_fields(value)

        raise ValueError(f"Unknown field type: {field_type}")

    # =============================================================================
    # Load Method
    # =============================================================================

    @staticmethod
    def load_csv(filename: str, interface: Any,
                 viz_config: Optional[VizConfig] = None) -> CSVLoadResult:
        """Load configuration from CSV file.

        Args:
            filename: Path to CSV file to load
            interface: Interface object to populate with configuration
            viz_config: Optional VizConfig to populate with visualization settings.
                       If None, a new VizConfig is created for the load and returned
                       as `result.viz_config`.

        Returns:
            CSVLoadResult with success status, viz_config, missing_fields,
            unrecognized fields and per-row warnings
        """
        logger = logging.getLogger('swi3s_visualizer.io')
        result = CSVLoadResult(success=False)

        # Create VizConfig if not provided
        if viz_config is None:
            viz_config = VizConfig()
        result.viz_config = viz_config

        # Reset interface device assignments to defaults before loading
        # This ensures stale MANAGER assignments don't persist across loads
        for dp_index in range(len(interface.data_ports)):
            interface.set_dp_device(dp_index, 0)  # Default to device 0

        # Reset interface parameters to defaults before loading
        # This ensures stale values don't persist across loads
        interface.reset_to_defaults()

        # Reset data port configs to defaults before loading
        # This ensures stale parameter values don't persist across loads
        from ..models.dataport import DataPortConfig
        for data_port in interface.data_ports:
            data_port.config = DataPortConfig()

        # Reset viz_config to defaults before loading
        viz_config.rows_to_draw = 64  # Default rows
        for dp_index in range(len(viz_config.data_ports)):
            viz_config.data_ports[dp_index] = DataPortVizConfig(name=f"DP{dp_index}")

        # Track which expected fields were found
        found_interface_fields: set = set()
        found_interface_viz_fields: set = set()
        found_dataport_fields: set = set()
        found_dataport_viz_fields: set = set()
        found_cds_guard_per_source = False
        found_cds_int_rows: set = set()      # which of _CDS_INT_PER_SOURCE were present
        cds_guard_enable_ps = None      # split guard rows, recombined after the loop
        cds_guard_polarity_ps = None
        legacy_cds_drive = None         # the scalar spelling; broadcast if seen
        # A negative legacy scalar CDS tail width or drive type (key -> (line, value)) is
        # not applied; the last row of its key decides, and it is reported after the loop
        # unless the per-source row is present.
        negative_scalar: Dict[str, Tuple[int, int]] = {}
        tail_default = interface.CDS_TailWidth_REG
        device_rows: Dict[str, List[Any]] = {}   # device-assignment rows, applied after the loop

        # The row being read, so an error raised while parsing or storing one of its cells
        # names the line, the field and (on a per-port row) the data port.
        where: Dict[str, Any] = {'line': None, 'field': '', 'port': None}

        try:
            with open(filename, encoding='utf-8-sig', newline='') as data_file:
                csv_data = csv.reader(data_file)
                row_count = 0
                first_row_of: Dict[str, int] = {}   # recognised field name -> first line
                known_fields = CSVHandler._known_field_names()

                def parse_viz(raw: str, field_type: FieldType, line: int, name: str,
                              strays_by_port: Optional[Dict[str, List[int]]] = None,
                              port: int = 0) -> Any:
                    """Parse a viz cell. A DisplayFields character outside s, c, b is
                    reported at once, or recorded under `strays_by_port` when the caller
                    reports one message for the whole row."""
                    if field_type == FieldType.DISPLAY_FIELDS:
                        strays = CSVHandler.display_field_strays(raw)
                        if strays and strays_by_port is not None:
                            strays_by_port.setdefault(strays, []).append(port)
                        elif strays:
                            result.warnings.append(
                                f"Line {line} ({name}): '{strays}' is not one of s, c, b "
                                f"and was ignored.")
                    return CSVHandler.parse_value(raw, field_type)

                def report_strays(strays_by_port: Dict[str, List[int]], line: int,
                                  name: str) -> None:
                    if not strays_by_port:
                        return
                    parts = "; ".join(
                        f"'{strays}' (data port{'s' if len(ports) != 1 else ''} "
                        f"{', '.join(map(str, ports))})"
                        for strays, ports in strays_by_port.items())
                    result.warnings.append(
                        f"Line {line} ({name}): characters other than s, c, b were "
                        f"ignored: {parts}.")

                def need_value(row: List[str], line: int, name: str) -> bool:
                    if len(row) >= 2:
                        return True
                    result.error_message = f"Line {line} ({name}): missing value"
                    return False

                last_line = 0
                for count, row in enumerate(csv_data):
                    row_count = count
                    # The physical line the record starts on; a quoted cell may span lines.
                    line_no = last_line + 1
                    last_line = csv_data.line_num
                    if not row or not row[0]:  # Skip empty rows
                        continue

                    field_name = row[0]
                    where.update(line=line_no, field=field_name, port=None)

                    # Skip legacy encoding flag if present in old files
                    if field_name == CSVFields.SAVE_FILE_USING_EXCESS_ONE:
                        continue

                    # AppVersion is special: captured into the result so callers
                    # can warn on legacy files or format mismatches.
                    if field_name == 'AppVersion':
                        result.file_app_version = row[1] if len(row) > 1 else ''
                        continue

                    if field_name in known_fields:
                        if field_name in first_row_of:
                            result.warnings.append(
                                f"Line {line_no} ({field_name}) repeats line "
                                f"{first_row_of[field_name]}; the later row is used.")
                        else:
                            first_row_of[field_name] = line_no

                    # A negative legacy scalar tail width is ignored, not refused.
                    if field_name == 'CDS_TailWidth_REG':
                        if not need_value(row, line_no, field_name):
                            return result
                        found_interface_fields.add(field_name)
                        tail_value = CSVHandler.parse_value(row[1], FieldType.INT)
                        if tail_value < 0:
                            negative_scalar[field_name] = (line_no, tail_value)
                            interface.CDS_TailWidth_REG = tail_default
                        else:
                            negative_scalar.pop(field_name, None)
                            tail_max = InterfaceRanges.MAX_CDS_TAIL_WIDTH
                            if tail_value > tail_max:
                                result.warnings.append(
                                    f"Line {line_no} (CDS_TailWidth_REG): {tail_value} does "
                                    f"not fit its {tail_max.bit_length()}-bit register field "
                                    f"({InterfaceRanges.MIN_CDS_TAIL_WIDTH}..{tail_max}); the "
                                    f"low {tail_max.bit_length()} bits are carried.")
                                tail_value &= tail_max
                            interface.CDS_TailWidth_REG = tail_value
                        continue

                    # Try interface parameter (exact match)
                    if field_name in CSVHandler._interface_field_dict:
                        if not need_value(row, line_no, field_name):
                            return result
                        found_interface_fields.add(field_name)
                        attr_name, field_type = CSVHandler._interface_field_dict[field_name]
                        value = CSVHandler.parse_value(row[1], field_type)
                        setattr(interface, attr_name, value)
                        continue

                    # Per-source CDS guard/tail rows are 13-wide (Manager +
                    # Device 0..11), not the 12-wide per-dataport shape, so
                    # they're handled specially rather than via the field maps.
                    # Guard arrives split into enable + polarity rows (mirroring the
                    # CDS_GuardEnabled/Polarity_REG scalars); they're recombined into
                    # CDS_Guard_PerSource (0=off, 1=G0, 2=G1) after the loop.
                    if field_name in (CDS_GUARD_ENABLE_PER_SOURCE,
                                      CDS_GUARD_POLARITY_PER_SOURCE,
                                      *_CDS_INT_PER_SOURCE):
                        values = row[1:]
                        if len(values) != CDS_NUM_SOURCES:
                            result.error_message = (
                                f"Line {line_no} ({field_name}): Expected {CDS_NUM_SOURCES} "
                                f"per-source values, found {len(values)}"
                            )
                            return result
                        if field_name in _CDS_INT_PER_SOURCE:
                            attr, default, lo, hi = _CDS_INT_PER_SOURCE[field_name]
                            target = getattr(interface, attr)
                            carried: List[str] = []
                            reserved: List[str] = []
                            for src_index, raw_value in enumerate(values):
                                # A blank cell takes the FIELD's default, not 0: on
                                # CDS_DriveType 0 means Special, so reading a gap as zero
                                # would silently relabel the bus.
                                cell = (CSVHandler.parse_value(raw_value, FieldType.INT)
                                        if str(raw_value).strip() else default)
                                if not lo <= cell <= hi:
                                    # A register field holds only its width: the low bits
                                    # are carried and the cell is reported.
                                    source = ("Manager" if src_index == 0
                                              else f"Device {src_index - 1}")
                                    carried.append(f"{source} = {cell}")
                                    cell &= hi
                                elif (field_name == CDS_TAIL_PER_SOURCE
                                      and cell == InterfaceRanges.RESERVED_CDS_TAIL_WIDTH):
                                    reserved.append(
                                        "Manager" if src_index == 0 else f"Device {src_index - 1}")
                                target[src_index] = cell
                            if reserved:
                                result.warnings.append(
                                    f"Line {line_no} ({field_name}): "
                                    f"{_reserved_tail_text(reserved)}")
                            if carried:
                                result.warnings.append(
                                    f"Line {line_no} ({field_name}): does not fit its "
                                    f"{hi.bit_length()}-bit register field ({lo}..{hi}) for "
                                    f"{'; '.join(carried)}; "
                                    + ("the low bit is carried." if hi == 1
                                       else f"the low {hi.bit_length()} bits are carried."))
                            found_cds_int_rows.add(field_name)
                        elif field_name == CDS_GUARD_ENABLE_PER_SOURCE:
                            cds_guard_enable_ps = [CSVHandler.parse_value(v, FieldType.BOOL)
                                                   for v in values]
                            found_cds_guard_per_source = True
                        else:  # CDS_GUARD_POLARITY_PER_SOURCE
                            cds_guard_polarity_ps = [CSVHandler.parse_value(v, FieldType.BOOL)
                                                     for v in values]
                        continue

                    # A SCALAR CDS_DriveType row broadcasts to every source, so such a
                    # file loads with a sensible value instead of landing in
                    # unrecognized_fields.
                    if field_name == _CDS_DRIVE_TYPE_SCALAR:
                        legacy_cds_drive = CSVHandler.parse_value(
                            row[1] if len(row) > 1 else "1", FieldType.INT)
                        if legacy_cds_drive < 0:
                            negative_scalar[field_name] = (line_no, legacy_cds_drive)
                            legacy_cds_drive = None
                            continue
                        negative_scalar.pop(field_name, None)
                        if legacy_cds_drive not in (0, 1):
                            result.warnings.append(
                                f"Line {line_no} ({_CDS_DRIVE_TYPE_SCALAR}): "
                                f"{legacy_cds_drive} does not fit its 1-bit register field "
                                f"(0..1); the low bit is carried.")
                            legacy_cds_drive &= 1
                        continue

                    # Try interface viz parameter (exact match)
                    if field_name in CSVHandler._interface_viz_field_dict:
                        if not need_value(row, line_no, field_name):
                            return result
                        found_interface_viz_fields.add(field_name)
                        attr_name, field_type = CSVHandler._interface_viz_field_dict[field_name]
                        value = parse_viz(row[1], field_type, line_no, field_name)
                        setattr(viz_config, attr_name, value)
                        continue

                    # Try data port parameter (exact match)
                    if field_name in CSVHandler._dataport_field_dict:
                        found_dataport_fields.add(field_name)
                        attr_name, field_type, _ = CSVHandler._dataport_field_dict[field_name]

                        # Validate row has correct number of data port values
                        num_data_ports = len(interface.data_ports)
                        if len(row) - 1 != num_data_ports:
                            result.error_message = (
                                f"Line {line_no} ({field_name}): Expected "
                                f"{num_data_ports} data port values, found {len(row) - 1}"
                            )
                            return result

                        # Set value for each data port
                        for dp_index, data_port in enumerate(interface.data_ports):
                            where['port'] = dp_index
                            value = CSVHandler.parse_value(
                                row[dp_index + 1], field_type
                            )
                            target_config = (
                                interface.get_fcp(dp_index).config
                                if field_name in CSVHandler._FCP_CSV_FIELDS
                                else data_port.config
                            )
                            setattr(target_config, attr_name, value)
                        continue

                    # Try data port viz parameter (exact match)
                    if field_name in CSVHandler._dataport_viz_field_dict:
                        found_dataport_viz_fields.add(field_name)
                        attr_name, field_type = CSVHandler._dataport_viz_field_dict[field_name]

                        # Validate row has correct number of data port values
                        num_data_ports = len(interface.data_ports)
                        if len(row) - 1 != num_data_ports:
                            result.error_message = (
                                f"Line {line_no} ({field_name}): Expected "
                                f"{num_data_ports} data port values, found {len(row) - 1}"
                            )
                            return result

                        # Set value for each data port viz config
                        strays_by_port: Dict[str, List[int]] = {}
                        for dp_index in range(num_data_ports):
                            where['port'] = dp_index
                            value = parse_viz(row[dp_index + 1], field_type,
                                              line_no, field_name, strays_by_port, dp_index)
                            setattr(viz_config.data_ports[dp_index], attr_name, value)
                        report_strays(strays_by_port, line_no, field_name)
                        continue

                    # Try device assignment fields (stored in interface, not dataport)
                    if field_name in CSVHandler.DEVICE_ASSIGNMENT_FIELDS:
                        found_dataport_fields.add(field_name)  # Track as found
                        field_type = CSVHandler.DEVICE_ASSIGNMENT_FIELDS[field_name]

                        # Validate row has correct number of data port values
                        num_data_ports = len(interface.data_ports)
                        if len(row) - 1 != num_data_ports:
                            result.error_message = (
                                f"Line {line_no} ({field_name}): Expected "
                                f"{num_data_ports} data port values, found {len(row) - 1}"
                            )
                            return result

                        # The two fields are separate rows, so both are held (a repeat
                        # replaces its earlier row) and applied once the loop ends.
                        parsed_cells: List[Any] = []
                        for dp_index in range(num_data_ports):
                            where['port'] = dp_index
                            parsed_cells.append(
                                CSVHandler.parse_value(row[dp_index + 1], field_type))
                        device_rows[field_name] = parsed_cells
                        continue

                    # Field was not recognized
                    result.unrecognized_fields.append((line_no, field_name))

                where['line'] = None

                # Device assignment is order-independent: a port flagged ManagerDataport
                # is the Manager's whatever its DeviceNumber, any other port takes its
                # DeviceNumber (device 0 when the row is absent).
                from ..config.constants import SpecialDevices
                numbers = device_rows.get(DATA_PORT_DEVICE_NUMBER)
                in_manager = device_rows.get(DATA_PORT_IN_MANAGER)
                for dp_index in range(len(interface.data_ports)):
                    if in_manager and in_manager[dp_index]:
                        interface.set_dp_device(dp_index, SpecialDevices.MANAGER)
                    elif numbers:
                        interface.set_dp_device(dp_index, numbers[dp_index])

                # Recombine the split per-source guard rows into CDS_Guard_PerSource
                # (0=off, 1=G0, 2=G1). A missing polarity row defaults enabled entries
                # to G0.
                if cds_guard_enable_ps is not None:
                    pol = cds_guard_polarity_ps or [False] * CDS_NUM_SOURCES
                    interface.CDS_Guard_PerSource = [
                        (2 if pol[i] else 1) if cds_guard_enable_ps[i] else 0
                        for i in range(CDS_NUM_SOURCES)
                    ]

                # Legacy files carry only the scalar CDS_GuardEnabled_REG /
                # CDS_GuardPolarity_REG / CDS_TailWidth_REG fields (global,
                # pre-per-source). Every device can use the CDS regardless of its
                # data ports, so the global guard/tail is SHARED by all sources.
                # When the 13-wide per-source rows are absent, derive EVERY entry
                # (Manager + all 12 devices) from those scalars — a uniform config
                # the engine collapses back to one universal symbol.
                if not found_cds_guard_per_source:
                    g = (2 if interface.CDS_GuardPolarity_REG else 1) \
                        if interface.CDS_GuardEnabled_REG else 0
                    interface.CDS_Guard_PerSource = [g] * 13
                if CDS_TAIL_PER_SOURCE not in found_cds_int_rows:
                    interface.CDS_Tail_PerSource = [interface.CDS_TailWidth_REG] * 13
                    if (interface.CDS_TailWidth_REG == InterfaceRanges.RESERVED_CDS_TAIL_WIDTH
                            and 'CDS_TailWidth_REG' in first_row_of):
                        result.warnings.append(
                            f"Line {first_row_of['CDS_TailWidth_REG']} (CDS_TailWidth_REG): "
                            + _reserved_tail_text(["Manager"] + [f"Device {d}" for d in range(12)]))
                for scalar, per_source in (('CDS_TailWidth_REG', CDS_TAIL_PER_SOURCE),
                                           (_CDS_DRIVE_TYPE_SCALAR, CDS_DRIVE_TYPE_PER_SOURCE)):
                    if scalar in negative_scalar and per_source not in found_cds_int_rows:
                        neg_line, neg_value = negative_scalar[scalar]
                        result.warnings.append(
                            f"Line {neg_line} ({scalar}): {neg_value} is negative and is "
                            f"ignored; the CDS sources keep their defaults.")
                # No per-source drive row: broadcast the scalar if the file carried one,
                # else leave reset_to_defaults' all-Normal in place. End-drive-early has no
                # scalar spelling.
                if (CDS_DRIVE_TYPE_PER_SOURCE not in found_cds_int_rows
                        and legacy_cds_drive is not None):
                    interface.CDS_DriveType_PerSource = [int(legacy_cds_drive)] * 13

                # The per-source rows are authoritative; derive the scalar CDS
                # registers FROM them for the engine layout (guard column presence /
                # tail-column count), since current files do not carry the scalars.
                interface.CDS_GuardEnabled_REG = any(v != 0 for v in interface.CDS_Guard_PerSource)
                interface.CDS_GuardPolarity_REG = (interface.CDS_Guard_PerSource[0] == 2)
                interface.CDS_TailWidth_REG = max(interface.CDS_Tail_PerSource)
                # The Manager's, like CDS_GuardPolarity_REG above, not an any()/max();
                # the renderer reads the whole list.
                interface.CDS_DriveType_REG = int(interface.CDS_DriveType_PerSource[0])
                interface.CDS_EndDriveEarly_REG = int(
                    interface.CDS_EndDriveEarly_PerSource[0])

                # Check for missing expected fields
                expected_interface = set(CSVHandler._interface_field_dict.keys())
                # A scalar CDS register is derived above from the per-source rows when
                # the file carries them, so its absence defaults nothing.
                if found_cds_guard_per_source:
                    expected_interface -= {'CDS_GuardEnabled_REG', 'CDS_GuardPolarity_REG'}
                if CDS_TAIL_PER_SOURCE in found_cds_int_rows:
                    expected_interface.discard('CDS_TailWidth_REG')
                expected_interface_viz = set(CSVHandler._interface_viz_field_dict.keys())
                expected_dataport = set(CSVHandler._dataport_field_dict.keys())
                expected_dataport_viz = set(CSVHandler._dataport_viz_field_dict.keys())
                # Include device assignment fields in expected dataport fields
                expected_dataport.update(CSVHandler.DEVICE_ASSIGNMENT_FIELDS.keys())

                missing_interface = expected_interface - found_interface_fields
                missing_interface_viz = expected_interface_viz - found_interface_viz_fields
                missing_dataport = expected_dataport - found_dataport_fields
                missing_dataport_viz = expected_dataport_viz - found_dataport_viz_fields

                # Report missing fields (sorted for consistent ordering)
                for field_name in sorted(missing_interface):
                    result.missing_fields.append(f"Interface: {field_name}")
                    logger.warning(f"CSV missing interface field: {field_name} (using default)")

                for field_name in sorted(missing_interface_viz):
                    result.missing_fields.append(f"InterfaceViz: {field_name}")
                    logger.warning(f"CSV missing interface viz field: {field_name} (using default)")

                # DP/FCP config fields have no defaults — missing any is a fatal error
                dp_config_fields = {entry[0] for entry in CSVHandler.DATAPORT_FIELD_MAP
                                    if entry[0] not in CSVHandler._FCP_CSV_FIELDS}
                fcp_config_fields = set(CSVHandler._FCP_CSV_FIELDS)
                missing_dp_required = sorted(missing_dataport & dp_config_fields)
                missing_fcp_required = sorted(missing_dataport & fcp_config_fields)
                missing_dp_other = sorted(missing_dataport - dp_config_fields - fcp_config_fields)

                for field_name in missing_dp_required:
                    result.missing_fields.append(f"DataPort: {field_name}")
                for field_name in missing_fcp_required:
                    result.missing_fields.append(f"FlowControlPort: {field_name}")
                for field_name in missing_dp_other:
                    result.missing_fields.append(f"DataPort: {field_name}")
                    logger.warning(f"CSV missing data port field: {field_name} (using default)")

                for field_name in sorted(missing_dataport_viz):
                    result.missing_fields.append(f"DataPortViz: {field_name}")
                    logger.warning(f"CSV missing data port viz field: {field_name} (using default)")

                if missing_dp_required or missing_fcp_required:
                    parts = ["CSV missing required field(s):"]
                    if missing_dp_required:
                        parts.append(f"  DataPort: {', '.join(missing_dp_required)}")
                    if missing_fcp_required:
                        parts.append(f"  FlowControlPort: {', '.join(missing_fcp_required)}")
                    result.error_message = "\n".join(parts)
                    logger.error(result.error_message)
                    return result

                # Reset all data ports to clear stale runtime state
                for data_port in interface.data_ports:
                    data_port.initialize()

                # The loaded RowsToDraw is reported as the result's rows_in_frame
                result.rows_in_frame = viz_config.rows_to_draw

                result.success = True
                logger.info(f'CSV file loaded successfully: {filename}')
                logger.debug(f'CSV loaded: {row_count + 1} rows, '
                           f'{len(result.unrecognized_fields)} unrecognized fields, '
                           f'{len(result.missing_fields)} missing fields')

        except Exception as e:
            logger.error(f'Failed to load CSV file: {filename}', exc_info=True)
            # A csv.Error or a UnicodeDecodeError is raised while the reader fetches the
            # next buffered chunk, so `where` names an earlier row than the fault: both are
            # reported without a line.
            if where['line'] is None or isinstance(e, (csv.Error, UnicodeDecodeError)):
                result.error_message = str(e)
            else:
                port = '' if where['port'] is None else f"data port {where['port']}: "
                result.error_message = f"Line {where['line']} ({where['field']}): {port}{e}"

        return result

    # =============================================================================
    # Save Method
    # =============================================================================

    @staticmethod
    def save_csv(filename: str, interface: Any, viz_config: VizConfig) -> None:
        """Save configuration to CSV file.

        Args:
            filename: Path to CSV file to create
            interface: Interface object containing configuration
            viz_config: VizConfig object containing visualization settings

        Raises:
            OSError: If file cannot be written
        """
        from ..config.constants import SpecialDevices
        from ..version import app_version
        logger = logging.getLogger('swi3s_visualizer.io')
        try:
            # The text is built whole and moved onto the target once complete, so a failed
            # write leaves an existing file as it was.
            with io.StringIO(newline='') as outfile:
                writer = csv.writer(outfile, delimiter=',', lineterminator='\n')

                # Stamp the LIVE Studio version (not a hardcoded literal) so saved
                # files always reflect the running app.
                writer.writerow(['AppVersion', app_version()])

                # Write interface parameters using field map. The scalar CDS guard/
                # tail registers are DERIVED from the per-source rows on load and are
                # not written; the CDS_*PerSource rows below are the single source of
                # truth.
                for csv_field, attr_name, field_type in CSVHandler.INTERFACE_FIELD_MAP:
                    if csv_field in _CDS_SCALAR_FIELDS:
                        continue
                    value = getattr(interface, attr_name)
                    writer.writerow([csv_field, str(value)])

                # Write interface viz parameters
                for csv_field, attr_name, field_type in CSVHandler.INTERFACE_VIZ_FIELD_MAP:
                    value = getattr(viz_config, attr_name)
                    writer.writerow([csv_field, str(value)])

                # Per-source CDS rows (Manager + 12 devices): these 13-wide rows carry
                # the authoritative per-source values, so a converted v2 file shows
                # every source's CDS setting explicitly. Guard is split into enable +
                # polarity rows mirroring the scalar registers (CDS_Guard_PerSource
                # packs both: 0=off, 1=G0, 2=G1).
                writer.writerow([CDS_GUARD_ENABLE_PER_SOURCE]
                                + ['1' if v != 0 else '0' for v in interface.CDS_Guard_PerSource])
                writer.writerow([CDS_GUARD_POLARITY_PER_SOURCE]
                                + ['1' if v == 2 else '0' for v in interface.CDS_Guard_PerSource])
                for csv_field, (attr, *_rest) in _CDS_INT_PER_SOURCE.items():
                    writer.writerow([csv_field]
                                    + [str(int(v)) for v in getattr(interface, attr)])

                # Write device assignment fields (from interface, not dataport)
                # DeviceNumber_REG
                device_values = []
                for dp_index in range(len(interface.data_ports)):
                    device = interface.get_dp_device(dp_index)
                    # If in manager, device number doesn't matter for CSV, write 0
                    if device == SpecialDevices.MANAGER:
                        device_values.append('0')
                    else:
                        device_values.append(str(device))
                writer.writerow([DATA_PORT_DEVICE_NUMBER] + device_values)

                # InManager
                manager_values = []
                for dp_index in range(len(interface.data_ports)):
                    is_manager = interface.is_dp_in_manager(dp_index)
                    manager_values.append(str(is_manager))
                writer.writerow([DATA_PORT_IN_MANAGER] + manager_values)

                # Write data port parameters using field mapping.
                # C15: when FlowMode is Normal (0) or Tx-Controlled (1) the FCP is
                # not in use, so emit 0/False for all six FCP_* fields regardless
                # of any stale config value. This prevents stale FCP register
                # values from round-tripping through a save and later tripping
                # the C15 validator.
                for csv_field, attr_name, field_type, _ in CSVHandler.DATAPORT_FIELD_MAP:
                    values = []
                    is_fcp_field = csv_field in CSVHandler._FCP_CSV_FIELDS
                    for dp_index, data_port in enumerate(interface.data_ports):
                        if is_fcp_field and data_port.config.FlowMode_REG in (0, 1):
                            # FCP disabled for this DP: force zero/False on the wire.
                            value: Any = False if field_type == FieldType.BOOL else 0
                        else:
                            source_config = (
                                interface.flow_control_ports[dp_index].config
                                if is_fcp_field
                                else data_port.config
                            )
                            value = getattr(source_config, attr_name)
                        # Format based on field type
                        if field_type == FieldType.BINARY_INT:
                            values.append(bin(value))
                        else:
                            values.append(str(value))
                    writer.writerow([csv_field] + values)

                # Write data port viz parameters
                for csv_field, attr_name, field_type in CSVHandler.DATAPORT_VIZ_FIELD_MAP:
                    values = []
                    for dp_index in range(len(viz_config.data_ports)):
                        value = getattr(viz_config.data_ports[dp_index], attr_name)
                        # Format based on field type
                        if field_type == FieldType.DISPLAY_FIELDS:
                            values.append(CSVHandler.display_fields_to_str(value))
                        elif csv_field == DATA_PORT_NUMBER and value < 0:
                            # Resolve the unset sentinel to the column index so the
                            # analyzer/converter always read a concrete DP number.
                            values.append(str(dp_index))
                        else:
                            values.append(str(value))
                    writer.writerow([csv_field] + values)

                _write_text_whole(filename, outfile.getvalue(), newline='')

            logger.info(f'Successfully saved CSV file: {filename}')
        except (OSError, IOError) as e:
            logger.error(f'Failed to write CSV file {filename}: {e}')
            raise

