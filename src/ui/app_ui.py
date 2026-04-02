"""
UI Manager for SWI3S Visualizer.

This module provides the UIManager class that handles all UI-related operations
for the main application, including theme management, widget creation, and callbacks.
"""

import tkinter as tk
from tkinter import messagebox
from types import ModuleType
from typing import Any, Optional, Tuple, TYPE_CHECKING

ctk: Optional[ModuleType] = None
try:
    import customtkinter as ctk
    HAS_CTK = True
except ImportError:
    HAS_CTK = False

from src.ui.constants import (
    PIXELS_PER_CHAR,
    ENTRY_PADX,
    ENTRY_PADY,
    ROW_SIZE,
    COLUMN_SIZE,
    SCROLLBAR_WIDTH,
    CHECKBOX_BORDER_WIDTH,
    CHECKBOX_WIDTH,
    CHECKBOX_HEIGHT,
    CHECKBOX_TOP_PADDING,
    CHECKBOX_RELX,
    CHECKBOX_RELY,
    APP_FONT,
    TEXT_SIZE as BASE_TEXT_SIZE,
)
from src.ui.theme import get_theme_colors, get_disabled_colors
from src.ui.helpers import validate_entry, validate_entry_values, safe_int
from src.utils.logging_config import get_logger

if TYPE_CHECKING:
    from src.models import Interface


class UIConfig:
    """Configuration for platform-specific UI adjustments."""

    def __init__(self, platform_config: Any):
        """Initialize UI configuration with platform-specific adjustments.

        Args:
            platform_config: Platform configuration object with text_size_offset and entry_width
        """
        self.entry_width = platform_config.entry_width
        self.text_size = BASE_TEXT_SIZE + platform_config.text_size_offset

        # Theme colors (updated by update_theme_colors)
        self.preferred_gray = '#d9d9d9'
        self.current_text_color = '#000000'
        self.dark_gray = '#707070'
        self.light_gray = '#707070'


