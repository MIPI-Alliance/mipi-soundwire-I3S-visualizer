"""
Validation utilities for MIPI SoundWire I3S Visualizer.

This module provides structured validation for interface and data port configurations,
replacing string concatenation error handling with type-safe validation results.

Classes:
    ErrorSeverity: Severity levels for validation errors
    ValidationError: Represents a single validation error
    ValidationResult: Result of a validation operation
    InterfaceValidator: Validates Interface configurations
    DataPortValidator: Validates DataPort configurations
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Any, Dict

# Import will be circular if we import DataPort here, so we'll use TYPE_CHECKING
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.models import DataPort
    from src.models import Interface


# =============================================================================
# Enums and Data Classes
# =============================================================================

class ErrorSeverity(Enum):
    """Severity levels for validation errors."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class ValidationError:
    """Represents a validation error."""
    field: str
    message: str
    severity: ErrorSeverity
    value: Optional[Any] = None
    context: Optional[Dict] = None

    def __str__(self) -> str:
        return f"{self.severity.value.upper()}: {self.field} - {self.message}"


# =============================================================================
# Validation Result Class
# =============================================================================

class ValidationResult:
    """Result of a validation operation."""

    def __init__(self):
        self.errors: List[ValidationError] = []

    @property
    def is_valid(self) -> bool:
        """Check if validation passed (no errors or criticals)."""
        return not any(
            e.severity in (ErrorSeverity.ERROR, ErrorSeverity.CRITICAL)
            for e in self.errors
        )

    @property
    def has_warnings(self) -> bool:
        """Check if there are any warnings."""
        return any(e.severity == ErrorSeverity.WARNING for e in self.errors)

    def add_error(self, field: str, message: str,
                  severity: ErrorSeverity = ErrorSeverity.ERROR,
                  value: Any = None, context: Optional[Dict] = None) -> None:
        """Add a validation error."""
        self.errors.append(ValidationError(field, message, severity, value, context))

    def get_summary(self) -> str:
        """Get a formatted summary of all errors."""
        if not self.errors:
            return "Validation passed"

        lines = []
        for error in self.errors:
            lines.append(str(error))
        return "\n".join(lines)


# =============================================================================
# DataPort Validator Class
# =============================================================================

