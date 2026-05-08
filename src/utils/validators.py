"""
Validation utilities for MIPI SoundWire I3S Visualizer.

Two validation categories, intentionally separated so the settings category
can serve as source material for written requirements in the SWI3S
specification:

    - Ranges   — register bit-field bounds. In hardware these are enforced
                 by the registers themselves; we check them here because the
                 visualizer lets users type arbitrary values via the UI/CSV.
    - Settings — semantic rules that cross fields (e.g. HorizontalStart +
                 HorizontalCount must fit within NumColumns; SRI mode
                 implies Interval = 0). These are the specification
                 requirements the visualizer enforces.

Classes:
    ErrorSeverity:       Severity levels for validation errors
    ValidationError:     A single validation error
    ValidationResult:    Result of a validation operation
    InterfaceValidator:  Validates Interface configurations
    DataPortValidator:   Validates DataPort (and associated FCP) configurations
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.models import DataPort, Interface
    from src.models.flow_control_port import FlowControlPortConfig


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
        return "\n".join(str(e) for e in self.errors)


def _check_range(result: ValidationResult, field: str, value: int,
                 min_val: int, max_val: int) -> None:
    """Shared helper: flag value outside [min_val, max_val] as ERROR."""
    if value < min_val:
        result.add_error(
            field, f"{field} ({value}) is below minimum ({min_val})",
            ErrorSeverity.ERROR, value
        )
    elif value > max_val:
        result.add_error(
            field, f"{field} ({value}) exceeds maximum ({max_val})",
            ErrorSeverity.ERROR, value
        )


class DataPortValidator:
    """Validates DataPort configurations (plus the associated FCP).

    validate() is the single public entry point. It runs range checks first,
    then settings checks. The two groups are cleanly separated internally so
    that settings rules can be enumerated as SWI3S specification requirements.
    """

    def __init__(self, interface: Any):
        self.interface = interface

    def validate(self, data_port: 'DataPort', dp_index: int) -> ValidationResult:
        """Validate DataPort ranges + settings. Returns a single ValidationResult."""
        result = ValidationResult()
        self._validate_ranges(result, data_port, dp_index)
        self._validate_settings(result, data_port)
        return result

    # ---------------------------------------------------------------
    # Range checks — hardware register bit-field bounds.
    # ---------------------------------------------------------------

    def _validate_ranges(self, result: ValidationResult,
                         data_port: 'DataPort', dp_index: int) -> None:
        """Run all register range checks."""
        from src.config.constants import DataPortRanges, SpecialDevices
        from src.models import FlowMode

        config = data_port.config

        # Device number (stored on interface, not DP)
        device_num = self.interface.get_dp_device(dp_index)
        if device_num != SpecialDevices.MANAGER:
            _check_range(result, 'DeviceNumber', device_num,
                         DataPortRanges.MIN_DEVICE_NUMBER, DataPortRanges.MAX_DEVICE_NUMBER)

        num_channels = bin(config.EnableCh_REG).count('1')
        _check_range(result, 'NumChannels', num_channels,
                     DataPortRanges.MIN_CHANNELS, DataPortRanges.MAX_CHANNELS)
        _check_range(result, 'ChannelGrouping_REG', config.ChannelGrouping_REG,
                     DataPortRanges.MIN_CHANNEL_GROUPING, DataPortRanges.MAX_CHANNEL_GROUPING)
        _check_range(result, 'Spacing_REG', config.Spacing_REG,
                     DataPortRanges.MIN_CHANNEL_GROUP_SPACING, DataPortRanges.MAX_CHANNEL_GROUP_SPACING)
        _check_range(result, 'SampleSize_REG', config.SampleSize_REG,
                     DataPortRanges.MIN_SAMPLE_SIZE, DataPortRanges.MAX_SAMPLE_SIZE)
        _check_range(result, 'SampleGrouping_REG', config.SampleGrouping_REG,
                     DataPortRanges.MIN_SAMPLE_GROUPING, DataPortRanges.MAX_SAMPLE_GROUPING)
        _check_range(result, 'Interval_REG', config.Interval_REG,
                     DataPortRanges.MIN_INTERVAL, DataPortRanges.MAX_INTERVAL)
        _check_range(result, 'SkippingNumerator_REG', config.SkippingNumerator_REG,
                     DataPortRanges.MIN_SKIPPING_NUMERATOR, DataPortRanges.MAX_SKIPPING_NUMERATOR)
        _check_range(result, 'Offset_REG', config.Offset_REG,
                     DataPortRanges.MIN_OFFSET, DataPortRanges.MAX_OFFSET)
        _check_range(result, 'HorizontalStart_REG', config.HorizontalStart_REG,
                     DataPortRanges.MIN_H_START, DataPortRanges.MAX_H_START)
        _check_range(result, 'HorizontalCount_REG', config.HorizontalCount_REG,
                     DataPortRanges.MIN_H_COUNT, DataPortRanges.MAX_H_COUNT)

        # FCP register ranges only matter when FlowMode activates the FCP
        if config.FlowMode_REG in (FlowMode.RX_CONTROLLED, FlowMode.ASYNC):
            fcp_config = self.interface.get_fcp(data_port.dp_index).config
            _check_range(result, 'FCP_HorizontalStart_REG', fcp_config.FCP_HorizontalStart_REG,
                         DataPortRanges.MIN_FCP_H_START, DataPortRanges.MAX_FCP_H_START)
            _check_range(result, 'FCP_BitWidth_REG', fcp_config.FCP_BitWidth_REG,
                         DataPortRanges.MIN_FCP_BIT_WIDTH, DataPortRanges.MAX_FCP_BIT_WIDTH)
            _check_range(result, 'FCP_TailWidth_REG', fcp_config.FCP_TailWidth_REG,
                         DataPortRanges.MIN_FCP_TAIL_WIDTH, DataPortRanges.MAX_FCP_TAIL_WIDTH)
            _check_range(result, 'FCP_Offset_REG', fcp_config.FCP_Offset_REG,
                         DataPortRanges.MIN_FCP_OFFSET, DataPortRanges.MAX_FCP_OFFSET)

    # ---------------------------------------------------------------
    # Settings checks — spec-level semantic requirements.
    # Each _check_* below is one requirement. The docstring is the
    # rule statement in human-readable form.
    # ---------------------------------------------------------------

    def _validate_settings(self, result: ValidationResult, data_port: 'DataPort') -> None:
        """Run all settings checks. Shared computations happen here once."""
        from src.models import FlowMode

        config = data_port.config
        num_channels = bin(config.EnableCh_REG).count('1')
        effective_channel_grouping = self._effective_channel_grouping(config, num_channels)
        drive_in_group = self._drive_in_group(config, effective_channel_grouping)
        last_data_column = self._last_data_column(config, drive_in_group)
        columns_after_data = self.interface.num_columns - 1 - last_data_column

        self._check_offset_within_interval(result, config)
        self._check_channel_grouping_within_num_channels(result, config, num_channels)
        self._check_sri_interval_zero(result, config)
        self._check_sri_skipping_disabled(result, config)
        self._check_sri_equal_spacing(result, config, drive_in_group)
        self._check_sri_pattern_fits(result, config, drive_in_group)
        self._check_horizontal_start_within_columns(result, config)
        self._check_horizontal_count_within_columns(result, config)
        self._check_horizontal_window_within_columns(result, config)
        self._check_tail_fits_row(result, config, columns_after_data)
        self._check_bitwidth_fits_remaining_columns(result, config)
        self._check_bitwidth_fits_horizontal_count(result, config)
        self._check_no_wide_bit_straddles_window(
            result, config, num_channels, effective_channel_grouping
        )
        self._check_guard_fits_row(result, config, columns_after_data)
        self._check_sink_no_guard(result, config)
        self._check_sink_no_tail(result, config)

        if config.FlowMode_REG in (FlowMode.RX_CONTROLLED, FlowMode.ASYNC):
            fcp_config = self.interface.get_fcp(data_port.dp_index).config
            self._check_fcp_offset_within_interval(result, config, fcp_config)
            self._check_fcp_fits_row(result, fcp_config)
        else:
            self._check_fcp_registers_zero_when_disabled(result, data_port, config)

    # Shared settings-scope helpers.

    @staticmethod
    def _effective_channel_grouping(config: Any, num_channels: int) -> int:
        """Channels per group (clamped to num_channels when register is 0)."""
        if config.ChannelGrouping_REG == 0:
            return num_channels
        return config.ChannelGrouping_REG

    @staticmethod
    def _drive_in_group(config: Any, effective_channel_grouping: int) -> int:
        """Number of bus slots required to transmit one complete channel group."""
        return (
            (config.SampleSize_REG + 1) *
            (config.SampleGrouping_REG + 1) *
            effective_channel_grouping *
            (config.BitWidth_REG + 1)
        )

    def _last_data_column(self, config: Any, drive_in_group: int) -> int:
        """Absolute column index of the last data slot in a row.

        Accounts for inter-group spacing when Spacing_REG > 0. Clamped to the
        last column in non-SRI mode (where the pattern stops at row boundary).
        """
        window_size = config.HorizontalCount_REG + 1

        if config.Spacing_REG == 0:
            data_bits_in_row = min(drive_in_group, window_size)
            last = config.HorizontalStart_REG + data_bits_in_row - 1
        else:
            cadence = drive_in_group + config.Spacing_REG - 1
            if cadence > 0:
                num_complete = window_size // cadence
                remaining = window_size % cadence
                if num_complete == 0:
                    data_bits = min(drive_in_group, window_size)
                    last = config.HorizontalStart_REG + data_bits - 1
                elif remaining > 0:
                    partial_data = min(drive_in_group, remaining)
                    last = config.HorizontalStart_REG + num_complete * cadence + partial_data - 1
                else:
                    last = config.HorizontalStart_REG + (num_complete - 1) * cadence + drive_in_group - 1
            else:
                last = config.HorizontalStart_REG

        if not config.SubRowInterval_REG:
            last = min(last, self.interface.num_columns - 1)
        return last

    # Individual settings checks — one method per spec requirement.

    def _check_offset_within_interval(self, result: ValidationResult, config: Any) -> None:
        """Offset_REG shall be less than or equal to Interval_REG."""
        if config.Offset_REG > config.Interval_REG:
            result.add_error(
                'Offset',
                f"Offset ({config.Offset_REG}) exceeds Interval ({config.Interval_REG})",
                ErrorSeverity.ERROR
            )

    def _check_channel_grouping_within_num_channels(self, result: ValidationResult,
                                                    config: Any, num_channels: int) -> None:
        """ChannelGrouping_REG shall be less than or equal to NumChannels."""
        if num_channels > 0 and config.ChannelGrouping_REG > num_channels:
            result.add_error(
                'ChannelGrouping_REG',
                f"ChannelGrouping ({config.ChannelGrouping_REG}) exceeds NumChannels ({num_channels})",
                ErrorSeverity.ERROR
            )

    def _check_sri_interval_zero(self, result: ValidationResult, config: Any) -> None:
        """In SRI mode (SubRowInterval_REG=1), Interval_REG shall be 0 (one-row interval)."""
        if config.SubRowInterval_REG and config.Interval_REG != 0:
            result.add_error(
                'Interval',
                f"SRI mode implies one-row interval; Interval_REG should be 0 (currently {config.Interval_REG})",
                ErrorSeverity.ERROR
            )

    def _check_sri_skipping_disabled(self, result: ValidationResult, config: Any) -> None:
        """In SRI mode, SkippingNumerator_REG shall be 0 (skipping not supported in SRI)."""
        if config.SubRowInterval_REG and config.SkippingNumerator_REG > 0:
            result.add_error(
                'SkippingNumerator',
                f"SRI mode does not support skipping; SkippingNumerator_REG should be 0 (currently {config.SkippingNumerator_REG})",
                ErrorSeverity.ERROR
            )

    def _check_sri_equal_spacing(self, result: ValidationResult, config: Any,
                                  drive_in_group: int) -> None:
        """In SRI mode, Transport Patterns shall be equally spaced across Row boundaries (C14).

        Three sub-rules, each emitted as a distinct error for diagnostic clarity:

          1. Spacing_REG >= 2.
             Spacing=0 permits at most one Transport Pattern per Row (defeating
             SRI); Spacing=1 places patterns back-to-back, violating equal-
             spacing across Row boundaries.

          2. NumColumns is an integer multiple of the TP cycle length.
             TP_cycle_length = (TP payload UIs) + (Spacing - 1). Requiring
             NumColumns (not HorizontalCount+1) to be a multiple of the TP
             cycle ensures Transport Patterns land at the same column
             position every Row; the last TP of a Row may place its payload
             at the end of the Horizontal Window with its gap UIs straddling
             into the cross-Row structural area — sub-rule 3 ensures that
             cross-Row gap matches the in-Row inter-pattern gap.

          3. NumColumns - (HorizontalCount + 1) == Spacing - 1.
             The unused Row UIs outside the Horizontal Window equal the in-Row
             inter-pattern gap, so the gap that crosses the Row boundary matches
             the gap between consecutive patterns inside the Row.
        """
        if not config.SubRowInterval_REG:
            return

        # Sub-rule 1: Spacing >= 2.
        if config.Spacing_REG < 2:
            result.add_error(
                'Spacing',
                f"SRI mode requires Spacing_REG >= 2 (currently {config.Spacing_REG}); "
                f"Spacing=0 defeats SRI and Spacing=1 violates equal-spacing across Row boundaries",
                ErrorSeverity.ERROR
            )
            # Sub-rules 2 and 3 are meaningful only when Spacing >= 2 (they
            # depend on a well-defined inter-pattern gap). Return early to
            # avoid emitting derivative errors that would confuse the user.
            return

        window_size = config.HorizontalCount_REG + 1
        tp_cycle_length = drive_in_group + config.Spacing_REG - 1
        num_columns = self.interface.num_columns

        # Sub-rule 2: NumColumns holds an integer number of TP cycles so
        # Transport Patterns align to the same column position every Row.
        if tp_cycle_length > 0 and num_columns % tp_cycle_length != 0:
            result.add_error(
                'HorizontalCount',
                f"SRI mode requires NumColumns ({num_columns}) to be a "
                f"multiple of the TP cycle length ({tp_cycle_length} = "
                f"{drive_in_group} payload + {config.Spacing_REG - 1} gap); "
                f"remainder {num_columns % tp_cycle_length} would cause "
                f"Transport Patterns to land at different column positions "
                f"from one Row to the next",
                ErrorSeverity.ERROR
            )

        # Sub-rule 3: unused Row columns match the in-Row inter-pattern gap.
        unused_columns = self.interface.num_columns - window_size
        expected_gap = config.Spacing_REG - 1
        if unused_columns != expected_gap:
            result.add_error(
                'HorizontalCount',
                f"SRI mode requires NumColumns-(HorizontalCount+1) ({unused_columns}) "
                f"to equal Spacing_REG-1 ({expected_gap}); cross-Row gap must "
                f"match in-Row gap so Transport Patterns remain equally spaced",
                ErrorSeverity.ERROR
            )

    def _check_sri_pattern_fits(self, result: ValidationResult, config: Any, drive_in_group: int) -> None:
        """In SRI mode, HorizontalCount shall be large enough to emit at least one complete channel group.

        When Spacing_REG == 0 (single group per row), HorizontalCount + 1 must be
        >= drive_in_group. When Spacing_REG > 0 (multiple groups per row), any
        partial trailing group must still fit a complete drive_in_group.
        """
        if not config.SubRowInterval_REG:
            return

        window_size = config.HorizontalCount_REG + 1
        if config.Spacing_REG == 0:
            if window_size < drive_in_group:
                result.add_error(
                    'HorizontalCount',
                    'Group, or sample, incomplete when HorizontalCount expires.',
                    ErrorSeverity.ERROR
                )
        else:
            cadence = drive_in_group + config.Spacing_REG - 1
            if cadence != 0 and window_size % cadence != 0:
                if window_size % cadence < drive_in_group:
                    result.add_error(
                        'HorizontalCount',
                        'Group, or sample, incomplete when HorizontalCount expires.',
                        ErrorSeverity.ERROR
                    )

    def _check_horizontal_start_within_columns(self, result: ValidationResult, config: Any) -> None:
        """HorizontalStart_REG shall be a valid column index (< NumColumns)."""
        if config.HorizontalStart_REG >= self.interface.num_columns:
            result.add_error(
                'HorizontalStart',
                'HorizontalStart exceeds NumColumns',
                ErrorSeverity.ERROR
            )

    def _check_horizontal_count_within_columns(self, result: ValidationResult, config: Any) -> None:
        """HorizontalCount_REG shall be a valid column index (< NumColumns)."""
        if config.HorizontalCount_REG >= self.interface.num_columns:
            result.add_error(
                'HorizontalCount',
                'HorizontalCount exceeds NumColumns',
                ErrorSeverity.ERROR
            )

    def _check_horizontal_window_within_columns(self, result: ValidationResult, config: Any) -> None:
        """HorizontalStart_REG + HorizontalCount_REG shall fit within NumColumns."""
        if config.HorizontalStart_REG + config.HorizontalCount_REG >= self.interface.num_columns:
            result.add_error(
                'HorizontalCount',
                'HorizontalStart + HorizontalCount exceeds NumColumns',
                ErrorSeverity.ERROR
            )

    def _check_tail_fits_row(self, result: ValidationResult, config: Any, columns_after_data: int) -> None:
        """TailWidth_REG shall fit in the columns remaining after the last data slot (source DP only)."""
        if config.TailWidth_REG > columns_after_data and not config.PortDirection_REG:
            result.add_error(
                'TailWidth',
                'Tail would overflow row',
                ErrorSeverity.ERROR
            )

    def _check_bitwidth_fits_remaining_columns(self, result: ValidationResult, config: Any) -> None:
        """BitWidth_REG shall not exceed the columns remaining after HorizontalCount (source DP only)."""
        if (config.BitWidth_REG > self.interface.num_columns - config.HorizontalCount_REG
                and not config.PortDirection_REG):
            result.add_error(
                'BitWidth',
                'Bit would overflow row',
                ErrorSeverity.ERROR
            )

    def _check_bitwidth_fits_horizontal_count(self, result: ValidationResult, config: Any) -> None:
        """BitWidth_REG shall not exceed HorizontalCount_REG (a bit must fit in the transport window)."""
        if config.BitWidth_REG > config.HorizontalCount_REG:
            result.add_error(
                'BitWidth',
                'Bit would overflow HorizontalCount',
                ErrorSeverity.ERROR
            )

    def _check_no_wide_bit_straddles_window(self, result: ValidationResult, config: Any,
                                            num_channels: int,
                                            effective_channel_grouping: int) -> None:
        """Outside SRI mode, no Wide Bit shall straddle the end of a Horizontal Window (C07).

        Simulates the non-SRI Transport Pattern UI-by-UI starting at HorizontalStart.
        Each bit occupies BitWidth+1 contiguous UIs; when a bit begins, there must be
        at least BitWidth+1 UIs remaining before the row's Horizontal Window end, or
        the bit's UIs would straddle into the next row.

        Channel groups within a row are separated by Spacing-1 gap UIs (when
        Spacing_REG != 0); when Spacing_REG == 0, each channel group starts a fresh
        row at HorizontalStart. Guard/Tail UIs live AFTER the last data UI and are
        covered by _check_tail_fits_row / _check_guard_fits_row; they do not appear
        inside the data-UI stream for this check.

        Only wide bits (BitWidth_REG > 0) can straddle; narrow bits (BitWidth_REG == 0)
        occupy a single UI. Bit = 0 case is skipped for efficiency.
        """
        # Only applies outside SRI, with at least one bit to transport, and only when
        # BitWidth > 0 (a single-UI bit cannot straddle by definition).
        if config.SubRowInterval_REG:
            return
        if num_channels == 0 or effective_channel_grouping == 0:
            return
        if config.BitWidth_REG == 0:
            return

        h_start = config.HorizontalStart_REG
        row_end = h_start + config.HorizontalCount_REG  # inclusive last column of window
        bit_ui_span = config.BitWidth_REG + 1
        spacing_gap = config.Spacing_REG - 1 if config.Spacing_REG != 0 else 0

        # Total channel groups in the Transport Pattern.
        num_groups = (num_channels + effective_channel_grouping - 1) // effective_channel_grouping
        # Bits per channel group.
        bits_per_group = (
            (config.SampleSize_REG + 1)
            * (config.SampleGrouping_REG + 1)
            * effective_channel_grouping
        )

        col = h_start
        for group_idx in range(num_groups):
            for bit_idx in range(bits_per_group):
                # If the previous bit landed exactly at row_end, col is now past
                # row_end — wrap to the next row before placing this bit.
                if col > row_end:
                    col = h_start
                # Check this bit's UIs fit entirely within the current row's window.
                # If col..col+bit_ui_span-1 would cross row_end, the bit straddles.
                if col + bit_ui_span - 1 > row_end:
                    result.add_error(
                        'HorizontalCount',
                        f"Wide Bit straddles Horizontal Window end: bit {bit_idx} of "
                        f"channel group {group_idx} starts at column {col} with "
                        f"BitWidth+1={bit_ui_span} UIs, but only "
                        f"{row_end - col + 1} UI(s) remain before row end "
                        f"(HorizontalStart={h_start}, HorizontalCount+1="
                        f"{config.HorizontalCount_REG + 1})",
                        ErrorSeverity.ERROR
                    )
                    return
                col += bit_ui_span

            # End of channel group. Insert inter-group spacing (or wrap to next row).
            if group_idx == num_groups - 1:
                break  # pattern complete

            if config.Spacing_REG == 0:
                # No inter-group spacing: next channel group starts a fresh row.
                col = h_start
            else:
                # Emit Spacing-1 gap UIs; they may wrap into the next row.
                col += spacing_gap
                if col > row_end:
                    # Gap (or part of it) spills past row end → wraps to next row.
                    col = h_start

    def _check_guard_fits_row(self, result: ValidationResult, config: Any, columns_after_data: int) -> None:
        """A post-data guard bit shall have at least one column remaining after the last data slot (source DP only)."""
        if config.GuardEnable_REG and not config.PortDirection_REG and columns_after_data < 1:
            result.add_error(
                'GuardEnable',
                'Guard would overflow row',
                ErrorSeverity.ERROR
            )

    def _check_sink_no_guard(self, result: ValidationResult, config: Any) -> None:
        """A sink DP shall not drive Guard bits (Guard comes from the source)."""
        if config.PortDirection_REG and config.GuardEnable_REG:
            result.add_error(
                'GuardEnable',
                'Sink data port has Guard enabled',
                ErrorSeverity.ERROR
            )

    def _check_sink_no_tail(self, result: ValidationResult, config: Any) -> None:
        """A sink DP shall not drive Tail bits (Tail comes from the source)."""
        if config.PortDirection_REG and config.TailWidth_REG > 0:
            result.add_error(
                'TailWidth',
                'Sink data port has Tail(s) enabled',
                ErrorSeverity.ERROR
            )

    def _check_fcp_offset_within_interval(self, result: ValidationResult,
                                          config: Any, fcp_config: 'FlowControlPortConfig') -> None:
        """FCP Offset_REG shall be less than or equal to the parent DP's Interval_REG."""
        if fcp_config.FCP_Offset_REG > config.Interval_REG:
            result.add_error(
                'FCP_Offset_REG',
                f"FCP Offset ({fcp_config.FCP_Offset_REG}) exceeds Interval ({config.Interval_REG})",
                ErrorSeverity.ERROR
            )

    def _check_fcp_fits_row(self, result: ValidationResult,
                            fcp_config: 'FlowControlPortConfig') -> None:
        """FCP (DRQ + optional guard + tails) shall fit in the row starting at FCP_HorizontalStart."""
        fcp_total_width = fcp_config.FCP_BitWidth_REG + 1
        if fcp_config.FCP_GuardEnable_REG:
            fcp_total_width += fcp_config.FCP_BitWidth_REG + 1
        fcp_total_width += fcp_config.FCP_TailWidth_REG

        if fcp_config.FCP_HorizontalStart_REG + fcp_total_width > self.interface.num_columns:
            result.add_error(
                'FCP_HorizontalStart_REG',
                "FCP bits would overflow row",
                ErrorSeverity.ERROR
            )

    def _check_fcp_registers_zero_when_disabled(self, result: ValidationResult,
                                                data_port: 'DataPort', config: Any) -> None:
        """When FlowMode_REG is Normal (0) or Tx-Controlled (1) the FCP is not in
        use and all six FCP registers shall be 0 (C15).

        FCP_HorizontalStart_REG, FCP_BitWidth_REG, FCP_TailWidth_REG,
        FCP_Offset_REG, FCP_GuardEnable_REG, FCP_GuardPolarity_REG.
        """
        fcp_config = self.interface.get_fcp(data_port.dp_index).config
        non_zero: List[str] = []
        if fcp_config.FCP_HorizontalStart_REG:
            non_zero.append(f"FCP_HorizontalStart_REG={fcp_config.FCP_HorizontalStart_REG}")
        if fcp_config.FCP_BitWidth_REG:
            non_zero.append(f"FCP_BitWidth_REG={fcp_config.FCP_BitWidth_REG}")
        if fcp_config.FCP_TailWidth_REG:
            non_zero.append(f"FCP_TailWidth_REG={fcp_config.FCP_TailWidth_REG}")
        if fcp_config.FCP_Offset_REG:
            non_zero.append(f"FCP_Offset_REG={fcp_config.FCP_Offset_REG}")
        if fcp_config.FCP_GuardEnable_REG:
            non_zero.append(f"FCP_GuardEnable_REG={fcp_config.FCP_GuardEnable_REG}")
        if fcp_config.FCP_GuardPolarity_REG:
            non_zero.append(f"FCP_GuardPolarity_REG={fcp_config.FCP_GuardPolarity_REG}")

        if non_zero:
            result.add_error(
                'FCP',
                f"DP{data_port.dp_index}: FlowMode={config.FlowMode_REG} "
                f"(FCP not in use) requires all FCP registers to be 0; "
                f"non-zero: {', '.join(non_zero)}",
                ErrorSeverity.ERROR
            )


