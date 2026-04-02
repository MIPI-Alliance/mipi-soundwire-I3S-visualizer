"""
Theme utilities for SWI3S Visualizer.

This module provides theme-related utilities for color handling
and appearance mode management.
"""

import tkinter as tk
from types import ModuleType
from typing import Any, Optional, Tuple

ctk: Optional[ModuleType] = None
try:
    import customtkinter as ctk
    HAS_CTK = True
except ImportError:
    HAS_CTK = False


# =============================================================================
# Theme Color Constants
# =============================================================================

# Line colors for canvas drawing (these don't change with theme)
DARK_GRAY = '#707070'
LIGHT_GRAY = '#707070'


# =============================================================================
# Color Utility Functions
# =============================================================================

def color_to_hex(color: Any) -> str:
    """Convert color name or value to hex format for canvas compatibility.

    Args:
        color: Color value (hex string, named color, or other)

    Returns:
        Hex color string (e.g., '#707070')
    """
    if isinstance(color, str):
        if color.startswith('#'):
            return color
        # Convert named colors like 'gray86' to hex
        try:
            # Create a temporary widget to convert the color
            temp = tk.Label()
            temp.config(bg=color)
            rgb = temp.winfo_rgb(color)
            temp.destroy()
            # Convert RGB to hex
            return f'#{rgb[0]//256:02x}{rgb[1]//256:02x}{rgb[2]//256:02x}'
        except Exception:
            return color  # Return as-is if conversion fails
    return str(color)


def get_theme_colors(mode: Optional[str] = None) -> Tuple[str, str]:
    """Get background and text colors from the current CustomTkinter theme.

    Args:
        mode: Appearance mode ('Light' or 'Dark'). If None, auto-detect.

    Returns:
        Tuple of (background_color_hex, text_color_hex)
    """
    if not HAS_CTK or ctk is None:
        # Fallback colors if CustomTkinter not available
        return ('#d9d9d9', '#000000')

    if mode is None:
        mode = ctk.get_appearance_mode()  # Returns "Light" or "Dark"

    # Get color index (0 for Light, 1 for Dark)
    color_index: int = 1 if mode == "Dark" else 0

    # Get colors from theme
    theme = ctk.ThemeManager.theme

    # Get background color - use CTkFrame's foreground color to match frame backgrounds
    bg_color = theme["CTkFrame"]["fg_color"]
    if isinstance(bg_color, (list, tuple)) and len(bg_color) > color_index:
        bg_value = bg_color[color_index]
    else:
        bg_value = bg_color

    # Get text color
    text_color = theme["CTkLabel"]["text_color"]
    if isinstance(text_color, (list, tuple)) and len(text_color) > color_index:
        text_value = text_color[color_index]
    else:
        text_value = text_color

    return (color_to_hex(bg_value), color_to_hex(text_value))


def get_disabled_colors(mode: Optional[str] = None) -> Tuple[str, str, str]:
    """Get disabled state colors for the current theme mode.

    Args:
        mode: Appearance mode ('Light' or 'Dark'). If None, auto-detect.

    Returns:
        Tuple of (disabled_text_color, disabled_label_color, disabled_checkbox_color)
    """
    if mode is None and HAS_CTK and ctk is not None:
        mode = ctk.get_appearance_mode()

    if mode == "Dark":
        return ('#888888', '#666666', '#555555')  # Dark mode disabled colors
    else:
        return ('#AAAAAA', '#AAAAAA', '#CCCCCC')  # Light mode disabled colors
