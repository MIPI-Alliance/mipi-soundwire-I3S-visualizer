"""Frame renderer for SoundWire I3S visualizer.

This module provides a pure rendering layer that takes a pre-built BusModel
and draws it to a tkinter Canvas. No business logic or model building happens here.

The rendering flow:
1. Main app: Load CSV → Interface
2. Main app: Build BusModel using BusModelBuilder
3. Main app: Call renderer.render(bus_model, canvas, config)

All intelligence is in the BusModel - the renderer just draws what it's told.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, List

from src.models.bus_model import BusModel, BitInfo
from src.models.enums import PortMode, SlotType
from src.config import SlotTypeStrings, Colors
from src.ui.constants import ROW_SIZE, COLUMN_SIZE, FRAME_Y_OFFSET, APP_FONT, HEADER_COLUMN_SIZE, HEADING_EXTRA_HEIGHT

if TYPE_CHECKING:
    from tkinter import Canvas


@dataclass
class RenderConfig:
    """Configuration for frame rendering.

    Attributes:
        text_size: Font size for canvas text
        text_color: Color for text labels
        background_color: Background color for canvas
        line_color: Color for grid lines
        dp_colors: List of colors for data ports
        settings_visible: Whether settings panel is visible (affects header layout)
    """
    text_size: int = 10
    text_color: str = '#000000'
    background_color: str = '#e0e0e0'
    line_color: str = '#707070'
    dp_colors: List[str] = field(default_factory=lambda: list(Colors.DP_COLORS))
    settings_visible: bool = True


class FrameRenderer:
    """Renders a pre-built BusModel to a tkinter Canvas.

    This class is a pure rendering layer with NO business logic.
    All intelligence (clash detection, bit placement, etc.) is in the BusModel
    which must be built before calling render().

    Example:
        # Build the model (all intelligence here)
        builder = BusModelBuilder(interface, num_rows)
        bus_model = builder.build()

        # Render the model (no intelligence, just drawing)
        renderer = FrameRenderer()
        renderer.render(bus_model, canvas, config)
    """

    def __init__(self):
        """Initialize the frame renderer."""
        self._canvas: Optional['Canvas'] = None
        self._config: Optional[RenderConfig] = None

    def render(self, bus_model: BusModel, canvas: 'Canvas',
               config: Optional[RenderConfig] = None,
               heading_canvas: Optional['Canvas'] = None) -> None:
        """Render a BusModel to the canvas.

        This method is a pure renderer - it reads the BusModel and draws.
        No model building, validation, or business logic happens here.

        Args:
            bus_model: Pre-built BusModel containing all bit information
            canvas: tkinter Canvas widget for main frame
            config: Optional render configuration (uses defaults if None)
            heading_canvas: Optional canvas for column headers
        """
        import tkinter as tk

        # Store rendering context
        self._canvas = canvas
        self._config = config or RenderConfig()

        # Clear canvases
        canvas.delete(tk.ALL)
        if heading_canvas:
            heading_canvas.delete(tk.ALL)

        # Calculate canvas dimensions
        canvas_width = int((bus_model.num_columns + 3.5) * COLUMN_SIZE)

        # Configure scroll region - start at ROW_SIZE * 2 to show top grid line
        canvas.config(
            scrollregion=(0, ROW_SIZE * 2 + FRAME_Y_OFFSET, canvas_width, (bus_model.num_rows + 3.5) * ROW_SIZE),
            bg=self._config.background_color
        )

        # Draw background - extend to full scroll region for SVG export
        canvas.create_rectangle(
            0, 0, canvas_width, (bus_model.num_rows + 3.5) * ROW_SIZE,
            width=0, fill=self._config.background_color
        )

        # Draw frame grid
        self._draw_grid(canvas, bus_model, heading_canvas)

        # Draw color key
        self._draw_color_key(canvas, bus_model)

        # Draw all bits from bus model
        self._draw_bits(canvas, bus_model)

        # Draw scrambler indicators
        self._draw_scrambler_indicators(canvas, bus_model)

        # Draw clash indicators (positions come from bus_model)
        self._draw_clash_indicators(canvas, bus_model)

    def _draw_grid(self, canvas: 'Canvas', bus_model: BusModel,
                   heading_canvas: Optional['Canvas']) -> None:
        """Draw the frame grid (rows, columns, headers)."""
        assert self._config is not None
        num_columns = bus_model.num_columns
        num_rows = bus_model.num_rows

        # Grid rectangle outline
        canvas.create_rectangle(
            COLUMN_SIZE * 1.5, ROW_SIZE * 2 + FRAME_Y_OFFSET,
            (num_columns + 1.5) * COLUMN_SIZE,
            (num_rows + 2) * ROW_SIZE + FRAME_Y_OFFSET,
            outline=self._config.line_color, width=1
        )

        # Key column vertical line
        key_right_x = (num_columns + 2.5) * COLUMN_SIZE + 9
        canvas.create_line(
            key_right_x, ROW_SIZE * 2 + FRAME_Y_OFFSET,
            key_right_x, (num_rows + 2) * ROW_SIZE + FRAME_Y_OFFSET,
            fill=self._config.line_color, width=1
        )

        # Column headers
        # When settings are hidden, the heading canvas is taller with button at top
        heading_y_offset = 0 if self._config.settings_visible else HEADING_EXTRA_HEIGHT
        for column in range(num_columns):
            canvas.create_text(
                (column + 2) * COLUMN_SIZE, ROW_SIZE * 1.5 + FRAME_Y_OFFSET,
                text=str(column), font=(APP_FONT, self._config.text_size),
                fill=self._config.text_color
            )
            if heading_canvas:
                heading_canvas.create_text(
                    (column + 2) * HEADER_COLUMN_SIZE, ROW_SIZE * 0.5 + heading_y_offset,
                    text=str(column), font=(APP_FONT, self._config.text_size),
                    fill=self._config.text_color
                )

        # Row headers and grid lines
        for row in range(num_rows):
            # Row number
            canvas.create_text(
                1.25 * COLUMN_SIZE, (row + 2.5) * ROW_SIZE + FRAME_Y_OFFSET,
                text=str(row), anchor='e',
                font=(APP_FONT, self._config.text_size),
                fill=self._config.text_color
            )

        # Row lines and source/sink key
        for count in range(1, num_rows + 1):
            canvas.create_line(
                COLUMN_SIZE * 1.5, ROW_SIZE * (count + 1) + FRAME_Y_OFFSET,
                (num_columns + 2.5) * COLUMN_SIZE + 9,
                ROW_SIZE * (count + 1) + FRAME_Y_OFFSET,
                fill=self._config.line_color, width=1
            )
            # Source/Sink key
            canvas.create_text(
                (num_columns + 2) * COLUMN_SIZE + 6,
                (count + 1.2) * ROW_SIZE + FRAME_Y_OFFSET + 1,
                font=(APP_FONT, self._config.text_size - 3),
                text='Source', fill=self._config.text_color
            )
            canvas.create_line(
                (num_columns + 1.5) * COLUMN_SIZE,
                ROW_SIZE * (count + 1.5) - 1 + FRAME_Y_OFFSET + 1,
                (num_columns + 2.5) * COLUMN_SIZE + 9,
                ROW_SIZE * (count + 1.5) - 1 + FRAME_Y_OFFSET + 1,
                fill=self._config.line_color
            )
            canvas.create_text(
                (num_columns + 2) * COLUMN_SIZE + 6,
                (count + 1.7) * ROW_SIZE + FRAME_Y_OFFSET + 1,
                font=(APP_FONT, self._config.text_size - 3),
                text='Sink', fill=self._config.text_color
            )

        # Bottom line
        canvas.create_line(
            COLUMN_SIZE * 1.5, ROW_SIZE * (num_rows + 2) + FRAME_Y_OFFSET,
            (num_columns + 2.5) * COLUMN_SIZE + 9,
            ROW_SIZE * (num_rows + 2) + FRAME_Y_OFFSET,
            fill=self._config.line_color, width=1
        )

    def _draw_color_key(self, canvas: 'Canvas', bus_model: BusModel) -> None:
        """Draw data port color key at the top of the frame."""
        assert self._config is not None
        # Get unique data port numbers from the bus model
        dp_numbers = sorted(set(bit.dp for bit in bus_model.bits if bit.dp is not None))

        if not dp_numbers:
            return

        # Draw key rectangles ABOVE column headers (which are at y = ROW_SIZE * 1.5)
        # Position key from y=0 to y=ROW_SIZE*0.9 to leave gap before column headers
        key_y1 = FRAME_Y_OFFSET
        key_y2 = ROW_SIZE * 0.9 + FRAME_Y_OFFSET
        key_width = 1.5  # columns per key entry (25% smaller than original 2)

        # Center the color key horizontally within the frame
        # Frame data area spans from column 1.5 to (num_columns + 1.5)
        total_key_columns = len(dp_numbers) * key_width
        frame_columns = bus_model.num_columns
        # Calculate starting offset to center keys within the frame
        center_offset = 1.5 + (frame_columns - total_key_columns) / 2

        for i, dp_num in enumerate(dp_numbers):
            color = self._config.dp_colors[dp_num % len(self._config.dp_colors)]
            x1 = (i * key_width + center_offset) * COLUMN_SIZE
            x2 = ((i + 1) * key_width + center_offset) * COLUMN_SIZE

            # Draw colored rectangle
            canvas.create_rectangle(x1, key_y1, x2, key_y2, fill=color, outline=self._config.line_color)

            # Draw label
            canvas.create_text(
                (x1 + x2) / 2, (key_y1 + key_y2) / 2,
                text=f"DP{dp_num}",
                font=(APP_FONT, self._config.text_size - 2),
                fill='black'
            )

    # =========================================================================
    # Bit Processing Helpers - Extract complex merging logic from _draw_bits()
    # =========================================================================

    def _count_consecutive_slots(self, row_bits: List['BitInfo'], start_idx: int,
                                  column: int, slot_type: 'SlotType',
                                  match_dp: bool = False, match_device: bool = False) -> int:
        """Count consecutive slots of the same type, skipping clash duplicates.

        Args:
            row_bits: List of bits in this row, sorted by column
            start_idx: Starting index in row_bits
            column: Starting column number
            slot_type: Slot type to match (or use bit.slot if matching same type)
            match_dp: Also require same data port
            match_device: Also require same device

        Returns:
            Number of consecutive matching slots found
        """
        from src.models.enums import SlotType
        bit = row_bits[start_idx]
        count = 1
        j = start_idx + 1

        while j < len(row_bits):
            next_bit = row_bits[j]
            expected_column = column + count

            if next_bit.column < expected_column:
                # Skip bits at columns we've already processed (clash duplicates)
                j += 1
            elif next_bit.column == expected_column and next_bit.slot == slot_type:
                # Check additional match requirements
                if match_dp and next_bit.dp != bit.dp:
                    if j + 1 < len(row_bits) and row_bits[j + 1].column == expected_column:
                        j += 1
                        continue
                    break
                if match_device and next_bit.device != bit.device:
                    if j + 1 < len(row_bits) and row_bits[j + 1].column == expected_column:
                        j += 1
                        continue
                    break
                count += 1
                j += 1
            elif next_bit.column > expected_column:
                # Passed expected column without finding match
                break
            elif j + 1 < len(row_bits) and row_bits[j + 1].column == expected_column:
                # At expected column but not matching, more bits at same column - skip (clash)
                j += 1
            else:
                # At expected column but not matching, no more bits at same column - stop
                break

        return count

    def _process_tail_bits(self, canvas: 'Canvas', row_bits: List['BitInfo'],
                           i: int, bit: 'BitInfo', row: int, column: int,
                           color: str) -> int:
        """Process and draw TAIL slot bits, merging consecutive tails.

        Returns:
            Number of bits consumed from row_bits
        """
        from src.models.enums import SlotType
        count = self._count_consecutive_slots(
            row_bits, i, column, SlotType.TAIL,
            match_dp=True, match_device=True
        )
        tail_width = count - 1  # Extra columns beyond first

        # System tails (S1, CDS) use full-height; data port tails use half-height
        if bit.dp is None:
            self._draw_full_height_tail(canvas, row, column, tail_width)
        else:
            self._draw_tail(canvas, row, column, color, tail_width)

        return count

    def _process_cds_bits(self, canvas: 'Canvas', row_bits: List['BitInfo'],
                          i: int, row: int, column: int) -> int:
        """Process and draw CDS slot bits, merging consecutive CDS slots.

        Returns:
            Number of bits consumed from row_bits
        """
        from src.models.enums import SlotType
        count = self._count_consecutive_slots(row_bits, i, column, SlotType.CDS)
        cds_width = count - 1

        cds_label = SlotTypeStrings.CDS
        if count > 1:
            cds_label = f"{SlotTypeStrings.CDS} x{count}"
        self._draw_full_height_slot(canvas, row, column, cds_label, width=cds_width)

        return count

    def _process_system_slot_bits(self, canvas: 'Canvas', row_bits: List['BitInfo'],
                                   i: int, bit: 'BitInfo', row: int, column: int) -> int:
        """Process and draw S0/S1 slot bits, merging consecutive system slots.

        Returns:
            Number of bits consumed from row_bits
        """
        from src.models.enums import SlotType
        count = self._count_consecutive_slots(row_bits, i, column, bit.slot)
        s_width = count - 1

        slot_text = SlotTypeStrings.S0 if bit.slot == SlotType.S0 else SlotTypeStrings.S1
        if count > 1:
            slot_text = f"{slot_text} x{count}"
        self._draw_full_height_slot(canvas, row, column, slot_text, width=s_width)

        return count

    def _process_data_bits(self, canvas: 'Canvas', row_bits: List['BitInfo'],
                           i: int, bit: 'BitInfo', row: int, column: int,
                           is_source: bool, color: str,
                           processed: set) -> int:
        """Process and draw data bits (NORMAL, TX_PRESENT, DRQ), merging by display fields.

        Args:
            processed: Set of row_bits indices already processed. Merged indices will be added.

        Returns:
            Number of bits consumed from row_bits (always 1, merged bits tracked in processed set)
        """
        from src.models.enums import SlotType, DisplayField
        from src.models.bit_slot import BitSlotData

        # Build label based on slot type
        if bit.slot == SlotType.TX_PRESENT:
            label = f"TxP{bit.channel}"
        elif bit.slot == SlotType.DRQ:
            label = SlotTypeStrings.DRQ
        elif bit.port_mode == PortMode.TEST_ONES:
            label = self._build_test_mode_label(bit, "T1")
        elif bit.port_mode == PortMode.TEST_ZEROS:
            label = self._build_test_mode_label(bit, "T0")
        else:
            bit_data = BitSlotData(
                sample=bit.sample,
                channel=bit.channel,
                bit=bit.bit
            )
            label = bit_data.to_label(bit.display_fields)

        # Count consecutive bits with same DISPLAYED properties
        # Track which indices are merged so we skip them later
        merge_count = 1
        j = i + 1
        fields = bit.display_fields

        while j < len(row_bits):
            next_bit = row_bits[j]
            expected_column = column + merge_count

            if next_bit.column < expected_column:
                j += 1
                continue

            if next_bit.column > expected_column:
                break

            # Check basic properties that must always match
            if not (next_bit.direction == bit.direction and
                    next_bit.dp == bit.dp and
                    next_bit.slot == bit.slot and
                    next_bit.device == bit.device):
                if j + 1 < len(row_bits) and row_bits[j + 1].column == expected_column:
                    j += 1
                    continue
                else:
                    break

            # Check displayed fields - only require match for fields that are shown
            if fields is None:
                # Default: all fields displayed, require all to match
                if (next_bit.channel == bit.channel and
                    next_bit.sample == bit.sample and
                    next_bit.bit == bit.bit):
                    processed.add(j)  # Mark this index as merged/processed
                    merge_count += 1
                    j += 1
                else:
                    break
            else:
                # Only check fields that are displayed
                sample_match = (DisplayField.SAMPLE not in fields or
                               next_bit.sample == bit.sample)
                channel_match = (DisplayField.CHANNEL not in fields or
                                next_bit.channel == bit.channel)
                bit_match = (DisplayField.BIT not in fields or
                            next_bit.bit == bit.bit)

                if sample_match and channel_match and bit_match:
                    processed.add(j)  # Mark this index as merged/processed
                    merge_count += 1
                    j += 1
                else:
                    break

        # Add 'xN' suffix only for wide bits (same bit number at multiple columns)
        if merge_count > 1:
            if fields is None or DisplayField.BIT in fields:
                label = f"{label} x{merge_count}"

        # Draw merged rectangle
        width = merge_count - 1
        self._draw_bit_rect(canvas, row, column, width, is_source, label, color)

        return 1  # Always consume just the starting bit; merged bits tracked in processed set

    # =========================================================================
    # Main Bit Drawing Method
    # =========================================================================

    def _draw_bits(self, canvas: 'Canvas', bus_model: BusModel) -> None:
        """Draw all bits from the bus model, merging adjacent same-label bits."""
        assert self._config is not None
        from src.models.enums import SlotType, DirectionType
        from src.utils.logging_config import get_logger

        logger = get_logger('renderer')

        # Group bits by row for merging
        bits_by_row: dict[int, List['BitInfo']] = {}
        for bit in bus_model.bits:
            if bit.row not in bits_by_row:
                bits_by_row[bit.row] = []
            bits_by_row[bit.row].append(bit)

        # Sort each row by column
        for row in bits_by_row:
            bits_by_row[row].sort(key=lambda b: b.column)

        # Track bits processed for validation
        total_bits_expected = len(bus_model.bits)
        total_bits_processed = 0

        # Process each row
        for row, row_bits in bits_by_row.items():
            i = 0
            # Track indices that were merged with a previous bit (for data bits)
            processed: set = set()
            while i < len(row_bits):
                # Skip bits that were already merged with a previous bit
                if i in processed:
                    total_bits_processed += 1
                    i += 1
                    continue

                bit = row_bits[i]
                column = bit.column
                is_source = bit.direction == DirectionType.SOURCE

                # Determine color
                if bit.dp is not None:
                    color = self._config.dp_colors[bit.dp % len(self._config.dp_colors)]
                else:
                    color = self._config.background_color

                # Dispatch to appropriate handler based on slot type
                if bit.slot == SlotType.HANDOVER:
                    self._draw_handover(canvas, row, column)
                    bits_consumed = 1

                elif bit.slot == SlotType.TAIL:
                    bits_consumed = self._process_tail_bits(
                        canvas, row_bits, i, bit, row, column, color)

                elif bit.slot in (SlotType.GUARD_0, SlotType.GUARD_1):
                    guard_text = SlotTypeStrings.GUARD_1 if bit.slot == SlotType.GUARD_1 else SlotTypeStrings.GUARD_0
                    if bit.dp is not None:
                        self._draw_bit_rect(canvas, row, column, 0, is_source, guard_text, color)
                    else:
                        self._draw_full_height_slot(canvas, row, column, guard_text)
                    bits_consumed = 1

                elif bit.slot == SlotType.CDS:
                    bits_consumed = self._process_cds_bits(canvas, row_bits, i, row, column)

                elif bit.slot in (SlotType.S0, SlotType.S1):
                    bits_consumed = self._process_system_slot_bits(
                        canvas, row_bits, i, bit, row, column)

                else:
                    # Data bits: NORMAL, TX_PRESENT, DRQ
                    bits_consumed = self._process_data_bits(
                        canvas, row_bits, i, bit, row, column, is_source, color,
                        processed)

                total_bits_processed += bits_consumed
                i += bits_consumed

        # Validate total bits processed matches expected
        if total_bits_processed != total_bits_expected:
            logger.error(
                f"RENDERER BUG: Bit count mismatch! "
                f"Expected {total_bits_expected}, processed {total_bits_processed}. "
                f"Some bits may not have been drawn."
            )

    def _draw_clash_indicators(self, canvas: 'Canvas', bus_model: BusModel) -> None:
        """Draw clash indicators from bus model clash lists."""
        from src.config import CanvasColors
        from src.models.enums import SlotType

        def is_full_height_clash(bit_index: int) -> bool:
            """Check if any bit at this position is a full-height symbol."""
            bits = bus_model.get_bits_at(bit_index)
            for bit in bits:
                # Full-height symbols: S0, S1, CDS, or system tails (TAIL with dp=None)
                if bit.slot in (SlotType.S0, SlotType.S1, SlotType.CDS):
                    return True
                if bit.slot == SlotType.TAIL and bit.dp is None:
                    return True
            return False

        # Bus clashes (different device - physical collision) - Red X
        for bit_index in bus_model.bus_clashes:
            row, column = bus_model.position(bit_index)
            x1 = (column + 1.5) * COLUMN_SIZE
            y1 = (row + 2) * ROW_SIZE + 2 + FRAME_Y_OFFSET
            x2 = (column + 2.5) * COLUMN_SIZE
            # Use full height if any clashing bit is a full-height symbol
            y2 = (row + 3) * ROW_SIZE - 2 + FRAME_Y_OFFSET if is_full_height_clash(bit_index) else (row + 2.5) * ROW_SIZE + FRAME_Y_OFFSET
            canvas.create_line(x1, y1, x2, y2, fill=CanvasColors.BUS_CLASH, width=2)
            canvas.create_line(x2, y1, x1, y2, fill=CanvasColors.BUS_CLASH, width=2)

        # Device clashes (same device - internal conflict) - Yellow X
        for bit_index in bus_model.device_clashes:
            row, column = bus_model.position(bit_index)
            x1 = (column + 1.5) * COLUMN_SIZE
            y1 = (row + 2) * ROW_SIZE + 2 + FRAME_Y_OFFSET
            x2 = (column + 2.5) * COLUMN_SIZE
            # Use full height if any clashing bit is a full-height symbol
            y2 = (row + 3) * ROW_SIZE - 2 + FRAME_Y_OFFSET if is_full_height_clash(bit_index) else (row + 2.5) * ROW_SIZE + FRAME_Y_OFFSET
            canvas.create_line(x1, y1, x2, y2, fill=CanvasColors.DEVICE_CLASH, width=2)
            canvas.create_line(x2, y1, x1, y2, fill=CanvasColors.DEVICE_CLASH, width=2)

        # Read overlaps (visual warning only) - Blue X (always in sink region)
        for bit_index in bus_model.read_overlaps:
            row, column = bus_model.position(bit_index)
            x1 = (column + 1.5) * COLUMN_SIZE
            y1 = (row + 2.5) * ROW_SIZE + FRAME_Y_OFFSET
            x2 = (column + 2.5) * COLUMN_SIZE
            y2 = (row + 3.0) * ROW_SIZE - 1 + FRAME_Y_OFFSET
            canvas.create_line(x1, y1, x2, y2, fill=CanvasColors.READ_OVERLAP, width=2)
            canvas.create_line(x2, y1, x1, y2, fill=CanvasColors.READ_OVERLAP, width=2)

    def _draw_scrambler_indicators(self, canvas: 'Canvas', bus_model: BusModel) -> None:
        """Draw scrambler indicators for data port bits with scrambler enabled.

        Draws a small black square in the upper-left corner of each scrambled bit.
        For wide bits (spanning multiple columns), only draws on the first column.
        """
        from src.models.enums import DirectionType, SlotType

        # Track positions we've already drawn indicators at (to avoid duplicates)
        drawn_positions = set()

        for bit in bus_model.bits:
            # Only draw for data port bits with scrambler enabled
            if (bit.dp is not None and
                bit.scrambler_enabled and
                bit.slot == SlotType.NORMAL):

                # Skip if we've already drawn at this position
                pos_key = (bit.row, bit.column, bit.direction)
                if pos_key in drawn_positions:
                    continue
                drawn_positions.add(pos_key)

                is_source = bit.direction == DirectionType.SOURCE
                direction = 1 if is_source else 0

                # Calculate position (matching _draw_bit_rect layout)
                rect_y1 = (bit.row + 2 + 0.5 * (direction == 0)) * ROW_SIZE + FRAME_Y_OFFSET

                # Small black square in upper-left corner
                indicator_size = 5  # 1px wider/taller
                x1 = (bit.column + 1.5) * COLUMN_SIZE + 1  # 1px left
                y1 = rect_y1 + 1  # 1px up
                x2 = x1 + indicator_size
                y2 = y1 + indicator_size

                canvas.create_rectangle(x1, y1, x2, y2, fill='black', outline='black')

    def _draw_text_slot(self, canvas: 'Canvas', row: int, column: int,
                        text: str, fill_color: Optional[str], outline: bool = False) -> None:
        """Draw a slot with centered text.

        Args:
            canvas: Canvas to draw on
            row: Row number
            column: Column number
            text: Text to display
            fill_color: Fill color for rectangle (None = no rectangle)
            outline: Whether to draw rectangle outline
        """
        assert self._config is not None
        if fill_color:
            rect_y1 = (row + 2) * ROW_SIZE + FRAME_Y_OFFSET
            rect_y2 = (row + 2.5) * ROW_SIZE + FRAME_Y_OFFSET
            outline_width = 1 if outline else 0
            canvas.create_rectangle(
                (column + 1.5) * COLUMN_SIZE, rect_y1,
                (column + 2.5) * COLUMN_SIZE, rect_y2,
                fill=fill_color, outline=self._config.line_color, width=outline_width
            )

        canvas.create_text(
            (column + 2) * COLUMN_SIZE,
            (row + 2.25) * ROW_SIZE + FRAME_Y_OFFSET,  # Center in source half
            text=text, font=(APP_FONT, self._config.text_size - 4),
            fill=self._config.text_color
        )

    def _draw_bit_rect(self, canvas: 'Canvas', row: int, column: int,
                       width: int, is_source: bool, text: str, color: str) -> None:
        """Draw a data bit rectangle with label."""
        assert self._config is not None
        direction = 1 if is_source else 0
        rect_y1 = (row + 2 + 0.5 * (direction == 0)) * ROW_SIZE + FRAME_Y_OFFSET
        rect_y2 = (row + 2.5 + 0.5 * (direction == 0)) * ROW_SIZE + FRAME_Y_OFFSET

        canvas.create_rectangle(
            (column + 1.5) * COLUMN_SIZE, rect_y1,
            (column + 2.5 + width) * COLUMN_SIZE, rect_y2,
            fill=color, outline=self._config.line_color, width=1
        )

        # Draw partial vertical boundary lines between merged columns (25% height from bottom)
        if width > 0:
            line_y1 = rect_y2 - (rect_y2 - rect_y1) * 0.25  # Start at 75% down
            for col_offset in range(1, width + 1):
                line_x = (column + 1.5 + col_offset) * COLUMN_SIZE
                canvas.create_line(
                    line_x, line_y1, line_x, rect_y2,
                    fill=self._config.line_color, width=1
                )

        text_x = (column + 2 + 0.5 * width) * COLUMN_SIZE
        text_y = (rect_y1 + rect_y2) / 2 + 0.5

        canvas.create_text(
            text_x, text_y,
            font=(APP_FONT, self._config.text_size - 4),
            anchor='center', text=text, fill='black'
        )

    def _draw_full_height_slot(self, canvas: 'Canvas', row: int, column: int,
                               text: str, fill_color: Optional[str] = None,
                               width: int = 0) -> None:
        """Draw a full-height slot spanning both source and sink regions.

        Args:
            canvas: Canvas to draw on
            row: Row number
            column: Column number
            text: Text to display
            fill_color: Fill color for rectangle (None = use background color)
            width: Extra columns beyond first (0=single, 1=double, 2=triple)
        """
        assert self._config is not None
        color = fill_color if fill_color else self._config.background_color

        # Full height: from row+2 to row+3 (spans both source and sink halves)
        # Rectangle edges align precisely with row lines (border overlaps the line)
        rect_y1 = (row + 2) * ROW_SIZE + FRAME_Y_OFFSET
        rect_y2 = (row + 3) * ROW_SIZE + FRAME_Y_OFFSET

        canvas.create_rectangle(
            (column + 1.5) * COLUMN_SIZE, rect_y1,
            (column + 2.5 + width) * COLUMN_SIZE, rect_y2,
            fill=color, outline=self._config.line_color, width=1
        )

        # Draw partial vertical boundary lines between merged columns (25% height from bottom)
        if width > 0:
            line_y1 = rect_y2 - (rect_y2 - rect_y1) * 0.25  # Start at 75% down
            for col_offset in range(1, width + 1):
                line_x = (column + 1.5 + col_offset) * COLUMN_SIZE
                canvas.create_line(
                    line_x, line_y1, line_x, rect_y2,
                    fill=self._config.line_color, width=1
                )

        # Center text in the middle of the full-height slot
        text_x = (column + 2 + 0.5 * width) * COLUMN_SIZE
        text_y = (rect_y1 + rect_y2) / 2

        canvas.create_text(
            text_x, text_y,
            font=(APP_FONT, self._config.text_size - 4),
            anchor='center', text=text, fill=self._config.text_color
        )

    def _draw_handover(self, canvas: 'Canvas', row: int, column: int) -> None:
        """Draw handover arrows."""
        assert self._config is not None
        import tkinter as tk

        # Top arrow (right-pointing)
        canvas.create_line(
            (column + 1.725) * COLUMN_SIZE,
            (row + 2.35) * ROW_SIZE + FRAME_Y_OFFSET,
            (column + 2.275) * COLUMN_SIZE,
            (row + 2.35) * ROW_SIZE + FRAME_Y_OFFSET,
            arrow=tk.LAST, fill=self._config.text_color
        )
        # Bottom arrow (left-pointing)
        canvas.create_line(
            (column + 1.725) * COLUMN_SIZE,
            (row + 2.65) * ROW_SIZE + FRAME_Y_OFFSET,
            (column + 2.275) * COLUMN_SIZE,
            (row + 2.65) * ROW_SIZE + FRAME_Y_OFFSET,
            arrow=tk.FIRST, fill=self._config.text_color
        )

    def _draw_full_height_tail(self, canvas: 'Canvas', row: int, column: int,
                                width: int = 0) -> None:
        """Draw a full-height tail (system slot) with exponential decay ringing pattern.

        Used for S1 and CDS tails which span both source and sink regions.
        The pattern shows a damped oscillation that decays exponentially from
        left to right, simulating the ringing behavior of a transmission line.

        Args:
            canvas: Canvas to draw on
            row: Row number
            column: Starting column number
            width: Extra columns beyond first (0=single, 1=double, 2=triple)
        """
        assert self._config is not None
        color = self._config.background_color

        # Full height: from row+2 to row+3 (spans both source and sink halves)
        rect_y1 = (row + 2) * ROW_SIZE + FRAME_Y_OFFSET
        rect_y2 = (row + 3) * ROW_SIZE + FRAME_Y_OFFSET

        canvas.create_rectangle(
            (column + 1.5) * COLUMN_SIZE, rect_y1,
            (column + 2.5 + width) * COLUMN_SIZE, rect_y2,
            fill=color, outline=self._config.line_color, width=1
        )

        # Draw partial vertical boundary lines between merged columns (25% height from bottom)
        if width > 0:
            line_y1 = rect_y2 - (rect_y2 - rect_y1) * 0.25
            for col_offset in range(1, width + 1):
                line_x = (column + 1.5 + col_offset) * COLUMN_SIZE
                canvas.create_line(
                    line_x, line_y1, line_x, rect_y2,
                    fill=self._config.line_color, width=1
                )

        # Exponential decay ringing pattern - simple zigzag with decreasing amplitude
        # Straight lines between peaks and troughs, amplitude decreases exponentially
        # Starts with a rising line from center to first peak
        squiggle_color = self._config.text_color
        center_y = 0.50  # Middle of full row
        start_amplitude = 0.35  # Initial amplitude (larger for visibility)
        decay_rate = 0.35  # Decay per peak/trough

        total_columns = width + 1
        # Number of peaks/troughs: 8 for single column, 4 per column for wider
        if total_columns == 1:
            num_peaks = 8
        else:
            num_peaks = 4 * total_columns

        points = []

        # Starting point: at center (rising line to first peak)
        x_start = (column + 1.5 + 0.10) * COLUMN_SIZE
        y_start = (row + 2 + center_y) * ROW_SIZE + FRAME_Y_OFFSET
        points.extend([x_start, y_start])

        # Generate peaks and troughs
        for i in range(num_peaks):
            # X position: evenly spaced from 0.15 to 0.85 of total width
            progress = i / (num_peaks - 1) if num_peaks > 1 else 0
            x_fraction = 0.15 + progress * (total_columns - 0.30)
            x = (column + 1.5 + x_fraction) * COLUMN_SIZE

            # Amplitude decreases exponentially with each point
            amplitude = start_amplitude * math.exp(-decay_rate * i)

            # Alternate between positive (peak) and negative (trough)
            # i=0: peak (+), i=1: trough (-), i=2: peak (+), etc.
            sign = 1 if i % 2 == 0 else -1
            y_offset = sign * amplitude

            y = (row + 2 + center_y + y_offset) * ROW_SIZE + FRAME_Y_OFFSET
            points.extend([x, y])

        canvas.create_line(*points, fill=squiggle_color)  # type: ignore[arg-type]

    def _draw_tail(self, canvas: 'Canvas', row: int, column: int, color: str,
                    width: int = 0) -> None:
        """Draw a tail bit with exponential decay ringing pattern.

        Used for data port tails which span the source half of the row.
        The pattern shows a damped oscillation that decays exponentially from
        left to right, simulating the ringing behavior of a transmission line.

        Args:
            canvas: Canvas to draw on
            row: Row number
            column: Starting column number
            color: Fill color for rectangle
            width: Extra columns beyond first (0=single, 1=double, 2=triple)
        """
        assert self._config is not None
        rect_y1 = (row + 2) * ROW_SIZE + FRAME_Y_OFFSET
        rect_y2 = (row + 2.5) * ROW_SIZE + FRAME_Y_OFFSET

        # Determine outline based on color
        outline_width = 0 if color == self._config.background_color else 1

        canvas.create_rectangle(
            (column + 1.5) * COLUMN_SIZE, rect_y1,
            (column + 2.5 + width) * COLUMN_SIZE, rect_y2,
            fill=color, outline=self._config.line_color, width=outline_width
        )

        # Draw partial vertical boundary lines between merged columns (25% height from bottom)
        if width > 0:
            line_y1 = rect_y2 - (rect_y2 - rect_y1) * 0.25  # Start at 75% down
            for col_offset in range(1, width + 1):
                line_x = (column + 1.5 + col_offset) * COLUMN_SIZE
                canvas.create_line(
                    line_x, line_y1, line_x, rect_y2,
                    fill=self._config.line_color, width=1
                )

        # Exponential decay ringing pattern - simple zigzag with decreasing amplitude
        # Straight lines between peaks and troughs, amplitude decreases exponentially
        # Starts with a rising line from center to first peak
        squiggle_color = self._config.text_color if color == self._config.background_color else 'black'
        center_y = 0.25  # Center of source half (row+2.0 to row+2.5)
        start_amplitude = 0.18  # Initial amplitude (fits within half-height)
        decay_rate = 0.35  # Decay per peak/trough

        total_columns = width + 1
        # Number of peaks/troughs: 8 for single column, 4 per column for wider
        if total_columns == 1:
            num_peaks = 8
        else:
            num_peaks = 4 * total_columns

        points = []

        # Starting point: at center (rising line to first peak)
        x_start = (column + 1.5 + 0.10) * COLUMN_SIZE
        y_start = (row + 2 + center_y) * ROW_SIZE + FRAME_Y_OFFSET
        points.extend([x_start, y_start])

        # Generate peaks and troughs
        for i in range(num_peaks):
            # X position: evenly spaced from 0.15 to 0.85 of total width
            progress = i / (num_peaks - 1) if num_peaks > 1 else 0
            x_fraction = 0.15 + progress * (total_columns - 0.30)
            x = (column + 1.5 + x_fraction) * COLUMN_SIZE

            # Amplitude decreases exponentially with each point
            amplitude = start_amplitude * math.exp(-decay_rate * i)

            # Alternate between positive (peak) and negative (trough)
            # i=0: peak (+), i=1: trough (-), i=2: peak (+), etc.
            sign = 1 if i % 2 == 0 else -1
            y_offset = sign * amplitude

            y = (row + 2 + center_y + y_offset) * ROW_SIZE + FRAME_Y_OFFSET
            points.extend([x, y])

        canvas.create_line(*points, fill=squiggle_color)  # type: ignore[arg-type]

    def _build_test_mode_label(self, bit: 'BitInfo', test_indicator: str) -> str:
        """Build a label for test mode bits respecting display_fields.

        Args:
            bit: The bit data from the bus model
            test_indicator: Either "t1" or "t0" for test mode

        Returns:
            Label string with test mode indicator replacing the bit component
        """
        from src.models.enums import DisplayField

        if bit.display_fields is None:
            # Default: channel + test indicator
            return f"C{bit.channel}{test_indicator}"

        parts = []
        if DisplayField.SAMPLE in bit.display_fields:
            parts.append(f"S{bit.sample}")
        if DisplayField.CHANNEL in bit.display_fields:
            parts.append(f"C{bit.channel}")
        if DisplayField.BIT in bit.display_fields:
            parts.append(test_indicator)

        # If no fields selected, just show test indicator
        return "".join(parts) if parts else test_indicator
