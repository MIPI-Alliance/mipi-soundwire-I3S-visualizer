"""SWI3S Visualizer application with full parameter editing UI.

This module provides the complete GUI for the SWI3S Visualizer with:
- Full parameter editing panel matching old_swi3s_visualizer.py
- All business logic delegated to BusModelBuilder
- All rendering delegated to FrameRenderer
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tkinter as tk
import tkinter.filedialog as filedialog
from tkinter import messagebox
from typing import Any, Optional

import customtkinter as ctk

from src.models import Interface
from src.models.bus_model import BusModel, BusModelJSONEncoder
from src.core.engine import BusModelBuilder
from src.io.csv_handler import CSVHandler
from src.viz import VizConfig
from src.ui.frame_renderer import FrameRenderer, RenderConfig
from src.ui.parameter_panel import ParameterPanel
from src.ui.app_ui import UIManager
# Dialogs are imported lazily when first used (see _open_channel_dialog, etc.)
from src.ui.error_panel import ErrorPanel
from src.ui.theme import get_theme_colors
from src.utils.logging_config import get_logger
from src.ui.constants import (
    ROW_SIZE, COLUMN_SIZE, SCROLLBAR_WIDTH,
    DEFAULT_WINDOW_HEIGHT, DEFAULT_CANVAS_HEIGHT,
    WINDOW_WIDTH_MULTIPLIER, APP_FONT,
    TOGGLE_BUTTON_WIDTH, TOGGLE_BUTTON_HEIGHT,
    TOGGLE_BUTTON_X_OFFSET, TOGGLE_BUTTON_Y_OFFSET,
    HEADING_EXTRA_HEIGHT,
)
from src.utils.platform import PlatformConfig

try:
    import canvasvg
except ImportError:
    canvasvg = None


class MinimalApp(ctk.CTkFrame):
    """SWI3S Visualizer application with full parameter editing.

    Clean architecture:
    - Main app handles window management and user interaction
    - ParameterPanel handles all parameter widgets
    - BusModelBuilder handles all business logic (model building)
    - FrameRenderer handles all drawing (pure rendering)
    """

    def __init__(self, master: ctk.CTk, args: argparse.Namespace, version: str = '2.1.0'):
        super().__init__(master)
        self.root: ctk.CTk = master
        self.args = args
        self.version = version
        self.logger = get_logger('app')

        # Platform configuration
        self.platform_config = PlatformConfig.for_current_platform()

        # Create UI Manager
        self.ui = UIManager(self, self.platform_config)

        # Initialize interface (holds configuration)
        self.interface = Interface()
        self.viz_config = VizConfig()  # Visualization settings

        # Frame renderer (pure rendering, no logic)
        self.renderer = FrameRenderer()

        # Current bus model (built before rendering)
        self.bus_model: Optional[BusModel] = None

        # Track settings visibility
        self.settings_visible = True

        # Heading canvas toggle button (shown only when settings hidden)
        self._heading_toggle_button: Optional[ctk.CTkButton] = None
        self._heading_toggle_window_id: Optional[int] = None

        # Debounced refresh tracking
        self._pending_refresh_id: Optional[str] = None
        self._refresh_delay_ms = 300  # Debounce delay in milliseconds

        # Setup window
        self._setup_window()

        # Create parameter panel
        self._create_parameter_panel()

        # Load CSV if provided
        if args.config_file:
            self._load_csv(args.config_file)

        # Initial UI update (fast - just sets widget values)
        self.parameter_panel.update_ui()

        # Initial render
        self._refresh()

        # Register appearance mode change callback
        ctk.AppearanceModeTracker.add(self._on_appearance_mode_change)

        # Show window after all setup is complete (prevents flash)
        self.root.update_idletasks()
        self.root.deiconify()

    def _setup_window(self) -> None:
        """Setup the main window and canvas."""
        # Hide window during setup to prevent flash
        self.root.withdraw()

        window_width = int(WINDOW_WIDTH_MULTIPLIER * COLUMN_SIZE)
        window_height = DEFAULT_WINDOW_HEIGHT

        self.root.title(f'SoundWire I3S v1.0 Payload Visualizer v{self.version}')
        self.root.minsize(window_width, window_height)
        self.root.geometry("+150+50")
        self.root.resizable(False, True)
        self.root.config(menu=tk.Menu(self.root))

        # Main frame
        self.main_frame = ctk.CTkFrame(self.root, width=window_width)
        self.main_frame.pack(fill=tk.Y, expand=tk.YES)

        # Bind Ctrl+H to toggle UI
        self.root.bind('<Control-h>', self._toggle_ui)

    def _create_parameter_panel(self) -> None:
        """Create the parameter panel with all widgets."""
        # Define callbacks
        callbacks = {
            'on_channel_click': self._open_channel_dialog,
            'on_device_click': self._on_device_click,
            'on_guard_click': self._on_guard_checkbox_click,
            'on_display_options_click': self._on_display_options_click,
            'on_flow_mode_click': self._on_flow_mode_checkbox_click,
            'on_port_mode_click': self._on_port_mode_checkbox_click,
            'on_phy3_toggle': self._schedule_refresh,
            'on_cds_guard_click': self._on_cds_guard_checkbox_click,
            'on_value_change': self._schedule_refresh,  # Debounced refresh on any value change
            'toggle_ui': self._toggle_ui,  # Toggle maximize/show settings
            'refresh': self._refresh,
            'load_csv': self._load_csv_dialog,
            'save_csv': self._save_csv_dialog,
            'save_svg': self._save_svg_dialog,
            'save_json': self._save_json_dialog,
            'reset': self._reset_to_init,
            'reset_all': self._reset_all_to_zero,
        }

        # Container frame for parameter panel and error panel side by side
        # Use transparent background to match main_frame
        self.top_container = ctk.CTkFrame(self.main_frame, fg_color='transparent')
        self.top_container.pack(expand=tk.NO, fill=tk.X)

        # Create parameter panel (left side)
        self.parameter_panel = ParameterPanel(
            self.top_container,
            self.interface,
            self.viz_config,
            self.ui,
            callbacks
        )
        self.parameter_panel.pack(side=tk.LEFT, expand=tk.NO, anchor=tk.W)

        # Create right container for description and error panel (stacked vertically)
        self.right_container = ctk.CTkFrame(self.top_container, fg_color='transparent')
        self.right_container.pack(side=tk.LEFT, expand=tk.YES, fill=tk.BOTH, padx=(5, 5))

        # Description label (above the bordered section)
        self.description_label = ctk.CTkLabel(
            self.right_container, text="Description",
            font=(APP_FONT, self.ui.config.text_size + 6),  # Match Other Parameters style
            anchor='center'
        )
        self.description_label.pack(fill=tk.X, pady=(0, 2))

        # Description section with gray border (narrower with padding)
        self.description_frame = ctk.CTkFrame(
            self.right_container,
            border_width=1,
            border_color='#979DA2',  # Match entry box border color
            corner_radius=6,
            fg_color='transparent'
        )
        self.description_frame.pack(side=tk.TOP, fill=tk.X, pady=(0, 5), padx=(28, 40))  # 25% narrower, reduced left gap

        self.description_textbox = ctk.CTkTextbox(
            self.description_frame, height=162, wrap='word',
            font=(APP_FONT, self.ui.config.text_size + 1),
            fg_color='transparent'  # Match rest of UI gray
        )
        self.description_textbox.pack(fill=tk.X, padx=5, pady=5)
        self.description_textbox.insert("1.0", "Enter a description to be saved/recalled here.")
        # Bind text changes to update interface description
        self.description_textbox.bind('<KeyRelease>', self._on_description_change)

        # Create error panel (below description)
        self.error_panel = ErrorPanel(self.right_container)

        # File status textbox (at bottom of right container, same row as System SSP Interval)
        # Uses CTkTextbox for selectable text (read-only)
        self.file_status_textbox = ctk.CTkTextbox(
            self.right_container,
            font=(APP_FONT, self.ui.config.text_size),
            height=24,
            wrap='none',
            fg_color='transparent',
            border_width=0,
            activate_scrollbars=False
        )
        self.file_status_textbox.configure(state='disabled')  # Start disabled (empty)
        self.file_status_textbox.pack(side=tk.BOTTOM, fill=tk.X, pady=(5, 0), padx=(28, 40))

        # Create render frame
        self.render_frame = ctk.CTkFrame(self.main_frame)
        self.render_frame.pack(side=tk.BOTTOM, expand=1, fill=tk.Y)

        # Heading canvas frame (frozen column headers)
        bg_color, _ = get_theme_colors()
        self.heading_frame = tk.Frame(self.render_frame, bg=bg_color)
        self.heading_frame.pack(expand=tk.FALSE, side=tk.TOP, fill=tk.X)

        canvas_width = int(WINDOW_WIDTH_MULTIPLIER * COLUMN_SIZE)
        self.heading_canvas = tk.Canvas(
            self.heading_frame, relief=tk.FLAT, bd=0, highlightthickness=0,
            bg=bg_color, width=canvas_width, height=ROW_SIZE
        )
        self.heading_canvas.pack(expand=tk.FALSE, side=tk.LEFT, fill=tk.BOTH)

        # Spacer for scrollbar alignment
        self.heading_spacer = tk.Frame(self.heading_frame, width=SCROLLBAR_WIDTH, bg=bg_color)
        self.heading_spacer.pack(side=tk.RIGHT, fill=tk.Y)

        # Main canvas
        self.frame_canvas = tk.Canvas(
            self.render_frame, relief=tk.FLAT, bd=0, highlightthickness=0,
            bg=bg_color, width=canvas_width, height=DEFAULT_CANVAS_HEIGHT
        )
        self.frame_canvas.pack(expand=tk.FALSE, side=tk.LEFT, fill=tk.Y)

        # Scrollbar
        self.vbar = ctk.CTkScrollbar(self.render_frame, command=self.frame_canvas.yview)
        self.frame_canvas.config(yscrollcommand=self.vbar.set)
        self.vbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Mouse scroll bindings
        self.frame_canvas.bind("<ButtonPress-1>", self._scroll_start)
        self.frame_canvas.bind("<B1-Motion>", self._scroll_move)

        # Mousewheel/trackpad scrolling
        # macOS uses <MouseWheel> with direct delta values
        self.frame_canvas.bind("<MouseWheel>", self._on_mousewheel)
        # Linux uses Button-4 (scroll up) and Button-5 (scroll down)
        self.frame_canvas.bind("<Button-4>", lambda e: self.frame_canvas.yview_scroll(-1, "units"))
        self.frame_canvas.bind("<Button-5>", lambda e: self.frame_canvas.yview_scroll(1, "units"))
        # Set focus when mouse enters canvas so scroll events are received
        self.frame_canvas.bind("<Enter>", lambda e: self.frame_canvas.focus_set())

    # =========================================================================
    # Dialog Callbacks
    # =========================================================================

    def _open_channel_dialog(self, dp_index: int) -> None:
        """Open channel selector dialog."""
        from src.ui.dialogs import ChannelSelectorDialog

        data_port = self.interface.data_ports[dp_index]

        dialog = ChannelSelectorDialog(
            self.root,
            initial_bitmask=data_port.config.EnableCh_REG,
            dp_index=dp_index
        )

        self.root.wait_window(dialog)

        if not dialog.cancelled:
            data_port.config.EnableCh_REG = dialog.result  # Auto-updates NumChannels

            # Update UI
            entry = self.parameter_panel.dp_num_channels_entries[dp_index]
            entry.configure(state='normal')
            entry.delete(0, tk.END)
            entry.insert(0, str(bin(data_port.config.EnableCh_REG).count('1')))
            entry.configure(state='readonly')

            self._refresh()

    def _on_device_click(self, dp_index: int) -> None:
        """Open device selector dialog."""
        from src.ui.dialogs import DeviceSelectorDialog
        from src.config.constants import SpecialDevices

        current_device = self.interface.get_dp_device(dp_index)

        dialog = DeviceSelectorDialog(
            self.root,
            initial_device=current_device,
            dp_index=dp_index
        )

        self.root.wait_window(dialog)

        if not dialog.cancelled:
            # Update the interface with selected device
            self.interface.set_dp_device(dp_index, dialog.result)

            # Update UI - device entry shows 'M' for manager or device number
            device_entry = self.parameter_panel.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 0 + dp_index]
            device_entry.configure(state='normal')
            device_entry.delete(0, tk.END)

            if dialog.result == SpecialDevices.MANAGER:
                device_entry.insert(0, 'M')
            else:
                device_entry.insert(0, str(dialog.result))
            device_entry.configure(state='readonly')

            self._refresh()

    def _on_guard_checkbox_click(self, dp_index: int) -> None:
        """Handle guard checkbox click."""
        from src.ui.dialogs import GuardSelectorDialog

        data_port = self.interface.data_ports[dp_index]

        dialog = GuardSelectorDialog(
            self.root,
            guard_enabled=data_port.config.GuardEnable_REG,
            guard_polarity=data_port.config.GuardPolarity_REG,
            dp_index=dp_index
        )

        self.root.wait_window(dialog)

        if not dialog.cancelled:
            data_port.config.GuardEnable_REG = dialog.guard_enabled
            data_port.config.GuardPolarity_REG = bool(dialog.guard_polarity)
            self.parameter_panel.dp_guard_vars[dp_index].set(dialog.guard_enabled)
            self._refresh()
        else:
            self.parameter_panel.dp_guard_vars[dp_index].set(data_port.config.GuardEnable_REG)

    def _on_display_options_click(self, dp_index: int) -> None:
        """Handle display options checkbox click."""
        from src.ui.dialogs import DisplayOptionsDialog

        dp_viz = self.viz_config.data_ports[dp_index]
        dialog_enabled = True if not dp_viz.enabled else dp_viz.enabled

        dialog = DisplayOptionsDialog(
            self.root,
            enabled=dialog_enabled,
            display_fields=dp_viz.display_fields,
            dp_index=dp_index
        )

        self.root.wait_window(dialog)

        if not dialog.cancelled:
            dp_viz.enabled = dialog.enabled
            dp_viz.display_fields = dialog.display_fields
            self.parameter_panel.dp_enable_vars[dp_index].set(dialog.enabled)
            self._refresh()
        else:
            self.parameter_panel.dp_enable_vars[dp_index].set(dp_viz.enabled)

    def _on_flow_mode_checkbox_click(self, dp_index: int) -> None:
        """Handle flow mode checkbox click."""
        from src.ui.dialogs import FlowModeSelectorDialog

        data_port = self.interface.data_ports[dp_index]
        fcp = self.interface.flow_control_ports[dp_index]

        dialog = FlowModeSelectorDialog(
            self.root,
            flow_mode=data_port.config.FlowMode_REG,
            dp_index=dp_index,
            fcp_h_start=fcp.config.FCP_HorizontalStart_REG,
            fcp_bit_width=fcp.config.FCP_BitWidth_REG,
            fcp_tail_width=fcp.config.FCP_TailWidth_REG,
            fcp_offset=fcp.config.FCP_Offset_REG,
            fcp_guard_enable=fcp.config.FCP_GuardEnable_REG,
            fcp_guard_polarity=fcp.config.FCP_GuardPolarity_REG
        )

        self.root.wait_window(dialog)

        if not dialog.cancelled:
            data_port.config.FlowMode_REG = dialog.flow_mode
            fcp.config.FCP_HorizontalStart_REG = dialog.fcp_h_start
            fcp.config.FCP_BitWidth_REG = dialog.fcp_bit_width
            fcp.config.FCP_TailWidth_REG = dialog.fcp_tail_width
            fcp.config.FCP_Offset_REG = dialog.fcp_offset
            fcp.config.FCP_GuardEnable_REG = dialog.fcp_guard_enable
            fcp.config.FCP_GuardPolarity_REG = bool(dialog.fcp_guard_polarity)
            self.parameter_panel.dp_flow_mode_vars[dp_index].set(dialog.flow_mode != 0)
            self._refresh()
        else:
            self.parameter_panel.dp_flow_mode_vars[dp_index].set(data_port.config.FlowMode_REG != 0)

    def _on_port_mode_checkbox_click(self, dp_index: int) -> None:
        """Handle port mode checkbox click."""
        from src.ui.dialogs import PortModeSelectorDialog

        data_port = self.interface.data_ports[dp_index]

        # Immediately revert checkbox to original state to prevent flicker
        # (checkbox auto-toggles when clicked, before dialog appears)
        original_state = data_port.config.PortMode_REG != 0
        self.parameter_panel.dp_port_mode_vars[dp_index].set(original_state)

        dialog = PortModeSelectorDialog(
            self.root,
            port_mode=data_port.config.PortMode_REG,
            dp_index=dp_index
        )

        self.root.wait_window(dialog)

        if not dialog.cancelled:
            data_port.config.PortMode_REG = dialog.port_mode
            self.parameter_panel.dp_port_mode_vars[dp_index].set(dialog.port_mode != 0)
            self._refresh()
        else:
            self.parameter_panel.dp_port_mode_vars[dp_index].set(data_port.config.PortMode_REG != 0)

    def _on_cds_guard_checkbox_click(self) -> None:
        """Handle CDS guard checkbox click."""
        from src.ui.dialogs import GuardSelectorDialog

        dialog = GuardSelectorDialog(
            self.root,
            guard_enabled=self.interface.CDS_GuardEnabled_REG,
            guard_polarity=self.interface.CDS_GuardPolarity_REG,
            dialog_title="CDS Guard Selection"
        )

        self.root.wait_window(dialog)

        if not dialog.cancelled:
            self.interface.CDS_GuardEnabled_REG = dialog.guard_enabled
            self.interface.CDS_GuardPolarity_REG = bool(dialog.guard_polarity)
            self.parameter_panel.interface_vars['cds_guard'].set(dialog.guard_enabled)
            self._refresh()
        else:
            self.parameter_panel.interface_vars['cds_guard'].set(self.interface.CDS_GuardEnabled_REG)

    # =========================================================================
    # File Operations
    # =========================================================================

    def _load_csv_dialog(self) -> None:
        """Open file dialog to load CSV."""
        filename = filedialog.askopenfilename(
            title="Select an input parameter filename",
            filetypes=[("CSV Files", "*.csv")]
        )
        if filename:
            self._load_csv(filename)
            self.parameter_panel.update_ui()
            self._refresh(skip_model_update=True)

    def _load_csv(self, filename: str) -> None:
        """Load interface configuration from CSV file."""
        result = CSVHandler.load_csv(filename, self.interface, self.viz_config)
        if result.success:
            # Update parameter panel with loaded viz_config
            self.parameter_panel.viz_config = self.viz_config
            self.logger.info(f"Loaded: {filename}")

            # Update description textbox from loaded interface
            self.description_textbox.delete("1.0", tk.END)
            if self.interface.description:
                self.description_textbox.insert("1.0", self.interface.description)

            # Update file status notification
            self._set_file_status("Loaded", filename)

            if result.unrecognized_fields:
                field_list = "\n".join([f"  Line {line}: '{name}'" for line, name in result.unrecognized_fields[:10]])
                if len(result.unrecognized_fields) > 10:
                    field_list += f"\n  ... and {len(result.unrecognized_fields) - 10} more"
                messagebox.showwarning(
                    'Unrecognized Fields',
                    f"The CSV file contains {len(result.unrecognized_fields)} unrecognized field(s).\n"
                    f"This may be an outdated file format.\n\n"
                    f"Unrecognized fields:\n{field_list}"
                )
        else:
            self.logger.error(f"Error loading {filename}: {result.error_message}")
            messagebox.showerror(
                'CSV Load Error',
                f"Failed to load CSV file:\n{filename}\n\n"
                f"Error: {result.error_message}"
            )

    def _save_csv_dialog(self) -> None:
        """Save configuration to CSV file."""
        self._refresh()

        filename = filedialog.asksaveasfilename(
            title="Select an output parameter file name",
            defaultextension=".csv",
            filetypes=[("CSV Files", "*.csv")]
        )
        if filename:
            CSVHandler.save_csv(filename, self.interface, self.viz_config)
            self.logger.info(f"Saved: {filename}")
            self._set_file_status("Saved", filename)

    def _save_svg_dialog(self) -> None:
        """Save the rendered frame as SVG."""
        if canvasvg:
            filename = filedialog.asksaveasfilename(
                title="Select an output SVG file name",
                defaultextension=".svg",
                filetypes=[("SVG Files", "*.svg")]
            )
            if filename:
                canvasvg.saveall(filename, self.frame_canvas)
                self.logger.info(f"Saved: {filename}")
                self._set_file_status("Saved", filename)
        else:
            messagebox.showwarning(
                'Warning',
                'No canvasvg module found. Install canvasvg to enable SVG export.'
            )

    def _save_json_dialog(self) -> None:
        """Save the bus model as JSON."""
        filename = filedialog.asksaveasfilename(
            title="Select an output JSON file name",
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json")]
        )
        if filename and self.bus_model:
            with open(filename, 'w') as f:
                json.dump(self.bus_model, f, cls=BusModelJSONEncoder, indent=2)
            self.logger.info(f"Saved: {filename}")
            self._set_file_status("Saved", filename)

    def _reset_to_init(self) -> None:
        """Reset configuration by loading init.csv if it exists."""
        self.root.focus()

        init_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'init.csv')

        if os.path.exists(init_file):
            self._load_csv(init_file)
            self.parameter_panel.update_ui()
            self._refresh(skip_model_update=True)
        else:
            messagebox.showinfo(
                'Reset',
                'No init.csv file found.\n\n'
                'To create a default configuration:\n'
                '1. Configure the settings as desired\n'
                '2. Click "Save Settings (CSV)"\n'
                '3. Save the file as "init.csv" in the application directory'
            )

    def _reset_all_to_zero(self) -> None:
        """Reset all parameters to default values.

        Resets data port parameters, interface 'Other Parameters', and description.
        """
        self.root.focus()

        # Reset all data ports
        for dp_index, dp in enumerate(self.interface.data_ports):
            self.interface.set_dp_device(dp_index, 0)  # Reset device assignment
            dp.config.EnableCh_REG = 0
            dp.config.SampleSize_REG = 0
            dp.config.SampleGrouping_REG = 0
            dp.config.ChannelGrouping_REG = 0
            dp.config.Spacing_REG = 0
            dp.config.Interval_REG = 0
            dp.config.Offset_REG = 0
            dp.config.HorizontalStart_REG = 0
            dp.config.HorizontalCount_REG = 0
            dp.config.TailWidth_REG = 0
            dp.config.BitWidth_REG = 0
            dp.config.SkippingNumerator_REG = 0
            dp.config.PortDirection_REG = False
            dp.config.GuardEnable_REG = False
            dp.config.GuardPolarity_REG = False
            dp.config.SubRowInterval_REG = False
            dp.config.FlowMode_REG = 0
            dp.config.PortMode_REG = 0
            dp.config.ScramblerEn_REG = False
            # Also reset FCP fields
            fcp = self.interface.flow_control_ports[dp_index]
            fcp.config.FCP_HorizontalStart_REG = 0
            fcp.config.FCP_BitWidth_REG = 0
            fcp.config.FCP_TailWidth_REG = 0
            fcp.config.FCP_Offset_REG = 0
            fcp.config.FCP_GuardEnable_REG = False
            fcp.config.FCP_GuardPolarity_REG = False

            # Reset viz config for this data port
            dp_viz = self.viz_config.data_ports[dp_index]
            dp_viz.name = f'DP{dp_index}'
            dp_viz.enabled = False
            dp_viz.enable_handover = True

        # Reset interface "Other Parameters" to defaults
        self.interface.NumColumns_REG = 15  # 16 columns
        self.interface.SkippingDenominator_REG = 1
        self.interface.phy3_enabled = False
        self.interface.s0_width = Interface.MIN_S0_WIDTH
        self.interface.s1_width = Interface.MIN_S1_WIDTH
        self.interface.tail_width = Interface.MIN_TAIL_WIDTH
        self.interface.s1_handover_enabled = True
        self.interface.CDS_BitWidth_REG = Interface.MIN_CDS_WIDTH
        self.interface.CDS_GuardEnabled_REG = False
        self.interface.CDS_GuardPolarity_REG = False
        self.interface.CDS_TailWidth_REG = Interface.MIN_CDS_TAIL_WIDTH
        self.interface.cds_handover_enabled = True
        self.interface.row_rate = 3072
        self.viz_config.rows_to_draw = 16

        # Reset description
        self.interface.description = ''
        self.description_textbox.delete("1.0", tk.END)

        self.parameter_panel.update_ui()
        self._refresh(skip_model_update=True)

    # =========================================================================
    # UI Event Handlers
    # =========================================================================

    def _toggle_ui(self, *_args) -> None:
        """Toggle visibility of the parameter panel and error panel."""
        try:
            self.top_container.pack_info()
        except tk.TclError:
            # Settings are hidden, show them
            self.top_container.pack(expand=tk.NO, fill=tk.X)
            self.settings_visible = True
            self.parameter_panel.update_toggle_button(True)
            # Remove heading canvas button
            self._remove_heading_toggle_button()
        else:
            # Settings are visible, hide them
            self.top_container.forget()
            self.settings_visible = False
            self.parameter_panel.update_toggle_button(False)
            # Add heading canvas button to restore settings
            self._add_heading_toggle_button()

        # Refresh to redraw headers at correct position for new canvas height
        self._refresh()

    def _add_heading_toggle_button(self) -> None:
        """Add 'Show Settings' button to heading canvas when settings are hidden.

        Increases heading canvas height to make room for the button row above column headers.
        """
        bg_color, _ = get_theme_colors()

        # Increase heading canvas height to add button row
        new_height = ROW_SIZE + HEADING_EXTRA_HEIGHT
        self.heading_canvas.configure(height=new_height)

        if self._heading_toggle_button is None:
            self._heading_toggle_button = ctk.CTkButton(
                self.heading_canvas,
                text="Show Settings",
                width=TOGGLE_BUTTON_WIDTH,
                height=TOGGLE_BUTTON_HEIGHT,
                bg_color=bg_color,
                command=self._toggle_ui,
                font=(APP_FONT, self.ui.config.text_size),
            )

        canvas_width = int(WINDOW_WIDTH_MULTIPLIER * COLUMN_SIZE)
        # Position button in the extra row at the top (centered vertically in the extra space)
        button_y = HEADING_EXTRA_HEIGHT // 2
        self._heading_toggle_window_id = self.heading_canvas.create_window(
            canvas_width - TOGGLE_BUTTON_X_OFFSET, button_y,
            window=self._heading_toggle_button, anchor=tk.E
        )

    def _remove_heading_toggle_button(self) -> None:
        """Remove 'Show Settings' button from heading canvas and restore height."""
        if self._heading_toggle_window_id is not None:
            self.heading_canvas.delete(self._heading_toggle_window_id)
            self._heading_toggle_window_id = None
        if self._heading_toggle_button is not None:
            self._heading_toggle_button.destroy()
            self._heading_toggle_button = None
        # Restore heading canvas to normal height
        self.heading_canvas.configure(height=ROW_SIZE)

    def _scroll_start(self, event) -> None:
        """Handle scroll start."""
        self._scroll_mark_x = event.x
        self.frame_canvas.scan_mark(event.x, event.y)

    def _scroll_move(self, event) -> None:
        """Handle scroll move."""
        self.frame_canvas.scan_dragto(self._scroll_mark_x, event.y, gain=2)

    def _on_mousewheel(self, event: tk.Event) -> None:
        """Handle mouse wheel/trackpad scrolling.

        On macOS, event.delta is the raw scroll amount (positive = up, negative = down).
        On Windows, event.delta is typically 120 or -120 per wheel notch.
        """
        if sys.platform == 'darwin':
            # macOS: delta is direct scroll amount, negative = scroll down
            self.frame_canvas.yview_scroll(int(-1 * event.delta), "units")
        else:
            # Windows: delta is 120 per wheel notch
            self.frame_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_appearance_mode_change(self, mode: str) -> None:
        """Callback when appearance mode changes."""
        self.ui.update_theme_colors(mode)
        bg_color, _ = get_theme_colors(mode)

        # Update parameter panel
        self.parameter_panel.update_theme_colors(mode)

        # Update error panel
        if hasattr(self, 'error_panel'):
            self.error_panel.update_theme_colors(mode)

        # Update canvases
        if hasattr(self, 'heading_canvas') and self.heading_canvas.winfo_exists():
            self.heading_canvas.configure(bg=bg_color)
        if hasattr(self, 'frame_canvas') and self.frame_canvas.winfo_exists():
            self.frame_canvas.configure(bg=bg_color)

        # Redraw if there's content
        if hasattr(self, 'frame_canvas') and len(self.frame_canvas.find_all()) > 0:
            self._refresh()

        self.root.update_idletasks()

    def _on_description_change(self, event: Any = None) -> None:
        """Callback when description textbox changes."""
        # Update interface description from textbox
        self.interface.description = self.description_textbox.get("1.0", tk.END).strip()

    def _set_file_status(self, operation: str, filename: str) -> None:
        """Update the file status textbox.

        Args:
            operation: Operation type ('Loaded' or 'Saved')
            filename: Full path to the file
        """
        basename = os.path.basename(filename)
        self.file_status_textbox.configure(state='normal')
        self.file_status_textbox.delete('1.0', tk.END)
        self.file_status_textbox.insert('1.0', f"{operation}: {basename}")
        self.file_status_textbox.configure(state='disabled')  # Read-only but selectable

    # =========================================================================
    # Core Methods
    # =========================================================================

    def _schedule_refresh(self) -> None:
        """Schedule a debounced refresh.

        Cancels any pending refresh and schedules a new one after the delay.
        This prevents excessive refreshes during rapid user input (typing).
        """
        # Cancel any pending refresh
        if self._pending_refresh_id is not None:
            self.root.after_cancel(self._pending_refresh_id)
            self._pending_refresh_id = None

        # Schedule new refresh after delay
        self._pending_refresh_id = self.root.after(
            self._refresh_delay_ms,
            self._do_scheduled_refresh
        )

    def _do_scheduled_refresh(self) -> None:
        """Execute the scheduled refresh."""
        self._pending_refresh_id = None
        self._refresh()

    def _refresh(self, skip_model_update: bool = False) -> None:
        """Build model and render frame.

        This method shows the clean separation:
        1. ParameterPanel.update_model() reads UI values to model
        2. BusModelBuilder does ALL the work (clash detection, bit placement, etc.)
        3. FrameRenderer just draws what's in the model (no intelligence)

        Args:
            skip_model_update: If True, skip reading UI values back to model.
                              Use when model was just loaded from file.
        """
        # Step 1: Read UI values to model (unless skipping)
        if not skip_model_update:
            self.parameter_panel.update_model()

        # Update UI to reflect any clamped values
        self.parameter_panel.update_ui()

        # Step 2: Build the model (all intelligence here)
        num_rows = self.viz_config.rows_to_draw
        builder = BusModelBuilder(self.interface, num_rows, self.viz_config)
        self.bus_model = builder.build()

        # Step 3: Render the model (pure drawing, no logic)
        bg_color, text_color = get_theme_colors()
        config = RenderConfig(
            text_size=self.ui.config.text_size,
            text_color=text_color,
            background_color=bg_color,
            line_color='#707070',
            settings_visible=self.settings_visible,
        )

        self.renderer.render(
            bus_model=self.bus_model,
            canvas=self.frame_canvas,
            config=config,
            heading_canvas=self.heading_canvas
        )

        # Re-add toggle button if settings are hidden (render clears heading_canvas)
        if not self.settings_visible:
            self._add_heading_toggle_button()

        # Update error panel with any issues
        self.error_panel.refresh_errors(self.bus_model)

        # Update canvas
        self.root.update()


def run_app(args: argparse.Namespace, version: str = '2.1.0') -> None:
    """Run the GUI application.

    Args:
        args: Command-line arguments (must have config_file attribute)
        version: Application version string
    """
    # Configure CustomTkinter
    # Use theme from command line args if available, otherwise default to system
    theme = getattr(args, 'theme', 'system')
    # Capitalize for customtkinter: 'light' -> 'Light', 'dark' -> 'Dark', 'system' -> 'System'
    ctk.set_appearance_mode(theme.capitalize())
    ctk.set_default_color_theme("blue")

    # Create and run application
    root = ctk.CTk()
    app = MinimalApp(root, args, version)
    root.mainloop()
