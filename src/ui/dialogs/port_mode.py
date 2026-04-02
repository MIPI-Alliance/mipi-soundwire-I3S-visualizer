"""
Port mode selector dialog for SWI3S Visualizer.

Provides a dialog for selecting port test mode (Normal/Test Ones/Test Zeros).
"""

import tkinter as tk
from typing import Any
import customtkinter as ctk

from src.ui.helpers import (
    center_dialog_on_parent,
    BUTTON_COLOR_SELECTED,
    BUTTON_COLOR_UNSELECTED,
)


class PortModeSelectorDialog(ctk.CTkToplevel):
    """Dialog for selecting port test mode (Normal/Test Ones/Test Zeros)."""

    BUTTON_WIDTH = 110
    BUTTON_HEIGHT = 32
    BUTTON_PADX = 5
    BUTTON_PADY = 4
    LABEL_FONT = ('TkDefaultFont', 12)

    # Port mode constants (matching PortMode enum)
    MODE_NORMAL = 0
    MODE_RESERVED = 1  # Not shown in UI
    MODE_TEST_ONES = 2
    MODE_TEST_ZEROS = 3

    def __init__(self, parent: Any, port_mode: int = 0, dp_index: int = 0):
        super().__init__(parent)

        # Immediately withdraw to prevent flicker during setup
        self.withdraw()

        self.title(f"DP{dp_index} Port Test Mode")
        self.resizable(False, False)

        # Make dialog modal
        self.transient(parent)
        self.grab_set()

        # Store results (convert Reserved to Normal for display purposes)
        self.port_mode: int = port_mode if port_mode != self.MODE_RESERVED else self.MODE_NORMAL
        self.cancelled: bool = True

        # Main frame
        main_frame = ctk.CTkFrame(self)
        main_frame.pack(padx=10, pady=10)

        # Label row
        label_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        label_frame.pack(pady=(0, 5))
        ctk.CTkLabel(label_frame, text="Port Test Mode", font=self.LABEL_FONT).pack()

        # Option buttons in a row
        buttons_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        buttons_frame.pack()

        self.normal_btn = ctk.CTkButton(
            buttons_frame,
            text="Normal (Off)",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._select_mode(self.MODE_NORMAL)
        )
        self.normal_btn.grid(row=0, column=0, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)

        self.test_ones_btn = ctk.CTkButton(
            buttons_frame,
            text="Ones",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._select_mode(self.MODE_TEST_ONES)
        )
        self.test_ones_btn.grid(row=0, column=1, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)

        self.test_zeros_btn = ctk.CTkButton(
            buttons_frame,
            text="Zeros",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._select_mode(self.MODE_TEST_ZEROS)
        )
        self.test_zeros_btn.grid(row=0, column=2, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)

        # Update button colors based on initial state
        self._update_button_colors()

        # OK button
        action_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        action_frame.pack(pady=(10, 0))

        ctk.CTkButton(action_frame, text="OK", width=60,
                      command=self._on_ok).pack()

        # Center dialog on parent
        center_dialog_on_parent(self, parent)

        # Handle window close
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # Show dialog after all setup is complete
        self.update_idletasks()
        self.deiconify()
        self.lift()
        self.focus_force()

    def _select_mode(self, mode: int) -> None:
        """Select a port mode."""
        self.port_mode = mode
        self._update_button_colors()

    def _update_button_colors(self) -> None:
        """Update button colors based on selected mode."""
        # Green for selected, gray for unselected
        mode_to_btn = {
            self.MODE_NORMAL: self.normal_btn,
            self.MODE_TEST_ONES: self.test_ones_btn,
            self.MODE_TEST_ZEROS: self.test_zeros_btn,
        }
        for mode, btn in mode_to_btn.items():
            if mode == self.port_mode:
                btn.configure(fg_color=BUTTON_COLOR_SELECTED)
            else:
                btn.configure(fg_color=BUTTON_COLOR_UNSELECTED)

    def _on_ok(self) -> None:
        """Accept the selection and close."""
        self.cancelled = False
        self.destroy()

    def _on_cancel(self) -> None:
        """Cancel and close without saving."""
        self.cancelled = True
        self.destroy()
