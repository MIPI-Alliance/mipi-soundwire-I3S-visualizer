"""
Flow mode selector dialog for SWI3S Visualizer.

Provides a dialog for selecting flow control mode (Normal/Tx/Rx/Async) with FCP parameters.
"""

import tkinter as tk
from typing import Any, Union
import customtkinter as ctk

from src.ui.helpers import (
    center_dialog_on_parent,
    BUTTON_COLOR_SELECTED,
    BUTTON_COLOR_UNSELECTED,
)


class FlowModeSelectorDialog(ctk.CTkToplevel):
    """Dialog for selecting flow control mode (Normal/Tx/Rx/Async) with FCP parameters."""

    BUTTON_WIDTH = 110
    BUTTON_HEIGHT = 32
    BUTTON_PADX = 5
    BUTTON_PADY = 4
    ENTRY_WIDTH = 6
    PIXELS_PER_CHAR = 8
    CHECKBOX_BORDER_WIDTH = 1
    CHECKBOX_WIDTH = 28
    CHECKBOX_HEIGHT = 28
    LABEL_WIDTH = 165
    LABEL_FONT = ('TkDefaultFont', 12)  # Match main UI font

    # Flow mode constants
    MODE_NORMAL = 0
    MODE_TX_CONTROLLED = 1
    MODE_RX_CONTROLLED = 2
    MODE_ASYNC = 3

    @staticmethod
    def _validate_range(action: str, value_if_allowed: str, low: Union[int, str], high: Union[int, str]) -> bool:
        """Validate entry value is within range (matches main UI validation)."""
        if action == '1':  # Entry action
            try:
                if int(low) <= int(value_if_allowed) <= int(high):
                    return True
                else:
                    return False
            except ValueError:
                return False
        else:
            return True

    def __init__(self, parent: Any, flow_mode: int = 0, dp_index: int = 0,
                 fcp_h_start: int = 4, fcp_bit_width: int = 0, fcp_tail_width: int = 0,
                 fcp_offset: int = 0, fcp_guard_enable: bool = False, fcp_guard_polarity: int = 0):
        super().__init__(parent)
        self.title(f"DP{dp_index} Flow Control")
        self.resizable(False, False)

        # Make dialog modal
        self.transient(parent)
        self.grab_set()

        # Store results
        self.flow_mode: int = flow_mode
        self.cancelled: bool = True

        # FCP parameter results
        self.fcp_h_start: int = fcp_h_start
        self.fcp_bit_width: int = fcp_bit_width
        self.fcp_tail_width: int = fcp_tail_width
        self.fcp_offset: int = fcp_offset
        self.fcp_guard_enable: bool = fcp_guard_enable
        self.fcp_guard_polarity: int = fcp_guard_polarity

        # Register validation commands for FCP parameters
        from src.config.constants import DataPortRanges
        self._h_start_vcmd = (self.register(self._validate_range), '%d', '%P',
                              DataPortRanges.MIN_FCP_H_START, DataPortRanges.MAX_FCP_H_START)
        self._bit_width_vcmd = (self.register(self._validate_range), '%d', '%P',
                                DataPortRanges.MIN_FCP_BIT_WIDTH, DataPortRanges.MAX_FCP_BIT_WIDTH)
        self._tail_width_vcmd = (self.register(self._validate_range), '%d', '%P',
                                 DataPortRanges.MIN_FCP_TAIL_WIDTH, DataPortRanges.MAX_FCP_TAIL_WIDTH)
        self._offset_vcmd = (self.register(self._validate_range), '%d', '%P',
                             DataPortRanges.MIN_FCP_OFFSET, DataPortRanges.MAX_FCP_OFFSET)
        self._guard_polarity_vcmd = (self.register(self._validate_range), '%d', '%P',
                                     DataPortRanges.MIN_FCP_GUARD_POLARITY, DataPortRanges.MAX_FCP_GUARD_POLARITY)

        # Main frame
        main_frame = ctk.CTkFrame(self)
        main_frame.pack(padx=10, pady=10)

        # Label row
        label_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        label_frame.pack(pady=(0, 5))
        ctk.CTkLabel(label_frame, text="Flow Control Mode", font=self.LABEL_FONT).pack()

        # Option buttons in 2x2 grid
        buttons_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        buttons_frame.pack()

        self.normal_btn = ctk.CTkButton(
            buttons_frame,
            text="Normal",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._select_mode(self.MODE_NORMAL)
        )
        self.normal_btn.grid(row=0, column=0, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)

        self.tx_btn = ctk.CTkButton(
            buttons_frame,
            text="Tx Synchronous",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._select_mode(self.MODE_TX_CONTROLLED)
        )
        self.tx_btn.grid(row=0, column=1, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)

        self.rx_btn = ctk.CTkButton(
            buttons_frame,
            text="Rx Synchronous",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._select_mode(self.MODE_RX_CONTROLLED)
        )
        self.rx_btn.grid(row=1, column=0, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)

        self.async_btn = ctk.CTkButton(
            buttons_frame,
            text="Asynchronous",
            width=self.BUTTON_WIDTH,
            height=self.BUTTON_HEIGHT,
            command=lambda: self._select_mode(self.MODE_ASYNC)
        )
        self.async_btn.grid(row=1, column=1, padx=self.BUTTON_PADX, pady=self.BUTTON_PADY)

        # Update button colors based on initial state
        self._update_button_colors()

        # FCP Parameters Frame (for DRQ bits in Rx Controlled/Async modes)
        self.fcp_frame = ctk.CTkFrame(main_frame)
        self.fcp_frame.pack(pady=(10, 0), fill='x')

        # Configure columns for centering
        self.fcp_frame.grid_columnconfigure(0, weight=1)
        self.fcp_frame.grid_columnconfigure(1, weight=1)

        fcp_label = ctk.CTkLabel(self.fcp_frame, text="Flow Control Port Parameters", font=self.LABEL_FONT)
        fcp_label.grid(row=0, column=0, columnspan=2, pady=(5, 10))

        # Store references to FCP parameter labels for enabling/disabling
        self.fcp_labels = []

        # Row 1: Horizontal Start (0-31)
        lbl = ctk.CTkLabel(self.fcp_frame, text="Horizontal Start (0-31)", font=self.LABEL_FONT,
                     anchor='center')
        lbl.grid(row=1, column=0, pady=2)
        self.fcp_labels.append(lbl)
        self.fcp_h_start_var = tk.IntVar(value=fcp_h_start)
        self.fcp_h_start_entry = ctk.CTkEntry(
            self.fcp_frame, width=self.ENTRY_WIDTH * self.PIXELS_PER_CHAR,
            textvariable=self.fcp_h_start_var, justify=tk.CENTER,
            validate='key', validatecommand=self._h_start_vcmd,
            border_width=self.CHECKBOX_BORDER_WIDTH, border_color="#979DA2", font=self.LABEL_FONT
        )
        self.fcp_h_start_entry.grid(row=1, column=1, pady=2)

        # Row 2: Bit Width (0-2)
        lbl = ctk.CTkLabel(self.fcp_frame, text="Bit Width (0-2)", font=self.LABEL_FONT,
                     anchor='center')
        lbl.grid(row=2, column=0, pady=2)
        self.fcp_labels.append(lbl)
        self.fcp_bit_width_var = tk.IntVar(value=fcp_bit_width)
        self.fcp_bit_width_entry = ctk.CTkEntry(
            self.fcp_frame, width=self.ENTRY_WIDTH * self.PIXELS_PER_CHAR,
            textvariable=self.fcp_bit_width_var, justify=tk.CENTER,
            validate='key', validatecommand=self._bit_width_vcmd,
            border_width=self.CHECKBOX_BORDER_WIDTH, border_color="#979DA2", font=self.LABEL_FONT
        )
        self.fcp_bit_width_entry.grid(row=2, column=1, pady=2)

        # Row 3: Tail Width (0-2)
        lbl = ctk.CTkLabel(self.fcp_frame, text="Tail Width (0-2)", font=self.LABEL_FONT,
                     anchor='center')
        lbl.grid(row=3, column=0, pady=2)
        self.fcp_labels.append(lbl)
        self.fcp_tail_width_var = tk.IntVar(value=fcp_tail_width)
        self.fcp_tail_width_entry = ctk.CTkEntry(
            self.fcp_frame, width=self.ENTRY_WIDTH * self.PIXELS_PER_CHAR,
            textvariable=self.fcp_tail_width_var, justify=tk.CENTER,
            validate='key', validatecommand=self._tail_width_vcmd,
            border_width=self.CHECKBOX_BORDER_WIDTH, border_color="#979DA2", font=self.LABEL_FONT
        )
        self.fcp_tail_width_entry.grid(row=3, column=1, pady=2)

        # Row 4: Offset (0-4095)
        lbl = ctk.CTkLabel(self.fcp_frame, text="Offset (0-4095)", font=self.LABEL_FONT,
                     anchor='center')
        lbl.grid(row=4, column=0, pady=2)
        self.fcp_labels.append(lbl)
        self.fcp_offset_var = tk.IntVar(value=fcp_offset)
        self.fcp_offset_entry = ctk.CTkEntry(
            self.fcp_frame, width=self.ENTRY_WIDTH * self.PIXELS_PER_CHAR,
            textvariable=self.fcp_offset_var, justify=tk.CENTER,
            validate='key', validatecommand=self._offset_vcmd,
            border_width=self.CHECKBOX_BORDER_WIDTH, border_color="#979DA2", font=self.LABEL_FONT
        )
        self.fcp_offset_entry.grid(row=4, column=1, pady=2)

        # Row 5: Guard Enable
        lbl = ctk.CTkLabel(self.fcp_frame, text="Guard Enable", font=self.LABEL_FONT,
                     anchor='center')
        lbl.grid(row=5, column=0, pady=2)
        self.fcp_labels.append(lbl)
        self.fcp_guard_enable_var = tk.BooleanVar(value=fcp_guard_enable)
        # Use a frame to match entry widget width for alignment
        check_frame = ctk.CTkFrame(self.fcp_frame, fg_color="transparent",
                                   width=self.ENTRY_WIDTH * self.PIXELS_PER_CHAR,
                                   height=self.CHECKBOX_HEIGHT)
        check_frame.grid(row=5, column=1, pady=2)
        check_frame.grid_propagate(False)
        self.fcp_guard_check = ctk.CTkCheckBox(
            check_frame, text="",
            variable=self.fcp_guard_enable_var,
            border_width=self.CHECKBOX_BORDER_WIDTH, width=self.CHECKBOX_WIDTH
        )
        self.fcp_guard_check.place(relx=0.55, rely=0.5, anchor='center')

        # Row 6: Guard Polarity
        lbl = ctk.CTkLabel(self.fcp_frame, text="Guard Polarity", font=self.LABEL_FONT,
                     anchor='center')
        lbl.grid(row=6, column=0, pady=2)
        self.fcp_labels.append(lbl)
        self.fcp_guard_polarity_var = tk.IntVar(value=fcp_guard_polarity)
        self.fcp_guard_polarity_entry = ctk.CTkEntry(
            self.fcp_frame, width=self.ENTRY_WIDTH * self.PIXELS_PER_CHAR,
            textvariable=self.fcp_guard_polarity_var, justify=tk.CENTER,
            validate='key', validatecommand=self._guard_polarity_vcmd,
            border_width=self.CHECKBOX_BORDER_WIDTH, border_color="#979DA2", font=self.LABEL_FONT
        )
        self.fcp_guard_polarity_entry.grid(row=6, column=1, pady=2)

        # Update FCP visibility based on initial mode
        self._update_fcp_visibility()

        # OK button
        action_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        action_frame.pack(pady=(10, 0))

        ctk.CTkButton(action_frame, text="OK", width=60,
                      command=self._on_ok).pack()

        # Center dialog on parent
        center_dialog_on_parent(self, parent)

        # Handle window close
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # CTkToplevel workaround: withdraw, update, then deiconify
        # This forces CustomTkinter to fully render internal widgets
        self.withdraw()
        self.update()
        self.deiconify()
        self.lift()
        self.focus_force()

    def _select_mode(self, mode: int) -> None:
        """Select a flow mode."""
        self.flow_mode = mode
        self._update_button_colors()
        self._update_fcp_visibility()

    def _update_button_colors(self) -> None:
        """Update button colors based on selected mode."""
        # Green for selected, gray for unselected
        buttons = [self.normal_btn, self.tx_btn, self.rx_btn, self.async_btn]
        for i, btn in enumerate(buttons):
            if i == self.flow_mode:
                btn.configure(fg_color=BUTTON_COLOR_SELECTED)
            else:
                btn.configure(fg_color=BUTTON_COLOR_UNSELECTED)

    def _update_fcp_visibility(self) -> None:
        """Enable/disable FCP parameters based on flow mode.

        FCP parameters are only relevant for Rx Controlled and Async modes.
        """
        # FCP is enabled for Rx Controlled (2) and Async (3) modes
        fcp_enabled = self.flow_mode in (self.MODE_RX_CONTROLLED, self.MODE_ASYNC)
        state = 'normal' if fcp_enabled else 'disabled'

        # Define disabled style colors based on current theme (matching PHY3 style)
        mode = ctk.get_appearance_mode()  # Returns "Light" or "Dark"
        color_index = 1 if mode == "Dark" else 0

        # Get the default text color from theme for enabled state
        theme = ctk.ThemeManager.theme
        entry_text_color = theme["CTkEntry"]["text_color"]
        if isinstance(entry_text_color, (list, tuple)) and len(entry_text_color) > color_index:
            enabled_text_color = entry_text_color[color_index]
        else:
            enabled_text_color = entry_text_color

        if mode == "Dark":
            disabled_text_color = '#888888'
            disabled_label_color = '#666666'
            disabled_checkbox_color = '#555555'
        else:
            disabled_text_color = '#AAAAAA'
            disabled_label_color = '#888888'
            disabled_checkbox_color = '#AAAAAA'

        # Update label text colors
        label_color = enabled_text_color if fcp_enabled else disabled_label_color
        for lbl in self.fcp_labels:
            lbl.configure(text_color=label_color)

        # Update all FCP entry widgets with text color
        text_color = enabled_text_color if fcp_enabled else disabled_text_color
        self.fcp_h_start_entry.configure(state=state, text_color=text_color)
        self.fcp_bit_width_entry.configure(state=state, text_color=text_color)
        self.fcp_tail_width_entry.configure(state=state, text_color=text_color)
        self.fcp_offset_entry.configure(state=state, text_color=text_color)
        self.fcp_guard_polarity_entry.configure(state=state, text_color=text_color)

        # Update checkbox
        if fcp_enabled:
            self.fcp_guard_check.configure(
                state=state,
                fg_color=("#3B8ED0", "#1F6AA5"),  # Default CTk blue
                border_color=("#3E454A", "#949A9F")
            )
        else:
            self.fcp_guard_check.configure(
                state=state,
                fg_color=disabled_checkbox_color,
                border_color=disabled_checkbox_color
            )

    def _on_ok(self) -> None:
        """Accept the selection and close, with range validation."""
        from src.config.constants import DataPortRanges

        # Only validate FCP parameters if they're enabled (Rx Controlled or Async mode)
        if self.flow_mode in (self.MODE_RX_CONTROLLED, self.MODE_ASYNC):
            errors = []

            # Validate Horizontal Start (0-31)
            h_start = self.fcp_h_start_var.get()
            if h_start < DataPortRanges.MIN_FCP_H_START or h_start > DataPortRanges.MAX_FCP_H_START:
                errors.append(f"Horizontal Start must be {DataPortRanges.MIN_FCP_H_START}-{DataPortRanges.MAX_FCP_H_START}")

            # Validate Bit Width (0-2)
            bit_width = self.fcp_bit_width_var.get()
            if bit_width < DataPortRanges.MIN_FCP_BIT_WIDTH or bit_width > DataPortRanges.MAX_FCP_BIT_WIDTH:
                errors.append(f"Bit Width must be {DataPortRanges.MIN_FCP_BIT_WIDTH}-{DataPortRanges.MAX_FCP_BIT_WIDTH}")

            # Validate Tail Width (0-2)
            tail_width = self.fcp_tail_width_var.get()
            if tail_width < DataPortRanges.MIN_FCP_TAIL_WIDTH or tail_width > DataPortRanges.MAX_FCP_TAIL_WIDTH:
                errors.append(f"Tail Width must be {DataPortRanges.MIN_FCP_TAIL_WIDTH}-{DataPortRanges.MAX_FCP_TAIL_WIDTH}")

            # Validate Offset (0-4095)
            offset = self.fcp_offset_var.get()
            if offset < DataPortRanges.MIN_FCP_OFFSET or offset > DataPortRanges.MAX_FCP_OFFSET:
                errors.append(f"Offset must be {DataPortRanges.MIN_FCP_OFFSET}-{DataPortRanges.MAX_FCP_OFFSET}")

            # Validate Guard Polarity (0-1)
            guard_polarity = self.fcp_guard_polarity_var.get()
            if guard_polarity < DataPortRanges.MIN_FCP_GUARD_POLARITY or guard_polarity > DataPortRanges.MAX_FCP_GUARD_POLARITY:
                errors.append(f"Guard Polarity must be {DataPortRanges.MIN_FCP_GUARD_POLARITY}-{DataPortRanges.MAX_FCP_GUARD_POLARITY}")

            if errors:
                from tkinter import messagebox
                messagebox.showerror("Validation Error", "\n".join(errors), parent=self)
                return

        self.cancelled = False
        # Copy FCP values from UI variables
        self.fcp_h_start = self.fcp_h_start_var.get()
        self.fcp_bit_width = self.fcp_bit_width_var.get()
        self.fcp_tail_width = self.fcp_tail_width_var.get()
        self.fcp_offset = self.fcp_offset_var.get()
        self.fcp_guard_enable = self.fcp_guard_enable_var.get()
        self.fcp_guard_polarity = self.fcp_guard_polarity_var.get()
        self.destroy()

    def _on_cancel(self) -> None:
        """Cancel and close without saving."""
        self.cancelled = True
        self.destroy()
