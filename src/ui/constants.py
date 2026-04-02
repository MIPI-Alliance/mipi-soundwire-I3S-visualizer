"""
UI layout constants for SWI3S Visualizer.

This module centralizes all UI-related sizing and spacing constants
used by the main application.
"""

from typing import Tuple


# =============================================================================
# Entry Box Constants
# =============================================================================

ENTRY_WIDTH = 6           # Entry box width in characters (fits "65535")
PIXELS_PER_CHAR = 8       # Font width approximation for CTkEntry
ENTRY_PADX = 1            # Horizontal padding between entry boxes
ENTRY_PADY = 1            # Vertical padding between entry boxes
NUM_DP_ENTRY_ROWS = 13    # Number of data port entry rows (for grid layout)


# =============================================================================
# Canvas Layout Constants
# =============================================================================

ROW_SIZE = 30             # Height of one row in canvas (pixels)
COLUMN_SIZE = 39          # Width of one column in canvas (pixels)
HEADER_COLUMN_SIZE = 39   # Width of one column in header canvas (independent from main canvas)
TEXT_SIZE = 12            # Font size for canvas text (points)
SCROLLBAR_WIDTH = 16      # Approximate scrollbar width for header alignment
FRAME_Y_OFFSET = 1        # Vertical offset for frame_canvas content to fix first row height
AUX_CANVAS_FUDGE = 7      # Alignment offset for header canvas


# =============================================================================
# Checkbox Constants (CustomTkinter specific)
# =============================================================================

CHECKBOX_BORDER_WIDTH = 1          # Border width to match entry boxes
CHECKBOX_WIDTH = 28                # Natural compact size in pixels
CHECKBOX_HEIGHT = 28               # Height for checkbox wrapper frames
CHECKBOX_TOP_PADDING: Tuple[int, int] = (7, 0)  # Extra padding before first checkbox row
CHECKBOX_RELX = 0.61               # Horizontal centering (0.0-1.0)
CHECKBOX_RELY = 0.5                # Vertical centering (0.0-1.0)


# =============================================================================
# Font Constants
# =============================================================================

APP_FONT = 'Helvetica'  # Cross-platform font that works in SVG


# =============================================================================
# Window Dimensions
# =============================================================================

DEFAULT_WINDOW_HEIGHT = 800
DEFAULT_DATA_FRAME_HEIGHT = 400
DEFAULT_CANVAS_HEIGHT = 200
WINDOW_WIDTH_MULTIPLIER = 35.5  # Used with COLUMN_SIZE to calculate window width


# =============================================================================
# Toggle Button Constants
# =============================================================================

TOGGLE_BUTTON_WIDTH = 120     # Width of the toggle button
TOGGLE_BUTTON_HEIGHT = 22     # Height of the toggle button
TOGGLE_BUTTON_X_OFFSET = 70   # X offset from right edge of canvas
TOGGLE_BUTTON_Y_OFFSET = 3    # Y offset from ROW_SIZE for vertical positioning
HEADING_EXTRA_HEIGHT = 28     # Extra height for heading canvas when settings hidden (for button row)
