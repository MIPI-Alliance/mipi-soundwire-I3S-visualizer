"""
Device selector dialog for SWI3S Visualizer.

Provides a dialog for selecting device number (Manager or 0-11) via toggle buttons.
"""

import tkinter as tk
from typing import Any, List
import customtkinter as ctk

from src.config.constants import SpecialDevices
from src.ui.helpers import (
    center_dialog_on_parent,
    BUTTON_COLOR_SELECTED,
    BUTTON_COLOR_UNSELECTED,
)


class DeviceSelectorDialog(ctk.CTkToplevel):
    """Dialog for selecting device number via radio-style toggle buttons."""

    BUTTON_WIDTH = 36
    BUTTON_HEIGHT = 28
    BUTTON_PADX = 2
    BUTTON_PADY = 4

    def __init__(self, parent: Any, initial_device: int = 0, dp_index: int = 0):
        """Initialize device selector dialog.

        Args:
            parent: Parent window
            initial_device: Current device number (0-11 or SpecialDevices.MANAGER)
            dp_index: Data port index for dialog title
        """
        super().__init__(parent)
        self.title(f"DP{dp_index} Device Selection")
        self.resizable(False, False)

        # Make dialog modal
        self.transient(parent)
        self.grab_set()

        # Store result
        self.result: int = initial_device
        self.cancelled: bool = True

        # Device toggle buttons (Manager + 0-11 = 13 total)
        self.device_buttons: List[ctk.CTkButton] = []
        self.selected_device: int = initial_device

        # Main frame
        main_frame = ctk.CTkFrame(self)
        main_frame.pack(padx=10, pady=10)

        # Label row
        label_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        label_frame.pack(pady=(0, 5))
        ctk.CTkLabel(label_frame, text="Select Device:").pack()

        # Toggle buttons frame (two rows: Manager + devices 0-6, devices 7-11)
        buttons_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        buttons_frame.pack()

        # First row: Manager button
        manager_btn = ctk.CTkButton(
            buttons_frame,
            text="M",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._select_device(SpecialDevices.MANAGER)
        )
        manager_btn.grid(row=0, column=0, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)
        self.device_buttons.append(manager_btn)

        # First row: Devices 0-6
        for i in range(7):
            btn = ctk.CTkButton(
                buttons_frame,
                text=str(i),
                width=self.BUTTON_WIDTH,
                height=self.BUTTON_HEIGHT,
                command=lambda dev=i: self._select_device(dev)
            )
            btn.grid(row=0, column=i + 1, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)
            self.device_buttons.append(btn)

        # Second row: Devices 7-11
        for i in range(7, 12):
            btn = ctk.CTkButton(
                buttons_frame,
                text=str(i),
                width=self.BUTTON_WIDTH,
                height=self.BUTTON_HEIGHT,
                command=lambda dev=i: self._select_device(dev)
            )
            btn.grid(row=1, column=i - 7, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)
            self.device_buttons.append(btn)

        # Update button colors based on initial state
        self._update_button_colors()

        # Selection display label
        self.selection_label = ctk.CTkLabel(main_frame, text="")
        self.selection_label.pack(pady=(5, 10))
        self._update_selection_label()

        # Action buttons frame
        action_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        action_frame.pack(pady=(5, 0))

        ctk.CTkButton(action_frame, text="OK", width=80,
                      command=self._on_ok).pack()

        # Center dialog on parent
        center_dialog_on_parent(self, parent)

        # Handle window close
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _select_device(self, device: int) -> None:
        """Select a device (radio button behavior - only one selected).

        Args:
            device: Device number (0-11) or SpecialDevices.MANAGER
        """
        self.selected_device = device
        self._update_button_colors()
        self._update_selection_label()

    def _update_button_colors(self) -> None:
        """Update button colors based on selected device."""
        # Manager button is first
        if self.selected_device == SpecialDevices.MANAGER:
            self.device_buttons[0].configure(fg_color=BUTTON_COLOR_SELECTED)
        else:
            self.device_buttons[0].configure(fg_color=BUTTON_COLOR_UNSELECTED)

        # Device buttons (0-11) are indices 1-12
        for i in range(12):
            btn_idx = i + 1  # Offset by 1 since Manager is at index 0
            if self.selected_device == i:
                self.device_buttons[btn_idx].configure(fg_color=BUTTON_COLOR_SELECTED)
            else:
                self.device_buttons[btn_idx].configure(fg_color=BUTTON_COLOR_UNSELECTED)

    def _update_selection_label(self) -> None:
        """Update the selection display."""
        if self.selected_device == SpecialDevices.MANAGER:
            text = "Selected: Manager"
        else:
            text = f"Selected: Device {self.selected_device}"
        self.selection_label.configure(text=text)

    def _on_ok(self) -> None:
        """Accept the selection and close."""
        self.result = self.selected_device
        self.cancelled = False
        self.destroy()

    def _on_cancel(self) -> None:
        """Cancel and close without saving."""
        self.cancelled = True
        self.destroy()