class DataPortValidator:
    """Validates DataPort configurations."""

    def __init__(self, interface: Any):
        """Initialize validator with interface for relationship checks.

        Args:
            interface: The Interface object containing global parameters
        """
        self.interface = interface

    def validate(self, data_port: 'DataPort', dp_index: int,
                 enable_handover: bool = False) -> ValidationResult:
        """Validate all DataPort parameters.

        Args:
            data_port: The DataPort to validate
            dp_index: Index of the data port (0-11)
            enable_handover: Whether handover visualization is enabled (from VizConfig)

        Returns:
            ValidationResult containing any errors or warnings found
        """
        result = ValidationResult()

        # Import ranges for validation
        from src.config.constants import DataPortRanges, SpecialDevices

        # Access config for all register values
        config = data_port.config

        # Store enable_handover for use in _validate_relationships
        self._enable_handover = enable_handover

        # Validate device number (stored in interface, not data port)
        # Note: Device -1 (MANAGER) is valid for manager data ports
        device_num = self.interface.get_dp_device(dp_index)
        if device_num != SpecialDevices.MANAGER:
            self._validate_range(
                result, 'DeviceNumber', device_num,
                DataPortRanges.MIN_DEVICE_NUMBER, DataPortRanges.MAX_DEVICE_NUMBER
            )

        num_channels = bin(config.EnableCh_REG).count('1')
        self._validate_range(
            result, 'NumChannels', num_channels,
            DataPortRanges.MIN_CHANNELS, DataPortRanges.MAX_CHANNELS
        )

        self._validate_range(
            result, 'ChannelGrouping_REG', config.ChannelGrouping_REG,
            DataPortRanges.MIN_CHANNEL_GROUPING, DataPortRanges.MAX_CHANNEL_GROUPING
        )

        self._validate_range(
            result, 'Spacing_REG', config.Spacing_REG,
            DataPortRanges.MIN_CHANNEL_GROUP_SPACING, DataPortRanges.MAX_CHANNEL_GROUP_SPACING
        )

        self._validate_range(
            result, 'SampleSize_REG', config.SampleSize_REG,
            DataPortRanges.MIN_SAMPLE_SIZE, DataPortRanges.MAX_SAMPLE_SIZE
        )

        self._validate_range(
            result, 'SampleGrouping_REG', config.SampleGrouping_REG,
            DataPortRanges.MIN_SAMPLE_GROUPING, DataPortRanges.MAX_SAMPLE_GROUPING
        )

        self._validate_range(
            result, 'Interval_REG', config.Interval_REG,
            DataPortRanges.MIN_INTERVAL, DataPortRanges.MAX_INTERVAL
        )

        self._validate_range(
            result, 'SkippingNumerator_REG', config.SkippingNumerator_REG,
            DataPortRanges.MIN_SKIPPING_NUMERATOR, DataPortRanges.MAX_SKIPPING_NUMERATOR
        )

        self._validate_range(
            result, 'Offset_REG', config.Offset_REG,
            DataPortRanges.MIN_OFFSET, DataPortRanges.MAX_OFFSET
        )

        self._validate_range(
            result, 'HorizontalStart_REG', config.HorizontalStart_REG,
            0, self.interface.num_columns - 1  # Max column index for current num_columns
        )

        self._validate_range(
            result, 'HorizontalCount_REG', config.HorizontalCount_REG,
            0, self.interface.num_columns - 1  # Max column index for current num_columns
        )

        # Validate relationships between parameters
        self._validate_relationships(result, data_port)

        return result

    def _validate_range(self, result: ValidationResult, field: str,
                       value: int, min_val: int, max_val: int) -> None:
        """Validate that a value is within range.

        Args:
            result: ValidationResult to add errors to
            field: Name of the field being validated
            value: Current value
            min_val: Minimum allowed value
            max_val: Maximum allowed value
        """
        if value < min_val:
            result.add_error(
                field,
                f"{field} ({value}) is below minimum ({min_val})",
                ErrorSeverity.ERROR,
                value
            )
        elif value > max_val:
            result.add_error(
                field,
                f"{field} ({value}) exceeds maximum ({max_val})",
                ErrorSeverity.ERROR,
                value
            )

    def _validate_relationships(self, result: ValidationResult,
                                data_port: 'DataPort') -> None:
        """Validate relationships between parameters.

        Args:
            result: ValidationResult to add errors to
            data_port: DataPort being validated
        """
        # Access config for all register values
        config = data_port.config

        # Compute effective_channel_grouping locally (same logic as DataPortAlgorithm)
        num_channels = bin(config.EnableCh_REG).count('1')
        if config.ChannelGrouping_REG == 0 or config.ChannelGrouping_REG > num_channels:
            effective_channel_grouping = num_channels  # Natural count
        else:
            effective_channel_grouping = config.ChannelGrouping_REG

        # Check offset vs interval FIRST (needed for bits_per_interval calculation)
        if config.Offset_REG > config.Interval_REG:
            result.add_error(
                'Offset',
                f"Offset ({config.Offset_REG}) exceeds Interval ({config.Interval_REG})",
                ErrorSeverity.ERROR
            )
            # Skip further checks if offset is invalid

        # Note: Sample overflow and HorizontalCount overflow are detected by
        # engine's _detect_interval_overflow() and reported as truncation warnings.

        # SRI mode validations
        if config.SubRowInterval_REG:
            if config.Interval_REG != 0:
                result.add_error(
                    'Interval',
                    f"SRI mode implies one-row interval; Interval_REG should be 0 (currently {config.Interval_REG})",
                    ErrorSeverity.WARNING
                )

            if config.SkippingNumerator_REG > 0:
                result.add_error(
                    'SkippingNumerator',
                    f"SRI mode does not support skipping; SkippingNumerator_REG should be 0 (currently {config.SkippingNumerator_REG})",
                    ErrorSeverity.ERROR
                )

            drive_in_group = (
                (config.SampleSize_REG + 1) *
                (config.SampleGrouping_REG + 1) *
                effective_channel_grouping *
                (config.BitWidth_REG + 1)
            )

            if config.Spacing_REG == 0:
                # Spacing_REG == 0 means "end row after one group" (only one group per row)
                # Just need enough bits for one complete group
                if (config.HorizontalCount_REG + 1) < drive_in_group:
                    result.add_error(
                        'HorizontalCount',
                        'Group, or sample, incomplete when HorizontalCount expires.',
                        ErrorSeverity.ERROR
                    )
            else:
                # Multiple groups per row - check if groups fit evenly
                cadence_of_group = drive_in_group + config.Spacing_REG - 1

                if cadence_of_group != 0 and (config.HorizontalCount_REG + 1) % cadence_of_group != 0:
                    if (config.HorizontalCount_REG + 1) % cadence_of_group < drive_in_group:
                        result.add_error(
                            'HorizontalCount',
                            'Group, or sample, incomplete when HorizontalCount expires.',
                            ErrorSeverity.ERROR
                        )

        # Check horizontal_start against NumColumns_REG
        if config.HorizontalStart_REG >= self.interface.num_columns:
            result.add_error(
                'HorizontalStart',
                'HorizontalStart exceeds NumColumns',
                ErrorSeverity.ERROR
            )

        # Check horizontal_count against NumColumns_REG
        if config.HorizontalCount_REG >= self.interface.num_columns:
            result.add_error(
                'HorizontalCount',
                'HorizontalCount exceeds NumColumns',
                ErrorSeverity.ERROR
            )

        # Check HorizontalStart + HorizontalCount overflow
        # In SRI mode this is an error (window must fit in one row)
        # In non-SRI mode this is a warning (wrapping can clash with CDS if not configured correctly)
        if config.HorizontalStart_REG + config.HorizontalCount_REG >= self.interface.num_columns:
            result.add_error(
                'HorizontalCount',
                'HorizontalStart + HorizontalCount exceeds NumColumns',
                ErrorSeverity.WARNING)

        # Check tail width overflow
        # Calculate actual last data column based on grouping pattern, not window boundary
        drive_in_group = (
            (config.SampleSize_REG + 1) *
            (config.SampleGrouping_REG + 1) *
            effective_channel_grouping *
            (config.BitWidth_REG + 1)
        )

        window_size = config.HorizontalCount_REG + 1

        if config.Spacing_REG == 0:
            # One group per row - data ends after drive_in_group bits (or window end)
            data_bits_in_row = min(drive_in_group, window_size)
            last_data_column = config.HorizontalStart_REG + data_bits_in_row - 1
        else:
            # Multiple groups - calculate based on cadence
            cadence = drive_in_group + config.Spacing_REG - 1
            if cadence > 0:
                num_complete_cadences = window_size // cadence
                remaining = window_size % cadence

                if num_complete_cadences == 0:
                    # Partial first group
                    data_bits = min(drive_in_group, window_size)
                    last_data_column = config.HorizontalStart_REG + data_bits - 1
                else:
                    # Position after last complete group's data
                    last_complete_data = config.HorizontalStart_REG + (num_complete_cadences - 1) * cadence + drive_in_group - 1

                    # Check if partial group fits in remaining space
                    if remaining > 0:
                        # After spacing from last cadence, new group starts
                        partial_data = min(drive_in_group, remaining)
                        last_data_column = config.HorizontalStart_REG + num_complete_cadences * cadence + partial_data - 1
                    else:
                        last_data_column = last_complete_data
            else:
                last_data_column = config.HorizontalStart_REG

        # Clamp to row boundary for non-SRI mode
        if not config.SubRowInterval_REG:
            last_data_column = min(last_data_column, self.interface.num_columns - 1)

        columns_after_data = self.interface.num_columns - 1 - last_data_column

        if config.TailWidth_REG > columns_after_data and not config.PortDirection_REG:
            result.add_error(
                'TailWidth',
                'Tail would overflow row',
                ErrorSeverity.WARNING
            )

        # Check bit width overflow
        if config.BitWidth_REG > self.interface.num_columns - config.HorizontalCount_REG and not config.PortDirection_REG:
            result.add_error(
                'BitWidth',
                'Bit would overflow row',
                ErrorSeverity.ERROR
            )

        # Check bit width vs horizontal_count
        if config.BitWidth_REG > config.HorizontalCount_REG:
            result.add_error(
                'BitWidth',
                'Bit would overflow HorizontalCount',
                ErrorSeverity.ERROR
            )

        # Check horizontal_count divisibility by bit_width (non-SRI mode)
        if not config.SubRowInterval_REG and (config.HorizontalCount_REG + 1) % (config.BitWidth_REG + 1) != 0:
            result.add_error(
                'HorizontalCount',
                'HorizontalCount + 1 should be a multiple of BitWidth + 1',
                ErrorSeverity.ERROR
            )

        # Check post guard overflow - reuse last_data_column calculated above
        if config.GuardEnable_REG and not config.PortDirection_REG and columns_after_data < 1:
            result.add_error(
                'GuardEnable',
                'Guard would overflow row',
                ErrorSeverity.WARNING
            )

        # Sink-specific validations (PortDirection_REG=True means sink)
        # Sinks read from the bus and shouldn't drive guards or tails
        if config.PortDirection_REG:
            if config.GuardEnable_REG:
                result.add_error(
                    'GuardEnable',
                    'Sink data port has Guard enabled',
                    ErrorSeverity.WARNING
                )
            if config.TailWidth_REG > 0:
                result.add_error(
                    'TailWidth',
                    'Sink data port has Tail(s) enabled',
                    ErrorSeverity.WARNING
                )

        # FCP (Flow Control Port) validations - only if flow control is enabled
        self._validate_fcp(result, data_port)

    def _validate_fcp(self, result: ValidationResult, data_port: 'DataPort') -> None:
        """Validate Flow Control Port (FCP/DRQ) parameters.

        Args:
            result: ValidationResult to add errors to
            data_port: DataPort being validated
        """
        config = data_port.config
        fcp_config = data_port.fcp.config

        # Import ranges and enums for validation
        from src.config.constants import DataPortRanges
        from src.models import FlowMode

        # Only validate FCP if DRQ bits are needed (RX_CONTROLLED or ASYNC flow modes)
        if config.FlowMode_REG not in (FlowMode.RX_CONTROLLED, FlowMode.ASYNC):
            return

        # Validate FCP parameter ranges
        self._validate_range(
            result, 'FCP_HorizontalStart_REG', fcp_config.HorizontalStart_REG,
            DataPortRanges.MIN_FCP_H_START, min(DataPortRanges.MAX_FCP_H_START, self.interface.num_columns - 1)
        )

        self._validate_range(
            result, 'FCP_BitWidth_REG', fcp_config.BitWidth_REG,
            DataPortRanges.MIN_FCP_BIT_WIDTH, DataPortRanges.MAX_FCP_BIT_WIDTH
        )

        self._validate_range(
            result, 'FCP_TailWidth_REG', fcp_config.TailWidth_REG,
            DataPortRanges.MIN_FCP_TAIL_WIDTH, DataPortRanges.MAX_FCP_TAIL_WIDTH
        )

        self._validate_range(
            result, 'FCP_Offset_REG', fcp_config.Offset_REG,
            DataPortRanges.MIN_FCP_OFFSET, DataPortRanges.MAX_FCP_OFFSET
        )

        # Check FCP offset vs interval
        if fcp_config.Offset_REG > config.Interval_REG:
            result.add_error(
                'FCP_Offset_REG',
                f"FCP Offset ({fcp_config.Offset_REG}) exceeds Interval ({config.Interval_REG})",
                ErrorSeverity.ERROR
            )

        # Calculate total FCP width (guard + data + tails)
        fcp_total_width = fcp_config.BitWidth_REG + 1  # Data width
        if fcp_config.GuardEnable_REG:
            fcp_total_width += fcp_config.BitWidth_REG + 1  # Guard width
        fcp_total_width += fcp_config.TailWidth_REG  # Tail width

        # Check FCP doesn't overflow row
        if fcp_config.HorizontalStart_REG + fcp_total_width > self.interface.num_columns:
            result.add_error(
                'FCP_HorizontalStart_REG',
                "FCP bits would overflow row",
                ErrorSeverity.ERROR
            )

