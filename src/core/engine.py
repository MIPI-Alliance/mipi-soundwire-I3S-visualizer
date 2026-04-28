"""Core engine for building BusModel from Interface configuration.

This module provides the BusModelBuilder class that builds a BusModel
without any UI dependencies. It can be used for headless batch processing.
"""

from __future__ import annotations

from typing import Optional, List

from src.models import (
    Interface,
    DataPort,
    SlotType,
    DirectionType,
)
from src.models.bit_slot import BitSlotState
from src.models.bus_model import BusModel, BitInfo, ClashType
from src.drawing.clash_detector import ClashDetector, SlotClashCategory
from src.config import SpecialDevices, DataPortRanges
from src.utils.validators import DataPortValidator
from src.utils.logging_config import get_logger
from src.viz import VizConfig


class BusModelBuilder:
    """Builds BusModel from Interface configuration without UI dependencies.

    This class extracts the bit iteration logic from the GUI's draw_data_port
    method and builds a sequential BusModel representation of the bus.
    """

    def __init__(self, interface: Interface, num_rows: int,
                 viz_config: Optional[VizConfig] = None):
        """Initialize the builder.

        Args:
            interface: Interface configuration with data ports
            num_rows: Number of rows in the frame
            viz_config: Optional visualization configuration. If None, creates default.
        """
        self.interface = interface
        self.num_rows = num_rows
        self.viz_config = viz_config if viz_config else VizConfig()
        self.logger = get_logger('core_engine')

        # Create clash detector
        self.clash_detector = ClashDetector(interface.num_columns)

        # Create bus model
        self.bus_model = BusModel(num_rows, interface.num_columns, interface.row_rate)

        # Create reusable data port validator (performance optimization)
        self._dp_validator = DataPortValidator(self.interface)

        # Track potential handover positions for post-processing
        # Structure: list of (row, column, dp_number) tuples
        self._potential_handovers: list = []

        # Per-DP running count of transports that have emitted at least one
        # slot. Incremented by the engine when it detects (via DP state
        # observation — see _process_data_port) that a fresh initialize_transport
        # has just run and the next emission is the first of a new transport.
        # Owned by the engine — the DataPort hardware model tracks no
        # running counter and emits no transport-start strobe (see CLAUDE.md
        # hardware-model policy).
        self._transport_counts: dict[int, int] = {}

    def build(self) -> BusModel:
        """Build the complete bus model.

        Returns:
            BusModel with all bits and clash information
        """
        self.logger.info(f'Building bus model: {self.num_rows} rows x {self.interface.num_columns} columns')

        # 0. Validate interface configuration
        self._validate_interface()

        # 1. Add system slots (S0, S1, CDS, handovers, tails)
        self._add_system_slots()

        # 2. For each device in sequence, process its data ports
        for device in range(SpecialDevices.MANAGER, DataPortRanges.MAX_DEVICE_NUMBER + 1):
            for dp_index, dp in enumerate(self.interface.data_ports):
                if self._is_target_device(dp_index, device):
                    if self.viz_config.data_ports[dp_index].enabled:
                        self._process_data_port(dp_index, dp)

        # 3. Generate visualizer handovers (post-processing to avoid DRQ collisions)
        self._generate_viz_handovers()

        # 4. Validate TxP and DRQ pairs (must happen before finalize_clashes)
        self.clash_detector.validate_txp_pairs()
        self.clash_detector.validate_txp_sinks()
        self.clash_detector.validate_drq_pairs()
        self.clash_detector.validate_drq_sinks()
        self._detect_truncated_drq()

        # 5. Transfer clash information to bus model
        self._finalize_clashes()

        # 6. Detect scrambler mismatches
        self._detect_scrambler_mismatches()

        # 7. Detect test mode mismatches
        self._detect_test_mode_mismatches()

        # 8. Detect interval overflow (bits don't fit in configured interval)
        self._detect_interval_overflow()

        # 9. Detect sample/bit mismatches between source and sink
        self._detect_sample_bit_mismatches()

        # 10. Detect sink dataports with handover but no FCP bits
        self._detect_sink_handover_warnings()

        # 11. Detect data ports with EnableDataPort but NumChannels=0
        self._detect_enabled_no_channels_warnings()

        summary = self.clash_detector.get_summary()
        self.logger.info(f'Bus model built: {len(self.bus_model.bits)} bits, '
                        f'{summary["different_device_clashes"]} physical clashes, '
                        f'{summary["same_device_clashes"]} internal clashes')

        return self.bus_model

    def _is_target_device(self, dp_index: int, device: int) -> bool:
        """Check if data port belongs to the target device.

        Args:
            dp_index: Index of the data port (0-11)
            device: Device number to match

        Returns:
            True if data port belongs to this device
        """
        dp_device = self.interface.get_dp_device(dp_index)
        if device == SpecialDevices.MANAGER:
            return dp_device == SpecialDevices.MANAGER
        return dp_device == device

    def _validate_interface(self) -> None:
        """Validate interface configuration settings.

        Adds validation warnings to bus_model.validation_issues.
        """
        from src.utils.validators import InterfaceValidator

        validator = InterfaceValidator(self.interface)
        result = validator.validate()

        if not result.is_valid or result.has_warnings:
            self.bus_model.validation_issues.append(('Interface', result))
            # Log each error for debugging
            for error in result.errors:
                self.logger.warning(f'Interface validation: {error.message}')

    def _add_system_slots(self) -> None:
        """Add system slots (S0, S1, CDS, handovers, tails)."""
        if self.interface.phy3_enabled:
            self._add_phy3_system_slots()
        else:
            self._add_non_phy3_system_slots()

    def _add_phy3_system_slots(self) -> None:
        """Add system slots for PHY3-enabled configuration.

        PHY3 layout (left to right):
        S1 | S1-tails | S1-handover | CDS | CDS-guard | CDS-tails | CDS-handover | ... data ... | S0
        """
        min_columns_needed = (
            self.interface.s0_width +
            (self.interface.CDS_BitWidth_REG + 1) +
            1 +  # S1
            int(self.interface.CDS_GuardEnabled_REG) +
            int(self.interface.cds_handover_enabled) +
            int(self.interface.s1_handover_enabled) +
            self.interface.tail_width +
            self.interface.CDS_TailWidth_REG
        )

        self.logger.debug(f'PHY3 system slots: min_columns_needed={min_columns_needed}, '
                        f'num_columns={self.interface.num_columns}')

        # S0 columns (at right edge)
        for col_offset in range(self.interface.s0_width):
            col = self.interface.num_columns - col_offset - 1
            self.logger.debug(f'S0 slot at column {col}')
            self._add_column_bits(col, SpecialDevices.MANAGER, SlotType.S0)

        # S1 column (at column 0)
        self.logger.debug(f'S1 slot at column 0')
        self._add_column_bits(0, SpecialDevices.MANAGER, SlotType.S1)

        # S1 tails (after S1, before S1 handover)
        for col_offset in range(self.interface.tail_width):
            col = 1 + col_offset
            self._add_column_bits(col, SpecialDevices.MANAGER, SlotType.TAIL)
        if self.interface.tail_width > 0:
            self.logger.debug(f'S1 tails at columns 1-{self.interface.tail_width}')

        # S1 handover (after S1 tails)
        s1_handover_width = 1 if self.interface.s1_handover_enabled else 0
        if self.interface.s1_handover_enabled:
            col = 1 + self.interface.tail_width
            self.logger.debug(f'S1 handover at column {col}')
            self._add_column_bits(col, SpecialDevices.MANAGER, SlotType.HANDOVER)

        # CDS columns (after S1 section)
        cds_start = 1 + s1_handover_width + self.interface.tail_width
        for col_offset in range(self.interface.CDS_BitWidth_REG + 1):
            col = cds_start + col_offset
            self._add_column_bits(col, SpecialDevices.UNIVERSAL, SlotType.CDS)
        self.logger.debug(f'CDS slots at columns {cds_start}-{cds_start + self.interface.CDS_BitWidth_REG}')

        # CDS guard (after CDS)
        if self.interface.CDS_GuardEnabled_REG:
            col = cds_start + (self.interface.CDS_BitWidth_REG + 1)
            slot_type = SlotType.GUARD_1 if self.interface.CDS_GuardPolarity_REG else SlotType.GUARD_0
            self.logger.debug(f'CDS guard at column {col}')
            self._add_column_bits(col, SpecialDevices.UNIVERSAL, slot_type)

        # CDS tails (after CDS guard)
        for col_offset in range(self.interface.CDS_TailWidth_REG):
            col = (cds_start +
                  (self.interface.CDS_BitWidth_REG + 1) +
                  int(self.interface.CDS_GuardEnabled_REG) + col_offset)
            self._add_column_bits(col, SpecialDevices.UNIVERSAL, SlotType.TAIL)
        if self.interface.CDS_TailWidth_REG > 0:
            tail_start = cds_start + (self.interface.CDS_BitWidth_REG + 1) + int(self.interface.CDS_GuardEnabled_REG)
            self.logger.debug(f'CDS tails at columns {tail_start}-{tail_start + self.interface.CDS_TailWidth_REG - 1}')

        # CDS handover (after CDS tails)
        if self.interface.cds_handover_enabled:
            col = (cds_start +
                  (self.interface.CDS_BitWidth_REG + 1) +
                  int(self.interface.CDS_GuardEnabled_REG) +
                  self.interface.CDS_TailWidth_REG)
            self.logger.debug(f'CDS handover at column {col}')
            self._add_column_bits(col, SpecialDevices.UNIVERSAL, SlotType.HANDOVER)

    def _add_non_phy3_system_slots(self) -> None:
        """Add system slots for non-PHY3 configuration.

        Non-PHY3 layout (left to right):
        CDS | CDS-guard | CDS-tails | CDS-handover | ... data ...
        """
        self.logger.debug(f'PHY3 disabled: CDS-only system slots')

        # CDS columns (starting at column 0)
        for col_offset in range(self.interface.CDS_BitWidth_REG + 1):
            self._add_column_bits(col_offset, SpecialDevices.UNIVERSAL, SlotType.CDS)
        self.logger.debug(f'CDS slots at columns 0-{self.interface.CDS_BitWidth_REG}')

        # CDS guard (after CDS)
        if self.interface.CDS_GuardEnabled_REG:
            col = self.interface.CDS_BitWidth_REG + 1
            slot_type = SlotType.GUARD_1 if self.interface.CDS_GuardPolarity_REG else SlotType.GUARD_0
            self.logger.debug(f'CDS guard at column {col}')
            self._add_column_bits(col, SpecialDevices.UNIVERSAL, slot_type)

        # CDS tails (after CDS guard)
        for col_offset in range(self.interface.CDS_TailWidth_REG):
            col = ((self.interface.CDS_BitWidth_REG + 1) +
                  int(self.interface.CDS_GuardEnabled_REG) + col_offset)
            self._add_column_bits(col, SpecialDevices.UNIVERSAL, SlotType.TAIL)
        if self.interface.CDS_TailWidth_REG > 0:
            tail_start = (self.interface.CDS_BitWidth_REG + 1) + int(self.interface.CDS_GuardEnabled_REG)
            self.logger.debug(f'CDS tails at columns {tail_start}-{tail_start + self.interface.CDS_TailWidth_REG - 1}')

        # CDS handover (after CDS tails)
        if self.interface.cds_handover_enabled:
            col = ((self.interface.CDS_BitWidth_REG + 1) +
                   int(self.interface.CDS_GuardEnabled_REG) +
                   self.interface.CDS_TailWidth_REG)
            self.logger.debug(f'CDS handover at column {col}')
            self._add_column_bits(col, SpecialDevices.UNIVERSAL, SlotType.HANDOVER)

    def _add_column_bits(self, column: int, device: int, slot_type: SlotType) -> None:
        """Add bits for an entire column.

        Args:
            column: Column number
            device: Device number
            slot_type: Type of slot
        """
        # Bounds check - warn about truncation when slot is outside frame boundary
        if column < 0 or column >= self.interface.num_columns:
            from src.utils.validators import ValidationResult, ErrorSeverity
            result = ValidationResult()
            result.add_error(
                'PHY3 Layout',
                f'{slot_type.name} slot at column {column} is outside frame boundary (0-{self.interface.num_columns - 1})',
                severity=ErrorSeverity.ERROR
            )
            self.bus_model.validation_issues.append(('PHY3 Truncation', result))
            self.logger.warning(f'_add_column_bits: column {column} out of bounds [0, {self.interface.num_columns}) - slot truncated')
            return

        for row in range(self.num_rows):
            bit_index = self.bus_model.bit_index(row, column)
            direction = DirectionType.SOURCE  # System slots are source

            bit_info = BitInfo(
                bit_index=bit_index,
                slot=slot_type,
                direction=direction,
                device=device,
                dp=None,  # System slot
            )
            self.bus_model.add_bit(bit_info)

            # Register with clash detector
            if slot_type in (SlotType.GUARD_0, SlotType.GUARD_1):
                self.clash_detector.check_guard_clash(row, column, device)
            elif slot_type == SlotType.TAIL:
                self.clash_detector.check_tail_clash(row, column, device)
            elif slot_type == SlotType.HANDOVER:
                self.clash_detector.add_handover(row, column, device, [])
            else:
                self.clash_detector.add_write(row, column, device)

    def _process_data_port(self, dp_index: int, dp: DataPort) -> None:
        """Process a single data port and add its bits to the model.

        Args:
            dp_index: Index of the data port (0-11)
            dp: Data port to process
        """
        # Validate data port configuration - warn but don't block
        # Uses reusable validator for performance
        dp_viz = self.viz_config.data_ports[dp_index]
        validation_result = self._dp_validator.validate(dp, dp_index)
        if not validation_result.is_valid or validation_result.has_warnings:
            self.logger.warning(f"Validation issues for DP{dp_index}: {validation_result.get_summary()}")
            # Store validation issues in bus_model for later display
            self.bus_model.validation_issues.append((f"DP{dp_index}", validation_result))
        # Continue processing - don't return early (real hardware has no validators)

        self.logger.debug(f'Processing data port DP{dp_index}')

        # Reset data port state for fresh drawing pass
        dp.initialize()

        # The FCP lives on Interface, not DataPort. Engine drives its lifecycle
        # explicitly at the same moments the DP transitions.
        fcp = self.interface.flow_control_ports[dp_index]
        fcp.initialize()

        # Engine-owned per-DP transport counter — starts at 0, incremented
        # when the engine observes that a fresh initialize_transport just ran
        # in the DP (state snapshot matches post-reset values) and a DATA
        # or TX_PRESENT emission follows. In SRI mode a transport cut by a
        # row boundary is re-emitted from bit 0 on the next row; that is
        # the same logical transport, so the engine detects "resume" by
        # observing the old transport did not finish — fewer bits were
        # emitted than the transport pattern requires — and suppresses the
        # count bump.
        self._transport_counts[dp_index] = 0
        has_emitted_in_current_transport = False
        bits_emitted_in_current_transport = 0
        # Expected DATA/TX_PRESENT emissions per transport: every enabled
        # channel emits (SampleSize_REG + 1) DATA bits + 1 TX_PRESENT (in
        # TX_CONTROLLED / ASYNC flow modes), times (SG + 1) samples, each
        # held for (BitWidth_REG + 1) columns by the wide-bit counter.
        txp_slot_per_channel_sample = 1 if dp.config._txp_enabled else 0
        bits_per_transport = (
            dp.config._num_channels
            * (dp.config.SampleSize_REG + 1 + txp_slot_per_channel_sample)
            * (dp.config.SampleGrouping_REG + 1)
            * (dp.config.BitWidth_REG + 1)
        )

        # Get device number from interface's device assignments
        device = self.interface.get_dp_device(dp_index)

        # Process using clock_tick() - it auto-advances and handles wide bits, guards, tails
        total_bits = self.num_rows * self.interface.num_columns
        last_bit_was_driven = False
        last_slot_was_tail = False  # Track if previous slot was TAIL
        last_slot_was_guard = False  # Track if previous slot was GUARD

        num_cols = self.interface.num_columns

        dp._device._last_slot_per_port.clear()
        dp._device._current_slot = None

        for bit_num in range(total_bits):
            # Calculate position from iteration index
            row = bit_num // num_cols
            column = bit_num % num_cols

            # DP data path emits first
            device_obj = dp._device
            device_obj._active_port = dp
            device_obj._current_slot = None
            dp.clock_tick()
            bit_slot = device_obj._current_slot
            if bit_slot is None:
                bit_slot = BitSlotState(slot_type=SlotType.EMPTY)
            bit_slot.device_num = device
            bit_slot.dp_num = dp_index

            is_owned_data = (
                bit_slot.is_owned()
                and bit_slot.slot_type in (SlotType.DATA, SlotType.TX_PRESENT)
            )

            # A new transport emission occurred IFF the slot was built in
            # the fresh-transport state AND this slot actually emitted data.
            new_transport_emission = is_owned_data and bit_slot.fresh_transport

            if new_transport_emission:
                # Resume detection: in SRI, if the prior transport had emitted
                # some bits but did not finish (fewer than bits_per_transport),
                # the row boundary cut it and this "new" emission is the same
                # logical transport re-started from bit 0. Don't bump the count.
                is_sri_resume = (
                    dp.config.SubRowInterval_REG
                    and has_emitted_in_current_transport
                    and 0 < bits_emitted_in_current_transport < bits_per_transport
                )
                if not is_sri_resume:
                    self._transport_counts[dp_index] += 1
                has_emitted_in_current_transport = True
                bits_emitted_in_current_transport = 0

            if is_owned_data:
                bits_emitted_in_current_transport += 1

            transport_index_at_emit = self._transport_counts[dp_index]

            # FCP emits as an independent parallel source. Any DP+FCP collision
            # is surfaced by the bus model's SAME_DEVICE clash detector (no
            # arbitration in the core loop).
            device_obj._active_port = fcp
            device_obj._current_slot = None
            fcp.clock_tick()
            fcp_slot = device_obj._current_slot
            if fcp_slot is None:
                fcp_slot = BitSlotState(slot_type=SlotType.EMPTY)
            fcp_slot.device_num = device
            fcp_slot.dp_num = dp_index

            # Dispatch DP slot to bus model (with handover tracking below)
            # Track tail_drawn across both paths for handover semantics.
            tail_drawn_any = False

            if not bit_slot.is_owned():
                dp_emitted = False
            else:
                dp_emitted = True
                if bit_slot.slot_type == SlotType.TAIL:
                    tail_drawn_any = self._add_tail_bit(row, column, dp_index, dp, device, bit_slot) or tail_drawn_any
                elif bit_slot.slot_type in (SlotType.GUARD_0, SlotType.GUARD_1):
                    self._add_guard_bit(row, column, dp_index, dp, device, bit_slot)
                else:
                    # Data bit (DATA, TX_PRESENT)
                    self._add_data_bit(row, column, dp_index, dp, device, bit_slot, transport_index_at_emit)

            # Dispatch FCP slot to bus model (separate write — clash detector
            # surfaces any overlap with the DP's emission as SAME_DEVICE).
            if fcp_slot.slot_type != SlotType.EMPTY:
                if fcp_slot.slot_type == SlotType.TAIL:
                    tail_drawn_any = self._add_tail_bit(row, column, dp_index, dp, device, fcp_slot) or tail_drawn_any
                elif fcp_slot.slot_type in (SlotType.GUARD_0, SlotType.GUARD_1):
                    self._add_guard_bit(row, column, dp_index, dp, device, fcp_slot)
                elif fcp_slot.slot_type == SlotType.DRQ:
                    self._add_data_bit(row, column, dp_index, dp, device, fcp_slot, transport_index_at_emit)

            # Handover tracking — use the emitted slot. In correct configs DP
            # and FCP are mutually exclusive at any column, so whichever is
            # non-EMPTY is "the" emission for handover purposes.
            effective_slot = fcp_slot if (fcp_slot.slot_type != SlotType.EMPTY and not dp_emitted) else bit_slot

            # Note: Don't reset flags at row boundaries - handovers should
            # appear after the last driven bit regardless of row position

            if not effective_slot.is_owned():
                # No data at this position - check for potential handover
                # Priority: tail > guard > data bit
                # - If tails exist: record after TAIL
                # - Else if guard exists: record after GUARD
                # - Else: record after data BIT
                has_tail = dp.config.TailWidth_REG > 0
                has_guard = dp.config.GuardEnable_REG
                should_record = (
                    (has_tail and last_slot_was_tail) or
                    (not has_tail and has_guard and last_slot_was_guard) or
                    (not has_tail and not has_guard and last_bit_was_driven)
                )
                if should_record and not dp.config.PortDirection_REG and dp_viz.enable_handover:
                    # Track this potential handover for post-processing
                    self._potential_handovers.append((row, column, dp_index))
                last_bit_was_driven = False
                last_slot_was_tail = False
                last_slot_was_guard = False
            elif effective_slot.slot_type == SlotType.TAIL:
                last_bit_was_driven = True
                last_slot_was_tail = tail_drawn_any  # Only mark for handover if tail was actually drawn
                last_slot_was_guard = False
            elif effective_slot.slot_type in (SlotType.GUARD_0, SlotType.GUARD_1):
                last_bit_was_driven = True
                last_slot_was_tail = False
                last_slot_was_guard = True  # Mark for handover if no tail
            else:
                # Data bit (DATA, TX_PRESENT, or DRQ). Only SOURCE-direction
                # slots drive the bus; SINK slots sample and shouldn't arm
                # a handover on the next non-owned tick.
                last_bit_was_driven = (effective_slot.direction == DirectionType.SOURCE)
                last_slot_was_tail = False
                last_slot_was_guard = False

    def _add_data_bit(self, row: int, column: int, dp_index: int, dp: DataPort,
                      device: int, bit_slot, transport_index_at_emit: int) -> None:
        """Add a data bit to the bus model.

        Args:
            row: Row number
            column: Column number
            dp_index: Data port index (0-11)
            dp: Data port
            device: Device number
            bit_slot: Bit slot state
            transport_index_at_emit: Engine-owned running count of transports
                that have emitted at least one slot for this DP, up to and
                including the transport that owns this slot. Used to
                reconstruct the global/absolute sample ordinal externally —
                the DataPort hardware model has no cross-interval counter.
        """
        bit_index = self.bus_model.bit_index(row, column)

        # For DRQ bits, use the direction from bit_slot (opposite to data direction)
        # For other bits, derive direction from port direction
        if bit_slot.slot_type == SlotType.DRQ:
            direction = bit_slot.direction
        else:
            direction = DirectionType.SINK if dp.config.PortDirection_REG else DirectionType.SOURCE

        # Get sample, channel, and bit info.
        # DataPort emits only the transport-scoped sample_in_group (0..SG).
        # The absolute/global sample ordinal is reconstructed here using
        # the engine-owned per-DP transport count — DataPort itself has no
        # cross-interval sample counter (hardware-accurate).
        if bit_slot.data:
            sample_base = max(0, transport_index_at_emit - 1) * (dp.config.SampleGrouping_REG + 1)
            global_sample = sample_base + bit_slot.data.sample_in_group
            channel = bit_slot.data.channel
            bit_in_sample = bit_slot.data.bit
        else:
            global_sample = 0
            channel = 0
            bit_in_sample = 0

        # Check for clashes
        clash_type = ClashType.NONE
        if not dp.config.PortDirection_REG:
            has_clash, _, suppress_slot = self.clash_detector.check_write_clash(row, column, device)
            if has_clash:
                clash_type = self._get_clash_type(bit_index)
            # If a same-device guard or tail needs to be suppressed, remove it from bus_model
            if suppress_slot == 'guard':
                # Remove both GUARD_0 and GUARD_1 (we don't know which polarity)
                self.bus_model.remove_bits_matching(bit_index, device, SlotType.GUARD_0)
                self.bus_model.remove_bits_matching(bit_index, device, SlotType.GUARD_1)
            elif suppress_slot == 'tail':
                self.bus_model.remove_bits_matching(bit_index, device, SlotType.TAIL)
            self.clash_detector.add_write(row, column, device)
        else:
            has_clash = self.clash_detector.check_read_clash(row, column, device)
            # Read clashes don't affect bus model clash type

        # Handle TxP tracking for flow control modes
        # TxP direction matches data port direction
        slot_type = bit_slot.slot_type
        if slot_type == SlotType.TX_PRESENT:
            if bit_slot.direction == DirectionType.SOURCE:
                self.clash_detector.add_txp_source(row, column, device)
            else:
                self.clash_detector.add_txp_sink(row, column, device)

        # Handle DRQ tracking for flow control modes
        # DRQ direction is OPPOSITE to data port direction:
        # - Sink data ports (PortDirection=True) SEND DRQ (SOURCE)
        # - Source data ports (PortDirection=False) RECEIVE DRQ (SINK)
        if slot_type == SlotType.DRQ:
            if bit_slot.direction == DirectionType.SOURCE:
                # Validate pairing only at the last UI of the wide DRQ — the
                # sink is sparse and only emits at the last UI, so earlier
                # source UIs have no matching sink by design.
                fcp_config = self.interface.flow_control_ports[dp_index].config
                last_ui_column = fcp_config.FCP_HorizontalStart_REG + fcp_config.FCP_BitWidth_REG
                self.clash_detector.add_drq_source(
                    row, column, device,
                    for_validation=(column == last_ui_column),
                )
            else:
                self.clash_detector.add_drq_sink(row, column, device)

        # Get display_fields from viz config
        dp_viz = self.viz_config.data_ports[dp_index]

        bit_info = BitInfo(
            bit_index=bit_index,
            slot=slot_type,
            direction=direction,
            device=device,
            dp=dp_index,
            channel=channel,
            sample=global_sample,
            bit=bit_in_sample,
            clash=clash_type,
            display_fields=dp_viz.display_fields,  # From viz config
            port_mode=dp.config.PortMode_REG,  # For test mode display
            scrambler_enabled=dp.config.ScramblerEn_REG,  # For scrambler indicator
        )
        self.bus_model.add_bit(bit_info)

    def _add_guard_bit(self, row: int, column: int, dp_index: int, dp: DataPort,
                       device: int, bit_slot) -> None:
        """Add a guard bit to the bus model."""
        bit_index = self.bus_model.bit_index(row, column)
        # Use direction from bit_slot (guards are always SOURCE)
        direction = bit_slot.direction

        # Check for clashes
        has_clash, should_draw, suppress_tail = self.clash_detector.check_guard_clash(row, column, device)
        clash_type = self._get_clash_type(bit_index) if has_clash else ClashType.NONE

        # If a same-device tail should be suppressed, remove it from bus_model
        if suppress_tail:
            self.bus_model.remove_bits_matching(bit_index, device, SlotType.TAIL)

        # Draw guard only when explicitly told to (should_draw=1)
        # Guard is suppressed when: same-device data bit has priority, or clash detected
        if should_draw:
            bit_info = BitInfo(
                bit_index=bit_index,
                slot=bit_slot.slot_type,
                direction=direction,
                device=device,
                dp=dp_index,
                clash=clash_type,
            )
            self.bus_model.add_bit(bit_info)

    def _add_tail_bit(self, row: int, column: int, dp_index: int, dp: DataPort,
                      device: int, bit_slot) -> bool:
        """Add a tail bit to the bus model.

        Returns:
            True if the tail was actually drawn, False if suppressed due to clash.
        """
        bit_index = self.bus_model.bit_index(row, column)
        # Use direction from bit_slot (tails are always SOURCE)
        direction = bit_slot.direction

        # Check for clashes
        has_clash, should_draw = self.clash_detector.check_tail_clash(row, column, device)
        clash_type = self._get_clash_type(bit_index) if has_clash else ClashType.NONE

        if should_draw:
            bit_info = BitInfo(
                bit_index=bit_index,
                slot=SlotType.TAIL,
                direction=direction,
                device=device,
                dp=dp_index,
                clash=clash_type,
            )
            self.bus_model.add_bit(bit_info)
            return True
        return False

    def _generate_viz_handovers(self) -> None:
        """Generate visualizer handover indicators (post-processing pass).

        Handovers are visualizer aids showing where data flow changes direction.
        They appear in the NEXT slot after driven bits of a SOURCE data port.

        Rules:
        1. NO skipping - handover goes at the exact recorded position
        2. If position is occupied:
           - Manager data port at S0/S1: S0/S1 takes priority (same device), no handover
           - Non-manager data port at S0/S1: place with DIFFERENT_DEVICE clash
           - Other clashes: place with appropriate clash type
        3. If position is empty: place without clash

        Uses SpecialDevices.VISUALIZER since handovers aren't physical bus traffic.
        """
        total_bits = self.num_rows * self.interface.num_columns

        # Iterate every recorded potential handover (no filtering).
        for start_row, start_column, dp_number in self._potential_handovers:
            bit_index = self.bus_model.bit_index(start_row, start_column)

            if bit_index >= total_bits:
                continue  # Don't place past end of frame

            # Check if this position is already occupied
            existing_bits = self.bus_model.get_bits_at(bit_index)

            if existing_bits:
                # Check what types of bits are at this position
                non_handover_bits = [b for b in existing_bits if b.slot != SlotType.HANDOVER]

                # Handovers don't clash with other handovers
                if not non_handover_bits:
                    # Only handovers at this position - place without clash
                    bit_info = BitInfo(
                        bit_index=bit_index,
                        slot=SlotType.HANDOVER,
                        direction=DirectionType.SOURCE,
                        device=SpecialDevices.VISUALIZER,
                        dp=dp_number,
                        clash=ClashType.NONE,
                    )
                    self.bus_model.add_bit(bit_info)
                    continue

                # There are non-handover bits - check clash rules
                dp_device = self.interface.get_dp_device(dp_number)

                # Check if ANY existing bit is from the same device
                # Handovers are visualizer-only aids, so they should be suppressed
                # (not placed at all) when colliding with real data from same device
                clashing_with_same_device = any(
                    b.device == dp_device for b in non_handover_bits
                )

                if clashing_with_same_device:
                    # Same device rule: real data takes priority, no handover placed
                    # No clash recorded because handovers are visualizer-only
                    continue

                # Different device clash - record properly with clash details
                # Get device of first non-handover bit for clash record
                existing_bit = non_handover_bits[0]
                self.clash_detector.record_clash(
                    bit_slot=bit_index,
                    category=SlotClashCategory.WRITE_CLASH,
                    device_a=existing_bit.device,
                    device_b=dp_device,
                    slot_type_a=existing_bit.slot.name,
                    slot_type_b="HANDOVER"
                )
                clash_type = ClashType.DIFFERENT_DEVICE

                bit_info = BitInfo(
                    bit_index=bit_index,
                    slot=SlotType.HANDOVER,
                    direction=DirectionType.SOURCE,
                    device=SpecialDevices.VISUALIZER,
                    dp=dp_number,
                    clash=clash_type,
                )
                self.bus_model.add_bit(bit_info)
                continue

            # Position is empty - place handover without clash
            bit_info = BitInfo(
                bit_index=bit_index,
                slot=SlotType.HANDOVER,
                direction=DirectionType.SOURCE,
                device=SpecialDevices.VISUALIZER,
                dp=dp_number,
                clash=ClashType.NONE,
            )
            self.bus_model.add_bit(bit_info)

    def _group_bits_by_index(self) -> dict:
        """Group all bits in the bus model by their bit_index.

        Returns:
            Dictionary mapping bit_index to list of BitInfo objects at that position.
        """
        bits_by_index: dict = {}
        for bit in self.bus_model.bits:
            if bit.bit_index not in bits_by_index:
                bits_by_index[bit.bit_index] = []
            bits_by_index[bit.bit_index].append(bit)
        return bits_by_index

    def _get_clash_type(self, bit_index: int) -> ClashType:
        """Get the clash type for a bit position based on clash detector state.

        Args:
            bit_index: The bit index to check

        Returns:
            ClashType.SAME_DEVICE, ClashType.DIFFERENT_DEVICE, or ClashType.NONE
        """
        if bit_index in self.clash_detector.same_device_clashes:
            return ClashType.SAME_DEVICE
        if bit_index in self.clash_detector.different_device_clashes:
            return ClashType.DIFFERENT_DEVICE
        return ClashType.NONE

    def _finalize_clashes(self) -> None:
        """Transfer clash information from detector to bus model."""
        # Bus clashes (different device)
        self.bus_model.bus_clashes = self.clash_detector.get_different_device_clashes()

        # Device clashes (same device)
        self.bus_model.device_clashes = self.clash_detector.get_same_device_clashes()

        # Read overlaps
        self.bus_model.read_overlaps = self.clash_detector.get_read_clashes()

        # Detailed clash info (with device info for notifications panel)
        self.bus_model.clash_details = self.clash_detector.get_clash_details()

        # TxP/DRQ flow control mismatches
        self.bus_model.txp_mismatches = self.clash_detector.get_txp_mismatches()
        self.bus_model.txp_orphan_sinks = self.clash_detector.get_txp_orphan_sinks()
        self.bus_model.drq_mismatches = self.clash_detector.get_drq_mismatches()
        self.bus_model.drq_orphan_sinks = self.clash_detector.get_drq_orphan_sinks()

        # Update BitInfo.clash for all bits at clashed positions
        # (system slots may have been created before clash was detected)
        for bit_index in self.bus_model.bus_clashes:
            bits_at_position = self.bus_model.get_bits_at(bit_index)
            for bit in bits_at_position:
                if bit.clash == ClashType.NONE:
                    bit.clash = ClashType.DIFFERENT_DEVICE

        for bit_index in self.bus_model.device_clashes:
            bits_at_position = self.bus_model.get_bits_at(bit_index)
            for bit in bits_at_position:
                if bit.clash == ClashType.NONE:
                    bit.clash = ClashType.SAME_DEVICE

    def _detect_scrambler_mismatches(self) -> None:
        """Detect scrambler setting mismatches between source and sink data ports.

        A mismatch occurs when a source data port with scrambler enabled writes
        to a bit position that a sink data port without scrambler reads, or vice versa.
        """
        bits_by_index = self._group_bits_by_index()

        # Check each position for source/sink scrambler mismatches
        for bit_index, bits in bits_by_index.items():
            # Find source and sink bits at this position (only DATA data bits)
            sources = [b for b in bits if b.direction == DirectionType.SOURCE
                       and b.dp is not None and b.slot == SlotType.DATA]
            sinks = [b for b in bits if b.direction == DirectionType.SINK
                     and b.dp is not None and b.slot == SlotType.DATA]

            # Check for mismatches between each source/sink pair
            for source in sources:
                for sink in sinks:
                    if source.scrambler_enabled != sink.scrambler_enabled:
                        # Mismatch found: (bit_index, source_dp, sink_dp)
                        mismatch = (bit_index, source.dp, sink.dp)
                        if mismatch not in self.bus_model.scrambler_mismatches:
                            self.bus_model.scrambler_mismatches.append(mismatch)
                            self.logger.debug(
                                f'Scrambler mismatch at bit {bit_index}: '
                                f'DP{source.dp} (scrambler={source.scrambler_enabled}) -> '
                                f'DP{sink.dp} (scrambler={sink.scrambler_enabled})'
                            )

    def _detect_test_mode_mismatches(self) -> None:
        """Detect test mode mismatches between data ports at the same bit position.

        A mismatch occurs when:
        - One data port is in test mode (test ones or test zeros) while another is in normal mode
        - Two data ports are in different test modes (test ones vs test zeros)

        The user specified: "test mode bits in the same bit slot as not test mode bits"
        and "The test mode need to match also i.e 1 vs 1, 0 vs 0"
        """
        bits_by_index = self._group_bits_by_index()

        # Check each position for test mode mismatches
        for bit_index, bits in bits_by_index.items():
            # Get all data port bits at this position (only DATA data bits)
            dp_bits = [b for b in bits if b.dp is not None and b.slot == SlotType.DATA]

            if len(dp_bits) < 2:
                continue

            # Compare all pairs for test mode mismatches
            seen_pairs = set()
            for i, bit1 in enumerate(dp_bits):
                for bit2 in dp_bits[i + 1:]:
                    # Create canonical pair key (smaller dp first) to avoid duplicates
                    pair_key = (min(bit1.dp, bit2.dp), max(bit1.dp, bit2.dp))
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)

                    # Check if port modes differ
                    if bit1.port_mode != bit2.port_mode:
                        # Mismatch found: (bit_index, (dp1, mode1), (dp2, mode2))
                        mismatch = (bit_index, (bit1.dp, bit1.port_mode), (bit2.dp, bit2.port_mode))
                        if mismatch not in self.bus_model.test_mode_mismatches:
                            self.bus_model.test_mode_mismatches.append(mismatch)
                            self.logger.debug(
                                f'Test mode mismatch at bit {bit_index}: '
                                f'DP{bit1.dp} (mode={bit1.port_mode}) vs '
                                f'DP{bit2.dp} (mode={bit2.port_mode})'
                            )

    def _detect_truncated_drq(self) -> None:
        """Detect DRQ configurations where the wide bit can't fit in a row.

        A wide DRQ must complete within a single row: the last UI is at column
        FCP_HorizontalStart_REG + FCP_BitWidth_REG. If that falls past the row
        end, the replay dies on row-wrap without ever emitting read_drq, so no
        flow-control handshake ever completes. Flagged as a configuration error.
        """
        num_columns = self.interface.num_columns
        for dp_index, dp in enumerate(self.interface.data_ports):
            if not dp.config._drq_enabled:
                continue
            fcp_config = self.interface.flow_control_ports[dp_index].config
            last_ui_column = fcp_config.FCP_HorizontalStart_REG + fcp_config.FCP_BitWidth_REG
            if last_ui_column >= num_columns:
                warning = (f"DP{dp_index}", last_ui_column, num_columns)
                self.bus_model.drq_truncation_warnings.append(warning)
                self.logger.warning(
                    f'DRQ truncation: DP{dp_index} wide DRQ last UI at column '
                    f'{last_ui_column} falls past row end ({num_columns} columns)'
                )

    def _detect_interval_overflow(self) -> None:
        """Detect data ports whose bits don't fit within their configured interval.

        Distinguishes between two types of truncation:
        1. Interval overflow: Data doesn't fit even with full interval displayed
        2. Display truncation: Data would fit if more rows were displayed

        Compares expected bits per interval against actual bits placed in the
        bus model. This is simpler and more reliable than trying to predict
        overflow from configuration parameters.
        """
        from src.models.enums import FlowMode, SlotType

        for dp_index, dp in enumerate(self.interface.data_ports):
            if not self.viz_config.data_ports[dp_index].enabled:
                continue

            # Use popcount _num_channels property for performance
            num_channels = dp.config._num_channels
            if num_channels == 0:
                continue

            # Calculate expected bits per interval
            sample_size = dp.config.SampleSize_REG + 1  # Excess-1 encoding
            sample_grouping = dp.config.SampleGrouping_REG + 1  # Excess-1 encoding

            # Bits per channel (includes TxP if applicable)
            bits_per_channel = sample_size
            if dp.config.FlowMode_REG in (FlowMode.TX_CONTROLLED, FlowMode.ASYNC):
                bits_per_channel += 1  # TxP bit per channel

            expected_bits = bits_per_channel * num_channels * sample_grouping

            # Count actual data bits placed in first interval
            # Interval spans rows 0 to Interval_REG (inclusive)
            interval_rows = dp.config.Interval_REG + 1
            interval_end_bit = interval_rows * self.interface.num_columns

            actual_bits = 0
            for bit_info in self.bus_model.bits:
                if bit_info.dp == dp_index and bit_info.bit_index < interval_end_bit:
                    # Count DATA data bits and TX_PRESENT bits
                    if bit_info.slot in (SlotType.DATA, SlotType.TX_PRESENT):
                        actual_bits += 1

            if actual_bits < expected_bits:
                # Determine if it's interval overflow or display truncation
                if self.num_rows >= interval_rows:
                    # Full interval is displayed, but data still doesn't fit
                    # This is true interval overflow (configuration error)
                    warning = (f"DP{dp_index}", expected_bits, actual_bits)
                    if warning not in self.bus_model.interval_overflow_warnings:
                        self.bus_model.interval_overflow_warnings.append(warning)
                        self.logger.warning(
                            f'Interval overflow: DP{dp_index} expected {expected_bits} bits '
                            f'but only {actual_bits} placed in interval'
                        )
                else:
                    # Not enough rows displayed to see full interval
                    # This is display truncation (user can increase RowsToDraw)
                    warning = (f"DP{dp_index}", interval_rows, self.num_rows)
                    if warning not in self.bus_model.display_truncation_warnings:
                        self.bus_model.display_truncation_warnings.append(warning)
                        self.logger.info(
                            f'Display truncation: DP{dp_index} interval is {interval_rows} rows '
                            f'but only {self.num_rows} rows displayed'
                        )

    def _detect_sample_bit_mismatches(self) -> None:
        """Detect sample/bit number mismatches between source and sink at same bit slot.

        When a source writes to a bit position and a sink reads from the same position,
        they should have matching sample and bit numbers (channels may differ).
        A mismatch indicates a configuration error.
        """
        bits_by_index = self._group_bits_by_index()

        # Check each position for source/sink sample/bit mismatches
        for bit_index, bits in bits_by_index.items():
            # Find source and sink DATA data bits at this position
            sources = [b for b in bits if b.direction == DirectionType.SOURCE
                       and b.dp is not None and b.slot == SlotType.DATA]
            sinks = [b for b in bits if b.direction == DirectionType.SINK
                     and b.dp is not None and b.slot == SlotType.DATA]

            # Check for mismatches between each source/sink pair
            for source in sources:
                for sink in sinks:
                    # Check sample number mismatch
                    if source.sample != sink.sample or source.bit != sink.bit:
                        mismatch = (
                            bit_index,
                            source.dp, source.sample, source.bit,
                            sink.dp, sink.sample, sink.bit
                        )
                        if mismatch not in self.bus_model.sample_bit_mismatches:
                            self.bus_model.sample_bit_mismatches.append(mismatch)
                            self.logger.warning(
                                f'Sample/bit mismatch at bit {bit_index}: '
                                f'DP{source.dp} (s{source.sample}b{source.bit}) vs '
                                f'DP{sink.dp} (s{sink.sample}b{sink.bit})'
                            )

    def _detect_sink_handover_warnings(self) -> None:
        """Detect sink dataports with EnableHandover but no FCP bits to write.

        A sink dataport (PortDirection=True) that has EnableHandover=True doesn't
        make sense unless it's in RX_CONTROLLED or ASYNC flow mode, because
        sinks read data and can't write handovers. However, in RX_CONTROLLED or
        ASYNC modes, sinks send DRQ bits which ARE writes, so handovers make sense.
        """
        from src.models.enums import FlowMode

        for dp_index, dp in enumerate(self.interface.data_ports):
            dp_viz = self.viz_config.data_ports[dp_index]
            if not dp_viz.enabled:
                continue

            # Check if it's a sink (PortDirection_REG = True means SINK)
            if not dp.config.PortDirection_REG:
                continue  # Source dataports can have handovers

            # Check if EnableHandover is set
            if not dp_viz.enable_handover:
                continue  # No handover enabled, no warning needed

            # Check if it's in a flow mode that sends DRQ bits
            # RX_CONTROLLED and ASYNC modes send DRQ bits (which are writes)
            if dp.config.FlowMode_REG in (FlowMode.RX_CONTROLLED, FlowMode.ASYNC):
                continue  # DRQ bits are writes, handovers make sense

            # Sink with handover enabled but no FCP/DRQ bits - issue warning
            warning = (f"DP{dp_index}", dp_index)
            if warning not in self.bus_model.sink_handover_warnings:
                self.bus_model.sink_handover_warnings.append(warning)
                self.logger.warning(
                    f'Sink dataport DP{dp_index} has EnableHandover '
                    f'but is not in RX_CONTROLLED or ASYNC flow mode - handover will not be drawn'
                )

    def _detect_enabled_no_channels_warnings(self) -> None:
        """Detect data ports with EnableDataPort=True but NumChannels=0.

        This is a common configuration mistake where the user enables drawing
        but hasn't configured any channels. The data port will appear to do
        nothing, which can confuse new users.
        """
        for dp_index, dp in enumerate(self.interface.data_ports):
            if not self.viz_config.data_ports[dp_index].enabled:
                continue  # Not enabled, no warning needed

            # Use popcount _num_channels property for performance
            if dp.config._num_channels > 0:
                continue  # Has channels, no warning needed

            # Enabled but no channels - issue warning
            warning = (f"DP{dp_index}", dp_index)
            if warning not in self.bus_model.enabled_no_channels_warnings:
                self.bus_model.enabled_no_channels_warnings.append(warning)
                self.logger.warning(
                    f'Data port DP{dp_index} has EnableDataPort=True but NumChannels=0 - '
                    f'nothing will be drawn'
                )
