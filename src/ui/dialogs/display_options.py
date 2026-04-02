"""
Display options dialog for SWI3S Visualizer.

Provides a dialog for selecting which fields to display (Sample, Channel, Bit).
"""

import tkinter as tk
from typing import Any, Optional, TYPE_CHECKING
import customtkinter as ctk

from src.ui.helpers import (
    center_dialog_on_parent,
    BUTTON_COLOR_SELECTED,
    BUTTON_COLOR_UNSELECTED,
)

if TYPE_CHECKING:
    from src.models import DisplayField


class DisplayOptionsDialog(ctk.CTkToplevel):
    """Dialog for selecting display options (any combination of Sample, Channel, Bit)."""

    BUTTON_WIDTH = 80
    BUTTON_HEIGHT = 32
    BUTTON_PADX = 5
    BUTTON_PADY = 4

    def __init__(self, parent: Any, enabled: bool = False,
                 display_fields: Optional['DisplayField'] = None, dp_index: int = 0):
        super().__init__(parent)
        self.title(f"DP{dp_index} Display Options")
        self.resizable(False, False)

        # Make dialog modal
        self.transient(parent)
        self.grab_set()

        # Import DisplayField
        from src.models import DisplayField

        # Store results
        self.enabled: bool = enabled
        if display_fields is None:
            display_fields = DisplayField.CHANNEL | DisplayField.BIT
        self.display_fields: DisplayField = display_fields
        self.cancelled: bool = True

        # Track selected options
        self.sample_selected = DisplayField.SAMPLE in display_fields
        self.channel_selected = DisplayField.CHANNEL in display_fields
        self.bit_selected = DisplayField.BIT in display_fields

        # Main frame
        main_frame = ctk.CTkFrame(self)
        main_frame.pack(padx=10, pady=10)

        # Enable checkbox row
        enable_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        enable_frame.pack(pady=(0, 10))
        self.enable_var = tk.BooleanVar(value=enabled)
        ctk.CTkCheckBox(enable_frame, text="Draw DataPort",
                       variable=self.enable_var).pack()

        # Label for field selection
        label_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        label_frame.pack(pady=(0, 5))
        ctk.CTkLabel(label_frame, text="Show Fields:").pack()

        # Field option buttons
        buttons_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        buttons_frame.pack()

        self.sample_btn = ctk.CTkButton(
            buttons_frame,
            text="Sample (s)",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._toggle_field("sample")
        )
        self.sample_btn.grid(row=0, column=0, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)

        self.channel_btn = ctk.CTkButton(
            buttons_frame,
            text="Channel (c)",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._toggle_field("channel")
        )
        self.channel_btn.grid(row=0, column=1, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)

        self.bit_btn = ctk.CTkButton(
            buttons_frame,
            text="Bit (b)",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._toggle_field("bit")
        )
        self.bit_btn.grid(row=0, column=2, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)

        # Preview label
        preview_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        preview_frame.pack(pady=(10, 5))
        self.preview_label = ctk.CTkLabel(preview_frame, text="Preview: c0b0")
        self.preview_label.pack()

        # Update button colors and preview
        self._update_button_colors()
        self._update_preview()

        # OK button
        action_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        action_frame.pack(pady=(10, 0))
        ctk.CTkButton(action_frame, text="OK", width=60,
                      command=self._on_ok).pack()

        # Center dialog on parent
        center_dialog_on_parent(self, parent)

        # Handle window close
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _toggle_field(self, field: str) -> None:
        """Toggle a field selection. Any combination of fields can be selected."""
        if field == "sample":
            self.sample_selected = not self.sample_selected
        elif field == "channel":
            self.channel_selected = not self.channel_selected
        else:  # bit
            self.bit_selected = not self.bit_selected

        self._update_button_colors()
        self._update_preview()

    def _update_button_colors(self) -> None:
        """Update button colors based on selection."""
        self.sample_btn.configure(fg_color=BUTTON_COLOR_SELECTED if self.sample_selected else BUTTON_COLOR_UNSELECTED)
        self.channel_btn.configure(fg_color=BUTTON_COLOR_SELECTED if self.channel_selected else BUTTON_COLOR_UNSELECTED)
        self.bit_btn.configure(fg_color=BUTTON_COLOR_SELECTED if self.bit_selected else BUTTON_COLOR_UNSELECTED)

    def _update_preview(self) -> None:
        """Update the preview label."""
        parts = []
        if self.sample_selected:
            parts.append("s0")
        if self.channel_selected:
            parts.append("c0")
        if self.bit_selected:
            parts.append("b0")
        preview_text = "".join(parts) if parts else "(no label)"
        self.preview_label.configure(text=f"Preview: {preview_text}")

    def _on_ok(self) -> None:
        """Accept selection and close."""
        from src.models import DisplayField
        self.enabled = self.enable_var.get()
        self.display_fields = DisplayField(0)
        if self.sample_selected:
            self.display_fields |= DisplayField.SAMPLE
        if self.channel_selected:
            self.display_fields |= DisplayField.CHANNEL
        if self.bit_selected:
            self.display_fields |= DisplayField.BIT
        self.cancelled = False
        self.destroy()

    def _on_cancel(self) -> None:
        """Cancel and close."""
        self.cancelled = True
        self.destroy()
