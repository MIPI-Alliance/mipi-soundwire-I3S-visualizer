"""
Parameter Panel for SWI3S Visualizer.

This module contains all ~350 parameter widgets in a scrollable frame,
keeping the main app file clean while matching the exact UI layout
of the original visualizer.
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

import customtkinter as ctk

from src.config import Colors, UI_IS_EXCESS_1, DataPortRanges, InterfaceRanges
from src.models import Interface
from src.viz import VizConfig
from src.ui.constants import (
    PIXELS_PER_CHAR,
    ENTRY_PADX,
    ENTRY_PADY,
    APP_FONT,
    CHECKBOX_BORDER_WIDTH,
    CHECKBOX_TOP_PADDING,
    NUM_DP_ENTRY_ROWS,
)
from src.ui.helpers import (
    DP_FIELD_NAMES,
    DP_FIELD_MAPPINGS,
    INTERFACE_FIELD_NAMES,
    INTERFACE_TOOLTIPS,
    DP_TOOLTIPS,
    get_interface_labels,
    get_dp_labels,
    safe_int,
    safe_float,
    MIN_ROWS_IN_FRAME,
    MAX_ROWS_IN_FRAME,
)
from src.ui.theme import get_theme_colors

if TYPE_CHECKING:
    from src.ui.app_ui import UIManager


class ParameterPanel(ctk.CTkFrame):
    """Panel containing all parameter widgets for data ports and interface settings.

    Grid Layout (matching old_swi3s_visualizer.py exactly):
    - Row 0: "Data Port Parameters" header (col 0), DP name entries (cols 1-16),
             "Other Parameters" header (cols 18-19)
    - Rows 1-13: DP parameter entries (rows), Interface entries (col 19)
    - Row 14+: Checkbox rows (Direction, Handover, Guard, SRI, Manager, Enable, Flow)
    - Row 21: Sample rate labels
    - Cols 18-19: Action buttons (rows 14-20)
    """

    def __init__(
        self,
        master: Any,
        interface: Interface,
        viz_config: VizConfig,
        ui_manager: 'UIManager',
        callbacks: Dict[str, Callable],
    ):
        """Initialize the parameter panel.

        Args:
            master: Parent widget
            interface: Interface model containing data ports
            viz_config: Visualization configuration
            ui_manager: UIManager for widget creation
            callbacks: Dict of callback functions:
                - 'on_channel_click': fn(dp_index)
                - 'on_guard_click': fn(dp_index)
                - 'on_display_options_click': fn(dp_index)
                - 'on_flow_mode_click': fn(dp_index)
                - 'on_port_mode_click': fn(dp_index)
                - 'on_manager_toggle': fn(dp_index)
                - 'on_phy3_toggle': fn()
                - 'on_cds_guard_click': fn()
                - 'refresh': fn()
                - 'load_csv': fn()
                - 'save_csv': fn()
                - 'save_svg': fn()
                - 'save_json': fn()
                - 'reset': fn()
        """
        super().__init__(master, fg_color=ui_manager.config.preferred_gray)
        self.interface = interface
        self.viz_config = viz_config
        self.ui = ui_manager
        self.callbacks = callbacks

        # Data port colors
        self.DP_COLORS = Colors.DP_COLORS

        # UI labels
        self.dp_labels = get_dp_labels()
        self.interface_labels = get_interface_labels()

        # Widget storage - data ports (16 each)
        self.dp_name_entries: List[ctk.CTkEntry] = []
        self.dp_num_channels_entries: List[ctk.CTkEntry] = []  # Read-only, clickable
        self.dp_entry_boxes: List[ctk.CTkEntry] = []  # All other DP params
        self.dp_sample_rate_labels: List[ctk.CTkLabel] = []
        self.dp_parameter_labels: List[ctk.CTkLabel] = []

        # Checkbox variables - data ports
        self.dp_direction_vars: List[tk.BooleanVar] = []
        self.dp_handover_vars: List[tk.BooleanVar] = []
        self.dp_guard_vars: List[tk.BooleanVar] = []
        self.dp_sri_vars: List[tk.BooleanVar] = []
        self.dp_enable_vars: List[tk.BooleanVar] = []
        self.dp_flow_mode_vars: List[tk.BooleanVar] = []
        self.dp_port_mode_vars: List[tk.BooleanVar] = []
        self.dp_scrambler_vars: List[tk.BooleanVar] = []

        # Interface widgets
        self.interface_entries: Dict[str, ctk.CTkEntry] = {}
        self.interface_vars: Dict[str, tk.BooleanVar] = {}
        self.frame_labels: List[ctk.CTkLabel] = []

        # PHY3-dependent widget references (for enable/disable)
        self.s0w_entry: Optional[ctk.CTkEntry] = None
        self.tail_width_entry: Optional[ctk.CTkEntry] = None
        self.s1_handover_cb: Optional[ctk.CTkCheckBox] = None
        self.s0_width_label: Optional[ctk.CTkLabel] = None
        self.s1_tail_width_label: Optional[ctk.CTkLabel] = None
        self.s1_handover_label: Optional[ctk.CTkLabel] = None

        # Toggle button reference
        self.toggle_button: Optional[ctk.CTkButton] = None

        # Settings visibility state (for toggle button)
        self.settings_visible = True

        # Create validation commands
        self.vcmds = self._create_validation_commands()

        # Build UI
        self._create_widgets()

    def _create_validation_commands(self) -> Dict[str, Any]:
        """Create validation command tuples for entry widgets."""
        # Import validate_entry from helpers for float validation
        from src.ui.helpers import validate_entry

        def validate(action: str, value_if_allowed: str, low: int, high: int) -> bool:
            if action == '1':
                try:
                    return int(low) <= int(value_if_allowed) <= int(high)
                except ValueError:
                    return False
            return True

        return {
            'device_number': (self.register(validate), '%d', '%P',
                             DataPortRanges.MIN_DEVICE_NUMBER, DataPortRanges.MAX_DEVICE_NUMBER),
            'channels': (self.register(validate), '%d', '%P',
                        DataPortRanges.MIN_CHANNELS, DataPortRanges.MAX_CHANNELS),
            'sample_size': (self.register(validate), '%d', '%P',
                           DataPortRanges.MIN_SAMPLE_SIZE, DataPortRanges.MAX_SAMPLE_SIZE),
            'sample_grouping': (self.register(validate), '%d', '%P',
                               DataPortRanges.MIN_SAMPLE_GROUPING, DataPortRanges.MAX_SAMPLE_GROUPING),
            'channel_grouping': (self.register(validate), '%d', '%P',
                                DataPortRanges.MIN_CHANNEL_GROUPING, DataPortRanges.MAX_CHANNEL_GROUPING),
            'channel_group_spacing': (self.register(validate), '%d', '%P',
                                     DataPortRanges.MIN_CHANNEL_GROUP_SPACING, DataPortRanges.MAX_CHANNEL_GROUP_SPACING),
            'interval': (self.register(validate), '%d', '%P',
                        DataPortRanges.MIN_INTERVAL, DataPortRanges.MAX_INTERVAL),
            'numerator': (self.register(validate), '%d', '%P',
                         DataPortRanges.MIN_SKIPPING_NUMERATOR, DataPortRanges.MAX_SKIPPING_NUMERATOR),
            'offset': (self.register(validate), '%d', '%P',
                      DataPortRanges.MIN_OFFSET, DataPortRanges.MAX_OFFSET),
            'column': (self.register(validate), '%d', '%P',
                      0, InterfaceRanges.MAX_COLUMNS_PER_ROW),
            'tail_width': (self.register(validate), '%d', '%P',
                          DataPortRanges.MIN_TAIL_WIDTH, DataPortRanges.MAX_TAIL_WIDTH),
            'bit_width': (self.register(validate), '%d', '%P',
                         DataPortRanges.MIN_BIT_WIDTH, DataPortRanges.MAX_BIT_WIDTH),
            'num_columns_reg': (self.register(validate), '%d', '%P',
                               InterfaceRanges.MIN_COLUMNS_PER_ROW, InterfaceRanges.MAX_COLUMNS_PER_ROW),
            'rows': (self.register(validate), '%d', '%P',
                    MIN_ROWS_IN_FRAME, MAX_ROWS_IN_FRAME),
            'denominator': (self.register(validate), '%d', '%P',
                           InterfaceRanges.MIN_SKIPPING_DENOMINATOR, InterfaceRanges.MAX_SKIPPING_DENOMINATOR),
            's0_width': (self.register(validate), '%d', '%P',
                        InterfaceRanges.MIN_S0_WIDTH, InterfaceRanges.MAX_S0_WIDTH),
            'cds_bit_width': (self.register(validate), '%d', '%P',
                             InterfaceRanges.MIN_CDS_WIDTH, InterfaceRanges.MAX_CDS_WIDTH),
            'cds_tail_width': (self.register(validate), '%d', '%P',
                              InterfaceRanges.MIN_CDS_TAIL_WIDTH, InterfaceRanges.MAX_CDS_TAIL_WIDTH),
            # Use validate_entry from helpers for row_rate (supports floats)
            'row_rate': (self.register(validate_entry), '%d', '%P',
                        InterfaceRanges.MIN_ROW_RATE, InterfaceRanges.MAX_ROW_RATE),
        }

    def _create_entry(self, parent: Any, validate_cmd: tuple, width_multiplier: float = 1.0) -> ctk.CTkEntry:
        """Create a styled entry widget with auto-refresh on focus-out or Enter.

        Args:
            parent: Parent widget
            validate_cmd: Validation command tuple
            width_multiplier: Multiplier for entry width (default 1.0, use 1.2 for 20% wider)
        """
        entry = ctk.CTkEntry(
            parent,
            width=int(self.ui.config.entry_width * PIXELS_PER_CHAR * width_multiplier),
            validate='key',
            validatecommand=validate_cmd,
            justify=tk.CENTER,
            font=("TkDefaultFont", self.ui.config.text_size),
            fg_color=self.ui.config.preferred_gray,
            border_width=CHECKBOX_BORDER_WIDTH,
            border_color="#979DA2"
        )
        # Trigger refresh on focus-out or Enter key
        entry.bind('<FocusOut>', lambda e: self._on_value_change())
        entry.bind('<Return>', lambda e: self._on_value_change())
        return entry

    def _create_wrapped_checkbox(
        self,
        parent: Any,
        variable: tk.BooleanVar,
        row: int,
        column: int,
        command: Optional[Callable] = None,
        top_padding: bool = False
    ) -> ctk.CTkCheckBox:
        """Create a checkbox centered in a wrapper frame."""
        from src.ui.constants import CHECKBOX_WIDTH, CHECKBOX_HEIGHT, CHECKBOX_RELX, CHECKBOX_RELY

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

    def _create_widgets(self) -> None:
        """Create all widgets matching old_swi3s_visualizer.py layout."""
        # Validation functions for DP entry boxes (in display order)
        dp_entry_validate_funcs = [
            self.vcmds['device_number'],
            self.vcmds['channels'],
            self.vcmds['sample_size'],
            self.vcmds['sample_grouping'],
            self.vcmds['channel_grouping'],
            self.vcmds['channel_group_spacing'],
            self.vcmds['interval'],
            self.vcmds['offset'],
            self.vcmds['column'],
            self.vcmds['column'],
            self.vcmds['tail_width'],
            self.vcmds['bit_width'],
            self.vcmds['numerator'],
        ]

        # =====================================================================
        # Row 0: Headers and Data Port Name Entries
        # =====================================================================

        # "Data Port Parameters" header
        header_label = ctk.CTkLabel(
            self, text='Data Port Parameters', anchor=tk.CENTER,
            font=(APP_FONT, self.ui.config.text_size + 6), padx=10
        )
        header_label.grid(row=0, column=0)
        self.frame_labels.append(header_label)

        # Data port name entries (columns 1-16)
        for count, color in enumerate(self.DP_COLORS):
            entry = ctk.CTkEntry(
                self, width=self.ui.config.entry_width * PIXELS_PER_CHAR,
                fg_color=color, text_color="black", justify=tk.CENTER,
                font=("TkDefaultFont", self.ui.config.text_size),
                border_width=CHECKBOX_BORDER_WIDTH, border_color="#979DA2"
            )
            entry.grid(row=0, column=count + 1, padx=ENTRY_PADX, pady=ENTRY_PADY)
            self.dp_name_entries.append(entry)

        # "Other Parameters" header
        other_header = ctk.CTkLabel(
            self, text='Other Parameters', anchor=tk.CENTER,
            font=(APP_FONT, self.ui.config.text_size + 6), padx=10
        )
        other_header.grid(row=0, column=Interface.NUM_DATA_PORTS + 2, columnspan=2)
        self.frame_labels.append(other_header)

        # =====================================================================
        # Data Port Parameter Labels (column 0)
        # =====================================================================
        for count, title in enumerate(self.dp_labels):
            label = ctk.CTkLabel(
                self, text=title, anchor=tk.E,
                font=(APP_FONT, self.ui.config.text_size)
            )
            self.dp_parameter_labels.append(label)

            # Get field name for tooltip
            if count < len(DP_FIELD_NAMES):
                field_name = DP_FIELD_NAMES[count]
                if field_name in DP_TOOLTIPS:
                    from src.ui import SimpleToolTip
                    SimpleToolTip(label, message=DP_TOOLTIPS[field_name],
                                  corner_radius=12, bg_color="#4a4a4a", alpha=0.95)

            # Add top padding to first checkbox row
            if count == NUM_DP_ENTRY_ROWS:
                label.grid(row=count + 1, column=0, pady=CHECKBOX_TOP_PADDING)
            else:
                label.grid(row=count + 1, column=0)

        # =====================================================================
        # DeviceNumber Entries (row 1) - Read-only, Clickable
        # MUST be created first so they occupy dp_entry_boxes indices 0-15
        # NOTE: No validation - entries can contain 'M' for Manager
        # =====================================================================
        for entry_column, data_port in enumerate(self.interface.data_ports):
            # Create entry without validation (allows 'M' for Manager)
            entry = ctk.CTkEntry(
                self, width=self.ui.config.entry_width * PIXELS_PER_CHAR,
                fg_color=self.ui.config.preferred_gray, justify=tk.CENTER,
                font=("TkDefaultFont", self.ui.config.text_size),
                border_width=CHECKBOX_BORDER_WIDTH, border_color="#979DA2"
            )
            entry.grid(row=1, column=entry_column + 1, padx=ENTRY_PADX, pady=ENTRY_PADY)
            entry.configure(state='readonly')
            entry.bind('<Button-1>', lambda e, idx=entry_column: self._on_device_click(idx))
            self.dp_entry_boxes.append(entry)

        # =====================================================================
        # NumChannels Entries (row 2) - Read-only, Clickable
        # =====================================================================
        for entry_column, data_port in enumerate(self.interface.data_ports):
            entry = self._create_entry(self, dp_entry_validate_funcs[1])
            entry.grid(row=2, column=entry_column + 1, padx=ENTRY_PADX, pady=ENTRY_PADY)
            entry.configure(state='readonly')
            entry.bind('<Button-1>', lambda e, idx=entry_column: self._on_channel_click(idx))
            self.dp_num_channels_entries.append(entry)

        # =====================================================================
        # Data Port Entry Widgets (skip row 0 = DeviceNumber, row 1 = NumChannels)
        # =====================================================================
        for entry_row in range(0, NUM_DP_ENTRY_ROWS):
            if entry_row in [0, 1]:  # Skip DeviceNumber and NumChannels - handled separately
                continue
            for entry_column, data_port in enumerate(self.interface.data_ports):
                entry = self._create_entry(self, dp_entry_validate_funcs[entry_row])
                entry.grid(row=entry_row + 1, column=entry_column + 1,
                          padx=ENTRY_PADX, pady=ENTRY_PADY)
                self.dp_entry_boxes.append(entry)

        # =====================================================================
        # Data Port Checkbox Rows
        # =====================================================================
        for count, data_port in enumerate(self.interface.data_ports):
            # Direction (Source/Sink) - invert display: checked=Source (PortDirection_REG=False)
            var = tk.BooleanVar(value=not data_port.config.PortDirection_REG)
            self.dp_direction_vars.append(var)
            self._create_wrapped_checkbox(
                self, var, row=NUM_DP_ENTRY_ROWS + 1,
                column=count + 1, top_padding=True,
                command=self._on_value_change
            )

            # Guard
            var = tk.BooleanVar(value=data_port.config.GuardEnable_REG)
            self.dp_guard_vars.append(var)
            self._create_wrapped_checkbox(
                self, var, row=NUM_DP_ENTRY_ROWS + 2, column=count + 1,
                command=lambda idx=count: self._on_guard_click(idx)
            )

            # SRI
            var = tk.BooleanVar(value=data_port.config.SubRowInterval_REG)
            self.dp_sri_vars.append(var)
            self._create_wrapped_checkbox(
                self, var, row=NUM_DP_ENTRY_ROWS + 3, column=count + 1,
                command=self._on_value_change
            )

            # Flow Mode
            var = tk.BooleanVar(value=data_port.config.FlowMode_REG != 0)
            self.dp_flow_mode_vars.append(var)
            self._create_wrapped_checkbox(
                self, var, row=NUM_DP_ENTRY_ROWS + 4, column=count + 1,
                command=lambda idx=count: self._on_flow_mode_click(idx)
            )

            # Port Mode
            var = tk.BooleanVar(value=data_port.config.PortMode_REG != 0)
            self.dp_port_mode_vars.append(var)
            self._create_wrapped_checkbox(
                self, var, row=NUM_DP_ENTRY_ROWS + 5, column=count + 1,
                command=lambda idx=count: self._on_port_mode_click(idx)
            )

            # Scrambler
            var = tk.BooleanVar(value=data_port.config.ScramblerEn_REG)
            self.dp_scrambler_vars.append(var)
            self._create_wrapped_checkbox(
                self, var, row=NUM_DP_ENTRY_ROWS + 6, column=count + 1,
                command=lambda idx=count: self._on_scrambler_toggle(idx)
            )

            # Handover (moved to bottom section)
            dp_viz = self.viz_config.data_ports[count]
            var = tk.BooleanVar(value=dp_viz.enable_handover)
            self.dp_handover_vars.append(var)
            self._create_wrapped_checkbox(
                self, var, row=NUM_DP_ENTRY_ROWS + 7, column=count + 1,
                command=self._on_value_change
            )

            # Enable (moved to bottom section)
            var = tk.BooleanVar(value=dp_viz.enabled)
            self.dp_enable_vars.append(var)
            self._create_wrapped_checkbox(
                self, var, row=NUM_DP_ENTRY_ROWS + 8, column=count + 1,
                command=lambda idx=count: self._on_display_options_click(idx)
            )

            # Sample rate label (calculated on demand)
            label = ctk.CTkLabel(
                self, text=self._calculate_sample_rate(count), anchor=tk.CENTER,
                font=(APP_FONT, self.ui.config.text_size)
            )
            label.grid(row=NUM_DP_ENTRY_ROWS + 9, column=count + 1)
            self.dp_sample_rate_labels.append(label)

        # =====================================================================
        # Interface Parameter Section
        # =====================================================================
        self._create_interface_widgets()

        # =====================================================================
        # Action Buttons
        # =====================================================================
        self._create_action_buttons()

    def _create_interface_widgets(self) -> None:
        """Create interface parameter labels and entries."""
        btn_col = Interface.NUM_DATA_PORTS + 2

        # Interface parameter labels
        for count, text in enumerate(self.interface_labels):
            label = ctk.CTkLabel(
                self, text=text, anchor=tk.CENTER,
                font=(APP_FONT, self.ui.config.text_size), padx=10
            )
            self.frame_labels.append(label)

            # Add tooltip if available
            if count < len(INTERFACE_FIELD_NAMES):
                field_name = INTERFACE_FIELD_NAMES[count]
                if field_name in INTERFACE_TOOLTIPS:
                    from src.ui import SimpleToolTip
                    SimpleToolTip(label, message=INTERFACE_TOOLTIPS[field_name],
                                  corner_radius=12, bg_color="#4a4a4a", alpha=0.95)

            label.grid(row=count + 1, column=btn_col)

        # Store references to PHY3-dependent labels
        # frame_labels indices: 0=DP header, 1=Other header, 2+=interface labels
        # Interface labels: 0=NumColumns, 1=Denominator, 2=PHY3, 3=S0Width, 4=S1Tail, 5=S1Hand
        self.s0_width_label = self.frame_labels[5]       # S0Width (index 3 + 2 headers)
        self.s1_tail_width_label = self.frame_labels[6]  # S1TailWidth (index 4 + 2 headers)
        self.s1_handover_label = self.frame_labels[7]    # EnforceS1Handover (index 5 + 2 headers)

        # =====================================================================
        # Interface Entry Widgets
        # =====================================================================
        entry_col = Interface.NUM_DATA_PORTS + 3

        # Row 1: Columns per row
        self.interface_entries['cpr_entry'] = self._create_entry(self, self.vcmds['num_columns_reg'], width_multiplier=1.2)
        self.interface_entries['cpr_entry'].grid(row=1, column=entry_col, padx=ENTRY_PADX, pady=ENTRY_PADY)

        # Row 2: Fractional skipping denominator
        self.interface_entries['fid_entry'] = self._create_entry(self, self.vcmds['denominator'], width_multiplier=1.2)
        self.interface_entries['fid_entry'].grid(row=2, column=entry_col, padx=ENTRY_PADX, pady=ENTRY_PADY)

        # Row 3: PHY3 enable
        self.interface_vars['phy3_enabled'] = tk.BooleanVar(value=self.interface.phy3_enabled)
        self._create_wrapped_checkbox(
            self, self.interface_vars['phy3_enabled'],
            row=3, column=entry_col,
            command=self._on_phy3_toggle
        )

        # Row 4: S0 width
        self.s0w_entry = self._create_entry(self, self.vcmds['s0_width'], width_multiplier=1.2)
        self.s0w_entry.grid(row=4, column=entry_col, padx=ENTRY_PADX, pady=ENTRY_PADY)
        self.interface_entries['s0w_entry'] = self.s0w_entry

        # Row 5: S1 Tail width
        self.tail_width_entry = self._create_entry(self, self.vcmds['tail_width'], width_multiplier=1.2)
        self.tail_width_entry.grid(row=5, column=entry_col, padx=ENTRY_PADX, pady=ENTRY_PADY)
        self.interface_entries['tail_width_entry'] = self.tail_width_entry

        # Row 6: S1 handover
        self.interface_vars['s1_handover'] = tk.BooleanVar(value=self.interface.s1_handover_enabled)
        self.s1_handover_cb = self._create_wrapped_checkbox(
            self, self.interface_vars['s1_handover'],
            row=6, column=entry_col,
            command=self._on_value_change
        )

        # Row 7: CDS width
        self.interface_entries['cds_width_entry'] = self._create_entry(self, self.vcmds['cds_bit_width'], width_multiplier=1.2)
        self.interface_entries['cds_width_entry'].grid(row=7, column=entry_col, padx=ENTRY_PADX, pady=ENTRY_PADY)

        # Row 8: CDS guard
        self.interface_vars['cds_guard'] = tk.BooleanVar(value=self.interface.CDS_GuardEnabled_REG)
        self._create_wrapped_checkbox(
            self, self.interface_vars['cds_guard'],
            row=8, column=entry_col,
            command=self._on_cds_guard_click
        )

        # Row 9: CDS Tail width
        self.interface_entries['cds_tail_entry'] = self._create_entry(self, self.vcmds['cds_tail_width'], width_multiplier=1.2)
        self.interface_entries['cds_tail_entry'].grid(row=9, column=entry_col, padx=ENTRY_PADX, pady=ENTRY_PADY)

        # Row 10: CDS handover
        self.interface_vars['cds_handover'] = tk.BooleanVar(value=self.interface.cds_handover_enabled)
        self._create_wrapped_checkbox(
            self, self.interface_vars['cds_handover'],
            row=10, column=entry_col,
            command=self._on_value_change
        )

        # Row 11: Row rate
        self.interface_entries['row_rate_entry'] = self._create_entry(self, self.vcmds['row_rate'], width_multiplier=1.2)
        self.interface_entries['row_rate_entry'].grid(row=11, column=entry_col, padx=ENTRY_PADX, pady=ENTRY_PADY)

        # Row 12: Rows to draw
        self.interface_entries['rpf_entry'] = self._create_entry(self, self.vcmds['rows'], width_multiplier=1.2)
        self.interface_entries['rpf_entry'].grid(row=12, column=entry_col, padx=ENTRY_PADX, pady=ENTRY_PADY)

        # System SSP Interval - label centered under buttons, value under entry column (same row)
        # Placed below the Maximize Frame button
        btn_col = Interface.NUM_DATA_PORTS + 2
        ssp_label = ctk.CTkLabel(
            self, text='System SSP Interval:', anchor=tk.CENTER,
            font=(APP_FONT, self.ui.config.text_size)
        )
        ssp_label.grid(row=NUM_DP_ENTRY_ROWS + 9, column=btn_col, columnspan=2)
        self.frame_labels.append(ssp_label)

        self.ssp_value_label = ctk.CTkLabel(
            self, text=str(self.interface.interval_lcm), anchor=tk.CENTER,
            font=(APP_FONT, self.ui.config.text_size)
        )
        self.ssp_value_label.grid(row=NUM_DP_ENTRY_ROWS + 9, column=entry_col)
        self.frame_labels.append(self.ssp_value_label)

    def _create_action_buttons(self) -> None:
        """Create action buttons."""
        btn_col = Interface.NUM_DATA_PORTS + 2

        def create_btn(text: str, cmd_key: str, row: int) -> ctk.CTkButton:
            btn = ctk.CTkButton(
                self, text=text,
                command=self.callbacks.get(cmd_key, lambda: None),
                font=(APP_FONT, self.ui.config.text_size + 1)
            )
            btn.grid(
                column=btn_col, columnspan=2, row=row,
                sticky=tk.N + tk.S + tk.E + tk.W,
                padx=40, pady=1
            )
            return btn

        # Buttons moved up one row (gap between Maximize Frame and SSP Interval)
        create_btn('Load Init (CSV)', 'reset', NUM_DP_ENTRY_ROWS + 2)
        create_btn('Reset', 'reset_all', NUM_DP_ENTRY_ROWS + 3)
        create_btn('Load Settings (CSV)', 'load_csv', NUM_DP_ENTRY_ROWS + 4)
        create_btn('Save Settings (CSV)', 'save_csv', NUM_DP_ENTRY_ROWS + 5)
        create_btn('Save Output (SVG)', 'save_svg', NUM_DP_ENTRY_ROWS + 6)
        create_btn('Save Output (JSON)', 'save_json', NUM_DP_ENTRY_ROWS + 7)

        # Toggle/maximize button (after the save buttons)
        self.toggle_button = ctk.CTkButton(
            self, text="Maximize Frame",
            command=self.callbacks.get('toggle_ui', lambda: None),
            font=(APP_FONT, self.ui.config.text_size + 1)
        )
        self.toggle_button.grid(
            column=btn_col, columnspan=2, row=NUM_DP_ENTRY_ROWS + 8,
            sticky=tk.N + tk.S + tk.E + tk.W,
            padx=40, pady=1
        )

    # =========================================================================
    # Callback Handlers
    # =========================================================================

    def _on_value_change(self) -> None:
        """Handle any parameter value change - triggers debounced refresh."""
        if 'on_value_change' in self.callbacks:
            self.callbacks['on_value_change']()

    def _on_channel_click(self, dp_index: int) -> None:
        """Handle click on NumChannels entry."""
        if 'on_channel_click' in self.callbacks:
            self.callbacks['on_channel_click'](dp_index)

    def _on_device_click(self, dp_index: int) -> None:
        """Handle click on DeviceNumber entry."""
        if 'on_device_click' in self.callbacks:
            self.callbacks['on_device_click'](dp_index)

    def _on_guard_click(self, dp_index: int) -> None:
        """Handle guard checkbox click."""
        if 'on_guard_click' in self.callbacks:
            self.callbacks['on_guard_click'](dp_index)

    def _on_display_options_click(self, dp_index: int) -> None:
        """Handle enable checkbox click."""
        if 'on_display_options_click' in self.callbacks:
            self.callbacks['on_display_options_click'](dp_index)

    def _on_flow_mode_click(self, dp_index: int) -> None:
        """Handle flow mode checkbox click."""
        if 'on_flow_mode_click' in self.callbacks:
            self.callbacks['on_flow_mode_click'](dp_index)

    def _on_port_mode_click(self, dp_index: int) -> None:
        """Handle port mode checkbox click."""
        if 'on_port_mode_click' in self.callbacks:
            self.callbacks['on_port_mode_click'](dp_index)

    def _on_scrambler_toggle(self, dp_index: int) -> None:
        """Handle scrambler checkbox toggle - triggers frame redraw."""
        # Update model with new scrambler state
        self.interface.data_ports[dp_index].config.ScramblerEn_REG = bool(
            self.dp_scrambler_vars[dp_index].get()
        )
        # Trigger debounced frame redraw
        self._on_value_change()

    def _on_phy3_toggle(self) -> None:
        """Handle PHY3 toggle."""
        self.update_phy3_dependent_widgets()
        if 'on_phy3_toggle' in self.callbacks:
            self.callbacks['on_phy3_toggle']()

    def _on_cds_guard_click(self) -> None:
        """Handle CDS guard checkbox click."""
        if 'on_cds_guard_click' in self.callbacks:
            self.callbacks['on_cds_guard_click']()

    # =========================================================================
    # UI Update Methods
    # =========================================================================

    def update_ui(self) -> None:
        """Sync model values to UI widgets."""
        # Update data port names from viz config
        for count, entry in enumerate(self.dp_name_entries):
            entry.delete(0, tk.END)
            entry.insert(0, self.viz_config.data_ports[count].name)

        # Update NumChannels entries (NumChannels is auto-computed from EnableCh_REG)
        for count, entry in enumerate(self.dp_num_channels_entries):
            data_port = self.interface.data_ports[count]
            entry.configure(state='normal')
            entry.delete(0, tk.END)
            entry.insert(0, str(bin(data_port.config.EnableCh_REG).count('1')))
            entry.configure(state='readonly')

        # Update data port entry widgets
        for entry_column, data_port in enumerate(self.interface.data_ports):
            # Device number - readonly, displays 'M' for manager or device number
            device_entry = self.dp_entry_boxes[Interface.NUM_DATA_PORTS * 0 + entry_column]
            device_entry.configure(state='normal')
            device_entry.delete(0, tk.END)

            if self.interface.is_dp_in_manager(entry_column):
                device_entry.insert(0, 'M')
            else:
                device_number = self.interface.get_dp_device(entry_column)
                device_entry.insert(0, str(device_number))
            device_entry.configure(state='readonly')

            # All other fields use the mapping
            for field_name, row_offset, ui_to_model, model_to_ui in DP_FIELD_MAPPINGS:
                entry = self.dp_entry_boxes[Interface.NUM_DATA_PORTS * row_offset + entry_column]
                entry.delete(0, tk.END)
                value = getattr(data_port.config, field_name)
                entry.insert(0, model_to_ui(value))

        # Update checkbox variables
        for count, data_port in enumerate(self.interface.data_ports):
            dp_viz = self.viz_config.data_ports[count]
            # Invert direction display: checked=Source (PortDirection_REG=False)
            self.dp_direction_vars[count].set(not data_port.config.PortDirection_REG)
            self.dp_handover_vars[count].set(dp_viz.enable_handover)
            self.dp_guard_vars[count].set(data_port.config.GuardEnable_REG)
            self.dp_sri_vars[count].set(data_port.config.SubRowInterval_REG)
            self.dp_enable_vars[count].set(dp_viz.enabled)
            self.dp_flow_mode_vars[count].set(data_port.config.FlowMode_REG != 0)
            self.dp_port_mode_vars[count].set(data_port.config.PortMode_REG != 0)
            self.dp_scrambler_vars[count].set(data_port.config.ScramblerEn_REG)

        # Update sample rate labels
        for count, label in enumerate(self.dp_sample_rate_labels):
            sample_rate = self._calculate_sample_rate(count)
            label.configure(text=sample_rate)

        # Update interface entries
        self.interface_entries['rpf_entry'].delete(0, tk.END)
        self.interface_entries['rpf_entry'].insert(tk.END, self.viz_config.rows_to_draw)

        self.interface_entries['cpr_entry'].delete(0, tk.END)
        self.interface_entries['cpr_entry'].insert(tk.END, self.interface.NumColumns_REG)

        self.interface_entries['fid_entry'].delete(0, tk.END)
        self.interface_entries['fid_entry'].insert(tk.END, self.interface.SkippingDenominator_REG)

        self.interface_vars['phy3_enabled'].set(self.interface.phy3_enabled)

        self.interface_entries['s0w_entry'].delete(0, tk.END)
        self.interface_entries['s0w_entry'].insert(tk.END, self.interface.s0_width)

        self.interface_entries['tail_width_entry'].delete(0, tk.END)
        self.interface_entries['tail_width_entry'].insert(tk.END, self.interface.tail_width)

        self.interface_vars['s1_handover'].set(self.interface.s1_handover_enabled)

        self.interface_entries['cds_width_entry'].delete(0, tk.END)
        self.interface_entries['cds_width_entry'].insert(tk.END, self.interface.CDS_BitWidth_REG)

        self.interface_vars['cds_guard'].set(self.interface.CDS_GuardEnabled_REG)

        self.interface_entries['cds_tail_entry'].delete(0, tk.END)
        self.interface_entries['cds_tail_entry'].insert(tk.END, self.interface.CDS_TailWidth_REG)

        self.interface_vars['cds_handover'].set(self.interface.cds_handover_enabled)

        self.interface_entries['row_rate_entry'].delete(0, tk.END)
        # Format to remove unnecessary trailing zeros (3072.0 -> 3072, but 2400.5 -> 2400.5)
        row_rate_str = f"{self.interface.row_rate:g}"
        self.interface_entries['row_rate_entry'].insert(tk.END, row_rate_str)

        # Update SSP label
        self.ssp_value_label.configure(text=str(self.interface.interval_lcm))

        # Update PHY3-dependent widget states
        self.update_phy3_dependent_widgets()

    def _calculate_sample_rate(self, dp_index: int) -> str:
        """Calculate sample rate for a data port."""
        data_port = self.interface.data_ports[dp_index]
        config = data_port.config

        if not config.SubRowInterval_REG and config.SkippingNumerator_REG == 0:
            sample_rate = "{:.1f}".format(
                (config.SampleGrouping_REG + 1) * self.interface.row_rate /
                (config.Interval_REG + 1)
            )
        elif not config.SubRowInterval_REG:
            if self.interface.SkippingDenominator_REG == 0:
                sample_rate = "{:.1f}".format(
                    (config.SampleGrouping_REG + 1) * self.interface.row_rate /
                    (config.Interval_REG + 1)
                )
            else:
                sample_rate = "{:.1f}".format(
                    (config.SampleGrouping_REG + 1) * self.interface.row_rate /
                    (config.Interval_REG + 1) *
                    (self.interface.SkippingDenominator_REG - config.SkippingNumerator_REG) /
                    self.interface.SkippingDenominator_REG
                )
        else:
            # SRI mode: transports repeat within the horizontal window.
            # Transport width = channels * (sample bits + optional TxP slot)
            # * samples-per-group * UIs-per-bit. Spacing_REG is a period in
            # UIs (first slot is the transport start), so the inter-transport
            # gap is Spacing_REG - 1. Available cols = HC + 1 (window runs
            # HS..HS+HC inclusive, per the engine).
            from src.models.enums import FlowMode
            num_channels = bin(config.EnableCh_REG).count('1')
            txp_slot = 1 if config.FlowMode_REG in (FlowMode.TX_CONTROLLED, FlowMode.ASYNC) else 0
            transport_width = (
                num_channels
                * (config.SampleSize_REG + 1 + txp_slot)
                * (config.SampleGrouping_REG + 1)
                * (config.BitWidth_REG + 1)
            )
            available_cols = config.HorizontalCount_REG + 1

            if transport_width == 0 or available_cols < transport_width:
                sample_groups_per_row = 0
            elif config.Spacing_REG == 0:
                # Hardware sets ROW_DONE after the first transport, so only
                # one fits per row regardless of leftover columns.
                sample_groups_per_row = 1
            else:
                gap = config.Spacing_REG - 1
                cadence = transport_width + gap
                sample_groups_per_row = (available_cols + gap) // cadence

            samples_per_row = sample_groups_per_row * (config.SampleGrouping_REG + 1)

            if sample_groups_per_row > 0:
                sample_rate = "{:.1f}".format(samples_per_row * self.interface.row_rate)
            else:
                sample_rate = "Error"

        return sample_rate

    def update_model(self) -> None:
        """Read UI values back to model with validation."""
        # Update data port names from UI (stored in viz_config, not data_port.config)
        for entry_column in range(len(self.interface.data_ports)):
            self.viz_config.data_ports[entry_column].name = self.dp_name_entries[entry_column].get()

        # Update data port entry widgets
        for entry_column, data_port in enumerate(self.interface.data_ports):
            # Device number - only update if changed (set_dp_device is expensive)
            device_str = self.dp_entry_boxes[Interface.NUM_DATA_PORTS * 0 + entry_column].get()
            current_device = self.interface.get_dp_device(entry_column)
            if device_str == 'M':
                from src.config.constants import SpecialDevices
                if current_device != SpecialDevices.MANAGER:
                    self.interface.set_dp_device(entry_column, SpecialDevices.MANAGER)
            elif device_str:  # non-empty numeric value
                new_device = safe_int(device_str)
                if current_device != new_device:
                    self.interface.set_dp_device(entry_column, new_device)

            # All other fields use the mapping
            for field_name, row_offset, ui_to_model, model_to_ui in DP_FIELD_MAPPINGS:
                entry = self.dp_entry_boxes[Interface.NUM_DATA_PORTS * row_offset + entry_column]
                value = ui_to_model(entry.get())
                setattr(data_port.config, field_name, value)

        # Update checkbox values
        for count, data_port in enumerate(self.interface.data_ports):
            dp_viz = self.viz_config.data_ports[count]
            # Invert direction: checked=Source means PortDirection_REG=False
            data_port.config.PortDirection_REG = not bool(self.dp_direction_vars[count].get())
            dp_viz.enable_handover = bool(self.dp_handover_vars[count].get())
            data_port.config.GuardEnable_REG = bool(self.dp_guard_vars[count].get())
            data_port.config.SubRowInterval_REG = bool(self.dp_sri_vars[count].get())
            # NOTE: DeviceNumber is handled by _on_device_click dialog which updates interface directly
            dp_viz.enabled = bool(self.dp_enable_vars[count].get())
            data_port.config.ScramblerEn_REG = bool(self.dp_scrambler_vars[count].get())

        # Update rows to draw (stored in viz_config)
        self.viz_config.rows_to_draw = max(MIN_ROWS_IN_FRAME,
                                           min(MAX_ROWS_IN_FRAME,
                                               safe_int(self.interface_entries['rpf_entry'].get())))

        # Update interface parameters
        self.interface.NumColumns_REG = max(Interface.MIN_COLUMNS_PER_ROW,
                                           min(Interface.MAX_COLUMNS_PER_ROW,
                                               safe_int(self.interface_entries['cpr_entry'].get())))

        self.interface.phy3_enabled = self.interface_vars['phy3_enabled'].get()

        self.interface.s0_width = max(Interface.MIN_S0_WIDTH,
                                     min(Interface.MAX_S0_WIDTH,
                                         safe_int(self.interface_entries['s0w_entry'].get())))

        self.interface.tail_width = max(Interface.MIN_TAIL_WIDTH,
                                       min(Interface.MAX_TAIL_WIDTH,
                                           safe_int(self.interface_entries['tail_width_entry'].get())))

        self.interface.s1_handover_enabled = self.interface_vars['s1_handover'].get()

        self.interface.CDS_BitWidth_REG = max(Interface.MIN_CDS_WIDTH,
                                             min(Interface.MAX_CDS_WIDTH,
                                                 safe_int(self.interface_entries['cds_width_entry'].get())))

        self.interface.CDS_GuardEnabled_REG = self.interface_vars['cds_guard'].get()

        self.interface.CDS_TailWidth_REG = max(Interface.MIN_CDS_TAIL_WIDTH,
                                              min(Interface.MAX_CDS_TAIL_WIDTH,
                                                  safe_int(self.interface_entries['cds_tail_entry'].get())))

        self.interface.cds_handover_enabled = self.interface_vars['cds_handover'].get()

        self.interface.row_rate = safe_float(self.interface_entries['row_rate_entry'].get(), 3072.0)

        self.interface.SkippingDenominator_REG = max(Interface.MIN_SKIPPING_DENOMINATOR,
                                                   min(Interface.MAX_SKIPPING_DENOMINATOR,
                                                       safe_int(self.interface_entries['fid_entry'].get())))

    def update_phy3_dependent_widgets(self) -> None:
        """Enable/disable PHY3-dependent widgets."""
        from src.ui.theme import get_disabled_colors

        phy3_enabled = self.interface_vars['phy3_enabled'].get()
        disabled_text, disabled_label, disabled_checkbox = get_disabled_colors()

        if phy3_enabled:
            if self.s0w_entry is not None:
                self.s0w_entry.configure(state='normal', text_color=self.ui.config.current_text_color)
            if self.tail_width_entry is not None:
                self.tail_width_entry.configure(state='normal', text_color=self.ui.config.current_text_color)
            if self.s1_handover_cb is not None:
                self.s1_handover_cb.configure(
                    state='normal',
                    text_color=self.ui.config.current_text_color,
                    fg_color=("#3B8ED0", "#1F6AA5"),
                    border_color=("#3E454A", "#949A9F")
                )
            if self.s0_width_label:
                self.s0_width_label.configure(text_color=self.ui.config.current_text_color)
            if self.s1_tail_width_label:
                self.s1_tail_width_label.configure(text_color=self.ui.config.current_text_color)
            if self.s1_handover_label:
                self.s1_handover_label.configure(text_color=self.ui.config.current_text_color)
        else:
            if self.s0w_entry is not None:
                self.s0w_entry.configure(state='disabled', text_color=disabled_text)
            if self.tail_width_entry is not None:
                self.tail_width_entry.configure(state='disabled', text_color=disabled_text)
            if self.s1_handover_cb is not None:
                self.s1_handover_cb.configure(
                    state='disabled',
                    text_color=disabled_text,
                    fg_color=disabled_checkbox,
                    border_color=disabled_checkbox
                )
            if self.s0_width_label:
                self.s0_width_label.configure(text_color=disabled_label)
            if self.s1_tail_width_label:
                self.s1_tail_width_label.configure(text_color=disabled_label)
            if self.s1_handover_label:
                self.s1_handover_label.configure(text_color=disabled_label)

    def update_theme_colors(self, mode: Optional[str] = None) -> None:
        """Update widget colors when theme changes."""
        self.configure(fg_color=self.ui.config.preferred_gray)

        # Update entry boxes
        for entry in self.dp_entry_boxes:
            if entry.winfo_exists():
                entry.configure(fg_color=self.ui.config.preferred_gray)

        for entry in self.dp_num_channels_entries:
            if entry.winfo_exists():
                entry.configure(fg_color=self.ui.config.preferred_gray)

        for name, entry in self.interface_entries.items():
            if entry.winfo_exists():
                entry.configure(fg_color=self.ui.config.preferred_gray)

        # Update manager device entries
        for count, data_port in enumerate(self.interface.data_ports):
            if self.interface.is_dp_in_manager(count):
                device_entry = self.dp_entry_boxes[Interface.NUM_DATA_PORTS * 0 + count]
                if device_entry.winfo_exists():
                    device_entry.configure(fg_color=self.ui.config.preferred_gray)

        # Update PHY3-dependent widget colors
        self.update_phy3_dependent_widgets()

    def update_toggle_button(self, settings_visible: bool) -> None:
        """Update toggle button text based on settings visibility.

        Args:
            settings_visible: Whether settings panel is currently visible
        """
        self.settings_visible = settings_visible
        # Only update text when settings are visible (button is hidden otherwise)
        if settings_visible and self.toggle_button is not None:
            self.toggle_button.configure(text="Maximize Frame")
