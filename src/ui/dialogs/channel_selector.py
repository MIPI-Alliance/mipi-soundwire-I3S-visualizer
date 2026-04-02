"""
Channel selector dialog for SWI3S Visualizer.

Provides a dialog for selecting enabled channels via toggle buttons.
"""

import tkinter as tk
from typing import Any, List
import customtkinter as ctk

from src.ui.helpers import (
    center_dialog_on_parent,
    BUTTON_COLOR_SELECTED,
    BUTTON_COLOR_UNSELECTED,
)


class ChannelSelectorDialog(ctk.CTkToplevel):
    """Dialog for selecting enabled channels via toggle buttons."""

    BUTTON_WIDTH = 28
    BUTTON_HEIGHT = 28
    BUTTON_PADX = 2
    BUTTON_PADY = 4

    def __init__(self, parent: Any, initial_bitmask: int = 0, dp_index: int = 0):
        super().__init__(parent)
        self.title(f"DP{dp_index} Channel Selection")
        self.resizable(False, False)

        # Make dialog modal
        self.transient(parent)
        self.grab_set()

        # Store result
        self.result: int = initial_bitmask
        self.cancelled: bool = True

        # Channel toggle buttons (0-15)
        self.channel_vars: List[tk.BooleanVar] = []
        self.channel_buttons: List[ctk.CTkButton] = []

        # Main frame
        main_frame = ctk.CTkFrame(self)
        main_frame.pack(padx=10, pady=10)

        # Label row
        label_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        label_frame.pack(pady=(0, 5))
        ctk.CTkLabel(label_frame, text="Channels 0-15:").pack()

        # Toggle buttons row (1x16 horizontal)
        buttons_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        buttons_frame.pack()

        for i in range(16):
            var = tk.BooleanVar(value=bool(initial_bitmask & (1 << i)))
            self.channel_vars.append(var)

            btn = ctk.CTkButton(
                buttons_frame,
                text=str(i),
                width=self.BUTTON_WIDTH,
                height=self.BUTTON_HEIGHT,
                command=lambda idx=i: self._toggle_channel(idx)
            )
            btn.grid(row=0, column=i, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)
            self.channel_buttons.append(btn)

        # Update button colors based on initial state
        self._update_button_colors()

        # Enabled count label
        self.count_label = ctk.CTkLabel(main_frame, text="")
        self.count_label.pack(pady=(5, 10))
        self._update_count_label()

        # Action buttons frame
        action_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        action_frame.pack(pady=(5, 0))

        ctk.CTkButton(action_frame, text="Clear", width=60,
                      command=self._clear_all).grid(row=0, column=0, padx=5)
        ctk.CTkButton(action_frame, text="All", width=60,
                      command=self._select_all).grid(row=0, column=1, padx=5)
        ctk.CTkButton(action_frame, text="OK", width=60,
                      command=self._on_ok).grid(row=0, column=2, padx=5)

        # Center dialog on parent
        center_dialog_on_parent(self, parent)

        # Handle window close
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _toggle_channel(self, idx: int) -> None:
        """Toggle a single channel."""
        current = self.channel_vars[idx].get()
        self.channel_vars[idx].set(not current)
        self._update_button_colors()
        self._update_count_label()

    def _update_button_colors(self) -> None:
        """Update button colors based on enabled state."""
        for i, (var, btn) in enumerate(zip(self.channel_vars, self.channel_buttons)):
            if var.get():
                btn.configure(fg_color=BUTTON_COLOR_SELECTED)  # Green for enabled
            else:
                btn.configure(fg_color=BUTTON_COLOR_UNSELECTED)  # Gray for disabled

    def _update_count_label(self) -> None:
        """Update the enabled channel count display."""
        count = sum(1 for var in self.channel_vars if var.get())
        self.count_label.configure(text=f"Enabled: {count} channels")

    def _get_bitmask(self) -> int:
        """Compute bitmask from current toggle states."""
        bitmask = 0
        for i, var in enumerate(self.channel_vars):
            if var.get():
                bitmask |= (1 << i)
        return bitmask

    def _clear_all(self) -> None:
        """Clear all channel selections."""
        for var in self.channel_vars:
            var.set(False)
        self._update_button_colors()
        self._update_count_label()

    def _select_all(self) -> None:
        """Select all channels."""
        for var in self.channel_vars:
            var.set(True)
        self._update_button_colors()
        self._update_count_label()

    def _on_ok(self) -> None:
        """Accept the selection and close."""
        self.result = self._get_bitmask()
        self.cancelled = False
        self.destroy()

    def _on_cancel(self) -> None:
        """Cancel and close without saving."""
        self.cancelled = True
        self.destroy()
