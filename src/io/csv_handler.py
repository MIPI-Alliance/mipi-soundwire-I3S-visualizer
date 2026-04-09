"""
Copyright (c) 2023 MIPI Alliance and other contributors. All Rights Reserved.

CSV file handler for loading and saving SoundWire I3S configurations.
"""

import csv
import io
import logging
from typing import List, Tuple, Any, Optional
from dataclasses import dataclass, field
from enum import Enum, auto

from src.config.constants import CSVFields
from src.models.enums import DisplayField
from src.viz import VizConfig, DataPortVizConfig


# =============================================================================
# Field Type Definitions
# =============================================================================

class FieldType(Enum):
    """Types of CSV field values."""
    INT = auto()       # Integer value
    FLOAT = auto()     # Float value
    BOOL = auto()      # Boolean value (True/False or 0/1)
    STRING = auto()    # String value
    BINARY_INT = auto() # Integer in binary format (0b prefix)
    DISPLAY_FIELDS = auto()  # DisplayField flags (s=sample, c=channel, b=bit)


# =============================================================================
# CSV Field Constants (aliases for readability)
# =============================================================================

# Data port field names
DATA_PORT_NAME = CSVFields.DP_NAME
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
        (DATA_PORT_FCP_H_START, 'FCP_HorizontalStart_REG', FieldType.INT, False),
        (DATA_PORT_FCP_BIT_WIDTH, 'FCP_BitWidth_REG', FieldType.INT, False),
        (DATA_PORT_FCP_TAIL_WIDTH, 'FCP_TailWidth_REG', FieldType.INT, False),
        (DATA_PORT_FCP_OFFSET, 'FCP_Offset_REG', FieldType.INT, False),
        (DATA_PORT_FCP_GUARD_ENABLE, 'FCP_GuardEnable_REG', FieldType.BOOL, False),
        (DATA_PORT_FCP_GUARD_POLARITY, 'FCP_GuardPolarity_REG', FieldType.BOOL, False),
    ]

    # Data port visualization fields (go to VizConfig.data_ports, not DataPortConfig)
    # Format: (csv_field_name, viz_attribute_name, field_type)
    DATAPORT_VIZ_FIELD_MAP = [
        (DATA_PORT_NAME, 'name', FieldType.STRING),
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

    # =============================================================================
    # Value Parsing Methods
    # =============================================================================

    @staticmethod
    def parse_int_value(value: str) -> int:
        """Parse an integer value that may be in decimal or binary (0b) format.

        Args:
            value: String value to parse (e.g., "123" or "0b1111011")

        Returns:
            Integer value

        Raises:
            ValueError: If value is empty or cannot be parsed as integer
        """
        value = value.strip()
        if not value:
            raise ValueError("Cannot parse integer from empty string")
        try:
            if value.startswith('0b') or value.startswith('0B'):
                return int(value, 2)
            return int(value)
        except ValueError:
            raise ValueError(f"Cannot parse integer from '{value}'")

    @staticmethod
    def parse_float_value(value: str) -> float:
        """Parse a float value from string.

        Args:
            value: String value to parse (e.g., "123.45" or "3072")

        Returns:
            Float value

        Raises:
            ValueError: If value is empty or cannot be parsed as float
        """
        value = value.strip()
        if not value:
            raise ValueError("Cannot parse float from empty string")
        try:
            return float(value)
        except ValueError:
            raise ValueError(f"Cannot parse float from '{value}'")

    @staticmethod
    def parse_bool_value(value: str) -> bool:
        """Parse a boolean value from string.

        Accepts: True/False, true/false, 1/0

        Args:
            value: String value to parse

        Returns:
            Boolean value
        """
        value = value.strip().lower()
        if value in ('true', '1'):
            return True
        elif value in ('false', '0'):
            return False
        else:
            raise ValueError(f"Cannot parse boolean from '{value}'")

    @staticmethod
    def parse_display_fields(value: str) -> DisplayField:
        """Parse DisplayField flags from string.

        Accepts: 's', 'c', 'b', 'sc', 'scb', etc.

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
                       If None, a new VizConfig is created and returned via result.

        Returns:
            CSVLoadResult with success status, viz_config, missing_fields,
            and any unrecognized fields
        """
        logger = logging.getLogger('swi3s_visualizer.io')
        result = CSVLoadResult(success=False)

        # Create VizConfig if not provided
        if viz_config is None:
            viz_config = VizConfig()

        # Reset interface device assignments to defaults before loading
        # This ensures stale MANAGER assignments don't persist across loads
        for dp_index in range(len(interface.data_ports)):
            interface.set_dp_device(dp_index, 0)  # Default to device 0

        # Reset interface parameters to defaults before loading
        # This ensures stale values don't persist across loads
        from src.models.interface import Interface as InterfaceClass
        interface.NumColumns_REG = 15  # Default: 16 columns
        interface.phy3_enabled = False
        interface.s0_width = InterfaceClass.MIN_S0_WIDTH
        interface.s1_width = InterfaceClass.MIN_S1_WIDTH
        interface.cds_handover_enabled = True
        interface.s1_handover_enabled = True
        interface.CDS_GuardEnabled_REG = False
        interface.CDS_GuardPolarity_REG = False
        interface.CDS_BitWidth_REG = InterfaceClass.MIN_CDS_WIDTH
        interface.CDS_TailWidth_REG = InterfaceClass.MIN_CDS_TAIL_WIDTH
        interface.tail_width = InterfaceClass.MIN_TAIL_WIDTH
        interface.SkippingDenominator_REG = 1
        interface.row_rate = 3072.0
        interface.description = ''

        # Reset data port configs to defaults before loading
        # This ensures stale parameter values don't persist across loads
        from src.models.dataport import DataPortConfig
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

        try:
            with open(filename, encoding='utf8') as data_file:
                csv_data = csv.reader(data_file)
                row_count = 0

                for count, row in enumerate(csv_data):
                    row_count = count
                    if not row or not row[0]:  # Skip empty rows
                        continue

                    field_name = row[0]

                    # Skip legacy encoding flag if present in old files
                    if field_name == CSVFields.SAVE_FILE_USING_EXCESS_ONE:
                        continue

                    # Try interface parameter (exact match)
                    if field_name in CSVHandler._interface_field_dict:
                        found_interface_fields.add(field_name)
                        attr_name, field_type = CSVHandler._interface_field_dict[field_name]
                        value = CSVHandler.parse_value(row[1], field_type)
                        setattr(interface, attr_name, value)
                        continue

                    # Try interface viz parameter (exact match)
                    if field_name in CSVHandler._interface_viz_field_dict:
                        found_interface_viz_fields.add(field_name)
                        attr_name, field_type = CSVHandler._interface_viz_field_dict[field_name]
                        value = CSVHandler.parse_value(row[1], field_type)
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
                                f"Row {count + 1} ({field_name}): Expected {num_data_ports} data port values, "
                                f"found {len(row) - 1}"
                            )
                            return result

                        # Set value for each data port
                        for dp_index, data_port in enumerate(interface.data_ports):
                            value = CSVHandler.parse_value(
                                row[dp_index + 1], field_type
                            )
                            setattr(data_port.config, attr_name, value)
                        continue

                    # Try data port viz parameter (exact match)
                    if field_name in CSVHandler._dataport_viz_field_dict:
                        found_dataport_viz_fields.add(field_name)
                        attr_name, field_type = CSVHandler._dataport_viz_field_dict[field_name]

                        # Validate row has correct number of data port values
                        num_data_ports = len(interface.data_ports)
                        if len(row) - 1 != num_data_ports:
                            result.error_message = (
                                f"Row {count + 1} ({field_name}): Expected {num_data_ports} data port values, "
                                f"found {len(row) - 1}"
                            )
                            return result

                        # Set value for each data port viz config
                        for dp_index in range(num_data_ports):
                            value = CSVHandler.parse_value(
                                row[dp_index + 1], field_type
                            )
                            setattr(viz_config.data_ports[dp_index], attr_name, value)
                        continue

                    # Try device assignment fields (stored in interface, not dataport)
                    if field_name in CSVHandler.DEVICE_ASSIGNMENT_FIELDS:
                        found_dataport_fields.add(field_name)  # Track as found
                        field_type = CSVHandler.DEVICE_ASSIGNMENT_FIELDS[field_name]

                        # Validate row has correct number of data port values
                        num_data_ports = len(interface.data_ports)
                        if len(row) - 1 != num_data_ports:
                            result.error_message = (
                                f"Row {count + 1} ({field_name}): Expected {num_data_ports} data port values, "
                                f"found {len(row) - 1}"
                            )
                            return result

                        # Handle device assignment fields
                        for dp_index in range(num_data_ports):
                            value = CSVHandler.parse_value(row[dp_index + 1], field_type)
                            if field_name == DATA_PORT_DEVICE_NUMBER:
                                # Only set if not already set to MANAGER by InManager field
                                if not interface.is_dp_in_manager(dp_index):
                                    interface.set_dp_device(dp_index, value)
                            elif field_name == DATA_PORT_IN_MANAGER:
                                # InManager=True means device is MANAGER
                                if value:
                                    from src.config.constants import SpecialDevices
                                    interface.set_dp_device(dp_index, SpecialDevices.MANAGER)
                        continue

                    # Field was not recognized
                    result.unrecognized_fields.append((count + 1, field_name))

                # Check for missing expected fields
                expected_interface = set(CSVHandler._interface_field_dict.keys())
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
                for field in sorted(missing_interface):
                    result.missing_fields.append(f"Interface: {field}")
                    logger.warning(f"CSV missing interface field: {field} (using default)")

                for field in sorted(missing_interface_viz):
                    result.missing_fields.append(f"InterfaceViz: {field}")
                    logger.warning(f"CSV missing interface viz field: {field} (using default)")

                for field in sorted(missing_dataport):
                    result.missing_fields.append(f"DataPort: {field}")
                    logger.warning(f"CSV missing data port field: {field} (using default)")

                for field in sorted(missing_dataport_viz):
                    result.missing_fields.append(f"DataPortViz: {field}")
                    logger.warning(f"CSV missing data port viz field: {field} (using default)")

                # Reset all data ports to clear stale runtime state
                for data_port in interface.data_ports:
                    data_port.reset()

                # Store rows_to_draw from viz_config into result for backward compatibility
                result.rows_in_frame = viz_config.rows_to_draw

                result.success = True
                logger.info(f'CSV file loaded successfully: {filename}')
                logger.debug(f'CSV loaded: {row_count + 1} rows, '
                           f'{len(result.unrecognized_fields)} unrecognized fields, '
                           f'{len(result.missing_fields)} missing fields')

        except Exception as e:
            logger.error(f'Failed to load CSV file: {filename}', exc_info=True)
            result.error_message = str(e)

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
        from src.config.constants import SpecialDevices
        logger = logging.getLogger('swi3s_visualizer.io')
        try:
            with io.open(filename, 'w', encoding='utf8') as outfile:
                writer = csv.writer(outfile, delimiter=',', lineterminator='\n')

                # Write interface parameters using field map
                for csv_field, attr_name, field_type in CSVHandler.INTERFACE_FIELD_MAP:
                    value = getattr(interface, attr_name)
                    writer.writerow([csv_field, str(value)])

                # Write interface viz parameters
                for csv_field, attr_name, field_type in CSVHandler.INTERFACE_VIZ_FIELD_MAP:
                    value = getattr(viz_config, attr_name)
                    writer.writerow([csv_field, str(value)])

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

                # Write data port parameters using field mapping
                for csv_field, attr_name, field_type, _ in CSVHandler.DATAPORT_FIELD_MAP:
                    values = []
                    for data_port in interface.data_ports:
                        value = getattr(data_port.config, attr_name)
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
                        else:
                            values.append(str(value))
                    writer.writerow([csv_field] + values)

            logger.info(f'Successfully saved CSV file: {filename}')
        except (OSError, IOError) as e:
            logger.error(f'Failed to write CSV file {filename}: {e}')
            raise