class InterfaceValidator:
    """Validates Interface configurations.

    validate() is the single public entry point. No range checks here yet —
    interface registers are validated through field descriptors elsewhere —
    but the method layout mirrors DataPortValidator so additions land
    consistently.
    """

    def __init__(self, interface: 'Interface'):
        self.interface = interface

    def validate(self) -> ValidationResult:
        """Validate Interface settings. Returns a single ValidationResult."""
        result = ValidationResult()
        self._validate_settings(result)
        return result

    # ---------------------------------------------------------------
    # Settings checks — spec-level semantic requirements.
    # ---------------------------------------------------------------

    def _validate_settings(self, result: ValidationResult) -> None:
        """Run all interface settings checks."""
        self._check_phy3_requires_even_columns(result)

    def _check_phy3_requires_even_columns(self, result: ValidationResult) -> None:
        """When PHY3 is disabled (FBSCE PHYs used), NumColumns shall be even.

        num_columns = NumColumns_REG + 1, so for even num_columns,
        NumColumns_REG must be odd.
        """
        if not self.interface.phy3_enabled and self.interface.num_columns % 2 != 0:
            result.add_error(
                'NumColumns',
                f'When FBSCE PHYs are used, number of columns must be even '
                f'(currently {self.interface.num_columns})',
                ErrorSeverity.ERROR
            )
