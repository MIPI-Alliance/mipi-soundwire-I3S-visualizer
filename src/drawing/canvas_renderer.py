"""
Copyright (c) 2023 MIPI Alliance and other contributors. All Rights Reserved.

Canvas rendering utilities for drawing SoundWire I3S frames.

This module provides the CanvasRenderer class that handles all tkinter canvas
drawing operations for visualizing SoundWire I3S frame structures.

Classes:
    CanvasRenderer: Handles canvas drawing operations for SoundWire I3S frames
"""

import tkinter as tk
from typing import Any, TYPE_CHECKING

from src.config import SlotTypeStrings, Debug_Drawing, Debug_Clash
from src.models.bit_slot import (
    PATTERN_CB as _PATTERN_CB,
    PATTERN_SC as _PATTERN_SC,
    PATTERN_SB as _PATTERN_SB,
    PATTERN_SCB as _PATTERN_SCB,
    PATTERN_C as _PATTERN_C,
    PATTERN_S as _PATTERN_S,
    PATTERN_TXP as _PATTERN_TXP,
)
from src.ui.constants import (
    ROW_SIZE,
    COLUMN_SIZE,
    FRAME_Y_OFFSET,
    APP_FONT,
)

if TYPE_CHECKING:
    from tkinter import Canvas
    from src.models import Frame_model
    from src.drawing import ClashDetector


# =============================================================================
# Canvas Renderer Class
# =============================================================================