class UIManager:
    """Manages UI operations for the SWI3S Visualizer application.

    This class handles theme management, widget creation, validation,
    and UI callbacks, keeping the main App class focused on business logic.
    """

    def __init__(self, app: Any, platform_config: Any):
        """Initialize the UI manager.

        Args:
            app: The main application instance
            platform_config: Platform configuration for UI adjustments
        """
        self.app = app
        self.config = UIConfig(platform_config)

        # Initialize theme colors
        self.update_theme_colors()

    # =========================================================================
    # Validation Command Factory
    # =========================================================================

    def create_validation_commands(self) -> dict:
        """Create all validation command tuples for entry widgets.

        Returns:
            Dict mapping command name to validation command tuple
        """
        from src.config.constants import DataPortRanges, InterfaceRanges
        from src.ui.helpers import MIN_ROWS_IN_FRAME, MAX_ROWS_IN_FRAME

        validate = self.app.validate
        register = self.app.register

        return {
            # Data port validation commands
            'device_number': (register(validate), '%d', '%P',
                             DataPortRanges.MIN_DEVICE_NUMBER, DataPortRanges.MAX_DEVICE_NUMBER),
            'channels': (register(validate), '%d', '%P',
                        DataPortRanges.MIN_CHANNELS, DataPortRanges.MAX_CHANNELS),
            'channel_grouping': (register(validate), '%d', '%P',
                                DataPortRanges.MIN_CHANNEL_GROUPING, DataPortRanges.MAX_CHANNEL_GROUPING),
            'channel_group_spacing': (register(validate), '%d', '%P',
                                     DataPortRanges.MIN_CHANNEL_GROUP_SPACING, DataPortRanges.MAX_CHANNEL_GROUP_SPACING),
            'sample_size': (register(validate), '%d', '%P',
                           DataPortRanges.MIN_SAMPLE_SIZE, DataPortRanges.MAX_SAMPLE_SIZE),
            'sample_grouping': (register(validate), '%d', '%P',
                               DataPortRanges.MIN_SAMPLE_GROUPING, DataPortRanges.MAX_SAMPLE_GROUPING),
            'interval': (register(validate), '%d', '%P',
                        DataPortRanges.MIN_INTERVAL, DataPortRanges.MAX_INTERVAL),
            'numerator': (register(validate), '%d', '%P',
                         DataPortRanges.MIN_SKIPPING_NUMERATOR, DataPortRanges.MAX_SKIPPING_NUMERATOR),
            'offset': (register(validate), '%d', '%P',
                      DataPortRanges.MIN_OFFSET, DataPortRanges.MAX_OFFSET),
            'column': (register(validate), '%d', '%P',
                      0, InterfaceRanges.MAX_COLUMNS_PER_ROW),  # 0-31 for max 32 columns
            'tail_width': (register(validate), '%d', '%P',
                          DataPortRanges.MIN_TAIL_WIDTH, DataPortRanges.MAX_TAIL_WIDTH),
            'bit_width': (register(validate), '%d', '%P',
                         DataPortRanges.MIN_BIT_WIDTH, DataPortRanges.MAX_BIT_WIDTH),

            # Interface validation commands
            'columns': (register(validate), '%d', '%P',
                       InterfaceRanges.MIN_COLUMNS, InterfaceRanges.MAX_COLUMNS),
            'num_columns_reg': (register(validate), '%d', '%P',
                               InterfaceRanges.MIN_COLUMNS_PER_ROW, InterfaceRanges.MAX_COLUMNS_PER_ROW),
            'rows': (register(validate), '%d', '%P',
                    MIN_ROWS_IN_FRAME, MAX_ROWS_IN_FRAME),
            'denominator': (register(validate), '%d', '%P',
                           InterfaceRanges.MIN_SKIPPING_DENOMINATOR, InterfaceRanges.MAX_SKIPPING_DENOMINATOR),
            's0_width': (register(validate), '%d', '%P',
                        InterfaceRanges.MIN_S0_WIDTH, InterfaceRanges.MAX_S0_WIDTH),
            'cds_bit_width': (register(validate), '%d', '%P',
                             InterfaceRanges.MIN_CDS_WIDTH, InterfaceRanges.MAX_CDS_WIDTH),
            'cds_tail_width': (register(validate), '%d', '%P',
                              InterfaceRanges.MIN_CDS_TAIL_WIDTH, InterfaceRanges.MAX_CDS_TAIL_WIDTH),
            'row_rate': (register(validate), '%d', '%P',
                        InterfaceRanges.MIN_ROW_RATE, InterfaceRanges.MAX_ROW_RATE),
        }

    # =========================================================================
    # Theme Management
    # =========================================================================

    def update_theme_colors(self, mode: Optional[str] = None) -> None:
        """Update colors based on current appearance mode.

        Args:
            mode: Appearance mode ('Light' or 'Dark'). If None, auto-detect.
        """
        self.config.preferred_gray, self.config.current_text_color = get_theme_colors(mode)
        self.config.dark_gray = '#707070'
        self.config.light_gray = '#707070'

    def on_appearance_mode_change(self, mode: str) -> None:
        """Callback when appearance mode changes.

        Args:
            mode: New appearance mode ('Light' or 'Dark')
        """
        self.update_theme_colors(mode)

        # Update settings_frame background
        if hasattr(self.app, 'settings_frame') and self.app.settings_frame.winfo_exists():
            self.app.settings_frame.configure(fg_color=self.config.preferred_gray)

        # Update all entry boxes with new background color
        if hasattr(self.app, 'dp_entry_boxes'):
            for entry in self.app.dp_entry_boxes:
                if entry.winfo_exists():
                    entry.configure(fg_color=self.config.preferred_gray)

        # Update NumChannels entries (read-only clickable entries)
        if hasattr(self.app, 'dp_num_channels_entries'):
            for entry in self.app.dp_num_channels_entries:
                if entry.winfo_exists():
                    entry.configure(fg_color=self.config.preferred_gray)

        # Update interface parameter entry boxes
        for entry_name in ['cpr_entry', 'row_rate_entry', 'rpf_entry', 'fid_entry',
                          's0w_entry', 'tail_width_entry', 'CDS_BitWidth_REG_entry',
                          'CDS_TailWidth_REG_entry']:
            if hasattr(self.app, entry_name):
                entry = getattr(self.app, entry_name)
                if entry.winfo_exists():
                    entry.configure(fg_color=self.config.preferred_gray)

        # Update device number entry boxes that show 'M' for manager
        if hasattr(self.app, 'dp_entry_boxes') and hasattr(self.app, 'interface'):
            from src.models import Interface
            for count, data_port in enumerate(self.app.interface.data_ports):
                if self.app.interface.is_dp_in_manager(count):
                    device_entry = self.app.dp_entry_boxes[Interface.NUM_DATA_PORTS * 0 + count]
                    if device_entry.winfo_exists():
                        device_entry.configure(fg_color=self.config.preferred_gray)

        # Update canvas backgrounds
        if hasattr(self.app, 'heading_canvas') and self.app.heading_canvas.winfo_exists():
            self.app.heading_canvas.configure(bg=self.config.preferred_gray)

        if hasattr(self.app, 'frame_canvas') and self.app.frame_canvas.winfo_exists():
            self.app.frame_canvas.configure(bg=self.config.preferred_gray)

        # Redraw the frame visualization with new colors
        if hasattr(self.app, 'frame_canvas') and len(self.app.frame_canvas.find_all()) > 0:
            self.app.refresh_data_ports()

        # Update PHY3-dependent widget colors for the new theme
        if hasattr(self.app, 's0w_entry'):
            self.update_phy3_dependent_widgets()

        # Force update to ensure changes are rendered
        if hasattr(self.app, 'master'):
            self.app.master.update_idletasks()

    # =========================================================================
    # Widget State Management
    # =========================================================================

    def update_phy3_dependent_widgets(self) -> None:
        """Enable/disable widgets that depend on PHY3 being enabled.

        When PHY3 is disabled, S0 Width, S1 Tail Width, and S1 Handover
        are grayed out and made read-only since they have no effect.
        """
        phy3_enabled = self.app.phy3_enabled_tk.get()

        # Get disabled colors for current theme
        disabled_text, disabled_label, disabled_checkbox = get_disabled_colors()

        if phy3_enabled:
            # Enable all PHY3-dependent widgets
            self.app.s0w_entry.configure(state='normal', text_color=self.config.current_text_color)
            self.app.tail_width_entry.configure(state='normal', text_color=self.config.current_text_color)
            # Restore checkbox to normal state with default theme colors
            self.app.s1_handover_cb.configure(
                state='normal',
                text_color=self.config.current_text_color,
                fg_color=("#3B8ED0", "#1F6AA5"),  # Default CTk blue
                border_color=("#3E454A", "#949A9F")  # Default CTk border
            )
            # Restore label colors
            if hasattr(self.app, 's0_width_label'):
                self.app.s0_width_label.configure(text_color=self.config.current_text_color)
                self.app.s1_tail_width_label.configure(text_color=self.config.current_text_color)
                self.app.s1_handover_label.configure(text_color=self.config.current_text_color)
        else:
            # Disable PHY3-dependent widgets (gray out and read-only)
            self.app.s0w_entry.configure(state='disabled', text_color=disabled_text)
            self.app.tail_width_entry.configure(state='disabled', text_color=disabled_text)
            # Gray out checkbox widget and text
            self.app.s1_handover_cb.configure(
                state='disabled',
                text_color=disabled_text,
                fg_color=disabled_checkbox,
                border_color=disabled_checkbox
            )
            # Gray out labels
            if hasattr(self.app, 's0_width_label'):
                self.app.s0_width_label.configure(text_color=disabled_label)
                self.app.s1_tail_width_label.configure(text_color=disabled_label)
                self.app.s1_handover_label.configure(text_color=disabled_label)

    def on_phy3_toggle(self) -> None:
        """Update PHY3-dependent widgets when PHY3 Enabled checkbox is toggled."""
        self.update_phy3_dependent_widgets()

    def on_manager_toggle(self, dp_index: int) -> None:
        """Update device number field when In Manager checkbox is toggled.

        Args:
            dp_index: Index of the data port
        """
        from src.models import Interface
        from src.config.constants import SpecialDevices

        is_manager = self.app.dp_manager_check_button_vars[dp_index].get()

        device_entry = self.app.dp_entry_boxes[Interface.NUM_DATA_PORTS * 0 + dp_index]

        if is_manager:
            self.app.interface.set_dp_device(dp_index, SpecialDevices.MANAGER)
            device_entry.configure(state='normal', validate='none')
            device_entry.delete(0, tk.END)
            device_entry.insert(0, 'M')
            device_entry.configure(state='readonly', fg_color=self.config.preferred_gray)
        else:
            # Restore device number (default to 0 when switching from manager)
            self.app.interface.set_dp_device(dp_index, 0)
            device_entry.configure(state='normal', validate='key')
            device_entry.delete(0, tk.END)
            device_entry.insert(0, '0')

    # =========================================================================
    # Widget Factory
    # =========================================================================

    def create_entry(self, parent: Any, validate_command: Tuple[Any, ...]) -> Any:
        """Create a styled entry widget.

        Args:
            parent: Parent widget
            validate_command: Validation command tuple

        Returns:
            Configured CTkEntry widget
        """
        if ctk is None:
            raise ImportError("customtkinter is required for UI widgets")
        return ctk.CTkEntry(
            parent,
            width=self.config.entry_width * PIXELS_PER_CHAR,
            validate='key',
            validatecommand=validate_command,
            justify=tk.CENTER,
            font=("TkDefaultFont", self.config.text_size),
            fg_color=self.config.preferred_gray,
            border_width=CHECKBOX_BORDER_WIDTH,
            border_color="#979DA2"
        )

    def create_wrapped_checkbox(
        self,
        parent: Any,
        variable: Any,
        row: int,
        column: int,
        command: Optional[Any] = None,
        top_padding: bool = False
    ) -> Any:
        """Create a checkbox centered in a wrapper frame.

        This pattern is used throughout the UI to ensure consistent checkbox sizing
        and positioning within grid cells.

        Args:
            parent: Parent widget
            variable: Tkinter variable to bind to checkbox
            row: Grid row
            column: Grid column
            command: Optional callback function
            top_padding: If True, add top padding to wrapper

        Returns:
            The checkbox widget (wrapper is created internally)
        """
        if ctk is None:
            raise ImportError("customtkinter is required for UI widgets")
        wrapper = ctk.CTkFrame(
            parent,
            fg_color="transparent",
            height=CHECKBOX_HEIGHT,
            width=CHECKBOX_WIDTH
        )
        wrapper.grid(
            row=row,
            column=column,
            padx=0,
            pady=CHECKBOX_TOP_PADDING if top_padding else 0
        )
        wrapper.grid_propagate(False)

        cb_kwargs = {
            'text': "",
            'variable': variable,
            'border_width': CHECKBOX_BORDER_WIDTH,
            'width': CHECKBOX_WIDTH
        }
        if command is not None:
            cb_kwargs['command'] = command

        cb = ctk.CTkCheckBox(wrapper, **cb_kwargs)
        cb.place(relx=CHECKBOX_RELX, rely=CHECKBOX_RELY, anchor="center")

        return cb

    def create_button(
        self,
        parent: Any,
        text: str,
        command: Any,
        row: int,
        column: int,
        columnspan: int = 2,
        padx: int = 40,
        pady: int = 1
    ) -> Any:
        """Create a styled button widget.

        Args:
            parent: Parent widget
            text: Button text
            command: Button callback
            row: Grid row
            column: Grid column
            columnspan: Number of columns to span
            padx: Horizontal padding
            pady: Vertical padding

        Returns:
            Configured CTkButton widget
        """
        if ctk is None:
            raise ImportError("customtkinter is required for UI widgets")
        btn = ctk.CTkButton(
            parent,
            text=text,
            command=command,
            font=(APP_FONT, self.config.text_size + 1)
        )
        btn.grid(
            column=column,
            columnspan=columnspan,
            row=row,
            sticky=tk.N + tk.S + tk.E + tk.W,
            padx=padx,
            pady=pady
        )
        return btn

    def configure_window(
        self,
        master: Any,
        title: str,
        width: int,
        height: int,
        position: str = "+150+50"
    ) -> None:
        """Configure the main application window.

        Args:
            master: The root window
            title: Window title
            width: Minimum window width
            height: Minimum window height
            position: Window position string (e.g., "+150+50")
        """
        master.title(title)
        master.minsize(width, height)
        master.geometry(position)
        master.resizable(False, True)
        master.config(menu=tk.Menu(master))

    # =========================================================================
    # Message Display
    # =========================================================================

    def show_message(self, message_type: str, title: str, message: str, batch_mode: bool = False) -> None:
        """Show message dialog in GUI mode, log in batch mode.

        Args:
            message_type: 'warning', 'error', or 'info'
            title: Dialog title
            message: Message text
            batch_mode: If True, log instead of showing dialog
        """
        logger = get_logger('ui')
        if batch_mode:
            if message_type == 'error':
                logger.error(f'{title}: {message}')
            else:
                logger.warning(f'{title}: {message}')
        else:
            if message_type == 'error':
                messagebox.showerror(title, message)
            elif message_type == 'info':
                messagebox.showinfo(title, message)
            else:
                messagebox.showwarning(title, message)

    # =========================================================================
    # Event Handlers
    # =========================================================================

    def scroll_start(self, event: Any) -> None:
        """Handle scroll start event."""
        self.app.frame_canvas.scan_mark(event.x, event.y)

    def scroll_move(self, event: Any) -> None:
        """Handle scroll move event."""
        self.app.frame_canvas.scan_dragto(event.x, event.y, gain=2)

    def toggle_ui(self, *_args: Any) -> None:
        """Toggle visibility of the settings frame."""
        try:
            self.app.settings_frame.pack_info()
        except tk.TclError:
            # Settings are hidden, show them
            self.app.settings_frame.pack(expand=tk.NO, anchor=tk.W)
            self.app.settings_visible = True
            if hasattr(self.app, 'toggle_button'):
                self.app.toggle_button.configure(text="Maximize Frame ⬆")
        else:
            # Settings are visible, hide them
            self.app.settings_frame.forget()
            self.app.settings_visible = False
            if hasattr(self.app, 'toggle_button'):
                self.app.toggle_button.configure(text="Show Settings ⬇")

    def master_focus(self, *_args: Any) -> None:
        """Set focus to master window."""
        self.app.master.focus()

    # =========================================================================
    # Validation
    # =========================================================================

    @staticmethod
    def validate(action: str, value_if_allowed: str, low: Any, high: Any) -> bool:
        """Validate entry widget text within a range.

        Args:
            action: Validation action
            value_if_allowed: Value to validate
            low: Minimum allowed value
            high: Maximum allowed value

        Returns:
            True if valid, False otherwise
        """
        return validate_entry(action, value_if_allowed, low, high)

    @staticmethod
    def validate_values(action: str, value_if_allowed: str, values: Any) -> bool:
        """Validate entry widget text against allowed values.

        Args:
            action: Validation action
            value_if_allowed: Value to validate
            values: Allowed values

        Returns:
            True if valid, False otherwise
        """
        return validate_entry_values(action, value_if_allowed, values)

    @staticmethod
    def st_int(str_in: str) -> int:
        """Convert string to int, returning 0 for empty strings.

        Args:
            str_in: String to convert

        Returns:
            Integer value or 0 if empty/invalid
        """
        return safe_int(str_in)
