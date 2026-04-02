"""
Guard selector dialog for SWI3S Visualizer.

Provides a dialog for selecting guard polarity (G0/G1) or Off.
"""

from typing import Any, Optional
import customtkinter as ctk

from src.ui.helpers import (
    center_dialog_on_parent,
    BUTTON_COLOR_SELECTED,
    BUTTON_COLOR_UNSELECTED,
)


class GuardSelectorDialog(ctk.CTkToplevel):
    """Dialog for selecting guard polarity (G0/G1) or Off."""

    BUTTON_WIDTH = 70
    BUTTON_HEIGHT = 32
    BUTTON_PADX = 5
    BUTTON_PADY = 4

    def __init__(self, parent: Any, guard_enabled: bool = False,
                 guard_polarity: int = 0, dp_index: int = 0,
                 dialog_title: Optional[str] = None):
        super().__init__(parent)
        # Use custom title if provided, otherwise default to DP format
        if dialog_title:
            self.title(dialog_title)
        else:
            self.title(f"DP{dp_index} Guard Selection")
        self.resizable(False, False)

        # Make dialog modal
        self.transient(parent)
        self.grab_set()

        # Store results
        self.guard_enabled: bool = guard_enabled
        self.guard_polarity: int = guard_polarity
        self.cancelled: bool = True

        # Track which option is selected
        self.selected_option: str = "off"
        if guard_enabled:
            self.selected_option = "g1" if guard_polarity else "g0"

        # Main frame
        main_frame = ctk.CTkFrame(self)
        main_frame.pack(padx=10, pady=10)

        # Label row
        label_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        label_frame.pack(pady=(0, 5))
        ctk.CTkLabel(label_frame, text="Select Guard:").pack()

        # Option buttons row
        buttons_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        buttons_frame.pack()

        self.g0_btn = ctk.CTkButton(
            buttons_frame,
            text="Guard 0",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._select_option("g0")
        )
        self.g0_btn.grid(row=0, column=0, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)

        self.g1_btn = ctk.CTkButton(
            buttons_frame,
            text="Guard 1",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._select_option("g1")
        )
        self.g1_btn.grid(row=0, column=1, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)

        self.off_btn = ctk.CTkButton(
            buttons_frame,
            text="Off",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._select_option("off")
        )
        self.off_btn.grid(row=0, column=2, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)

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

    def _select_option(self, option: str) -> None:
        """Select an option (g0, g1, or off)."""
        self.selected_option = option
        self._update_button_colors()

    def _update_button_colors(self) -> None:
        """Update button colors based on selected option."""
        # Green for selected, gray for unselected
        if self.selected_option == "g0":
            self.g0_btn.configure(fg_color=BUTTON_COLOR_SELECTED)
            self.g1_btn.configure(fg_color=BUTTON_COLOR_UNSELECTED)
            self.off_btn.configure(fg_color=BUTTON_COLOR_UNSELECTED)
        elif self.selected_option == "g1":
            self.g0_btn.configure(fg_color=BUTTON_COLOR_UNSELECTED)
            self.g1_btn.configure(fg_color=BUTTON_COLOR_SELECTED)
            self.off_btn.configure(fg_color=BUTTON_COLOR_UNSELECTED)
        else:  # off
            self.g0_btn.configure(fg_color=BUTTON_COLOR_UNSELECTED)
            self.g1_btn.configure(fg_color=BUTTON_COLOR_UNSELECTED)
            self.off_btn.configure(fg_color=BUTTON_COLOR_SELECTED)

    def _on_ok(self) -> None:
        """Accept the selection and close."""
        if self.selected_option == "g0":
            self.guard_enabled = True
            self.guard_polarity = 0
        elif self.selected_option == "g1":
            self.guard_enabled = True
            self.guard_polarity = 1
        else:  # off
            self.guard_enabled = False
            self.guard_polarity = 0
        self.cancelled = False
        self.destroy()

    def _on_cancel(self) -> None:
        """Cancel and close without saving."""
        self.cancelled = True
        self.destroy()