class CanvasRenderer:
    """Handles canvas drawing operations for SoundWire I3S frames.

    This class encapsulates the rendering logic for drawing frames, separating
    it from the main UI class while maintaining access to necessary dependencies.
    """

    # -------------------------------------------------------------------------
    # Initialization
    # -------------------------------------------------------------------------

    def __init__(self, app: Any):
        """Initialize renderer with reference to main app.

        Args:
            app: Main application instance (provides canvas, dimensions, colors, etc.)
        """
        self.app = app
        # Cache frequently accessed properties for convenience
        self.frame_canvas: 'Canvas' = app.frame_canvas
        # Note: Don't cache frame_model or clash_detector - they get recreated in refresh_data_ports()
        # Access them via self.app.frame_model and self.app.clash_detector instead

    # -------------------------------------------------------------------------
    # Column and System Slot Drawing
    # -------------------------------------------------------------------------

    def draw_column(self, column: int, device: int, text: str) -> None:
        """Draw a single repeating column in a frame.

        Args:
            column: Column index
            device: Device number
            text: Text to display in the column
        """
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if not isinstance(text, str):
            raise TypeError('Expected str for text')

        for row in range(0, self.app.rows_in_frame):
            if text == SlotTypeStrings.HANDOVER:
                handover_drawn = self.draw_handover(row, column, device)
                # Only update frame model if handover was actually drawn
                if handover_drawn:
                    self.update_col_in_frame_model(row, column, 0, False, 0, text, device)
            else:
                self.frame_canvas.create_text(
                    (column + 2) * COLUMN_SIZE,
                    (row + 2.5) * ROW_SIZE + FRAME_Y_OFFSET,
                    text=text,
                    font=(APP_FONT, self.app.TEXT_SIZE - 4),
                    fill=self.app.current_text_color
                )
                self.app.check_bus_clash(row, column, device, 'write')
                self.update_col_in_frame_model(row, column, 0, False, 0, text, 0, device)

    # -------------------------------------------------------------------------
    # Handover Drawing
    # -------------------------------------------------------------------------

    def draw_handover(self, row: int, column: int, device: int) -> bool:
        """Draw a handover bit slot.

        Args:
            row: Row index
            column: Column index
            device: Device number

        Returns:
            True if handover was drawn, False if suppressed (same-device write exists)
        """
        if not isinstance(row, int):
            raise TypeError('Expected int for row')
        if not isinstance(column, int):
            raise TypeError('Expected int for column')

        if Debug_Drawing:
            print(f"draw_handover called for column: {column}, row: {row}, device: {device}")

        from src.utils.logging_config import get_logger
        logger = get_logger('drawing')
        logger.debug(f'Drawing handover at row {row}, column {column}', extra={
            'row': row,
            'column': column,
            'device': device
        })

        # Create the two arrow lines and save their canvas IDs
        id1 = self.frame_canvas.create_line(
            (column + 1.725) * COLUMN_SIZE,
            (row + 2.35) * ROW_SIZE + FRAME_Y_OFFSET,
            (column + 2.275) * COLUMN_SIZE,
            (row + 2.35) * ROW_SIZE + FRAME_Y_OFFSET,
            arrow=tk.LAST,
            fill=self.app.current_text_color
        )
        id2 = self.frame_canvas.create_line(
            (column + 1.725) * COLUMN_SIZE,
            (row + 2.65) * ROW_SIZE + FRAME_Y_OFFSET,
            (column + 2.275) * COLUMN_SIZE,
            (row + 2.65) * ROW_SIZE + FRAME_Y_OFFSET,
            arrow=tk.FIRST,
            fill=self.app.current_text_color
        )

        # Check for clashes and whether to suppress handover
        has_read_clash, has_write_clash, should_suppress = self.app.clash_detector.add_handover(
            row, column, device, [id1, id2]
        )

        from src.config import CanvasColors

        # Always delete arrows if suppressed or write clash
        if should_suppress or has_write_clash:
            self.frame_canvas.delete(id1)
            self.frame_canvas.delete(id2)

        if has_read_clash:
            # Read overlaps are visual-only warnings - reads are passive and don't clash
            # Don't add to frame model, just draw X indicator for visualization
            x1 = (column + 1.5) * COLUMN_SIZE
            y1 = (row + 2.5) * ROW_SIZE + FRAME_Y_OFFSET
            x2 = (column + 2.5) * COLUMN_SIZE
            y2 = (row + 3.0) * ROW_SIZE - 1 + FRAME_Y_OFFSET
            # Draw X pattern with two diagonal lines
            self.frame_canvas.create_line(x1, y1, x2, y2, fill=CanvasColors.READ_OVERLAP, width=2)
            self.frame_canvas.create_line(x2, y1, x1, y2, fill=CanvasColors.READ_OVERLAP, width=2)

        if has_write_clash:
            # Add CLASH to frame model (write clash)
            from src.models.frame import Slot_info
            from src.models.enums import SlotType, DirectionType
            slot_info = Slot_info()
            slot_info.slot_type = SlotType.CLASH
            slot_info.dir = DirectionType.SOURCE
            slot_info.device_num = device
            slot_info.dp_num = "clash"
            self.app.frame_model.get_row(row).get_col(column).append_slot(slot_info)
            # Draw black write clash X indicator
            x1 = (column + 1.5) * COLUMN_SIZE
            y1 = (row + 2) * ROW_SIZE + 2 + FRAME_Y_OFFSET
            x2 = (column + 2.5) * COLUMN_SIZE
            y2 = (row + 2.5) * ROW_SIZE + FRAME_Y_OFFSET
            # Draw X pattern with two diagonal lines
            self.frame_canvas.create_line(x1, y1, x2, y2, fill=CanvasColors.BUS_CLASH, width=2)
            self.frame_canvas.create_line(x2, y1, x1, y2, fill=CanvasColors.BUS_CLASH, width=2)
            # Record handover in JSON even when clashing - shows what's clashing
            return True

        if should_suppress:
            # Same-device real slot exists - don't record handover
            return False

        return True

    # -------------------------------------------------------------------------
    # Tail and Guard Drawing
    # -------------------------------------------------------------------------

    def draw_tail(self, row: int, column: int, device: int, color: str) -> bool:
        """Draw a tail bit slot.

        Args:
            row: Row index
            column: Column index
            device: Device number
            color: Fill color

        Returns:
            True if tail was drawn, False if suppressed by higher-priority slot
        """
        if Debug_Drawing:
            print(f'draw_tail called with row={row}, column={column}, device={device}')
        if not isinstance(row, int):
            raise TypeError('Expected int for row')
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if not isinstance(color, str):
            raise TypeError('Expected str for color')

        from src.utils.logging_config import get_logger
        logger = get_logger('drawing')
        logger.debug(f'Drawing tail at row {row}, column {column}', extra={
            'row': row,
            'column': column,
            'device': device,
            'color': color
        })

        direction: int = 1
        if self.app.check_bus_clash(row, column, device, SlotTypeStrings.TAIL):
            # CDS tails have no outline, data port tails have gray outline
            outline_width = 0 if color == self.app.PREFERRED_GRAY else 1

            # Calculate rectangle coordinates - start AT row line to overlap
            rect_y1 = (row + 2 + 0.5 * (direction == 0)) * ROW_SIZE + FRAME_Y_OFFSET
            rect_y2 = (row + 2.5 + 0.5 * (direction == 0)) * ROW_SIZE + FRAME_Y_OFFSET

            self.frame_canvas.create_rectangle(
                (column + 1.5) * COLUMN_SIZE,
                rect_y1,
                (column + 2.5) * COLUMN_SIZE,
                rect_y2,
                fill=color,
                outline=self.app.DARK_GRAY,
                width=outline_width
            )

            # Draw squiggle pattern - use theme text color for system tails
            if color == self.app.PREFERRED_GRAY:
                squiggle_color = self.app.current_text_color
                squiggle_coords = (
                    (column + 1.65) * COLUMN_SIZE, (row + 2.45 + 0.25) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 1.75) * COLUMN_SIZE, (row + 2.10 + 0.25) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 1.85) * COLUMN_SIZE, (row + 2.40 + 0.25) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 1.95) * COLUMN_SIZE, (row + 2.15 + 0.25) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 2.05) * COLUMN_SIZE, (row + 2.35 + 0.25) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 2.15) * COLUMN_SIZE, (row + 2.20 + 0.25) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 2.25) * COLUMN_SIZE, (row + 2.30 + 0.25) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 2.35) * COLUMN_SIZE, (row + 2.25 + 0.25) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                )
                self.frame_canvas.create_line(*squiggle_coords, fill=squiggle_color)  # type: ignore[arg-type]
            else:
                squiggle_coords = (
                    (column + 1.65) * COLUMN_SIZE, (row + 2.45) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 1.75) * COLUMN_SIZE, (row + 2.10) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 1.85) * COLUMN_SIZE, (row + 2.40) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 1.95) * COLUMN_SIZE, (row + 2.15) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 2.05) * COLUMN_SIZE, (row + 2.35) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 2.15) * COLUMN_SIZE, (row + 2.20) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 2.25) * COLUMN_SIZE, (row + 2.30) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                    (column + 2.35) * COLUMN_SIZE, (row + 2.25) * ROW_SIZE + FRAME_Y_OFFSET - 0.5,
                )
                self.frame_canvas.create_line(*squiggle_coords, fill='black')  # type: ignore[arg-type]
            return True
        return False

    def draw_guard(self, row: int, column: int, device: int, color: str,
                   guard_text: str = "G0") -> None:
        """Draw a guard bit slot.

        Args:
            row: Row index
            column: Column index
            device: Device number
            color: Fill color
            guard_text: Text to display ("G0" or "G1")
        """
        if Debug_Drawing:
            print(f'draw_guard called with row={row}, column={column}, device={device}')
        if not isinstance(row, int):
            raise TypeError('Expected int for row')
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if not isinstance(color, str):
            raise TypeError('Expected str for color')

        from src.utils.logging_config import get_logger
        logger = get_logger('drawing')
        logger.debug(f'Drawing guard at row {row}, column {column}', extra={
            'row': row,
            'column': column,
            'device': device,
            'color': color
        })

        direction: int = 1
        if self.app.check_bus_clash(row, column, device, 'guard'):
            if Debug_Clash:
                print("clash check returned True")

            # Calculate rectangle coordinates - start AT row line to overlap
            rect_y1 = (row + 2 + 0.5 * (direction == 0)) * ROW_SIZE + FRAME_Y_OFFSET
            rect_y2 = (row + 2.5 + 0.5 * (direction == 0)) * ROW_SIZE + FRAME_Y_OFFSET

            self.frame_canvas.create_rectangle(
                (column + 1.5) * COLUMN_SIZE,
                rect_y1,
                (column + 2.5) * COLUMN_SIZE,
                rect_y2,
                fill=color,
                outline=self.app.DARK_GRAY,
                width=1
            )

            # Draw text centered in the rectangle
            self.frame_canvas.create_text(
                (column + 2) * COLUMN_SIZE,
                (rect_y1 + rect_y2) / 2 + 0.5,  # Add 0.5 for consistent positioning
                font=(APP_FONT, self.app.TEXT_SIZE - 2),
                anchor='center',
                text=guard_text,
                fill='black'
            )
        else:
            if Debug_Clash:
                print("clash check returned False")

    # -------------------------------------------------------------------------
    # Bit Slot Writing
    # -------------------------------------------------------------------------

    def write_bit_slot(self, row: int, column: int, width: int, source: bool,
                      text: str, color: str, data_port_number: int) -> None:
        """Write a data bit slot to the canvas.

        Args:
            row: Row index
            column: Column index
            width: Bit width (for wide bits)
            source: True if source (writes), False if sink (reads)
            text: Label text for the slot
            color: Fill color
            data_port_number: Data port number
        """
        if Debug_Drawing:
            print(f'write_bit_slot called with row={row}, column={column}, text={text}')

        from src.utils.logging_config import get_logger
        logger = get_logger('drawing')
        logger.debug(f'Writing bit slot at row {row}, column {column}', extra={
            'row': row,
            'column': column,
            'text': text,
            'source': source,
            'color': color
        })

        direction = 1 if source else 0

        # Calculate rectangle coordinates
        # Source bits start AT row line to overlap, sink bits start at midpoint
        rect_y1 = (row + 2 + 0.5 * (direction == 0)) * ROW_SIZE + FRAME_Y_OFFSET
        rect_y2 = (row + 2.5 + 0.5 * (direction == 0)) * ROW_SIZE + FRAME_Y_OFFSET

        # Draw rectangle for the bit slot
        self.frame_canvas.create_rectangle(
            (column + 1.5) * COLUMN_SIZE,
            rect_y1,
            (column + 2.5 + width) * COLUMN_SIZE,
            rect_y2,
            fill=color,
            outline=self.app.DARK_GRAY,
            width=1
        )

        # Draw text label centered in the rectangle
        text_x = (column + 2 + 0.5 * width) * COLUMN_SIZE
        text_y = (rect_y1 + rect_y2) / 2 + 0.5  # Add 0.5 for consistent positioning

        self.frame_canvas.create_text(
            text_x,
            text_y,
            font=(APP_FONT, self.app.TEXT_SIZE - 4),
            anchor='center',
            text=text,
            fill='black'
        )

    # -------------------------------------------------------------------------
    # Frame Model Updates
    # -------------------------------------------------------------------------

    def update_col_in_frame_model(self, row: int, column: int, dp_num: int,
                                  is_source: bool, width: int, slot_label: str,
                                  device: int,
                                  sample: int = 0) -> None:
        """Update column information in the frame model.

        Args:
            row: Row index
            column: Column index
            dp_num: Data port number
            is_source: True if source direction
            width: Bit width
            slot_label: Label string for the slot (e.g., "c0b7", "s2c1", "CDS", "TxP0")
            device: Device number
            sample: Absolute sample counter (default 0)
        """
        from src.models import DirectionType, SlotType, Slot_info
        from src.config import SlotTypeStrings

        col_info = self.app.frame_model.get_row(row).get_col(column)

        # Parse the label string to extract sample, channel, and bit information
        # Use pre-compiled patterns for performance
        parsed_sample = sample
        chan = 0
        bit = 0

        # Try each pattern in order until one matches
        # Each entry: (pattern, sample_group, chan_group, bit_group)
        # None means that field is not extracted from this pattern
        PATTERNS = [
            (_PATTERN_CB, None, 1, 2),      # cXbY: channel in group 1, bit in group 2
            (_PATTERN_SC, 1, 2, None),      # sXcY: sample in group 1, channel in group 2
            (_PATTERN_SB, 1, None, 2),      # sXbY: sample in group 1, bit in group 2
            (_PATTERN_SCB, 1, 2, 3),        # sXcYbZ: sample in group 1, channel in group 2, bit in group 3
            (_PATTERN_C, None, 1, None),    # cX: channel only
            (_PATTERN_S, 1, None, None),    # sX: sample only
        ]

        m = None
        for pattern, sample_grp, chan_grp, bit_grp in PATTERNS:
            m = pattern.fullmatch(slot_label)
            if m:
                if sample_grp is not None:
                    parsed_sample = int(m.group(sample_grp))
                if chan_grp is not None:
                    chan = int(m.group(chan_grp))
                if bit_grp is not None:
                    bit = int(m.group(bit_grp))
                break

        if m:
            # Data slot (any of the above formats matched)
            slot_info = Slot_info()
            slot_info.slot_type = SlotType.NORMAL
            slot_info.dir = DirectionType.SOURCE if is_source else DirectionType.SINK
            slot_info.device_num = device
            slot_info.dp_num = dp_num
            slot_info.sample = parsed_sample
            slot_info.channel = chan
            slot_info.bit = bit

            col_info.append_slot(slot_info)
        else:
            # Check for TxPresent bit (TxPn format)
            txp_match = _PATTERN_TXP.fullmatch(slot_label)
            if txp_match:
                slot_info = Slot_info()
                slot_info.slot_type = SlotType.TX_PRESENT
                slot_info.dir = DirectionType.SOURCE if is_source else DirectionType.SINK
                slot_info.device_num = device
                slot_info.dp_num = dp_num
                slot_info.sample = sample
                slot_info.channel = int(txp_match.group(1))  # Extract channel number from TxPn
                slot_info.bit = 0
                col_info.append_slot(slot_info)
            # Check for DRQ bit (Data Request for Rx Controlled or Async flow modes)
            elif slot_label == SlotTypeStrings.DRQ:
                slot_info = Slot_info()
                slot_info.slot_type = SlotType.DRQ
                slot_info.dir = DirectionType.SOURCE if is_source else DirectionType.SINK
                slot_info.device_num = device
                slot_info.dp_num = dp_num
                slot_info.sample = sample
                slot_info.channel = 0
                slot_info.bit = 0
                col_info.append_slot(slot_info)
            # System slot (S1, S0, CDS, HANDOVER, G, tail)
            else:
                slot_info = Slot_info()

                # Map label string to slot type using dictionary lookup
                SLOT_TYPE_MAP = {
                    SlotTypeStrings.S1: SlotType.S1,
                    SlotTypeStrings.S0: SlotType.S0,
                    SlotTypeStrings.CDS: SlotType.CDS,
                    SlotTypeStrings.HANDOVER: SlotType.HANDOVER,
                    SlotTypeStrings.GUARD_0: SlotType.GUARD_0,
                    SlotTypeStrings.GUARD_1: SlotType.GUARD_1,
                    SlotTypeStrings.TAIL: SlotType.TAIL,
                }

                slot_type = SLOT_TYPE_MAP.get(slot_label)
                if slot_type is None:
                    # Unknown slot type - this should not happen
                    # Log warning to help catch bugs like unhandled label formats
                    from src.utils.logging_config import get_logger
                    logger = get_logger('drawing')
                    logger.warning(
                        f'Unrecognized slot label "{slot_label}" at row={row}, col={column}, '
                        f'dp={dp_num} - slot NOT added to frame model'
                    )
                    return
                slot_info.slot_type = slot_type

                # System slots are always SOURCE direction
                slot_info.dir = DirectionType.SOURCE
                slot_info.device_num = device

                # GUARD_0, GUARD_1, and TAIL belong to specific data ports, others use "None"
                if slot_label in (SlotTypeStrings.GUARD_0, SlotTypeStrings.GUARD_1, SlotTypeStrings.TAIL):
                    slot_info.dp_num = dp_num
                else:
                    slot_info.dp_num = "None"  # String "None" for system slots

                slot_info.sample = 0
                slot_info.channel = 0
                slot_info.bit = 0

                col_info.append_slot(slot_info)