# =============================================================================
# Interface Validator Class
# =============================================================================

class InterfaceValidator:
    """Validates Interface configurations.

    Performs cross-field validations for interface-level parameters that
    cannot be validated by individual field descriptors.
    """

    def __init__(self, interface: 'Interface'):
        """Initialize validator with interface to validate.

        Args:
            interface: The Interface object to validate
        """
        self.interface = interface

    def validate(self) -> ValidationResult:
        """Validate all Interface parameters.

        Returns:
            ValidationResult containing any errors or warnings found
        """
        result = ValidationResult()

        # PHY3 disabled requires even number of columns
        self._validate_phy3_columns(result)

        return result

    def _validate_phy3_columns(self, result: ValidationResult) -> None:
        """Validate PHY3/columns relationship.

        When PHY3 is disabled, the number of columns must be even.
        num_columns = NumColumns_REG + 1, so for even num_columns,
        NumColumns_REG must be odd.

        Args:
            result: ValidationResult to add errors to
        """
        if not self.interface.phy3_enabled:
            if self.interface.num_columns % 2 != 0:  # Odd number of columns
                result.add_error(
                    'NumColumns',
                    f'When FBSCE PHYs are used, number of columns must be even '
                    f'(currently {self.interface.num_columns})',
                    ErrorSeverity.WARNING
                )
